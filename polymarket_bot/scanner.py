"""
polymarket_bot/scanner.py

CEX-DEX Arbitrage Scanner comparing Bybit Spot and Raydium/Solana DEX (via Jupiter API).
"""
from __future__ import annotations
import asyncio
import random
import aiohttp
from loguru import logger

import math
import time
from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed
from polymarket_bot.state import PortfolioState

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_MINT = "So11111111111111111111111111111111111111112"


class CexDexArbitrageScanner:
    def __init__(self, price_feed: BybitPriceFeed):
        self.price_feed = price_feed
        self.session = None
        self.last_dex_buy = 0.0
        self.last_dex_sell = 0.0
        self.jupiter_cooldown_until = 0.0

    async def init_session(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.0))

    async def close_session(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def get_jupiter_quote(self, input_mint: str, output_mint: str, amount_raw: int, max_retries: int = 3) -> int | None:
        """Queries the Jupiter Quote API with simulated/real 429 rate limit handling & exponential backoff."""
        now = asyncio.get_event_loop().time()
        if settings.trading_mode == "paper" and now < self.jupiter_cooldown_until:
            return None

        await self.init_session()
        url = "https://api.jup.ag/swap/v1/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": "50"  # 0.5% slippage tolerance
        }
        
        base_delay = 0.5
        for attempt in range(max_retries):
            # Simulated 429 errors in paper mode
            if settings.trading_mode == "paper" and settings.jupiter_429_sim_prob > 0:
                if random.random() < settings.jupiter_429_sim_prob:
                    logger.warning(f"⚠️ [Simulated 429] Jupiter API Rate Limit (Attempt {attempt+1}/{max_retries}). Retrying...")
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue

            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return int(data.get("outAmount", 0))
                    elif resp.status == 429:
                        if settings.trading_mode == "paper":
                            self.jupiter_cooldown_until = asyncio.get_event_loop().time() + 60.0
                            logger.warning("⚠️ [HTTP 429] Jupiter API Rate Limit hit. Backing off real API calls for 60 seconds.")
                            break
                        else:
                            logger.warning(f"⚠️ [HTTP 429] Jupiter API Rate Limit (Attempt {attempt+1}/{max_retries}). Retrying...")
                            await asyncio.sleep(base_delay * (2 ** attempt))
                            continue
                    else:
                        logger.warning(f"⚠️ Jupiter Quote API returned HTTP {resp.status}")
                        break
            except Exception as e:
                logger.warning(f"⚠️ Failed to reach Jupiter Quote API: {e}")
                await asyncio.sleep(base_delay * (2 ** attempt))
                
        return None

    async def get_latest_dex_prices(self, mid_price: float, trade_size: float, sol_in: float) -> tuple[float, float, float, float]:
        """Queries Jupiter Quote API or falls back to simulated prices if rate-limited."""
        usdt_in_raw = int(trade_size * 1_000_000)
        sol_out_raw = await self.get_jupiter_quote(USDT_MINT, SOL_MINT, usdt_in_raw)

        sol_in_raw = int(sol_in * 1_000_000_000.0)
        usdt_out_raw = await self.get_jupiter_quote(SOL_MINT, USDT_MINT, sol_in_raw)

        # Handle Fallback if Jupiter is rate-limited
        if not sol_out_raw or not usdt_out_raw:
            offset = 0.015 * math.sin(time.time() / 15.0)
            dex_buy_price = mid_price * (1.002 + offset)
            sol_out = trade_size / dex_buy_price
            
            dex_sell_price = mid_price * (0.998 + offset)
            usdt_out = sol_in * dex_sell_price
        else:
            sol_out = sol_out_raw / 1_000_000_000.0
            dex_buy_price = trade_size / sol_out
            
            usdt_out = usdt_out_raw / 1_000_000.0
            dex_sell_price = usdt_out / sol_in

        return dex_buy_price, sol_out, dex_sell_price, usdt_out

    async def scan(self, state: PortfolioState) -> dict | None:
        """
        Scans for profitable CEX-DEX spreads between Bybit and Solana DEX.
        Returns the best opportunity details if net spread > settings.min_arbitrage_spread_pct.
        """
        # Check if trading is paused due to drawdown
        if state.max_drawdown_paused:
            logger.warning("⚠️ Trading paused due to Max Drawdown Guard limit breach.")
            return None

        # 1. Fetch Bybit Spot SOLUSDT ticker
        bid, ask, _, _ = self.price_feed.get_best_bid_ask("SOLUSDT")
        if not bid or not ask:
            return None

        # Check Bybit price freshness
        now = asyncio.get_event_loop().time()
        price_age = now - self.price_feed.last_update_ts
        if price_age > settings.max_price_age_s:
            logger.warning(f"⚠️ Stale Bybit prices (Age: {price_age:.2f}s). Skipping scan.")
            return None

        mid_price = (bid + ask) / 2.0
        trade_size = settings.trade_size_usdt

        # 2. Query Jupiter Quote API for DEX prices
        sol_in = trade_size / mid_price
        dex_buy_price, sol_out, dex_sell_price, usdt_out = await self.get_latest_dex_prices(mid_price, trade_size, sol_in)

        self.last_dex_buy = dex_buy_price
        self.last_dex_sell = dex_sell_price

        # 3. Calculate Spread Options
        # Option A: DEX Buy (USDT->SOL) and CEX Sell (Spot Sell SOL)
        # Target capital checks
        opt_a_net = -999.0
        if state.dex_cash >= trade_size and state.cex_asset >= sol_out:
            gross_a = (bid / dex_buy_price) - 1.0
            # Fees: 0.10% Bybit Spot, plus flat $0.05 SOL network fee buffer
            fees_a = 0.0010 + (0.05 / trade_size)
            opt_a_net = gross_a - fees_a

        # Option B: CEX Buy (Spot Buy SOL) and DEX Sell (SOL->USDT)
        opt_b_net = -999.0
        if state.cex_cash >= trade_size and state.dex_asset >= sol_in:
            gross_b = (dex_sell_price / ask) - 1.0
            # Fees: 0.10% Bybit Spot, plus flat $0.05 SOL network fee buffer
            fees_b = 0.0010 + (0.05 / trade_size)
            opt_b_net = gross_b - fees_b

        # 4. Trigger Check
        best_opt = None
        if opt_a_net >= settings.min_arbitrage_spread_pct and opt_a_net >= opt_b_net:
            best_opt = {
                "route": "DEX-BUY_CEX-SELL",
                "net_spread": opt_a_net,
                "gross_spread": (bid / dex_buy_price) - 1.0,
                "buy_price": dex_buy_price,
                "sell_price": bid,
                "size_asset": sol_out,
                "size_usdt": trade_size,
            }
        elif opt_b_net >= settings.min_arbitrage_spread_pct and opt_b_net >= opt_a_net:
            best_opt = {
                "route": "CEX-BUY_DEX-SELL",
                "net_spread": opt_b_net,
                "gross_spread": (dex_sell_price / ask) - 1.0,
                "buy_price": ask,
                "sell_price": dex_sell_price,
                "size_asset": sol_in,
                "size_usdt": trade_size,
            }

        return best_opt

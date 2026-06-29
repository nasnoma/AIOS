"""
polymarket_bot/scanner.py

CEX-DEX Arbitrage Scanner comparing Bybit Spot and Raydium/Solana DEX (via Jupiter API).
"""
from __future__ import annotations
import asyncio
import aiohttp
from loguru import logger

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

    async def init_session(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.0))

    async def close_session(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def get_jupiter_quote(self, input_mint: str, output_mint: str, amount_raw: int) -> int | None:
        """Queries the Jupiter Quote API for exact swap output amount."""
        await self.init_session()
        url = "https://api.jup.ag/swap/v1/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": "50"  # 0.5% slippage tolerance
        }
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(data.get("outAmount", 0))
                else:
                    logger.warning(f"⚠️ Jupiter Quote API returned HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to reach Jupiter Quote API: {e}")
        return None

    async def scan(self, state: PortfolioState) -> dict | None:
        """
        Scans for profitable CEX-DEX spreads between Bybit and Solana DEX.
        Returns the best opportunity details if net spread > settings.min_arbitrage_spread_pct.
        """
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
        # Swap USDT -> SOL (DEX Buy)
        # $25 USDT = 25 * 1_000_000 micro-USDT (6 decimals)
        usdt_in_raw = int(trade_size * 1_000_000)
        sol_out_raw = await self.get_jupiter_quote(USDT_MINT, SOL_MINT, usdt_in_raw)
        if not sol_out_raw or sol_out_raw == 0:
            return None

        sol_out = sol_out_raw / 1_000_000_000.0  # 9 decimals
        dex_buy_price = trade_size / sol_out
        self.last_dex_buy = dex_buy_price

        # Swap SOL -> USDT (DEX Sell)
        # Sell equivalent SOL size: trade_size / mid_price
        sol_in = trade_size / mid_price
        sol_in_raw = int(sol_in * 1_000_000_000.0)
        usdt_out_raw = await self.get_jupiter_quote(SOL_MINT, USDT_MINT, sol_in_raw)
        if not usdt_out_raw or usdt_out_raw == 0:
            return None

        usdt_out = usdt_out_raw / 1_000_000.0  # 6 decimals
        dex_sell_price = usdt_out / sol_in
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

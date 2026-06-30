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

SOL_MINT = "So11111111111111111111111111111111111111112"
WBTC_MINT = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
WETH_MINT = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
JUP_MINT = "JUPyiwrEE3E2nvwJPEe2GYCtRPCWvLcUMptcLUgJ575"
PYTH_MINT = "HZ128uz4g7D51MNGDxDwAd4NZf68Lrxm8ExKQ52C1mEe"
JTO_MINT = "jtojtome5hxZURJuKdfbeJrgQAebCc51Bt2VWggdf8B"
WIF_MINT = "EKpQEPVJj67vw7dKj48KVJ6uy1m978Cg7C1xpx1Xm791"
BONK_MINT = "DezXAZ8z7PnrFcdubkPGoG6SLxxRQQfZH3PpZrPM24a"
POPCAT_MINT = "7GCihJUkPG4thJZmW212pqGN8gQJ83Un2FzeztWmoPot"

DECIMALS = {
    "SOL": 9,
    "BTC": 8,
    "ETH": 8,
    "USDC": 6,
    "USDT": 6,
    "JUP": 6,
    "PYTH": 6,
    "JTO": 9,
    "WIF": 6,
    "BONK": 5,
    "POPCAT": 9
}


class CexDexArbitrageScanner:
    def __init__(self, price_feed: BybitPriceFeed):
        self.price_feed = price_feed
        self.session = None
        self.last_dex_buy = 0.0
        self.last_dex_sell = 0.0
        self.jupiter_cooldown_until = 0.0
        
        # Configure multiple CEX-DEX spatial routes to scan
        self.routes = [
            {"base": "SOL", "quote": "USDT", "base_mint": SOL_MINT, "quote_mint": USDT_MINT},
            {"base": "BTC", "quote": "USDT", "base_mint": WBTC_MINT, "quote_mint": USDT_MINT},
            {"base": "ETH", "quote": "USDT", "base_mint": WETH_MINT, "quote_mint": USDT_MINT},
            {"base": "JUP", "quote": "USDT", "base_mint": JUP_MINT, "quote_mint": USDT_MINT},
            {"base": "PYTH", "quote": "USDT", "base_mint": PYTH_MINT, "quote_mint": USDT_MINT},
            {"base": "JTO", "quote": "USDT", "base_mint": JTO_MINT, "quote_mint": USDT_MINT},
            {"base": "WIF", "quote": "USDT", "base_mint": WIF_MINT, "quote_mint": USDT_MINT},
            {"base": "BONK", "quote": "USDT", "base_mint": BONK_MINT, "quote_mint": USDT_MINT},
            {"base": "POPCAT", "quote": "USDT", "base_mint": POPCAT_MINT, "quote_mint": USDT_MINT},
            
            {"base": "SOL", "quote": "USDC", "base_mint": SOL_MINT, "quote_mint": USDC_MINT},
            {"base": "BTC", "quote": "USDC", "base_mint": WBTC_MINT, "quote_mint": USDC_MINT},
            {"base": "ETH", "quote": "USDC", "base_mint": WETH_MINT, "quote_mint": USDC_MINT},
            {"base": "JUP", "quote": "USDC", "base_mint": JUP_MINT, "quote_mint": USDC_MINT},
            {"base": "PYTH", "quote": "USDC", "base_mint": PYTH_MINT, "quote_mint": USDC_MINT},
            {"base": "JTO", "quote": "USDC", "base_mint": JTO_MINT, "quote_mint": USDC_MINT},
            {"base": "WIF", "quote": "USDC", "base_mint": WIF_MINT, "quote_mint": USDC_MINT},
            {"base": "BONK", "quote": "USDC", "base_mint": BONK_MINT, "quote_mint": USDC_MINT},
            {"base": "POPCAT", "quote": "USDC", "base_mint": POPCAT_MINT, "quote_mint": USDC_MINT},
        ]
        self.route_index = 0

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
        headers = {}
        if settings.jupiter_api_key:
            headers["x-api-key"] = settings.jupiter_api_key
        
        base_delay = 0.5
        for attempt in range(max_retries):
            # Simulated 429 errors in paper mode
            if settings.trading_mode == "paper" and settings.jupiter_429_sim_prob > 0:
                if random.random() < settings.jupiter_429_sim_prob:
                    logger.warning(f"⚠️ [Simulated 429] Jupiter API Rate Limit (Attempt {attempt+1}/{max_retries}). Retrying...")
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue

            try:
                async with self.session.get(url, params=params, headers=headers) as resp:
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

    async def get_jupiter_quote_full(self, input_mint: str, output_mint: str, amount_raw: int, max_retries: int = 3) -> dict | None:
        """Queries the Jupiter Quote API and returns the entire JSON response dict (useful for live swaps)."""
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
        headers = {}
        if settings.jupiter_api_key:
            headers["x-api-key"] = settings.jupiter_api_key
        
        base_delay = 0.5
        for attempt in range(max_retries):
            if settings.trading_mode == "paper" and settings.jupiter_429_sim_prob > 0:
                if random.random() < settings.jupiter_429_sim_prob:
                    logger.warning(f"⚠️ [Simulated 429] Jupiter API Rate Limit (Attempt {attempt+1}/{max_retries}). Retrying...")
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue

            try:
                async with self.session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
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

    async def get_latest_dex_prices(self, base: str, quote: str, base_mint: str, quote_mint: str, mid_price: float, trade_size: float, base_in: float) -> tuple[float, float, float, float]:
        """Queries Jupiter Quote API or falls back to simulated prices if rate-limited."""
        base_dec = DECIMALS.get(base, 9)
        quote_dec = DECIMALS.get(quote, 6)

        quote_in_raw = int(trade_size * (10 ** quote_dec))
        base_out_raw = await self.get_jupiter_quote(quote_mint, base_mint, quote_in_raw)

        base_in_raw = int(base_in * (10 ** base_dec))
        quote_out_raw = await self.get_jupiter_quote(base_mint, quote_mint, base_in_raw)

        # Handle Fallback if Jupiter is rate-limited
        if not base_out_raw or not quote_out_raw:
            # Neutral spread matching Bybit mid-price (0.1% spread adjustment)
            # This ensures no artificial arbitrage trades are triggered during rate limits.
            dex_buy_price = mid_price * 1.001
            base_out = trade_size / dex_buy_price
            
            dex_sell_price = mid_price * 0.999
            quote_out = base_in * dex_sell_price
        else:
            base_out = base_out_raw / (10 ** base_dec)
            dex_buy_price = trade_size / base_out
            
            quote_out = quote_out_raw / (10 ** quote_dec)
            dex_sell_price = quote_out / base_in

        return dex_buy_price, base_out, dex_sell_price, quote_out

    async def scan(self, state: PortfolioState) -> dict | None:
        """
        Scans for profitable CEX-DEX spreads using a round-robin schedule
        to respect rate limits.
        """
        # Check if trading is paused due to drawdown
        if state.max_drawdown_paused:
            logger.warning("⚠️ Trading paused due to Max Drawdown Guard limit breach.")
            return None

        if not self.routes:
            return None

        # Round-robin selection of route
        r = self.routes[self.route_index]
        self.route_index = (self.route_index + 1) % len(self.routes)

        base = r["base"]
        quote = r["quote"]
        base_mint = r["base_mint"]
        quote_mint = r["quote_mint"]

        # Bybit ticker symbol e.g., "SOLUSDT" or "BTCUSDC"
        bybit_symbol = f"{base}{quote}"
        bid, ask, _, _ = self.price_feed.get_best_bid_ask(bybit_symbol)
        if not bid or not ask:
            return None

        # Check Bybit price freshness
        now = asyncio.get_event_loop().time()
        price_age = now - self.price_feed.last_update_ts
        if price_age > settings.max_price_age_s:
            logger.warning(f"⚠️ Stale Bybit prices for {bybit_symbol} (Age: {price_age:.2f}s). Skipping.")
            return None

        mid_price = (bid + ask) / 2.0
        trade_size = settings.trade_size_usdt

        # 2. Query Jupiter Quote API for DEX prices
        base_in = trade_size / mid_price
        dex_buy_price, base_out, dex_sell_price, quote_out = await self.get_latest_dex_prices(
            base, quote, base_mint, quote_mint, mid_price, trade_size, base_in
        )
        if base == "SOL" and quote == "USDT":
            self.last_dex_buy = dex_buy_price
            self.last_dex_sell = dex_sell_price

        # Option A: DEX Buy (Cash->Asset) and CEX Sell (Spot Sell Asset)
        opt_a_net = -999.0
        cex_asset_bal = state.cex_assets.get(base, 0.0)
        if state.dex_cash >= trade_size and cex_asset_bal >= base_out:
            gross_a = (bid / dex_buy_price) - 1.0
            fees_a = 0.0010 + (0.05 / trade_size)
            opt_a_net = gross_a - fees_a

        # Option B: CEX Buy (Spot Buy Asset) and DEX Sell (Asset->Cash)
        opt_b_net = -999.0
        dex_asset_bal = state.dex_assets.get(base, 0.0)
        if state.cex_cash >= trade_size and dex_asset_bal >= base_in:
            gross_b = (dex_sell_price / ask) - 1.0
            fees_b = 0.0010 + (0.05 / trade_size)
            opt_b_net = gross_b - fees_b

        # 4. Find Best Opportunity
        best_opt = None
        if opt_a_net >= settings.min_arbitrage_spread_pct and opt_a_net >= opt_b_net:
            quote_resp = None
            if settings.trading_mode == "live":
                quote_dec = DECIMALS.get(quote, 6)
                quote_in_raw = int(trade_size * (10 ** quote_dec))
                quote_resp = await self.get_jupiter_quote_full(quote_mint, base_mint, quote_in_raw)
            best_opt = {
                "route": "DEX-BUY_CEX-SELL",
                "net_spread": opt_a_net,
                "gross_spread": (bid / dex_buy_price) - 1.0,
                "buy_price": dex_buy_price,
                "sell_price": bid,
                "size_asset": base_out,
                "size_usdt": trade_size,
                "base": base,
                "quote": quote,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "quote_response": quote_resp,
            }
        elif opt_b_net >= settings.min_arbitrage_spread_pct and opt_b_net >= opt_a_net:
            quote_resp = None
            if settings.trading_mode == "live":
                base_dec = DECIMALS.get(base, 9)
                base_in_raw = int(base_in * (10 ** base_dec))
                quote_resp = await self.get_jupiter_quote_full(base_mint, quote_mint, base_in_raw)
            best_opt = {
                "route": "CEX-BUY_DEX-SELL",
                "net_spread": opt_b_net,
                "gross_spread": (dex_sell_price / ask) - 1.0,
                "buy_price": ask,
                "sell_price": dex_sell_price,
                "size_asset": base_in,
                "size_usdt": trade_size,
                "base": base,
                "quote": quote,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "quote_response": quote_resp,
            }

        # Keep for UI compatibility if profitable
        if best_opt:
            self.last_dex_buy = best_opt["buy_price"]
            self.last_dex_sell = best_opt["sell_price"]

        return best_opt

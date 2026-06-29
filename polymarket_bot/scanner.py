"""
polymarket_bot/scanner.py

Multi-route triangular arbitrage scanning engine for Bybit Spot.
Supports BTC-ETH and BTC-SOL routes.
"""
from __future__ import annotations
import asyncio
from loguru import logger

from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed


class ArbitrageScanner:
    def __init__(self, price_feed: BybitPriceFeed):
        self.price_feed = price_feed
        # Track log rate-limiting per route and direction
        self.last_log_time = {}

    def scan(self) -> dict | None:
        """
        Scans all configured triangular routes.
        Returns the first profitable opportunity found, or None.
        """
        # Ensure price data is fresh
        now = asyncio.get_event_loop().time()
        if self.price_feed.last_update_ts == 0.0:
            return None
        price_age = now - self.price_feed.last_update_ts
        if price_age > settings.max_price_age_s:
            logger.warning(f"⚠️ Stale market prices (Age: {price_age:.2f}s > limit {settings.max_price_age_s}s). Skipping scan.")
            return None

        # Define triangular routes
        # Name: route identifier
        # Base: e.g. BTCUSDT (Base asset)
        # Target: e.g. ETHUSDT or SOLUSDT (Target asset)
        # Cross: e.g. ETHBTC or SOLBTC (Target traded in Base asset)
        routes = [
            {
                "name": "BTC-ETH",
                "base_pair": "BTCUSDT",
                "target_pair": "ETHUSDT",
                "cross_pair": "ETHBTC",
                "base_symbol": "BTC",
                "target_symbol": "ETH",
            },
            {
                "name": "BTC-SOL",
                "base_pair": "BTCUSDT",
                "target_pair": "SOLUSDT",
                "cross_pair": "SOLBTC",
                "base_symbol": "BTC",
                "target_symbol": "SOL",
            }
        ]

        fee_rate = 0.0010  # Bybit standard spot fee (0.1% for maker/taker)
        total_fees = 3 * fee_rate

        for route in routes:
            name = route["name"]
            base_p = route["base_pair"]
            target_p = route["target_pair"]
            cross_p = route["cross_pair"]
            base_s = route["base_symbol"]
            target_s = route["target_symbol"]

            # Fetch best bid/ask
            bid_base, ask_base, _, _ = self.price_feed.get_best_bid_ask(base_p)
            bid_target, ask_target, _, _ = self.price_feed.get_best_bid_ask(target_p)
            bid_cross, ask_cross, _, _ = self.price_feed.get_best_bid_ask(cross_p)

            # Ensure all pairs in the route are initialized
            if not all([bid_base, ask_base, bid_target, ask_target, bid_cross, ask_cross]):
                continue

            # ── 1. FORWARD CYCLE (USDT -> Base -> Target -> USDT) ──
            # Leg 1: Buy base/USDT (spend USDT, get base)
            # Leg 2: Buy target/base (spend base, get target)
            # Leg 3: Sell target/USDT (sell target, get USDT)
            forward_gross = (1.0 / ask_base) * (1.0 / ask_cross) * bid_target
            forward_net_edge = (forward_gross - 1.0) - total_fees

            # ── 2. REVERSE CYCLE (USDT -> Target -> Base -> USDT) ──
            # Leg 1: Buy target/USDT (spend USDT, get target)
            # Leg 2: Sell target/base (sell target, get base)
            # Leg 3: Sell base/USDT (sell base, get USDT)
            reverse_gross = (1.0 / ask_target) * bid_cross * bid_base
            reverse_net_edge = (reverse_gross - 1.0) - total_fees

            # Check forward opportunity
            if forward_net_edge >= settings.min_net_edge_pct:
                logger.success(f"🔥 [{name}] Forward Arb Found! Gross: {forward_gross-1.0:.4%}, Fees: {total_fees:.2%}, Net Edge: {forward_net_edge:.4%}")
                # Translate symbols to slash pairs for CCXT (e.g. BTCUSDT -> BTC/USDT)
                return {
                    "route_name": name,
                    "direction": "FORWARD",
                    "net_edge": forward_net_edge,
                    "prices": {
                        base_p: ask_base,
                        cross_p: ask_cross,
                        target_p: bid_target,
                    },
                    "legs": [
                        {"pair": f"{base_s}/USDT", "side": "buy", "type": "cost", "currency": "USDT"},
                        {"pair": f"{target_s}/{base_s}", "side": "buy", "type": "cost", "currency": base_s},
                        {"pair": f"{target_s}/USDT", "side": "sell", "type": "amount", "currency": target_s},
                    ]
                }
            else:
                log_key = f"{name}_FORWARD"
                if (forward_gross - 1.0) >= 0.0020:
                    last_log = self.last_log_time.get(log_key, 0.0)
                    if now - last_log >= 10.0:
                        self.last_log_time[log_key] = now
                        logger.info(f"⏭️ [{name}] Forward Arb Rejected: Gross {forward_gross-1.0:.4%} (Fees {total_fees:.2%}) -> Net {forward_net_edge:.4%} below threshold {settings.min_net_edge_pct:.4%}")

            # Check reverse opportunity
            if reverse_net_edge >= settings.min_net_edge_pct:
                logger.success(f"🔥 [{name}] Reverse Arb Found! Gross: {reverse_gross-1.0:.4%}, Fees: {total_fees:.2%}, Net Edge: {reverse_net_edge:.4%}")
                return {
                    "route_name": name,
                    "direction": "REVERSE",
                    "net_edge": reverse_net_edge,
                    "prices": {
                        target_p: ask_target,
                        cross_p: bid_cross,
                        base_p: bid_base,
                    },
                    "legs": [
                        {"pair": f"{target_s}/USDT", "side": "buy", "type": "cost", "currency": "USDT"},
                        {"pair": f"{target_s}/{base_s}", "side": "sell", "type": "amount", "currency": target_s},
                        {"pair": f"{base_s}/USDT", "side": "sell", "type": "amount", "currency": base_s},
                    ]
                }
            else:
                log_key = f"{name}_REVERSE"
                if (reverse_gross - 1.0) >= 0.0020:
                    last_log = self.last_log_time.get(log_key, 0.0)
                    if now - last_log >= 10.0:
                        self.last_log_time[log_key] = now
                        logger.info(f"⏭️ [{name}] Reverse Arb Rejected: Gross {reverse_gross-1.0:.4%} (Fees {total_fees:.2%}) -> Net {reverse_net_edge:.4%} below threshold {settings.min_net_edge_pct:.4%}")

        return None

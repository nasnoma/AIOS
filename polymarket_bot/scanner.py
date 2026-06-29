"""
polymarket_bot/scanner.py

Triangular arbitrage scanning engine for USDT -> BTC -> ETH -> USDT.
"""
from __future__ import annotations
from loguru import logger

from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed

class ArbitrageScanner:
    def __init__(self, price_feed: BybitPriceFeed):
        self.price_feed = price_feed

    def scan(self) -> dict | None:
        """
        Scans for triangular arbitrage opportunities.
        Returns opportunity dict if found, else None.
        """
        # Fetch best bid/ask for all three pairs
        bid_btc_usdt, ask_btc_usdt, bid_sz_btc_usdt, ask_sz_btc_usdt = self.price_feed.get_best_bid_ask("BTCUSDT")
        bid_eth_usdt, ask_eth_usdt, bid_sz_eth_usdt, ask_sz_eth_usdt = self.price_feed.get_best_bid_ask("ETHUSDT")
        bid_eth_btc, ask_eth_btc, bid_sz_eth_btc, ask_sz_eth_btc = self.price_feed.get_best_bid_ask("ETHBTC")

        # Ensure feed is fully initialized
        if not all([bid_btc_usdt, ask_btc_usdt, bid_eth_usdt, ask_eth_usdt, bid_eth_btc, ask_eth_btc]):
            return None

        fee_rate = 0.0010  # Bybit standard spot fee (0.1% for maker/taker)
        total_fees = 3 * fee_rate

        # ── 1. FORWARD CYCLE (USDT -> BTC -> ETH -> USDT) ──
        # Buy BTC/USDT at ask_btc_usdt
        # Buy ETH/BTC at ask_eth_btc (selling BTC for ETH)
        # Sell ETH/USDT at bid_eth_usdt
        forward_gross = (1.0 / ask_btc_usdt) * (1.0 / ask_eth_btc) * bid_eth_usdt
        forward_net_edge = (forward_gross - 1.0) - total_fees

        # ── 2. REVERSE CYCLE (USDT -> ETH -> BTC -> USDT) ──
        # Buy ETH/USDT at ask_eth_usdt
        # Sell ETH/BTC at bid_eth_btc (obtaining BTC)
        # Sell BTC/USDT at bid_btc_usdt
        reverse_gross = (1.0 / ask_eth_usdt) * bid_eth_btc * bid_btc_usdt
        reverse_net_edge = (reverse_gross - 1.0) - total_fees

        # Check thresholds
        if forward_net_edge >= settings.min_net_edge_pct:
            logger.success(f"🔥 Forward Arb Found! Net Edge: {forward_net_edge:.4%}")
            return {
                "direction": "FORWARD",
                "net_edge": forward_net_edge,
                "gross_edge": forward_gross - 1.0,
                "prices": {
                    "BTCUSDT": ask_btc_usdt,
                    "ETHBTC": ask_eth_btc,
                    "ETHUSDT": bid_eth_usdt,
                },
                "sizes": {
                    "BTCUSDT": ask_sz_btc_usdt,
                    "ETHBTC": ask_sz_eth_btc,
                    "ETHUSDT": bid_sz_eth_usdt,
                }
            }

        if reverse_net_edge >= settings.min_net_edge_pct:
            logger.success(f"🔥 Reverse Arb Found! Net Edge: {reverse_net_edge:.4%}")
            return {
                "direction": "REVERSE",
                "net_edge": reverse_net_edge,
                "gross_edge": reverse_gross - 1.0,
                "prices": {
                    "ETHUSDT": ask_eth_usdt,
                    "ETHBTC": bid_eth_btc,
                    "BTCUSDT": bid_btc_usdt,
                },
                "sizes": {
                    "ETHUSDT": ask_sz_eth_usdt,
                    "ETHBTC": bid_sz_eth_btc,
                    "BTCUSDT": bid_sz_btc_usdt,
                }
            }

        return None

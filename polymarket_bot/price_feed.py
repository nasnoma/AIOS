"""
polymarket_bot/price_feed.py

Price feed for the Polymarket Scalping Bot.
Subscribes to Bybit public Spot WebSocket to receive real-time price updates for BTC, ETH, and SOL.
Tracks price history to support strike price retrieval and momentum calculations.
"""
from __future__ import annotations
import asyncio
import json
import time
from loguru import logger
import websockets

from polymarket_bot.config import settings


class PriceFeed:
    def __init__(self, assets: list[str]):
        self.assets = [a.upper() for a in assets]
        self.latest_prices: dict[str, float] = {a: 0.0 for a in self.assets}
        # Keep price history as a list of (timestamp, price) tuples per asset
        self.history: dict[str, list[tuple[float, float]]] = {a: [] for a in self.assets}
        # CVD: running cumulative volume delta and its history (timestamp, cumulative_delta)
        self._cvd_running: dict[str, float] = {a: 0.0 for a in self.assets}
        self.cvd_history: dict[str, list[tuple[float, float]]] = {a: [] for a in self.assets}
        self.is_connected = False
        self._ws_task = None
        self._shutdown_event = asyncio.Event()

    def get_latest(self, asset: str) -> float:
        """Returns the latest price for the asset."""
        return self.latest_prices.get(asset.upper(), 0.0)

    def get_strike(self, asset: str, timestamp: float) -> float:
        """
        Returns the historical price for the asset closest to the specified timestamp.
        Falls back to the latest price if history is empty.
        """
        asset_upper = asset.upper()
        history = self.history.get(asset_upper, [])
        if not history:
            return self.get_latest(asset_upper)

        # Find the entry closest to the timestamp
        closest_price = history[0][1]
        min_diff = abs(history[0][0] - timestamp)
        for ts, price in history:
            diff = abs(ts - timestamp)
            if diff < min_diff:
                min_diff = diff
                closest_price = price
        return closest_price

    def get_momentum(self, asset: str, lookback_s: int) -> float:
        """
        Calculates momentum (latest price - historical price at lookback_s seconds ago).
        """
        asset_upper = asset.upper()
        latest = self.get_latest(asset_upper)
        if latest == 0.0:
            return 0.0

        now = time.time()
        target_ts = now - lookback_s
        history = self.history.get(asset_upper, [])
        if not history:
            return 0.0

        # Find closest price at target_ts
        closest_price = history[0][1]
        min_diff = abs(history[0][0] - target_ts)
        for ts, price in history:
            diff = abs(ts - target_ts)
            if diff < min_diff:
                min_diff = diff
                closest_price = price

        return latest - closest_price

    def get_cvd_delta(self, asset: str, lookback_s: int) -> float:
        """
        Returns the change in Cumulative Volume Delta over the last lookback_s seconds.
        Positive = net buying pressure; Negative = net selling pressure.
        """
        asset_upper = asset.upper()
        history = self.cvd_history.get(asset_upper, [])
        if not history:
            return 0.0
        now = time.time()
        target_ts = now - lookback_s
        # Find CVD value closest to lookback point
        past_cvd = history[0][1]
        min_diff = abs(history[0][0] - target_ts)
        for ts, cvd_val in history:
            diff = abs(ts - target_ts)
            if diff < min_diff:
                min_diff = diff
                past_cvd = cvd_val
        current_cvd = history[-1][1]
        return current_cvd - past_cvd

    async def start(self) -> None:
        """Starts the price feed listener background task."""
        self._shutdown_event.clear()
        self._ws_task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        """Stops the price feed listener background task."""
        self._shutdown_event.set()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def _listen_loop(self) -> None:
        """Websocket client main loop with auto-reconnect."""
        # Map target assets to Bybit spot symbols (e.g. BTC -> BTCUSDT)
        bybit_symbols = {a: f"{a}USDT" for a in self.assets}
        inverse_symbols = {f"{a}USDT": a for a in self.assets}
        orderbook_topics = [f"orderbook.1.{sym}" for sym in bybit_symbols.values()]
        trade_topics = [f"publicTrade.{sym}" for sym in bybit_symbols.values()]
        topics = orderbook_topics + trade_topics

        ws_url = "wss://stream.bybit.com/v5/public/spot"

        while not self._shutdown_event.is_set():
            try:
                logger.info(f"Connecting to Bybit WebSocket at {ws_url}...")
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self.is_connected = True
                    logger.info("Connected to Bybit public Spot WebSocket.")

                    # Subscribe to orderbook + trade topics
                    sub_msg = {"op": "subscribe", "args": topics}
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Subscribed to Bybit orderbook + trade topics: {orderbook_topics + trade_topics}")

                    async for message in ws:
                        if self._shutdown_event.is_set():
                            break

                        data = json.loads(message)
                        if "topic" not in data or "data" not in data:
                            continue

                        topic = data["topic"]
                        now = time.time()

                        # ── Orderbook: update mid price ──────────────────────
                        if topic.startswith("orderbook."):
                            s_data = data["data"]
                            symbol = s_data.get("s")
                            asset = inverse_symbols.get(symbol)
                            if not asset:
                                continue
                            bids = s_data.get("b", [])
                            asks = s_data.get("a", [])
                            if bids and asks:
                                bid = float(bids[0][0])
                                ask = float(asks[0][0])
                                mid = (bid + ask) / 2.0
                                self.latest_prices[asset] = mid
                                self.history[asset].append((now, mid))
                                one_hour_ago = now - 3600
                                self.history[asset] = [
                                    item for item in self.history[asset] if item[0] > one_hour_ago
                                ]

                        # ── Trades: accumulate CVD ───────────────────────────
                        elif topic.startswith("publicTrade."):
                            for trade in data["data"]:
                                symbol = trade.get("s", "")
                                asset = inverse_symbols.get(symbol)
                                if not asset:
                                    continue
                                size = float(trade.get("v", 0))
                                # Taker side Buy = aggressive buyer lifting ask → positive delta
                                delta = size if trade.get("S") == "Buy" else -size
                                self._cvd_running[asset] += delta
                                self.cvd_history[asset].append((now, self._cvd_running[asset]))
                                # Keep last 1 hour only
                                one_hour_ago = now - 3600
                                self.cvd_history[asset] = [
                                    item for item in self.cvd_history[asset] if item[0] > one_hour_ago
                                ]

            except (websockets.exceptions.ConnectionClosed, Exception) as e:
                self.is_connected = False
                if not self._shutdown_event.is_set():
                    logger.error(f"Bybit WebSocket error: {e}. Reconnecting in 3s...")
                    await asyncio.sleep(3)

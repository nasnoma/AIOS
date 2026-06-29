"""
polymarket_bot/price_feed.py

Low-latency WebSocket price feed subscribing to Bybit v5 Spot orderbook depth 1.
"""
from __future__ import annotations
import asyncio
import json
from loguru import logger
import websockets

from polymarket_bot.config import settings

class BybitPriceFeed:
    def __init__(self):
        self.orderbooks = {
            "BTCUSDT": {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0},
            "ETHUSDT": {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0},
            "ETHBTC": {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0},
            "SOLUSDT": {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0},
            "SOLBTC": {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0},
        }
        self.is_connected = False
        # In paper trading, always pull from mainnet for real price data.
        # In live trading, use testnet/mainnet based on bybit_testnet configuration.
        if settings.trading_mode == "paper":
            self.ws_url = "wss://stream.bybit.com/v5/public/spot"
        else:
            self.ws_url = (
                "wss://stream-testnet.bybit.com/v5/public/spot"
                if settings.bybit_testnet
                else "wss://stream.bybit.com/v5/public/spot"
            )
        self.last_update_ts = 0.0

    def get_best_bid_ask(self, symbol: str) -> tuple[float, float, float, float]:
        """Returns (bid, ask, bid_size, ask_size) for the symbol."""
        ob = self.orderbooks.get(symbol, {"bid": 0.0, "ask": 0.0, "bid_size": 0.0, "ask_size": 0.0})
        return ob["bid"], ob["ask"], ob["bid_size"], ob["ask_size"]

    async def connect_and_listen(self):
        """Websocket client main loop with auto-reconnect."""
        while True:
            try:
                logger.info(f"Connecting to Bybit WebSocket at {self.ws_url}...")
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self.is_connected = True
                    logger.info("Connected to Bybit public Spot WebSocket.")

                    # Subscribe to depth 1 orderbooks
                    topics = [
                        "orderbook.1.BTCUSDT",
                        "orderbook.1.ETHUSDT",
                        "orderbook.1.ETHBTC",
                        "orderbook.1.SOLUSDT",
                        "orderbook.1.SOLBTC",
                    ]
                    sub_msg = {
                        "op": "subscribe",
                        "args": topics
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Subscribed to Bybit orderbook topics: {topics}")

                    async for message in ws:
                        data = json.loads(message)
                        if "topic" in data and "data" in data:
                            topic = data["topic"]
                            s_data = data["data"]
                            symbol = s_data.get("s")
                            
                            # Parse best bids and asks
                            bids = s_data.get("b", [])
                            asks = s_data.get("a", [])
                            if bids and asks:
                                self.orderbooks[symbol] = {
                                    "bid": float(bids[0][0]),
                                    "ask": float(asks[0][0]),
                                    "bid_size": float(bids[0][1]),
                                    "ask_size": float(asks[0][1]),
                                }
                                self.last_update_ts = asyncio.get_event_loop().time()

            except (websockets.exceptions.ConnectionClosed, Exception) as e:
                self.is_connected = False
                logger.error(f"Bybit WebSocket error: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

"""
polymarket_bot/clob_client.py

Polymarket CLOB REST + WebSocket client.
Handles:
  - L1 wallet signature authentication (EIP-712 via py-clob-client)
  - L2 HMAC-signed REST calls (order book, place/cancel orders)
  - Real-time order book streaming (Polymarket WS)

The in-memory book cache is updated by the stream and read zero-copy
by the strategy module — no blocking I/O in the hot path.
"""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
import aiohttp
import websockets
from loguru import logger

CLOB_HOST = "https://clob.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_RECONNECT_DELAY = 3


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)   # sorted desc
    asks: list[OrderBookLevel] = field(default_factory=list)   # sorted asc
    updated_at: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round((self.best_bid + self.best_ask) / 2, 6)
        return self.best_bid or self.best_ask

    def check_liquidity(self, size_usd: float, max_slippage_usd: float) -> tuple[bool, float]:
        """
        Evaluate L2 asks to check if there is sufficient depth to fill size_usd
        with expected slippage <= max_slippage_usd.
        Returns (is_approved, expected_slippage).
        """
        if not self.asks:
            return False, 999.0

        best_ask = self.asks[0].price
        limit_price = best_ask + max_slippage_usd

        filled_usd = 0.0
        filled_shares = 0.0
        for level in self.asks:
            if level.price > limit_price:
                break

            needed_usd = size_usd - filled_usd
            level_value_usd = level.size * level.price

            if level_value_usd >= needed_usd:
                shares = needed_usd / level.price
                filled_shares += shares
                filled_usd += needed_usd
                break
            else:
                filled_shares += level.size
                filled_usd += level_value_usd

        if filled_usd < size_usd:
            return False, 999.0

        expected_avg_price = filled_usd / filled_shares
        expected_slippage = expected_avg_price - best_ask
        return True, round(expected_slippage, 6)


def _build_client():
    """
    Build and authenticate a py-clob-client ClobClient.
    Returns None if credentials are not configured (e.g. paper mode without live keys).
    """
    from polymarket_bot.config import settings
    if not settings.polymarket_private_key or settings.polymarket_private_key.startswith("0x..."):
        logger.debug("CLOB: No private key configured — REST auth disabled (OK for paper mode)")
        return None
    try:
        from py_clob_client_v2.client import ClobClient
        
        # Instantiate a temporary client to derive the EOA address
        temp_client = ClobClient(
            CLOB_HOST,
            key=settings.polymarket_private_key,
            chain_id=137,
            signature_type=settings.polymarket_signature_type,
            funder=settings.polymarket_funder or None,
        )
        eoa = temp_client.get_address()
        funder = settings.polymarket_funder or None
        sig_type = settings.polymarket_signature_type
        
        # If funder is not set or set to EOA itself, attempt to resolve via Polymarket profile API
        if not funder or funder.lower() == eoa.lower():
            import urllib.request
            import json
            url = f"https://polymarket.com/api/profile/userData?address={eoa}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    res_body = response.read().decode("utf-8")
                    if res_body and res_body.strip() != "null":
                        data = json.loads(res_body)
                        if data and isinstance(data, dict):
                            proxy_wallet = data.get("proxyWallet")
                            if proxy_wallet and proxy_wallet.lower() != eoa.lower():
                                logger.info(f"CLOB: Auto-detected Polymarket proxy wallet (Deposit Wallet): {proxy_wallet}")
                                funder = proxy_wallet
                                if sig_type == 0:
                                    # Default to 1 (POLY_PROXY / Magic Link) or 3 (POLY_1271 / Privy)
                                    sig_type = 1
                                    logger.info("CLOB: Auto-switching signature type to 1 (Proxy/Magic Link)")
            except Exception as ex:
                logger.debug(f"CLOB: Failed to auto-detect proxy wallet: {ex}")
        
        # Proactively warn if using a proxy/smart wallet signature type but funder is not set
        if sig_type > 0 and not funder:
            logger.warning(
                "CLOB: POLYMARKET_SIGNATURE_TYPE is set to proxy/deposit flow, but "
                "no funder is configured! Order placement will likely fail."
            )
            
        client = ClobClient(
            CLOB_HOST,
            key=settings.polymarket_private_key,
            chain_id=137,  # Polygon Mainnet
            signature_type=sig_type,
            funder=funder,
        )
        client.set_api_creds(client.create_or_derive_api_key())
        logger.info("CLOB: Authenticated successfully (L1+L2)")
        return client
    except Exception as e:
        logger.error(f"CLOB: Authentication failed: {e}")
        return None


class ClobBookCache:
    """
    In-memory cache of live order books, updated by the WebSocket stream.
    Thread-safe reads via asyncio.Lock.
    """

    def __init__(self):
        self._books: dict[str, OrderBook] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._subscribed_token_ids: set[str] = set()
        self._client = None  # lazy-init
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def client(self):
        if self._client is None:
            self._client = _build_client()
        return self._client

    async def subscribe(self, token_ids: list[str]) -> None:
        """Set the active token IDs and trigger a clean reconnect if the list changed."""
        async with self._lock:
            new_set = set(token_ids)
            if new_set == self._subscribed_token_ids:
                return
            self._subscribed_token_ids = new_set
            ws = self._ws

        if ws:
            try:
                logger.info(f"ClobBookCache: Token subscription list changed. Reconnecting to WS...")
                await ws.close()
            except Exception as e:
                logger.debug(f"ClobBookCache: Error closing WS: {e}")

    async def get_book(self, token_id: str) -> Optional[OrderBook]:
        """Return the latest cached order book for a token."""
        async with self._lock:
            return self._books.get(token_id)

    async def get_mid_price(self, token_id: str) -> Optional[float]:
        book = await self.get_book(token_id)
        return book.mid_price if book else None

    async def start(self) -> None:
        """Start the background WebSocket streaming task."""
        self._running = True
        self._task = asyncio.create_task(self._stream_loop(), name="clob_stream")
        logger.info("ClobBookCache: WebSocket streaming started")

    async def stop(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _stream_loop(self) -> None:
        """Connect to Polymarket CLOB WebSocket and receive order book updates."""
        reconnect_delay = 3.0
        while self._running:
            try:
                async with websockets.connect(CLOB_WS, ping_interval=20) as ws:
                    logger.info("ClobBookCache: Polymarket WS connected")
                    reconnect_delay = 3.0  # Reset delay on success
                    async with self._lock:
                        self._ws = ws
                        token_ids = list(self._subscribed_token_ids)
                    if token_ids:
                        sub_msg = json.dumps({"type": "market", "assets_ids": token_ids})
                        await ws.send(sub_msg)
                        logger.info(f"ClobBookCache: Subscribed to {len(token_ids)} tokens on connect")

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle_ws_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                # If we closed the socket intentionally, log at info/debug and reconnect immediately
                async with self._lock:
                    self._ws = None
                if not self._running:
                    break
                logger.warning(f"ClobBookCache: WS disconnected ({e}), reconnecting in {reconnect_delay:.1f}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(60.0, reconnect_delay * 1.5)

    async def _handle_ws_message(self, raw: str) -> None:
        """Parse and apply an order book update from the WS stream."""
        try:
            if not raw or raw.strip() == "":
                return
            msgs = json.loads(raw)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for msg in msgs:
                token_id = msg.get("asset_id") or msg.get("market")
                if not token_id:
                    continue
                event_type = msg.get("event_type", "")
                if event_type in ("book", "price_change"):
                    await self._update_book(token_id, msg)
        except Exception as e:
            logger.debug(f"ClobBookCache: WS parse error: {e} | Raw message: {raw!r}")

    async def _update_book(self, token_id: str, msg: dict) -> None:
        def _parse_levels(raw_levels: list) -> list[OrderBookLevel]:
            levels = []
            for lv in (raw_levels or []):
                try:
                    levels.append(OrderBookLevel(price=float(lv["price"]), size=float(lv["size"])))
                except (KeyError, ValueError):
                    pass
            return levels

        bids = _parse_levels(msg.get("bids", []))
        asks = _parse_levels(msg.get("asks", []))
        bids.sort(key=lambda x: -x.price)
        asks.sort(key=lambda x: x.price)

        async with self._lock:
            self._books[token_id] = OrderBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                updated_at=time.time(),
            )

    # ── REST order placement (live mode only) ───────────────────────────────

    async def fetch_book_rest(self, token_id: str) -> Optional[OrderBook]:
        """
        Fetch the current order book via REST (public endpoint, works in paper mode too).
        """
        now = time.time()
        if not hasattr(self, "_last_rest_fetch_time"):
            self._last_rest_fetch_time = {}
        
        # Cooldown: limit REST fetches to once per 10 seconds per token to prevent HTTP 429
        last_fetch = self._last_rest_fetch_time.get(token_id, 0.0)
        if now - last_fetch < 10.0:
            async with self._lock:
                return self._books.get(token_id)
                
        self._last_rest_fetch_time[token_id] = now
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"CLOB REST book fetch failed with HTTP {resp.status} for {token_id}")
                    return None
                raw = await resp.json()
            
            bids = [OrderBookLevel(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])]
            asks = [OrderBookLevel(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])]
            bids.sort(key=lambda x: -x.price)
            asks.sort(key=lambda x: x.price)
            book = OrderBook(token_id=token_id, bids=bids, asks=asks, updated_at=time.time())
            async with self._lock:
                self._books[token_id] = book
            return book
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            logger.warning(f"CLOB REST book fetch failed for {token_id}: {type(e).__name__} ({err_msg})")
            return None

    async def place_limit_order(
        self,
        token_id: str,
        side: str,    # "BUY" | "SELL"
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Optional[tuple[str, float, float]]:
        """
        Place a limit order.
        Returns (order_id, filled_shares, fill_price) or None on failure.
        fill_price is the actual avg fill price reported by the CLOB; falls back
        to the submitted limit price if the field is absent.
        Must only be called in live mode.
        """
        if not self.client:
            logger.error("CLOB: Cannot place order — client not authenticated")
            return None
        try:
            from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
            if order_type == "GTC":
                order_type_enum = OrderType.GTC
            elif order_type in ("IOC", "FAK"):
                order_type_enum = OrderType.FAK
            else:
                order_type_enum = OrderType.FOK
            # Polymarket CLOB precision constraints:
            # 1. Taker amount (shares) supports max 4 decimals.
            # 2. Maker amount (cost in USD = size * price) supports max 2 decimals.
            target_usd = size * price
            target_k = int(round(target_usd * 100))
            valid_shares = None
            for i in range(200):
                for sign in (1, -1):
                    k = target_k + sign * i
                    if k <= 0:
                        continue
                    shares = round((k / 100.0) / price, 4)
                    # Check: shares × price rounds to exactly k/100 at 2 decimal places.
                    # Use round() comparison, not exact float equality — prices like 0.525
                    # produce floating-point residuals that would always fail a < 1e-9 test.
                    if round(shares * price, 2) == round(k / 100.0, 2):
                        valid_shares = shares
                        break
                if valid_shares is not None:
                    break

            if valid_shares is not None:
                size = valid_shares
            else:
                size = round(size, 4)
            price = round(price, 4)

            args = OrderArgsV2(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None,
                lambda: self.client.create_order(args)
            )
            resp = await loop.run_in_executor(
                None,
                 lambda: self.client.post_order(signed_order, order_type_enum)
            )
            order_id = resp.get("orderID") or resp.get("order_id")
            if not order_id:
                return None

            # Verify actual fill status for IOC/FAK orders
            if order_type_enum == OrderType.FAK or order_type_enum == OrderType.FOK:
                await asyncio.sleep(0.15)  # wait 150ms for order matching to process
                order_details = await loop.run_in_executor(
                    None,
                    lambda: self.client.get_order(order_id)
                )
                size_matched = float(order_details.get("size_matched") or 0.0)
                if size_matched <= 0.0:
                    logger.info(f"CLOB: FOK/IOC order expired with 0 fills | {side} {size:.2f} shares @ {price:.3f} | id={order_id}")
                    return None
                # Capture actual avg fill price; fall back to limit price if field absent
                fill_price = float(
                    order_details.get("avg_price")
                    or order_details.get("price")
                    or price
                )
                logger.success(
                    f"CLOB: Order filled | {side} {size_matched:.4f} shares "
                    f"@ fill={fill_price:.4f} (limit={price:.3f}) | token={token_id[:8]}... | id={order_id}"
                )
                return order_id, size_matched, fill_price

            # GTC: no immediate fill confirmation; assume filled at limit price
            logger.success(f"CLOB: Order placed | {side} {size:.2f} shares @ {price:.3f} | token={token_id[:8]}... | id={order_id}")
            return order_id, size, price
        except Exception as e:
            err_msg = str(e)
            if "maker address not allowed" in err_msg or "deposit wallet flow" in err_msg:
                logger.error(
                    f"CLOB: Order placement failed: {e}\n"
                    "👉 Polymarket requires the deposit wallet flow for this account. Please update your environment variables (.env):\n"
                    "  1. Set POLYMARKET_SIGNATURE_TYPE=1 (or 3 for POLY_1271 / Privy)\n"
                    "  2. Set POLYMARKET_FUNDER=<your Polymarket deposit/proxy wallet address>"
                )
            elif any(x in err_msg for x in ("fully filled or killed", "couldn't be fully filled", "FAK", "IOC")):
                logger.info(f"CLOB: FOK/IOC order not filled (price moved or spread changed) | {side} {size:.2f} shares @ {price:.3f}")
            else:
                logger.error(f"CLOB: Order placement failed: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        if not self.client:
            return False
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.client.cancel, order_id)
            logger.info(f"CLOB: Order {order_id} cancelled")
            return True
        except Exception as e:
            logger.warning(f"CLOB: Cancel failed for {order_id}: {e}")
            return False


# Singleton instance shared across all modules
clob_cache = ClobBookCache()

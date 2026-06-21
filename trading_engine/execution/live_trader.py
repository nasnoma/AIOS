"""
trading_engine/execution/live_trader.py

Live trading execution layer.
Places actual market orders on Bybit (for Crypto Spot) and Alpaca (for Stock Spot)
when settings.trading_mode == "live".
Tracks active live positions, P&L, and saves state to live_state.json.
"""
from __future__ import annotations
import json
import os
import sys
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

import ccxt
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from trading_engine.config import settings
from trading_engine.storage import db
from trading_engine.alerts.telegram_bot import send_message
from trading_engine.utils.bamboo_client import bamboo_client

STATE_FILE = Path(__file__).parent.parent / "live_state.json"

ENTRY_FEE_RATE = 0.0006   # 0.06% entry (fee + slippage estimate)
EXIT_FEE_RATE  = 0.0006   # 0.06% exit  (fee + slippage estimate)


@dataclass
class Position:
    symbol: str
    direction: str         # 'long' | 'short'
    entry_price: float
    size_usd: float
    stop_loss: float
    take_profit: float
    opened_at: str
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    fee_usd: Optional[float] = None   # total fees paid on this trade (entry + exit)
    status: str = "open"   # 'open' | 'closed' | 'stopped'
    atr: float = 0.0                  # ATR at entry, used for trailing stop ratchet
    trailing_high: Optional[float] = None   # best price seen since entry (long)
    trailing_low: Optional[float] = None    # best price seen since entry (short)
    sl_order_id: Optional[str] = None       # ID of the stop loss order on the broker/exchange
    tp_order_id: Optional[str] = None       # ID of the take profit order on the broker/exchange


@dataclass
class LivePortfolio:
    account_size: float = field(default_factory=lambda: settings.account_size)
    cash: float = field(default_factory=lambda: settings.account_size)
    positions: list[Position] = field(default_factory=list)
    closed_trades: list[Position] = field(default_factory=list)
    total_pnl: float = 0.0
    total_fees: float = 0.0   # cumulative fees paid across all trades
    win_count: int = 0
    loss_count: int = 0
    self_healing_state: dict[str, dict] = field(default_factory=dict)
    pending_self_healing: list[str] = field(default_factory=list)

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "open"]

    @property
    def portfolio_heat(self) -> float:
        """Current total risk as % of account."""
        total_risk = sum(
            p.size_usd * abs(p.entry_price - p.stop_loss) / p.entry_price
            for p in self.open_positions
        )
        return total_risk / self.account_size if self.account_size > 0 else 0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.5

    def to_dict(self):
        return {
            "account_size": self.account_size,
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions],
            "closed_trades": [asdict(p) for p in self.closed_trades],
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "self_healing_state": self.self_healing_state,
            "pending_self_healing": self.pending_self_healing,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LivePortfolio":
        p = cls(account_size=d["account_size"], cash=d["cash"],
                total_pnl=d["total_pnl"], win_count=d["win_count"],
                loss_count=d["loss_count"],
                total_fees=d.get("total_fees", 0.0),
                self_healing_state=d.get("self_healing_state", {}),
                pending_self_healing=d.get("pending_self_healing", []))
        p.positions = [Position(**pos) for pos in d.get("positions", [])]
        p.closed_trades = [Position(**pos) for pos in d.get("closed_trades", [])]
        return p


def _load_state() -> LivePortfolio:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return LivePortfolio.from_dict(json.load(f))
        except Exception as e:
            logger.warning(f"Could not load live state: {e}")
    return LivePortfolio()


def _save_state(portfolio: LivePortfolio):
    with open(STATE_FILE, "w") as f:
        json.dump(portfolio.to_dict(), f, indent=2)
    try:
        db.sync_closed_trades_to_db(portfolio.closed_trades)
    except Exception as e:
        logger.warning(f"Failed to sync closed trades to DB on save: {e}")


# ── Exchange Clients Initializers ─────────────────────────────────────────

def get_bybit_exchange() -> ccxt.bybit:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise ValueError("Bybit API key and secret must be configured for live mode.")
    
    params = {
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {
            "defaultType": "spot",
        }
    }
    exchange = ccxt.bybit(params)
    if settings.crypto_testnet:
        if settings.bybit_demo_trading:
            exchange.enable_demo_trading(True)
        else:
            exchange.set_sandbox_mode(True)
    return exchange


def get_alpaca_client() -> TradingClient:
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise ValueError("Alpaca API key and secret key must be configured for live mode.")
    return TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        url_override=settings.alpaca_base_url
    )


# ── Execution Logic ───────────────────────────────────────────────────────

def retry_and_log_order(symbol: str, side: str, qty: float, price: float, order_type: str, exchange_func, *args, **kwargs):
    """
    Executes exchange_func with retries, exponential backoff, and logs the result to db.
    Sends Telegram alert on permanent failure.
    """
    max_retries = 3
    delay = 1.0
    backoff_factor = 2.0
    
    last_error = None
    payload = {"symbol": symbol, "side": side, "qty": qty, "price": price, "order_type": order_type, "args": args, "kwargs": kwargs}
    
    for attempt in range(1, max_retries + 1):
        start_time = time.time()
        try:
            # Execute exchange order call
            response = exchange_func(*args, **kwargs)
            duration_ms = (time.time() - start_time) * 1000.0
            
            # Log successful order to DB
            db.log_order(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
                payload=payload,
                response=response,
                status="success"
            )
            # Log external API call for auditing
            db.log_api_call(
                endpoint=f"{symbol}:{side}:order",
                method="POST",
                params=payload,
                status_code=200,
                response=response,
                duration_ms=duration_ms
            )
            return response
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000.0
            last_error = e
            err_msg = str(e)
            
            # Log failed API attempt to DB
            db.log_api_call(
                endpoint=f"{symbol}:{side}:order",
                method="POST",
                params=payload,
                status_code=500,
                response=err_msg,
                duration_ms=duration_ms
            )
            
            # Determine if error is retryable
            is_retryable = False
            # CCXT retryable errors
            if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.RateLimitExceeded)):
                is_retryable = True
            # General string check for rate limits or timeouts
            elif any(k in err_msg.lower() for k in ["rate limit", "timeout", "network", "api keys", "conn"]):
                is_retryable = True
                
            if attempt < max_retries and is_retryable:
                logger.warning(
                    f"Order execution failed for {symbol} on attempt {attempt}/{max_retries}: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error(f"Order execution permanently failed for {symbol} on attempt {attempt}/{max_retries}: {e}")
                break
                
    # If we got here, it permanently failed
    db.log_order(
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        order_type=order_type,
        payload=payload,
        response=None,
        status="error",
        error_message=str(last_error)
    )
    
    # Send Telegram error notification
    send_message(
        f"⚠️ <b>LIVE ORDER FAILED: {symbol}</b>\n"
        f"Side: {side.upper()}\n"
        f"Qty: {qty:.4f} @ {price:.4f}\n"
        f"Error: <code>{str(last_error)[:200]}</code>"
    )
    raise last_error


def place_bybit_market_order(symbol: str, side: str, amount_usd: float, current_price: float) -> float:
    """
    Submits a market order on Bybit Spot.
    Returns the actual average fill price.
    """
    exchange = get_bybit_exchange()
    exchange.load_markets()

    qty         = amount_usd / current_price
    qty_str     = exchange.amount_to_precision(symbol, qty)
    qty_formatted = float(qty_str)

    logger.info(f"Placing Bybit Spot Market {side.upper()} order for {symbol}: qty={qty_formatted}")

    def _place():
        return exchange.create_order(symbol, 'market', side, qty_formatted)

    order = retry_and_log_order(
        symbol=symbol,
        side=side,
        qty=qty_formatted,
        price=current_price,
        order_type="market",
        exchange_func=_place,
    )

    fill_price = order.get("average") or order.get("price")
    return float(fill_price) if fill_price else current_price


def place_bybit_linear_order(symbol: str, side: str, amount_usd: float, current_price: float) -> float:
    """
    Submits a market order on Bybit Linear perpetuals (CFD stocks + precious metals).
    Supports both 'buy' (long) and 'sell' (short) sides.
    Returns the actual average fill price.
    """
    params: dict = {
        "options": {"defaultType": "linear"},
    }
    if settings.bybit_api_key and settings.bybit_api_secret:
        params["apiKey"] = settings.bybit_api_key
        params["secret"] = settings.bybit_api_secret

    exchange = ccxt.bybit(params)
    if settings.crypto_testnet:
        if settings.bybit_demo_trading:
            exchange.enable_demo_trading(True)
        else:
            exchange.set_sandbox_mode(True)
    exchange.load_markets()

    qty           = amount_usd / current_price
    qty_str       = exchange.amount_to_precision(symbol, qty)
    qty_formatted = float(qty_str)

    logger.info(f"Placing Bybit Linear Market {side.upper()} order for {symbol}: qty={qty_formatted}")

    # For linear perpetuals, closing a position requires reduceOnly=True on the opposite side.
    # Opening uses positionIdx=0 (one-way mode) which is the Bybit default.
    def _place():
        return exchange.create_order(
            symbol, "market", side, qty_formatted,
            params={"positionIdx": 0},
        )

    order = retry_and_log_order(
        symbol=symbol,
        side=side,
        qty=qty_formatted,
        price=current_price,
        order_type="market",
        exchange_func=_place,
    )

    fill_price = order.get("average") or order.get("price")
    return float(fill_price) if fill_price else current_price


def place_alpaca_market_order(symbol: str, side_str: str, qty: float, current_price: float) -> float:
    """
    Submits a market order on Alpaca.
    Returns the actual average fill price.
    """
    client = get_alpaca_client()
    side = OrderSide.BUY if side_str.lower() == "buy" else OrderSide.SELL
    
    order_data = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    
    logger.info(f"Submitting Alpaca Spot Market {side_str.upper()} order for {symbol}: qty={qty}")
    
    def _place():
        try:
            return client.submit_order(order_data=order_data)
        except Exception as e:
            if "not fractionable" in str(e).lower():
                int_qty = int(round(qty))
                if int_qty < 1:
                    int_qty = 1
                logger.warning(f"Asset {symbol} is not fractionable. Retrying with integer quantity: {int_qty}")
                retry_order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=int_qty,
                    side=side,
                    time_in_force=TimeInForce.DAY
                )
                return client.submit_order(order_data=retry_order_data)
            raise e
            
    order = retry_and_log_order(
        symbol=symbol,
        side=side_str,
        qty=qty,
        price=current_price,
        order_type="market",
        exchange_func=_place
    )
    
    fill_price = order.filled_avg_price
    if fill_price is not None:
        fill_price = float(fill_price)
    else:
        # Wait up to 3 seconds for the order to fill
        for _ in range(3):
            time.sleep(1)
            refreshed = client.get_order_by_id(order.id)
            if refreshed.filled_avg_price is not None:
                fill_price = float(refreshed.filled_avg_price)
                break
        if fill_price is None:
            fill_price = current_price
            
    return fill_price


# ── Public API ────────────────────────────────────────────────────────────

def open_trade(
    symbol: str,
    direction: str,
    entry: float,
    size_usd: float,
    stop_loss: float,
    take_profit: float,
    **kwargs
) -> Optional[Position]:
    """
    Open a live trade on the appropriate exchange.

    Routing:
      - Bybit CFD linear perpetuals (AAPL/USDT:USDT, XAU/USDT:USDT) → place_bybit_linear_order
        • Supports both 'long' (buy) and 'short' (sell)
        • Market hours guard: raises RuntimeError if market is closed
      - Bybit crypto spot (BTC/USDT etc.)                            → place_bybit_market_order
        • Long only (spot cannot go short without margin)
      - Plain stock tickers (AAPL, TSLA)                             → place_alpaca_market_order
        • Long only
    """
    from trading_engine.market_hours import classify_symbol, AssetClass, market_status

    asset_class = classify_symbol(symbol)
    # Map plain stocks to Bybit linear perpetual CFDs to enforce Bybit-only execution
    if asset_class == AssetClass.STOCK:
        logger.info(f"Mapping plain stock symbol {symbol} to Bybit CFD: {symbol}/USDT:USDT")
        symbol = f"{symbol}/USDT:USDT"
        asset_class = AssetClass.STOCK_CFD

    is_cfd       = asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL)
    is_crypto    = asset_class == AssetClass.CRYPTO
    is_ngx       = asset_class == AssetClass.NGX_STOCK
    is_bamboo_us = asset_class == AssetClass.BAMBOO_US_STOCK
    is_bamboo    = is_ngx or is_bamboo_us

    # Map crypto to perpetual linear contracts if shorting OR if configured
    use_crypto_perpetual = False
    if is_crypto:
        use_perps = getattr(settings, "crypto_use_perpetuals", False)
        if direction == "short" or use_perps:
            use_crypto_perpetual = True
            if not symbol.endswith(":USDT"):
                logger.info(f"Mapping crypto symbol {symbol} to Bybit Linear Perpetual: {symbol}:USDT")
                symbol = f"{symbol}:USDT"
            # Reclassify after mapping
            asset_class = classify_symbol(symbol)
            is_cfd = False

    # ── Market hours guard for CFDs & NGX ───────────────────────────────────
    if is_cfd or is_bamboo:
        status = market_status(symbol, extended_stock_hours=settings.extended_cfd_hours)
        if not status.is_open:
            status.log(symbol)
            logger.warning(
                f"⏸️  Blocking live {direction.upper()} on {symbol} — market is closed. "
                f"Reason: {status.reason}"
            )
            return None

    # ── Short validation ─────────────────────────────────────────────────────
    if direction == "short" and not is_cfd and not use_crypto_perpetual:
        logger.warning(
            f"Short positions are only supported on Bybit linear CFDs and mapped crypto perpetuals. "
            f"{symbol} ({asset_class.value}) does not support shorting. Skipping."
        )
        return None

    # ── Execute ──────────────────────────────────────────────────────────────
    calc = None
    try:
        if is_bamboo:
            if is_ngx:
                qty = int(round(size_usd / entry))
                if qty <= 0:
                    logger.warning(f"NGX stock quantity too small for {symbol} ({qty}). Skipping.")
                    return None
            else:
                qty = float(size_usd / entry)
                if qty <= 0.0001:
                    logger.warning(f"US stock quantity too small for {symbol} ({qty}). Skipping.")
                    return None
            logger.info(f"Calculating Bamboo {'NGX' if is_ngx else 'US'} order for {symbol}: qty={qty}...")
            calc = bamboo_client.calculate_order(symbol, "BUY", qty, entry)
            
            clean_symbol = symbol.split("/")[0].split(":")[0].upper()
            order_payload = {
                "fee": float(calc["fee"]),
                "order_type": "MARKET",
                "order_value": float(calc["total_price"]),
                "price": float(calc["price_per_share"]),
                "price_per_share": float(calc["price_per_share"]),
                "quantity": float(calc["quantity"]),
                "side": "BUY",
                "source_wallet_id": 0,
                "symbol": clean_symbol,
                "total_price": float(calc["order_price"])
            }
            logger.info(f"Placing Bamboo {'NGX' if is_ngx else 'US'} BUY order for {symbol}...")
            order_resp = bamboo_client.place_order(order_payload, symbol=symbol)
            fill_price = float(calc["price_per_share"])
            actual_qty = float(calc["quantity"])
            size_usd = float(calc["total_price"])

        elif is_cfd or use_crypto_perpetual:
            # Bybit linear perpetual — supports both buy (long) and sell (short)
            side       = "buy" if direction == "long" else "sell"
            fill_price = place_bybit_linear_order(symbol, side, size_usd, entry)
            actual_qty = size_usd / fill_price

        elif is_crypto:
            # Bybit spot — long only
            fill_price = place_bybit_market_order(symbol, "buy", size_usd, entry)
            actual_qty = size_usd / fill_price

        else:
            # Plain stock via Alpaca — long only
            qty         = size_usd / entry
            qty_rounded = round(qty, 4)
            if qty_rounded <= 0:
                logger.warning(f"Stock quantity too small for {symbol} ({qty}). Skipping.")
                return None
            fill_price = place_alpaca_market_order(symbol, "buy", qty_rounded, entry)
            actual_qty = size_usd / fill_price

    except Exception as e:
        logger.error(f"Failed to execute live open trade for {symbol}: {e}")
        send_message(f"⚠️ <b>LIVE execution error</b> for {symbol}: {e}")
        return None

    # ── Place Stop Loss order on the broker/exchange ─────────────────────────
    sl_order_id = None
    if not is_bamboo:
        try:
            if is_cfd or is_crypto:
                exchange = get_bybit_exchange()
                is_linear = is_cfd or use_crypto_perpetual
                if is_linear:
                    exchange.options["defaultType"] = "linear"
                else:
                    exchange.options["defaultType"] = "spot"
                exchange.load_markets()

                qty_str = exchange.amount_to_precision(symbol, actual_qty)
                qty_formatted = float(qty_str)

                sl_side = "sell" if direction == "long" else "buy"
                sl_params = {
                    "triggerPrice": exchange.price_to_precision(symbol, stop_loss),
                    "triggerBy": "LastPrice",
                    "triggerDirection": "descending" if direction == "long" else "ascending",
                }
                if is_linear:
                    sl_params["reduceOnly"] = True

                logger.info(f"Placing Bybit exchange Stop Loss order for {symbol} at {stop_loss:.4f}...")
                sl_order = exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=sl_side,
                    amount=qty_formatted,
                    price=None,
                    params=sl_params
                )
                sl_order_id = sl_order.get("id")
                logger.success(f"Successfully placed Bybit Stop Loss order: {sl_order_id}")

            else:
                # Alpaca stock Stop Loss
                client = get_alpaca_client()
                from alpaca.trading.requests import StopOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce

                qty_rounded = round(actual_qty, 4)
                if qty_rounded > 0:
                    sl_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
                    stop_order_data = StopOrderRequest(
                        symbol=symbol,
                        qty=qty_rounded,
                        side=sl_side,
                        stop_price=stop_loss,
                        time_in_force=TimeInForce.GTC
                    )
                    logger.info(f"Placing Alpaca Stop Loss order for {symbol} at {stop_loss:.4f}...")
                    sl_order = client.submit_order(order_data=stop_order_data)
                    sl_order_id = str(sl_order.id)
                    logger.success(f"Successfully placed Alpaca Stop Loss order: {sl_order_id}")

        except Exception as e_sl:
            logger.error(f"Failed to place broker-side Stop Loss order for {symbol}: {e_sl}. Fallback to local monitoring.")

    # ── Place Take Profit order on the broker/exchange ─────────────────────────
    tp_order_id = None
    if not is_bamboo:
        try:
            tp_order_id = _place_broker_take_profit(
                symbol=symbol,
                direction=direction,
                take_profit=take_profit,
                qty=actual_qty,
            )
        except Exception as e_tp:
            logger.error(f"Failed to place broker-side Take Profit order for {symbol}: {e_tp}. Fallback to local monitoring.")

    portfolio = _load_state()

    # Deduct transaction fee
    if is_bamboo and calc:
        entry_fee = float(calc["fee"])
    else:
        entry_fee = size_usd * ENTRY_FEE_RATE
    portfolio.cash       -= entry_fee
    portfolio.total_fees += entry_fee

    pos = Position(
        symbol=symbol,
        direction=direction,
        entry_price=fill_price,
        size_usd=size_usd,
        stop_loss=stop_loss,
        take_profit=take_profit,
        opened_at=datetime.now(timezone.utc).isoformat(),
        fee_usd=entry_fee,
        atr=kwargs.get("atr", 0.0),
        trailing_high=fill_price if direction == "long" else None,
        trailing_low=fill_price if direction == "short" else None,
        sl_order_id=sl_order_id,
        tp_order_id=tp_order_id,
    )
    portfolio.positions.append(pos)
    portfolio.cash -= size_usd
    _save_state(portfolio)

    direction_icon = "📈" if direction == "long" else "📉"
    logger.success(
        f"📝 LIVE {direction.upper()} opened: {symbol} ({asset_class.value}) | "
        f"Size=${size_usd:,.0f} | SL={stop_loss:.4f} | TP={take_profit:.4f} | "
        f"Entry={fill_price:.4f}"
    )

    send_message(
        f"{direction_icon} <b>LIVE {direction.upper()} Opened</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: {symbol}\n"
        f"📂 Asset: {asset_class.value}\n"
        f"💵 Size: ${size_usd:,.2f}\n"
        f"📈 Entry: <code>{fill_price:.4f}</code>\n"
        f"🛑 Stop Loss: <code>{stop_loss:.4f}</code>\n"
        f"🎯 Take Profit: <code>{take_profit:.4f}</code>"
    )
    return pos


def _cancel_broker_stop_loss(pos: Position):
    if not pos.sl_order_id:
        return
    
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(pos.symbol)
    
    try:
        if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL, AssetClass.CRYPTO):
            exchange = get_bybit_exchange()
            is_linear = ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) or (ac == AssetClass.CRYPTO and ":" in pos.symbol)
            if is_linear:
                exchange.options["defaultType"] = "linear"
            else:
                exchange.options["defaultType"] = "spot"
            exchange.load_markets()
            
            logger.info(f"Cancelling Bybit Stop Loss order {pos.sl_order_id} for {pos.symbol}...")
            exchange.cancel_order(id=pos.sl_order_id, symbol=pos.symbol)
            logger.success(f"Successfully cancelled Bybit Stop Loss order {pos.sl_order_id}")
        else:
            # Alpaca
            client = get_alpaca_client()
            logger.info(f"Cancelling Alpaca Stop Loss order {pos.sl_order_id} for {pos.symbol}...")
            client.cancel_order_by_id(pos.sl_order_id)
            logger.success(f"Successfully cancelled Alpaca Stop Loss order {pos.sl_order_id}")
    except Exception as e:
        logger.warning(f"Failed to cancel broker-side Stop Loss order {pos.sl_order_id} for {pos.symbol}: {e}")


def _place_broker_take_profit(
    symbol: str,
    direction: str,
    take_profit: float,
    qty: float,
) -> Optional[str]:
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    
    try:
        if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL, AssetClass.CRYPTO):
            exchange = get_bybit_exchange()
            is_linear = ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) or (ac == AssetClass.CRYPTO and ":" in symbol)
            if is_linear:
                exchange.options["defaultType"] = "linear"
            else:
                exchange.options["defaultType"] = "spot"
            exchange.load_markets()

            qty_str = exchange.amount_to_precision(symbol, qty)
            qty_formatted = float(qty_str)

            tp_side = "sell" if direction == "long" else "buy"
            tp_params = {
                "triggerPrice": exchange.price_to_precision(symbol, take_profit),
                "triggerBy": "LastPrice",
                "triggerDirection": "ascending" if direction == "long" else "descending",
            }
            if is_linear:
                tp_params["reduceOnly"] = True

            logger.info(f"Placing Bybit exchange Take Profit order for {symbol} at {take_profit:.4f}...")
            tp_order = exchange.create_order(
                symbol=symbol,
                type="market",
                side=tp_side,
                amount=qty_formatted,
                price=None,
                params=tp_params
            )
            tp_order_id = tp_order.get("id")
            logger.success(f"Successfully placed Bybit Take Profit order: {tp_order_id}")
            return tp_order_id

        else:
            # Alpaca stock Limit GTC
            client = get_alpaca_client()
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            qty_rounded = round(qty, 4)
            if qty_rounded > 0:
                tp_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
                limit_order_data = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty_rounded,
                    side=tp_side,
                    limit_price=take_profit,
                    time_in_force=TimeInForce.GTC
                )
                logger.info(f"Placing Alpaca Limit Take Profit order for {symbol} at {take_profit:.4f}...")
                tp_order = client.submit_order(order_data=limit_order_data)
                tp_order_id = str(tp_order.id)
                logger.success(f"Successfully placed Alpaca Take Profit order: {tp_order_id}")
                return tp_order_id
    except Exception as e:
        logger.error(f"Failed to place broker-side Take Profit order for {symbol}: {e}")
    return None


def _cancel_broker_take_profit(pos: Position):
    if not pos.tp_order_id:
        return
    
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(pos.symbol)
    
    try:
        if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL, AssetClass.CRYPTO):
            exchange = get_bybit_exchange()
            is_linear = ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) or (ac == AssetClass.CRYPTO and ":" in pos.symbol)
            if is_linear:
                exchange.options["defaultType"] = "linear"
            else:
                exchange.options["defaultType"] = "spot"
            exchange.load_markets()
            
            logger.info(f"Cancelling Bybit Take Profit order {pos.tp_order_id} for {pos.symbol}...")
            exchange.cancel_order(id=pos.tp_order_id, symbol=pos.symbol)
            logger.success(f"Successfully cancelled Bybit Take Profit order {pos.tp_order_id}")
        else:
            # Alpaca
            client = get_alpaca_client()
            logger.info(f"Cancelling Alpaca Take Profit order {pos.tp_order_id} for {pos.symbol}...")
            client.cancel_order_by_id(pos.tp_order_id)
            logger.success(f"Successfully cancelled Alpaca Take Profit order {pos.tp_order_id}")
    except Exception as e:
        logger.warning(f"Failed to cancel broker-side Take Profit order {pos.tp_order_id} for {pos.symbol}: {e}")


def _update_broker_stop_loss(pos: Position) -> Optional[str]:
    """
    Cancels the existing broker stop loss order and places a new one at pos.stop_loss.
    Returns the new sl_order_id, or None if it fails.
    """
    if pos.sl_order_id:
        try:
            _cancel_broker_stop_loss(pos)
        except Exception as e:
            logger.warning(f"Could not cancel old stop loss order {pos.sl_order_id}: {e}")
            
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(pos.symbol)
    is_cfd = ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL)
    is_crypto = ac == AssetClass.CRYPTO
    is_linear = is_cfd or (is_crypto and ":" in pos.symbol)
    
    new_sl_order_id = None
    try:
        actual_qty = pos.size_usd / pos.entry_price
        if is_cfd or is_crypto:
            exchange = get_bybit_exchange()
            if is_linear:
                exchange.options["defaultType"] = "linear"
            else:
                exchange.options["defaultType"] = "spot"
            exchange.load_markets()
            
            qty_str = exchange.amount_to_precision(pos.symbol, actual_qty)
            qty_formatted = float(qty_str)
            
            sl_side = "sell" if pos.direction == "long" else "buy"
            sl_params = {
                "triggerPrice": exchange.price_to_precision(pos.symbol, pos.stop_loss),
                "triggerBy": "LastPrice",
                "triggerDirection": "descending" if pos.direction == "long" else "ascending",
            }
            if is_linear:
                sl_params["reduceOnly"] = True
                
            logger.info(f"Ratcheting Bybit stop loss for {pos.symbol} to {pos.stop_loss:.4f}...")
            sl_order = exchange.create_order(
                symbol=pos.symbol,
                type="market",
                side=sl_side,
                amount=qty_formatted,
                price=None,
                params=sl_params
            )
            new_sl_order_id = sl_order.get("id")
            logger.success(f"Placed new Bybit stop loss order: {new_sl_order_id}")
        else:
            # Alpaca
            client = get_alpaca_client()
            from alpaca.trading.requests import StopOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            
            qty_rounded = round(actual_qty, 4)
            if qty_rounded > 0:
                sl_side = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
                stop_order_data = StopOrderRequest(
                    symbol=pos.symbol,
                    qty=qty_rounded,
                    side=sl_side,
                    stop_price=pos.stop_loss,
                    time_in_force=TimeInForce.GTC
                )
                logger.info(f"Ratcheting Alpaca stop loss for {pos.symbol} to {pos.stop_loss:.4f}...")
                sl_order = client.submit_order(order_data=stop_order_data)
                new_sl_order_id = str(sl_order.id)
                logger.success(f"Placed new Alpaca stop loss order: {new_sl_order_id}")
    except Exception as e_sl:
        logger.error(f"Failed to place new broker-side Stop Loss order for {pos.symbol} at {pos.stop_loss:.4f}: {e_sl}")
        
    return new_sl_order_id


def _apply_trailing_stop(pos: Position, price: float) -> None:
    """
    ATR trailing stop ratchet — called before SL/TP check.
    Updates the broker-side Stop Loss order on ratchet trigger.
    """
    if pos.atr <= 0:
        return

    atr = pos.atr
    ratcheted = False

    if pos.direction == "long":
        if pos.trailing_high is None or price > pos.trailing_high:
            pos.trailing_high = price

        profit_in_atr = (pos.trailing_high - pos.entry_price) / atr
        if profit_in_atr >= 2.0:
            new_sl = pos.entry_price + atr
            if new_sl > pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (2×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → {new_sl:.4f}"
                )
                pos.stop_loss = new_sl
                ratcheted = True
        elif profit_in_atr >= 1.0:
            if pos.entry_price > pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (1×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → breakeven {pos.entry_price:.4f}"
                )
                pos.stop_loss = pos.entry_price
                ratcheted = True

    else:  # short
        if pos.trailing_low is None or price < pos.trailing_low:
            pos.trailing_low = price

        profit_in_atr = (pos.entry_price - pos.trailing_low) / atr
        if profit_in_atr >= 2.0:
            new_sl = pos.entry_price - atr
            if new_sl < pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (2×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → {new_sl:.4f}"
                )
                pos.stop_loss = new_sl
                ratcheted = True
        elif profit_in_atr >= 1.0:
            if pos.entry_price < pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (1×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → breakeven {pos.entry_price:.4f}"
                )
                pos.stop_loss = pos.entry_price
                ratcheted = True

    if ratcheted:
        new_id = _update_broker_stop_loss(pos)
        if new_id:
            pos.sl_order_id = new_id


def update_prices(current_prices: dict[str, float]):
    """Check if any open positions hit SL or TP. Applies trailing stop ratchet first."""
    # Sync with broker first to handle stop-out detection
    try:
        sync_with_broker()
    except Exception as e:
        logger.warning(f"Failed to sync with broker before checking prices: {e}")
        
    portfolio = _load_state()
    for pos in portfolio.open_positions:
        price = current_prices.get(pos.symbol)
        if not price:
            continue

        # Apply trailing stop ratchet
        _apply_trailing_stop(pos, price)

        if pos.direction == "long":
            if price <= pos.stop_loss:
                if not pos.sl_order_id:
                    _close_position(portfolio, pos, price, "stopped")
                else:
                    logger.info(f"Stop loss level hit for {pos.symbol} but exchange-side SL order {pos.sl_order_id} is active. Relying on broker to trigger.")
            elif price >= pos.take_profit:
                if not pos.tp_order_id:
                    _close_position(portfolio, pos, price, "closed")
                else:
                    logger.info(f"Take profit level hit for {pos.symbol} but exchange-side TP order {pos.tp_order_id} is active. Relying on broker to trigger.")
        else:  # short
            if price >= pos.stop_loss:
                if not pos.sl_order_id:
                    _close_position(portfolio, pos, price, "stopped")
                else:
                    logger.info(f"Stop loss level hit for {pos.symbol} but exchange-side SL order {pos.sl_order_id} is active. Relying on broker to trigger.")
            elif price <= pos.take_profit:
                if not pos.tp_order_id:
                    _close_position(portfolio, pos, price, "closed")
                else:
                    logger.info(f"Take profit level hit for {pos.symbol} but exchange-side TP order {pos.tp_order_id} is active. Relying on broker to trigger.")

    _save_state(portfolio)


def _close_position(portfolio: LivePortfolio, pos: Position, exit_price: float, status: str):
    from trading_engine.market_hours import classify_symbol, AssetClass
    
    asset_class = classify_symbol(pos.symbol)
    is_cfd       = asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL)
    is_crypto    = asset_class == AssetClass.CRYPTO
    is_linear_crypto = is_crypto and ":" in pos.symbol
    is_ngx       = asset_class == AssetClass.NGX_STOCK
    is_bamboo_us = asset_class == AssetClass.BAMBOO_US_STOCK
    is_bamboo    = is_ngx or is_bamboo_us
    
    exit_fee = None
    try:
        if is_bamboo:
            if is_ngx:
                qty = int(round(pos.size_usd / pos.entry_price))
                if qty <= 0:
                    qty = 1
            else:
                qty = float(pos.size_usd / pos.entry_price)
                if qty <= 0.0001:
                    qty = 0.0001
            logger.info(f"Calculating Bamboo {'NGX' if is_ngx else 'US'} order close for {pos.symbol}: qty={qty}...")
            calc = bamboo_client.calculate_order(pos.symbol, "SELL", qty, exit_price)
            
            clean_symbol = pos.symbol.split("/")[0].split(":")[0].upper()
            order_payload = {
                "fee": float(calc["fee"]),
                "order_type": "MARKET",
                "order_value": float(calc["total_price"]),
                "price": float(calc["price_per_share"]),
                "price_per_share": float(calc["price_per_share"]),
                "quantity": float(calc["quantity"]),
                "side": "SELL",
                "source_wallet_id": 0,
                "symbol": clean_symbol,
                "total_price": float(calc["order_price"])
            }
            logger.info(f"Placing Bamboo {'NGX' if is_ngx else 'US'} SELL order close for {pos.symbol}...")
            order_resp = bamboo_client.place_order(order_payload, symbol=pos.symbol)
            fill_price = float(calc["price_per_share"])
            exit_fee = float(calc["fee"])
        elif is_cfd or is_linear_crypto:
            # Linear perpetual — close with reduceOnly-equivalent opposite order
            side = "sell" if pos.direction == "long" else "buy"
            fill_price = place_bybit_linear_order(pos.symbol, side, pos.size_usd, exit_price)
        elif is_crypto:
            # Crypto Spot — close long with sell order
            fill_price = place_bybit_market_order(pos.symbol, "sell", pos.size_usd, exit_price)
        else:
            # Alpaca Spot — close long with sell order
            qty = pos.size_usd / pos.entry_price
            qty_rounded = round(qty, 4)
            fill_price = place_alpaca_market_order(pos.symbol, "sell", qty_rounded, exit_price)
    except Exception as e:
        logger.error(f"Failed to execute live close trade for {pos.symbol}: {e}")
        send_message(f"⚠️ <b>LIVE close execution error</b> for {pos.symbol}: {e}")
        # Return and do not modify state, so we retry on next monitoring tick
        return

    # Cancel the stop loss order on the broker/exchange (skip for NGX)
    if not is_bamboo:
        _cancel_broker_stop_loss(pos)
        _cancel_broker_take_profit(pos)

    # Exit fee
    exit_fee = pos.size_usd * EXIT_FEE_RATE

    _finalize_closed_position(
        portfolio=portfolio,
        pos=pos,
        fill_price=fill_price,
        fee_cost=exit_fee,
        closed_at=datetime.now(timezone.utc).isoformat(),
        local_close_cash_update=True
    )


_last_sync_time = 0.0


def sync_with_broker() -> bool:
    """
    Synchronizes the local live_state.json with actual positions on the broker/exchange.
    Updates any locally open position that has been closed on the broker, ONLY if the broker queries succeed.
    """
    try:
        portfolio = _load_state()
        changed = False

        if settings.trading_mode == "live" and not portfolio.positions and not portfolio.closed_trades:
            logger.info("Local live state is empty. Reconstructing from DB and Bybit execution history...")
            try:
                # 1. Load historical closed trades from DB first
                db_trades_dict = db.get_db_closed_trades()
                db_closed_trades = []
                for t in db_trades_dict:
                    db_closed_trades.append(Position(
                        symbol=t["symbol"],
                        direction=t["direction"],
                        entry_price=t["entry_price"],
                        exit_price=t["exit_price"],
                        size_usd=t["size_usd"],
                        pnl_usd=t["pnl_usd"],
                        fee_usd=t["fee_usd"],
                        opened_at=t["opened_at"],
                        closed_at=t["closed_at"],
                        status=t["status"],
                        stop_loss=0.0,
                        take_profit=0.0,
                        atr=0.0
                    ))
                portfolio.closed_trades = db_closed_trades
            except Exception as e_db_load:
                logger.warning(f"Failed to load closed trades from DB for reconstruction: {e_db_load}")

            try:
                # First fetch active stop loss and take profit orders from Bybit and Alpaca
                active_orders = {}  # normalized_symbol -> list of (order_id, price)
                try:
                    bybit_ex = get_bybit_exchange()
                    for default_type in ("spot", "linear"):
                        bybit_ex.options["defaultType"] = default_type
                        try:
                            orders = bybit_ex.fetch_open_orders(symbol=None, params={"stop": True, "orderFilter": "StopOrder"})
                            for o in orders:
                                sym = o.get("symbol")
                                o_id = o.get("id")
                                raw_trigger = o.get("triggerPrice") or o.get("stopPrice") or (o.get("info") and o["info"].get("triggerPrice"))
                                if sym and o_id and raw_trigger:
                                    norm_sym = sym.upper().replace("/", "").replace(":", "").replace("-", "").strip()
                                    active_orders.setdefault(norm_sym, []).append((o_id, float(raw_trigger)))
                        except Exception as e_inner:
                            logger.warning(f"Failed to fetch Bybit {default_type} open conditional orders: {e_inner}")
                except Exception as e_bybit_sl:
                    logger.warning(f"Failed to fetch Bybit conditional orders for reconstruction: {e_bybit_sl}")

                try:
                    from alpaca.trading.requests import GetOrdersRequest
                    from alpaca.trading.enums import QueryOrderStatus
                    alpaca_client = get_alpaca_client()
                    orders = alpaca_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
                    for o in orders:
                        sym = o.symbol
                        o_id = str(o.id)
                        norm_sym = sym.upper().replace("/", "").replace(":", "").replace("-", "").strip()
                        if o.stop_price is not None:
                            active_orders.setdefault(norm_sym, []).append((o_id, float(o.stop_price)))
                        elif o.limit_price is not None:
                            active_orders.setdefault(norm_sym, []).append((o_id, float(o.limit_price)))
                except Exception as e_alpaca_sl:
                    logger.warning(f"Failed to fetch Alpaca open orders for reconstruction: {e_alpaca_sl}")

                # Fetch Spot executions
                spot_execs = []
                try:
                    bybit_spot = get_bybit_exchange()
                    bybit_spot.options["defaultType"] = "spot"
                    spot_execs = bybit_spot.fetch_my_trades(limit=100)
                except Exception as e:
                    logger.warning(f"Failed to fetch Spot executions for reconstruction: {e}")
                    
                # Fetch Linear executions
                linear_execs = []
                try:
                    bybit_linear = get_bybit_exchange()
                    bybit_linear.options["defaultType"] = "linear"
                    linear_execs = bybit_linear.fetch_my_trades(limit=100)
                except Exception as e:
                    logger.warning(f"Failed to fetch Linear executions for reconstruction: {e}")

                all_execs = spot_execs + linear_execs
                if all_execs:
                    by_symbol = {}
                    for ex in all_execs:
                        sym = ex["symbol"]
                        by_symbol.setdefault(sym, []).append(ex)
                        
                    reconstructed_positions = []
                    reconstructed_closed = []
                    
                    for sym, symbol_execs in by_symbol.items():
                        symbol_execs.sort(key=lambda x: x["timestamp"])
                        
                        open_runs = []
                        for ex in symbol_execs:
                            side = ex["side"].lower()
                            price = float(ex["price"])
                            cost = float(ex["cost"])
                            timestamp = int(ex["timestamp"])
                            dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
                            
                            fee_cost = 0.0
                            if ex.get("fee") and isinstance(ex["fee"], dict):
                                fee_cost = float(ex["fee"].get("cost", 0.0))
                                fee_currency = ex["fee"].get("currency")
                                if fee_currency and fee_currency not in ("USDT", "USD"):
                                    # Convert base-currency fee to USDT using transaction price
                                    fee_cost = fee_cost * price
                            
                            if side == "buy":
                                # Check if it closes an open short run
                                short_run = None
                                for r in open_runs:
                                    if r.direction == "short":
                                        short_run = r
                                        break
                                if short_run:
                                    open_runs.remove(short_run)
                                    short_run.exit_price = price
                                    short_run.closed_at = dt
                                    short_run.fee_usd = round((short_run.fee_usd or 0.0) + fee_cost, 4)
                                    qty = short_run.size_usd / short_run.entry_price
                                    # PnL for short: entry - exit
                                    pnl = (short_run.entry_price - price) * qty
                                    short_run.pnl_usd = round(pnl, 4)
                                    short_run.status = "closed" if pnl >= 0 else "stopped"
                                    reconstructed_closed.append(short_run)
                                else:
                                    # Opens a new long position
                                    norm_sym = sym.upper().replace("/", "").replace(":", "").replace("-", "").strip()
                                    sl_order_id = None
                                    tp_order_id = None
                                    stop_loss = round(price * 0.95, 4)  # 5% default SL fallback
                                    take_profit = round(price * 1.10, 4)    # 10% default TP fallback
                                    atr_val = 0.0
                                    
                                    if norm_sym in active_orders:
                                        for o_id, o_price in active_orders[norm_sym]:
                                            if o_price < price:
                                                sl_order_id = o_id
                                                stop_loss = o_price
                                            elif o_price > price:
                                                tp_order_id = o_id
                                                take_profit = o_price
                                                
                                        if sl_order_id:
                                            from trading_engine.risk_agent import _load_risk_params
                                            _rp = _load_risk_params()
                                            atr_mult = _rp.get("atr_stop_multiplier", 2.0)
                                            if atr_mult > 0:
                                                atr_val = abs(price - stop_loss) / atr_mult
                                            logger.info(f"Reconstructed SL order {sl_order_id} and stop_loss {stop_loss:.4f} (ATR={atr_val:.4f}) for {sym}")
                                        if tp_order_id:
                                            logger.info(f"Reconstructed TP order {tp_order_id} and take_profit {take_profit:.4f} for {sym}")
                                    
                                    open_runs.append(Position(
                                        symbol=sym,
                                        direction="long",
                                        entry_price=price,
                                        size_usd=cost,
                                        stop_loss=stop_loss,
                                        take_profit=take_profit,
                                        opened_at=dt,
                                        status="open",
                                        fee_usd=fee_cost,
                                        atr=atr_val,
                                        trailing_high=price,
                                        sl_order_id=sl_order_id,
                                        tp_order_id=tp_order_id
                                    ))
                            elif side == "sell":
                                # Check if it closes an open long run
                                long_run = None
                                for r in open_runs:
                                    if r.direction == "long":
                                        long_run = r
                                        break
                                if long_run:
                                    open_runs.remove(long_run)
                                    long_run.exit_price = price
                                    long_run.closed_at = dt
                                    long_run.fee_usd = round((long_run.fee_usd or 0.0) + fee_cost, 4)
                                    qty = long_run.size_usd / long_run.entry_price
                                    # PnL for long: exit - entry
                                    pnl = (price - long_run.entry_price) * qty
                                    long_run.pnl_usd = round(pnl, 4)
                                    long_run.status = "closed" if pnl >= 0 else "stopped"
                                    reconstructed_closed.append(long_run)
                                else:
                                    # Opens a new short position (for perpetuals / CFDs)
                                    norm_sym = sym.upper().replace("/", "").replace(":", "").replace("-", "").strip()
                                    sl_order_id = None
                                    tp_order_id = None
                                    stop_loss = round(price * 1.05, 4)  # 5% default SL fallback for short
                                    take_profit = round(price * 0.90, 4)    # 10% default TP fallback
                                    atr_val = 0.0
                                    
                                    if norm_sym in active_orders:
                                        for o_id, o_price in active_orders[norm_sym]:
                                            if o_price > price:
                                                sl_order_id = o_id
                                                stop_loss = o_price
                                            elif o_price < price:
                                                tp_order_id = o_id
                                                take_profit = o_price
                                                
                                        if sl_order_id:
                                            from trading_engine.risk_agent import _load_risk_params
                                            _rp = _load_risk_params()
                                            atr_mult = _rp.get("atr_stop_multiplier", 2.0)
                                            if atr_mult > 0:
                                                atr_val = abs(price - stop_loss) / atr_mult
                                            logger.info(f"Reconstructed SL order {sl_order_id} and stop_loss {stop_loss:.4f} (ATR={atr_val:.4f}) for {sym}")
                                        if tp_order_id:
                                            logger.info(f"Reconstructed TP order {tp_order_id} and take_profit {take_profit:.4f} for {sym}")
                                    
                                    open_runs.append(Position(
                                        symbol=sym,
                                        direction="short",
                                        entry_price=price,
                                        size_usd=cost,
                                        stop_loss=stop_loss,
                                        take_profit=take_profit,
                                        opened_at=dt,
                                        status="open",
                                        fee_usd=fee_cost,
                                        atr=atr_val,
                                        trailing_low=price,
                                        sl_order_id=sl_order_id,
                                        tp_order_id=tp_order_id
                                    ))
                                    
                        reconstructed_positions.extend(open_runs)
                            
                    portfolio.positions = reconstructed_positions
                    
                    # Merge reconstructed closed trades with DB-loaded trades to avoid duplicates
                    merged_closed = list(portfolio.closed_trades)  # Starts with DB loaded trades
                    for r_trade in reconstructed_closed:
                        exists = False
                        for m_trade in merged_closed:
                            if (m_trade.symbol == r_trade.symbol and
                                m_trade.direction == r_trade.direction and
                                m_trade.opened_at == r_trade.opened_at and
                                m_trade.closed_at == r_trade.closed_at):
                                exists = True
                                break
                        if not exists:
                            merged_closed.append(r_trade)
                            
                    portfolio.closed_trades = merged_closed
                    
                    # Reconstruct consecutive losses for each symbol from closed trades history
                    portfolio.self_healing_state = {}
                    sorted_closed = sorted(portfolio.closed_trades, key=lambda x: x.closed_at or "")
                    for t in sorted_closed:
                        sym = t.symbol
                        sh_state = portfolio.self_healing_state.setdefault(sym, {"consecutive_losses": 0, "last_optimized_at": None})
                        if t.pnl_usd is not None:
                            if t.pnl_usd >= 0:
                                sh_state["consecutive_losses"] = 0
                            else:
                                sh_state["consecutive_losses"] += 1
                                
                    # Load persistent last_optimized_at timestamps from database
                    for sym in portfolio.self_healing_state:
                        db_state = db.get_symbol_state(sym)
                        if db_state and db_state.get("last_optimized_at"):
                            portfolio.self_healing_state[sym]["last_optimized_at"] = db_state["last_optimized_at"].isoformat()
                                
                    portfolio.win_count = sum(1 for t in portfolio.closed_trades if t.status == "closed")
                    portfolio.loss_count = sum(1 for t in portfolio.closed_trades if t.status == "stopped")
                    portfolio.total_pnl = sum(t.pnl_usd for t in portfolio.closed_trades if t.pnl_usd is not None)
                    portfolio.total_fees = sum(t.fee_usd for t in portfolio.closed_trades if t.fee_usd is not None) + \
                                           sum(p.fee_usd for p in reconstructed_positions if p.fee_usd is not None)
                    changed = True
                    logger.success(f"Reconstructed {len(portfolio.positions)} open positions and {len(portfolio.closed_trades)} closed trades from DB and Bybit.")
            except Exception as e:
                logger.error(f"Failed to reconstruct portfolio from Bybit: {e}")

        # 1. Fetch Alpaca positions
        alpaca_fetched = False
        alpaca_symbols = set()
        alpaca_cash = 0.0
        alpaca_equity = 0.0
        try:
            alpaca = get_alpaca_client()
            for pos in alpaca.get_all_positions():
                alpaca_symbols.add(pos.symbol.upper())
            alpaca_fetched = True
            
            acct = alpaca.get_account()
            alpaca_cash = float(acct.cash)
            alpaca_equity = float(acct.portfolio_value)
        except Exception as e:
            logger.warning(f"Failed to fetch Alpaca positions/account during sync: {e}")

        # 2. Fetch Bybit Spot balances
        bybit_spot_fetched = False
        bybit_spot_symbols = set()
        usdt_free = 0.0
        try:
            bybit = get_bybit_exchange()
            balance = bybit.fetch_balance()
            
            # Sync USDT cash balance from exchange
            usdt_free_val = balance.get('USDT', {}).get('free')
            if usdt_free_val is not None:
                usdt_free = float(usdt_free_val)

            tickers = {}
            try:
                tickers = bybit.fetch_tickers()
            except Exception as e_tick:
                logger.warning(f"Failed to fetch tickers for balance valuation: {e_tick}")

            for currency, total in balance.get('total', {}).items():
                if currency not in ('USDT', 'USDC', 'USD') and total > 0.00001:
                    symbol = f"{currency}/USDT".upper()
                    value_usd = 999.0  # Default to bypass filter if tickers API failed or in tests
                    if tickers:
                        price = None
                        ticker_key = symbol
                        if ticker_key not in tickers:
                            ticker_key = symbol.replace("/", "")
                        if ticker_key in tickers:
                            price = float(tickers[ticker_key].get('last') or 0.0)
                        
                        if price is not None:
                            value_usd = total * price
                    
                    if value_usd >= 10.0:
                        bybit_spot_symbols.add(symbol)
                    else:
                        logger.info(f"Sync: Ignoring dust balance for {currency} (qty={total:.6f}, val=${value_usd:.2f})")
            bybit_spot_fetched = True
        except Exception as e:
            logger.warning(f"Failed to fetch Bybit Spot balance during sync: {e}")

        # 3. Fetch Bybit Linear positions
        bybit_linear_fetched = False
        bybit_linear_symbols = set()
        try:
            bybit_linear = get_bybit_exchange()
            bybit_linear.options["defaultType"] = "linear"
            bybit_linear.load_markets()
            for pos in bybit_linear.fetch_positions():
                if float(pos.get('size', 0)) > 0:
                    symbol = pos.get('symbol', '').upper()
                    bybit_linear_symbols.add(symbol)
            bybit_linear_fetched = True
        except Exception as e:
            logger.warning(f"Failed to fetch Bybit Linear positions during sync: {e}")

        # 4. Fetch Bamboo balance & holdings (both NGX and US Stocks)
        from trading_engine.market_hours import AssetClass
        bamboo_fetched = False
        bamboo_symbols = set()
        bamboo_cash = 0.0
        bamboo_equity = 0.0
        try:
            if settings.bamboo_username and settings.bamboo_password:
                # A. Fetch NGX Breakdown
                try:
                    breakdown_ng = bamboo_client.get_portfolio_breakdown(asset_class=AssetClass.NGX_STOCK)
                    cash_ng = float(breakdown_ng.get("cash") or breakdown_ng.get("cash_balance") or breakdown_ng.get("withdrawable_cash") or 0.0)
                    equity_ng = float(breakdown_ng.get("equity") or breakdown_ng.get("portfolio_value") or breakdown_ng.get("total_portfolio_value") or 0.0)
                except Exception as e_ng:
                    logger.warning(f"Failed to fetch Bamboo NGX breakdown during sync: {e_ng}")
                    cash_ng, equity_ng = 0.0, 0.0
                
                # B. Fetch US Breakdown
                try:
                    breakdown_us = bamboo_client.get_portfolio_breakdown(asset_class=AssetClass.BAMBOO_US_STOCK)
                    cash_us = float(breakdown_us.get("cash") or breakdown_us.get("cash_balance") or breakdown_us.get("withdrawable_cash") or 0.0)
                    equity_us = float(breakdown_us.get("equity") or breakdown_us.get("portfolio_value") or breakdown_us.get("total_portfolio_value") or 0.0)
                except Exception as e_us:
                    logger.warning(f"Failed to fetch Bamboo US breakdown during sync: {e_us}")
                    cash_us, equity_us = 0.0, 0.0
                    
                bamboo_cash = cash_ng + cash_us
                bamboo_equity = equity_ng + equity_us
                
                # C. Fetch NGX active holdings
                try:
                    my_stocks_ng = bamboo_client.get_my_stocks(asset_class=AssetClass.NGX_STOCK)
                    holdings_ng = my_stocks_ng if isinstance(my_stocks_ng, list) else (my_stocks_ng.get("holdings") or my_stocks_ng.get("results") or my_stocks_ng.get("stocks") or [])
                    for holding in holdings_ng:
                        sym = holding.get("symbol")
                        if sym:
                            bamboo_symbols.add(f"{sym}/NGX".upper())
                except Exception as e_my_ng:
                    logger.warning(f"Failed to fetch Bamboo NGX holdings: {e_my_ng}")
                    
                # D. Fetch US active holdings
                try:
                    my_stocks_us = bamboo_client.get_my_stocks(asset_class=AssetClass.BAMBOO_US_STOCK)
                    holdings_us = my_stocks_us if isinstance(my_stocks_us, list) else (my_stocks_us.get("holdings") or my_stocks_us.get("results") or my_stocks_us.get("stocks") or [])
                    for holding in holdings_us:
                        sym = holding.get("symbol")
                        if sym:
                            bamboo_symbols.add(f"{sym}/BAMBOO".upper())
                except Exception as e_my_us:
                    logger.warning(f"Failed to fetch Bamboo US holdings: {e_my_us}")
                
                bamboo_fetched = True
        except Exception as e:
            logger.warning(f"Failed to fetch Bamboo portfolio during sync: {e}")

        # Aggregate and combine portfolio cash and account size
        if alpaca_fetched or bybit_spot_fetched or bamboo_fetched:
            bybit_cash = float(usdt_free) if bybit_spot_fetched else 0.0
            combined_cash = alpaca_cash + bybit_cash + bamboo_cash
            combined_equity = alpaca_equity + bybit_cash + bamboo_equity
            
            if abs(portfolio.cash - combined_cash) > 0.01:
                portfolio.cash = combined_cash
                changed = True
                
            if abs(portfolio.account_size - combined_equity) > 0.01:
                portfolio.account_size = combined_equity
                changed = True

        # Helper to check if symbol is active on the broker
        def is_symbol_open(symbol: str, asset_class) -> bool:
            from trading_engine.market_hours import AssetClass
            s = symbol.upper().replace("/", "").replace(":", "").replace("-", "").strip()
            
            if asset_class == AssetClass.NGX_STOCK:
                for b_sym in bamboo_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False
            elif asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
                for b_sym in bybit_linear_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False
            elif asset_class == AssetClass.CRYPTO:
                # Check spot symbols
                for b_sym in bybit_spot_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                # Check linear symbols (in case it is a crypto perpetual short or mapped long)
                for b_sym in bybit_linear_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False
            else:
                for a_sym in alpaca_symbols:
                    if s == a_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False

        # 5. Check all open local positions
        from trading_engine.market_hours import classify_symbol, AssetClass
        open_local_positions = [p for p in portfolio.positions if p.status == "open"]
        
        for pos in open_local_positions:
            ac = classify_symbol(pos.symbol)
            
            # Skip checking if we failed to fetch data for the corresponding asset class
            if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
                if not bybit_linear_fetched:
                    continue
            elif ac == AssetClass.CRYPTO:
                if not bybit_spot_fetched:
                    continue
            elif ac == AssetClass.NGX_STOCK:
                if not bamboo_fetched:
                    continue
            else:
                if not alpaca_fetched:
                    continue

            if not is_symbol_open(pos.symbol, ac):
                logger.warning(f"Sync: {pos.symbol} is open in live_state.json but closed on broker. Closing locally.")
                # Cancel remaining broker-side trigger/limit orders if any are left
                _cancel_broker_stop_loss(pos)
                _cancel_broker_take_profit(pos)

                # Fetch actual exit details from broker
                fill_price, fee_cost, closed_at = _fetch_exit_details_from_broker(pos, ac)

                _finalize_closed_position(
                    portfolio=portfolio,
                    pos=pos,
                    fill_price=fill_price,
                    fee_cost=fee_cost,
                    closed_at=closed_at,
                    local_close_cash_update=False
                )
                changed = True

        if changed:
            portfolio.positions = [p for p in portfolio.positions if p.status == "open"]
            _save_state(portfolio)
            logger.info("Sync complete. State updated.")
            
        # Process the self-healing queue
        process_self_healing_queue(portfolio)
        return True

    except Exception as e:
        logger.error(f"Error in sync_with_broker: {e}")
    return False


def get_status() -> dict:
    global _last_sync_time
    now = time.time()
    if now - _last_sync_time > 60.0:
        try:
            sync_with_broker()
        except Exception as e:
            logger.warning(f"Failed to sync with broker: {e}")
        _last_sync_time = now

    portfolio = _load_state()
    open_trades = [asdict(p) for p in portfolio.open_positions]
    closed_trades = [asdict(p) for p in portfolio.closed_trades[-20:]]
    return {
        "account_size": portfolio.account_size,
        "cash": round(portfolio.cash, 2),
        "total_pnl": round(portfolio.total_pnl, 2),
        "total_pnl_pct": round(portfolio.total_pnl / portfolio.account_size * 100, 2) if portfolio.account_size > 0 else 0.0,
        "total_fees": round(portfolio.total_fees, 2),
        "total_fees_pct": round(portfolio.total_fees / portfolio.account_size * 100, 3) if portfolio.account_size > 0 else 0.0,
        "open_positions": len(portfolio.open_positions),
        "portfolio_heat": round(portfolio.portfolio_heat * 100, 2),
        "win_rate": round(portfolio.win_rate * 100, 1),
        "win_count": portfolio.win_count,
        "loss_count": portfolio.loss_count,
        "trades": open_trades + closed_trades,
        "self_healing_state": portfolio.self_healing_state,
    }


def _trigger_self_healing(symbol: str) -> bool:
    """Launches the self-healing optimization script in the background for a lost trade symbol. Returns True if successfully launched, False otherwise."""
    import subprocess
    import sys
    import os
    from pathlib import Path
    
    trading_engine_dir = Path(__file__).resolve().parent.parent
    python_bin = sys.executable
    script_path = trading_engine_dir / "run_self_healing.py"
    
    # Check for active self-healing process lock
    lock_file = Path("/tmp/self_healing.lock")
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            os.kill(pid, 0)  # Throws OSError if process is not running
            logger.warning(
                f"Self-Healing skipped for {symbol}: Another optimization process (PID {pid}) "
                f"is currently running. Skipping to prevent CPU/RAM overload."
            )
            return False
        except (ValueError, OSError):
            # Lock is stale or invalid, we can proceed
            pass

    cmd = [
        str(python_bin),
        str(script_path),
        "--symbol", symbol
    ]
    try:
        logger.info(f"❤️  Self-Healing: launching background optimization for {symbol}...")
        # Launch non-blocking background process
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detached from parent process group
            cwd=str(trading_engine_dir)
        )
        # Record new PID to lock file
        lock_file.write_text(str(proc.pid))
        return True
    except Exception as e:
        logger.error(f"Failed to launch self-healing for {symbol}: {e}")
        return False



def process_self_healing_queue(portfolio: LivePortfolio):
    """
    Checks if the self-healing process lock is free and launches the next pending optimization if any.
    Runs one-by-one to avoid CPU/RAM overload.
    """
    if not portfolio.pending_self_healing:
        return
    
    # 1. Check if lock file exists and has an active PID running
    lock_file = Path("/tmp/self_healing.lock")
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            os.kill(pid, 0)  # Throws OSError if process is not running
            # Still running, do not start another one
            logger.info(f"Self-Healing Queue: skipping processing because another optimization (PID {pid}) is running.")
            return
        except (ValueError, OSError):
            # Stale or invalid lock file
            pass
            
    # 2. Lock is free. Try to launch the first pending symbol.
    symbol = portfolio.pending_self_healing[0]
    try:
        # Actually launch the background process
        launched = _trigger_self_healing(symbol)
        if launched:
            # Remove from pending queue since it successfully launched
            portfolio.pending_self_healing.pop(0)
            
            # Update timestamp and database
            sh_state = portfolio.self_healing_state.setdefault(symbol, {"consecutive_losses": 0, "last_optimized_at": None})
            now_dt = datetime.now(timezone.utc)
            sh_state["last_optimized_at"] = now_dt.isoformat()
            db.save_symbol_state(symbol, last_optimized_at=now_dt)
            _save_state(portfolio)
        else:
            # Failed to launch (execution exception, not PID check)
            logger.error(f"Self-Healing: Failed to launch for {symbol}. Removing from queue to prevent blocking.")
            portfolio.pending_self_healing.pop(0)
            _save_state(portfolio)
    except Exception as e:
        logger.error(f"Self-Healing: Error processing queue for {symbol}: {e}. Removing from queue.")
        if portfolio.pending_self_healing:
            portfolio.pending_self_healing.pop(0)
            _save_state(portfolio)


def _finalize_closed_position(portfolio: LivePortfolio, pos: Position, fill_price: float, fee_cost: float, closed_at: str, local_close_cash_update: bool):
    # Gross PnL
    if pos.direction == "long":
        gross_pnl = (fill_price - pos.entry_price) / pos.entry_price * pos.size_usd
    else:
        gross_pnl = (pos.entry_price - fill_price) / pos.entry_price * pos.size_usd
    
    net_pnl = gross_pnl - fee_cost
    status = "closed" if net_pnl >= 0 else "stopped"

    portfolio.total_fees += fee_cost
    if pos.fee_usd is not None:
        pos.fee_usd = round(pos.fee_usd + fee_cost, 4)
    else:
        pos.fee_usd = round(fee_cost, 4)

    pos.exit_price = fill_price
    pos.pnl_usd = round(net_pnl, 2)
    pos.closed_at = closed_at
    pos.status = status
    
    if local_close_cash_update:
        portfolio.cash += pos.size_usd + net_pnl
    
    portfolio.total_pnl += net_pnl
    if net_pnl > 0:
        portfolio.win_count += 1
        # Reset consecutive losses for this symbol on a win
        sh_state = portfolio.self_healing_state.setdefault(pos.symbol, {"consecutive_losses": 0, "last_optimized_at": None})
        sh_state["consecutive_losses"] = 0
    else:
        portfolio.loss_count += 1
        
        # Track self-healing state
        sh_state = portfolio.self_healing_state.setdefault(pos.symbol, {"consecutive_losses": 0, "last_optimized_at": None})
        sh_state["consecutive_losses"] += 1
        
        # Check consecutive loss threshold and cooldown
        trigger_healing = False
        consec_losses = sh_state["consecutive_losses"]
        last_opt_str = sh_state.get("last_optimized_at")
        
        if consec_losses >= settings.self_healing_consecutive_losses:
            if not last_opt_str:
                trigger_healing = True
            else:
                try:
                    last_opt_dt = datetime.fromisoformat(last_opt_str)
                    time_elapsed = datetime.now(timezone.utc) - last_opt_dt
                    if time_elapsed.total_seconds() >= settings.self_healing_cooldown_hours * 3600:
                        trigger_healing = True
                    else:
                        logger.info(f"Self-Healing: {pos.symbol} skipped optimization — cooldown active (last optimized {time_elapsed.total_seconds()/3600:.1f}h ago).")
                except Exception as e_dt:
                    logger.warning(f"Error parsing last_optimized_at for {pos.symbol}: {e_dt}. Triggering anyway.")
                    trigger_healing = True
        else:
            logger.info(f"Self-Healing: {pos.symbol} has {consec_losses} consecutive loss(es) (requires {settings.self_healing_consecutive_losses}). Skipping optimization.")
                    
        if trigger_healing:
            if pos.symbol not in portfolio.pending_self_healing:
                portfolio.pending_self_healing.append(pos.symbol)
                logger.info(f"Self-Healing: Added {pos.symbol} to the pending optimization queue.")
            process_self_healing_queue(portfolio)

    if pos in portfolio.positions:
        portfolio.positions.remove(pos)
    if not any(c.symbol == pos.symbol and c.opened_at == pos.opened_at for c in portfolio.closed_trades):
        portfolio.closed_trades.append(pos)

    emoji = "✅" if net_pnl > 0 else "❌"
    logger.info(
        f"{emoji} LIVE {status.upper()}: {pos.symbol} | "
        f"Gross P&L=${gross_pnl:+,.2f} | Fees=${fee_cost:.2f} | "
        f"Net P&L=${net_pnl:+,.2f} | Total P&L=${portfolio.total_pnl:+,.2f}"
    )

    # Send Telegram notification
    send_message(
        f"{emoji} <b>LIVE Position Closed ({status.upper()})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: {pos.symbol}\n"
        f"💵 Size: ${pos.size_usd:,.2f}\n"
        f"📈 Entry Price: <code>{pos.entry_price:.4f}</code>\n"
        f"📉 Exit Price: <code>{fill_price:.4f}</code>\n"
        f"💰 Net P&L: <b>${net_pnl:+,.2f}</b>\n"
        f"🏷️ Fees paid: ${pos.fee_usd:.2f}\n"
        f"📊 Total Portfolio P&L: <b>${portfolio.total_pnl:+,.2f}</b>"
    )


def _fetch_exit_details_from_broker(pos: Position, asset_class) -> tuple[float, float, str]:
    """
    Queries the broker to find the actual exit price, fee, and closed timestamp.
    Falls back to defaults if any API call fails.
    """
    from trading_engine.market_hours import AssetClass
    
    # Defaults
    exit_price = pos.exit_price or pos.entry_price
    fee_cost = pos.size_usd * EXIT_FEE_RATE
    closed_at = datetime.now(timezone.utc).isoformat()
    
    try:
        if asset_class == AssetClass.CRYPTO or asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
            exchange = get_bybit_exchange()
            if asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) or ":" in pos.symbol:
                exchange.options["defaultType"] = "linear"
            else:
                exchange.options["defaultType"] = "spot"
                
            trades = exchange.fetch_my_trades(symbol=pos.symbol, limit=20)
            if trades:
                trades.sort(key=lambda x: x["timestamp"], reverse=True)
                target_side = "sell" if pos.direction == "long" else "buy"
                opened_dt = datetime.fromisoformat(pos.opened_at)
                for t in trades:
                    if t["side"].lower() == target_side:
                        t_time = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
                        if t_time >= opened_dt:
                            exit_price = float(t["price"])
                            
                            # Parse fee
                            fee_val = 0.0
                            if t.get("fee") and isinstance(t["fee"], dict):
                                fee_val = float(t["fee"].get("cost", 0.0))
                                fee_currency = t["fee"].get("currency")
                                if fee_currency and fee_currency not in ("USDT", "USD"):
                                    fee_val = fee_val * exit_price
                            fee_cost = fee_val
                            closed_at = t_time.isoformat()
                            logger.info(f"Sync: Found broker execution for {pos.symbol} at {exit_price:.4f} with fee ${fee_cost:.4f}")
                            break
        elif asset_class == AssetClass.NGX_STOCK:
            logger.info(f"Sync: Using local defaults for NGX stock {pos.symbol}")
        else:
            # Alpaca
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus, OrderSide
            client = get_alpaca_client()
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                symbols=[pos.symbol],
                limit=20
            )
            orders = client.get_orders(req)
            target_side = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
            opened_dt = datetime.fromisoformat(pos.opened_at)
            
            filled_orders = [o for o in orders if o.status.value == "filled" and o.side == target_side]
            if filled_orders:
                filled_orders.sort(key=lambda o: o.filled_at, reverse=True)
                for o in filled_orders:
                    if o.filled_at >= opened_dt:
                        exit_price = float(o.filled_avg_price)
                        closed_at = o.filled_at.isoformat()
                        fee_cost = 0.0
                        logger.info(f"Sync: Found Alpaca execution for {pos.symbol} at {exit_price:.4f}")
                        break
    except Exception as e:
        logger.warning(f"Failed to fetch exit details from broker for {pos.symbol}: {e}")
        
    return exit_price, fee_cost, closed_at



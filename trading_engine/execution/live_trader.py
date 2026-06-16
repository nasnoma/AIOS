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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LivePortfolio":
        p = cls(account_size=d["account_size"], cash=d["cash"],
                total_pnl=d["total_pnl"], win_count=d["win_count"],
                loss_count=d["loss_count"],
                total_fees=d.get("total_fees", 0.0))
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

    is_cfd      = asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL)
    is_crypto   = asset_class == AssetClass.CRYPTO

    # ── Market hours guard for CFDs ─────────────────────────────────────────
    if is_cfd:
        status = market_status(symbol, extended_stock_hours=settings.extended_cfd_hours)
        if not status.is_open:
            status.log(symbol)
            logger.warning(
                f"⏸️  Blocking live {direction.upper()} on {symbol} — market is closed. "
                f"Reason: {status.reason}"
            )
            return None

    # ── Short validation ─────────────────────────────────────────────────────
    if direction == "short" and not is_cfd:
        logger.warning(
            f"Short positions are only supported on Bybit linear CFDs. "
            f"{symbol} ({asset_class.value}) does not support shorting. Skipping."
        )
        return None

    # ── Execute ──────────────────────────────────────────────────────────────
    try:
        if is_cfd:
            # Bybit linear perpetual — supports both buy (long) and sell (short)
            side       = "buy" if direction == "long" else "sell"
            fill_price = place_bybit_linear_order(symbol, side, size_usd, entry)

        elif is_crypto:
            # Bybit spot — long only
            fill_price = place_bybit_market_order(symbol, "buy", size_usd, entry)

        else:
            # Plain stock via Alpaca — long only
            qty         = size_usd / entry
            qty_rounded = round(qty, 4)
            if qty_rounded <= 0:
                logger.warning(f"Stock quantity too small for {symbol} ({qty}). Skipping.")
                return None
            fill_price = place_alpaca_market_order(symbol, "buy", qty_rounded, entry)

    except Exception as e:
        logger.error(f"Failed to execute live open trade for {symbol}: {e}")
        send_message(f"⚠️ <b>LIVE execution error</b> for {symbol}: {e}")
        return None

    portfolio = _load_state()

    # Deduct transaction fee
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


def update_prices(current_prices: dict[str, float]):
    """Check if any open positions hit SL or TP."""
    portfolio = _load_state()
    for pos in portfolio.open_positions:
        price = current_prices.get(pos.symbol)
        if not price:
            continue

        if pos.direction == "long":
            if price <= pos.stop_loss:
                _close_position(portfolio, pos, price, "stopped")
            elif price >= pos.take_profit:
                _close_position(portfolio, pos, price, "closed")
        else:  # short
            if price >= pos.stop_loss:
                _close_position(portfolio, pos, price, "stopped")
            elif price <= pos.take_profit:
                _close_position(portfolio, pos, price, "closed")

    _save_state(portfolio)


def _close_position(portfolio: LivePortfolio, pos: Position, exit_price: float, status: str):
    from trading_engine.market_hours import classify_symbol, AssetClass
    
    asset_class = classify_symbol(pos.symbol)
    is_cfd      = asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL)
    is_crypto   = asset_class == AssetClass.CRYPTO
    
    try:
        if is_cfd:
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

    # Gross PnL
    gross_pnl = (fill_price - pos.entry_price) / pos.entry_price * pos.size_usd
    
    # Exit fee
    exit_fee = pos.size_usd * EXIT_FEE_RATE
    net_pnl = gross_pnl - exit_fee

    portfolio.total_fees += exit_fee
    if pos.fee_usd is not None:
        pos.fee_usd = round(pos.fee_usd + exit_fee, 4)
    else:
        pos.fee_usd = round(exit_fee, 4)

    pos.exit_price = fill_price
    pos.pnl_usd = round(net_pnl, 2)
    pos.closed_at = datetime.now(timezone.utc).isoformat()
    pos.status = status
    portfolio.cash += pos.size_usd + net_pnl
    portfolio.total_pnl += net_pnl
    if net_pnl > 0:
        portfolio.win_count += 1
    else:
        portfolio.loss_count += 1
        try:
            _trigger_self_healing(pos.symbol)
        except Exception as ex_sh:
            logger.error(f"Failed to trigger self-healing for {pos.symbol}: {ex_sh}")
            
    portfolio.positions.remove(pos)
    portfolio.closed_trades.append(pos)

    emoji = "✅" if net_pnl > 0 else "❌"
    logger.info(
        f"{emoji} LIVE {status.upper()}: {pos.symbol} | "
        f"Gross P&L=${gross_pnl:+,.2f} | Fees=${exit_fee:.2f} | "
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


_last_sync_time = 0.0


def sync_with_broker() -> bool:
    """
    Synchronizes the local live_state.json with actual positions on the broker/exchange.
    Updates any locally open position that has been closed on the broker, ONLY if the broker queries succeed.
    """
    try:
        portfolio = _load_state()
        changed = False

        # If running in live mode and the local portfolio is completely empty (e.g. after container redeployment),
        # attempt to reconstruct positions and closed trades from Bybit execution history.
        if settings.trading_mode == "live" and not portfolio.positions and not portfolio.closed_trades:
            logger.info("Local live state is empty. Reconstructing from Bybit execution history...")
            try:
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
                        
                        current_pos = None
                        for ex in symbol_execs:
                            side = ex["side"].lower()
                            price = float(ex["price"])
                            cost = float(ex["cost"])
                            timestamp = int(ex["timestamp"])
                            dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
                            
                            fee_cost = 0.0
                            if ex.get("fee") and isinstance(ex["fee"], dict):
                                fee_cost = float(ex["fee"].get("cost", 0.0))
                            
                            if side == "buy":
                                if current_pos is None:
                                    current_pos = Position(
                                        symbol=sym,
                                        direction="long",
                                        entry_price=price,
                                        size_usd=cost,
                                        stop_loss=round(price * 0.95, 4),      # 5% default SL fallback
                                        take_profit=round(price * 1.10, 4),    # 10% default TP fallback
                                        opened_at=dt,
                                        status="open",
                                        fee_usd=fee_cost
                                    )
                            elif side == "sell":
                                if current_pos is not None:
                                    current_pos.exit_price = price
                                    current_pos.closed_at = dt
                                    current_pos.fee_usd = (current_pos.fee_usd or 0.0) + fee_cost
                                    
                                    qty = current_pos.size_usd / current_pos.entry_price
                                    pnl = (price - current_pos.entry_price) * qty
                                    current_pos.pnl_usd = round(pnl, 4)
                                    current_pos.status = "closed" if pnl >= 0 else "stopped"
                                    
                                    reconstructed_closed.append(current_pos)
                                    current_pos = None
                                    
                        if current_pos is not None:
                            reconstructed_positions.append(current_pos)
                            
                    portfolio.positions = reconstructed_positions
                    portfolio.closed_trades = reconstructed_closed
                    portfolio.win_count = sum(1 for t in reconstructed_closed if t.status == "closed")
                    portfolio.loss_count = sum(1 for t in reconstructed_closed if t.status == "stopped")
                    portfolio.total_pnl = sum(t.pnl_usd for t in reconstructed_closed if t.pnl_usd is not None)
                    portfolio.total_fees = sum(t.fee_usd for t in reconstructed_closed if t.fee_usd is not None) + \
                                           sum(p.fee_usd for p in reconstructed_positions if p.fee_usd is not None)
                    changed = True
                    logger.success(f"Reconstructed {len(portfolio.positions)} open positions and {len(portfolio.closed_trades)} closed trades from Bybit.")
            except Exception as e:
                logger.error(f"Failed to reconstruct portfolio from Bybit: {e}")

        # 1. Fetch Alpaca positions
        alpaca_fetched = False
        alpaca_symbols = set()
        try:
            alpaca = get_alpaca_client()
            for pos in alpaca.get_all_positions():
                alpaca_symbols.add(pos.symbol.upper())
            alpaca_fetched = True
        except Exception as e:
            logger.warning(f"Failed to fetch Alpaca positions during sync: {e}")

        # 2. Fetch Bybit Spot balances
        bybit_spot_fetched = False
        bybit_spot_symbols = set()
        try:
            bybit = get_bybit_exchange()
            balance = bybit.fetch_balance()
            
            # Sync USDT cash balance from exchange
            usdt_free = balance.get('USDT', {}).get('free')
            if usdt_free is not None:
                cash_val = float(usdt_free)
                if abs(portfolio.cash - cash_val) > 0.01:
                    portfolio.cash = cash_val
                    # Scale initial account_size to match current balance if it is at default
                    if portfolio.account_size == settings.account_size or portfolio.account_size == 10000.0:
                        portfolio.account_size = cash_val
                    changed = True

            for currency, total in balance.get('total', {}).items():
                if currency not in ('USDT', 'USDC', 'USD') and total > 0.00001:
                    bybit_spot_symbols.add(f"{currency}/USDT".upper())
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

        # Helper to check if symbol is active on the broker
        def is_symbol_open(symbol: str, asset_class) -> bool:
            from trading_engine.market_hours import AssetClass
            s = symbol.upper().replace("/", "").replace(":", "").replace("-", "").strip()
            
            if asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
                for b_sym in bybit_linear_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False
            elif asset_class == AssetClass.CRYPTO:
                for b_sym in bybit_spot_symbols:
                    if s == b_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False
            else:
                for a_sym in alpaca_symbols:
                    if s == a_sym.replace("/", "").replace(":", "").replace("-", "").strip():
                        return True
                return False

        # 4. Check all open local positions
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
            else:
                if not alpaca_fetched:
                    continue

            if not is_symbol_open(pos.symbol, ac):
                logger.warning(f"Sync: {pos.symbol} is open in live_state.json but closed on broker. Closing locally.")
                pos.status = "closed"
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                pos.exit_price = pos.exit_price or pos.entry_price
                pos.pnl_usd = pos.pnl_usd or 0.0
                
                if not any(c.symbol == pos.symbol and c.opened_at == pos.opened_at for c in portfolio.closed_trades):
                    portfolio.closed_trades.append(pos)
                changed = True

        if changed:
            portfolio.positions = [p for p in portfolio.positions if p.status == "open"]
            _save_state(portfolio)
            logger.info("Sync complete. State updated.")
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
    }


def _trigger_self_healing(symbol: str):
    """Launches the self-healing optimization script in the background for a lost trade symbol."""
    import subprocess
    from pathlib import Path
    
    project_root = Path(__file__).resolve().parent.parent
    python_bin = project_root / "trading_engine" / "venv" / "bin" / "python"
    script_path = project_root / "trading_engine" / "run_self_healing.py"
    
    cmd = [
        str(python_bin),
        str(script_path),
        "--symbol", symbol
    ]
    try:
        logger.info(f"❤️  Self-Healing: launching background optimization for {symbol}...")
        # Launch non-blocking background process
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detached from parent process group
            cwd=str(project_root)
        )
    except Exception as e:
        logger.error(f"Failed to launch self-healing for {symbol}: {e}")



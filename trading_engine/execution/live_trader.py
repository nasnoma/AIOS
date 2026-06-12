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

def place_bybit_market_order(symbol: str, side: str, amount_usd: float, current_price: float) -> float:
    """
    Submits a market order on Bybit.
    For buy order: places a market buy for base currency quantity.
    Returns the actual average fill price.
    """
    exchange = get_bybit_exchange()
    exchange.load_markets()
    
    # Calculate amount in base currency
    qty = amount_usd / current_price
    # Format amount with CCXT precision helper
    qty_str = exchange.amount_to_precision(symbol, qty)
    qty_formatted = float(qty_str)
    
    logger.info(f"Placing Bybit Spot Market {side.upper()} order for {symbol}: qty={qty_formatted}")
    order = exchange.create_order(symbol, 'market', side, qty_formatted)
    
    fill_price = order.get("average") or order.get("price")
    if fill_price is None:
        fill_price = current_price
    return float(fill_price)


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
    try:
        order = client.submit_order(order_data=order_data)
    except Exception as e:
        if "not fractionable" in str(e).lower():
            int_qty = int(round(qty))
            if int_qty < 1:
                int_qty = 1
            logger.warning(f"Asset {symbol} is not fractionable. Retrying with integer quantity: {int_qty}")
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=int_qty,
                side=side,
                time_in_force=TimeInForce.DAY
            )
            order = client.submit_order(order_data=order_data)
        else:
            raise e
    
    fill_price = order.filled_avg_price
    if fill_price is not None:
        fill_price = float(fill_price)
    else:
        # Wait up to 3 seconds for the order to fill
        import time
        for _ in range(3):
            time.sleep(1)
            order = client.get_order_by_id(order.id)
            if order.filled_avg_price is not None:
                fill_price = float(order.filled_avg_price)
                break
        if fill_price is None:
            fill_price = current_price
            
    return fill_price


# ── Public API ────────────────────────────────────────────────────────────

def open_trade(symbol: str, direction: str, entry: float,
               size_usd: float, stop_loss: float, take_profit: float) -> Optional[Position]:
    if direction == "short":
        logger.warning(f"Spot trading does not support short positions for {symbol}. Skipping.")
        return None

    is_crypto = "/" in symbol or symbol.endswith("USDT") or symbol.endswith("USD")
    
    try:
        if is_crypto:
            fill_price = place_bybit_market_order(symbol, "buy", size_usd, entry)
        else:
            qty = size_usd / entry
            qty_rounded = round(qty, 4)
            if qty_rounded <= 0:
                logger.warning(f"Calculated stock quantity for {symbol} is too small: {qty}. Skipping.")
                return None
            fill_price = place_alpaca_market_order(symbol, "buy", qty_rounded, entry)
            
    except Exception as e:
        logger.error(f"Failed to execute live open trade for {symbol}: {e}")
        return None

    portfolio = _load_state()

    # Deduct transaction fee
    entry_fee = size_usd * ENTRY_FEE_RATE
    portfolio.cash -= entry_fee
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
    
    logger.success(
        f"📝 LIVE {direction.upper()} opened: {symbol} | "
        f"Size=${size_usd:,.0f} | SL={stop_loss:.4f} | TP={take_profit:.4f} | "
        f"Entry price={fill_price:.4f}"
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
    is_crypto = "/" in pos.symbol or pos.symbol.endswith("USDT") or pos.symbol.endswith("USD")
    
    try:
        if is_crypto:
            fill_price = place_bybit_market_order(pos.symbol, "sell", pos.size_usd, exit_price)
        else:
            qty = pos.size_usd / pos.entry_price
            qty_rounded = round(qty, 4)
            fill_price = place_alpaca_market_order(pos.symbol, "sell", qty_rounded, exit_price)
    except Exception as e:
        logger.error(f"Failed to execute live close trade for {pos.symbol}: {e}")
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
    portfolio.positions.remove(pos)
    portfolio.closed_trades.append(pos)

    emoji = "✅" if net_pnl > 0 else "❌"
    logger.info(
        f"{emoji} LIVE {status.upper()}: {pos.symbol} | "
        f"Gross P&L=${gross_pnl:+,.2f} | Fees=${exit_fee:.2f} | "
        f"Net P&L=${net_pnl:+,.2f} | Total P&L=${portfolio.total_pnl:+,.2f}"
    )


def get_status() -> dict:
    portfolio = _load_state()
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
        "trades": [asdict(p) for p in portfolio.closed_trades[-20:]],
    }

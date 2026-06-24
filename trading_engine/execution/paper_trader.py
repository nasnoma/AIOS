"""
trading_engine/execution/paper_trader.py

Paper trading simulator.
Tracks virtual positions, P&L, and portfolio heat.
Persists state to JSON file between runs.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger
import threading
from contextlib import contextmanager

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

from trading_engine.config import settings
from trading_engine.alerts.telegram_bot import send_message

STATE_FILE = Path(__file__).parent.parent / "paper_state.json"
LOCK_FILE = Path(__file__).parent.parent / "paper_state.lock"
_lock_state = threading.local()

@contextmanager
def state_lock():
    """
    Acquires an exclusive file lock on paper_state.lock.
    Re-entrant within the same thread.
    """
    if not hasattr(_lock_state, "depth"):
        _lock_state.depth = 0
        _lock_state.fd = None

    if _lock_state.depth == 0:
        if HAS_FCNTL:
            if not LOCK_FILE.exists():
                LOCK_FILE.touch()
            _lock_state.fd = open(LOCK_FILE, "r+")
            fcntl.flock(_lock_state.fd.fileno(), fcntl.LOCK_EX)
            
    _lock_state.depth += 1
    try:
        yield
    finally:
        _lock_state.depth -= 1
        if _lock_state.depth == 0:
            if HAS_FCNTL and _lock_state.fd:
                fcntl.flock(_lock_state.fd.fileno(), fcntl.LOCK_UN)
                _lock_state.fd.close()
                _lock_state.fd = None

def locked(func):
    """
    Decorator to wrap a function call with state_lock().
    """
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        with state_lock():
            return func(*args, **kwargs)
    return wrapper

# ── Transaction Cost Model ───────────────────────────────────────────────
# Entry fee (taker order): 0.04% (Binance market order)
# Exit fee (taker order):  0.04% (Binance market order)
# Slippage estimate:       0.02% (conservative, liquid pairs)
# Total round-trip cost:   ~0.10% of position size
ENTRY_FEE_RATE = 0.0006   # 0.06% entry (fee + slippage)
EXIT_FEE_RATE  = 0.0006   # 0.06% exit  (fee + slippage)


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
    tp_order_id: Optional[str] = None       # ID of the take profit order on the broker/exchange


@dataclass
class PaperPortfolio:
    account_size: float = field(default_factory=lambda: settings.account_size)
    cash: float = field(default_factory=lambda: settings.account_size)
    positions: list[Position] = field(default_factory=list)
    closed_trades: list[Position] = field(default_factory=list)
    total_pnl: float = 0.0
    total_fees: float = 0.0   # cumulative fees paid across all trades
    win_count: int = 0
    loss_count: int = 0
    self_healing_state: dict[str, dict] = field(default_factory=dict)

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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperPortfolio":
        p = cls(account_size=d["account_size"], cash=d["cash"],
                total_pnl=d["total_pnl"], win_count=d["win_count"],
                loss_count=d["loss_count"],
                total_fees=d.get("total_fees", 0.0),
                self_healing_state=d.get("self_healing_state", {}))
        p.positions = [Position(**pos) for pos in d.get("positions", [])]
        p.closed_trades = [Position(**pos) for pos in d.get("closed_trades", [])]
        return p


def _load_state() -> PaperPortfolio:
    with state_lock():
        if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
            try:
                with open(STATE_FILE) as f:
                    return PaperPortfolio.from_dict(json.load(f))
            except Exception as e:
                logger.error(f"CRITICAL: Could not parse paper state file: {e}")
                raise RuntimeError(f"Failed to load portfolio state: {e}") from e
        return PaperPortfolio()


def _save_state(portfolio: PaperPortfolio):
    with state_lock():
        with open(STATE_FILE, "w") as f:
            json.dump(portfolio.to_dict(), f, indent=2)


def open_trade(symbol: str, direction: str, entry: float,
               size_usd: float, stop_loss: float, take_profit: float,
               **kwargs) -> Position:
    from trading_engine.market_hours import classify_symbol, AssetClass
    asset_class = classify_symbol(symbol)
    if asset_class == AssetClass.CRYPTO:
        use_perps = getattr(settings, "crypto_use_perpetuals", False)
        if direction == "short" or use_perps:
            if not symbol.endswith(":USDT"):
                logger.info(f"Mapping paper crypto symbol {symbol} to Bybit Linear Perpetual: {symbol}:USDT")
                symbol = f"{symbol}:USDT"

    with state_lock():
        portfolio = _load_state()

        # Prevent duplicate positions on the same asset
        if any(p.symbol == symbol for p in portfolio.open_positions):
            logger.info(f"⏭️ [Lock Guard] Skipping paper execution for {symbol}: position already open.")
            return None

        # Check max positions under lock
        max_positions = settings.max_concurrent_positions
        if len(portfolio.open_positions) >= max_positions:
            logger.warning(f"Paper execution blocked for {symbol}: Max concurrent positions ({max_positions}) reached.")
            return None

        # Deduct entry fee + slippage from cash immediately
        entry_fee = size_usd * ENTRY_FEE_RATE
        total_needed = size_usd + entry_fee

        if portfolio.cash < total_needed:
            logger.warning(f"Paper execution blocked for {symbol}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${total_needed:,.2f})")
            return None

        portfolio.cash -= entry_fee
        portfolio.total_fees += entry_fee

        pos = Position(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            size_usd=size_usd,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.now(timezone.utc).isoformat(),
            fee_usd=entry_fee,   # will be updated on close
            atr=kwargs.get("atr", 0.0),
            trailing_high=entry if direction == "long" else None,
            trailing_low=entry if direction == "short" else None,
        )
        portfolio.positions.append(pos)
        portfolio.cash -= size_usd
        _save_state(portfolio)
        logger.success(
            f"📝 PAPER {direction.upper()} opened: {symbol} | "
            f"Size=${size_usd:,.0f} | SL={stop_loss:.4f} | TP={take_profit:.4f} | "
            f"Entry fee=${entry_fee:.2f} ({ENTRY_FEE_RATE:.2%})"
        )
        
        # Send Telegram notification
        send_message(
            f"🟢 <b>PAPER {direction.upper()} Opened</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Symbol: {symbol}\n"
            f"💵 Size: ${size_usd:,.2f}\n"
            f"📈 Entry Price: <code>{entry:.4f}</code>\n"
            f"🛑 Stop Loss: <code>{stop_loss:.4f}</code>\n"
            f"🎯 Take Profit: <code>{take_profit:.4f}</code>"
        )
        return pos


def _apply_trailing_stop(pos: Position, price: float) -> None:
    """
    ATR trailing stop ratchet — called before SL/TP check.

    Ratchet levels (long example, mirrored for short):
      • Price reaches entry + 1×ATR → stop moves to entry (breakeven)
      • Price reaches entry + 2×ATR → stop moves to entry + 1×ATR (lock profit)

    The stop only ever moves in the profitable direction — never widens.
    """
    if pos.atr <= 0:
        return   # ATR not stored — trailing stop not active for this position

    atr = pos.atr

    if pos.direction == "long":
        # Update trailing high
        if pos.trailing_high is None or price > pos.trailing_high:
            pos.trailing_high = price

        profit_in_atr = (pos.trailing_high - pos.entry_price) / atr
        if profit_in_atr >= 2.0:
            # Lock in +1 ATR of profit
            new_sl = pos.entry_price + atr
            if new_sl > pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (2×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → {new_sl:.4f}"
                )
                pos.stop_loss = new_sl
        elif profit_in_atr >= 1.0:
            # Move to breakeven
            if pos.entry_price > pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (1×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → breakeven {pos.entry_price:.4f}"
                )
                pos.stop_loss = pos.entry_price

    else:  # short
        # Update trailing low
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
        elif profit_in_atr >= 1.0:
            if pos.entry_price < pos.stop_loss:
                logger.info(
                    f"🔒 Trailing stop ratchet (1×ATR): {pos.symbol} SL {pos.stop_loss:.4f} → breakeven {pos.entry_price:.4f}"
                )
                pos.stop_loss = pos.entry_price


@locked
def update_prices(current_prices: dict[str, float]):
    """Check if any open positions hit SL or TP. Applies ATR trailing stop ratchet first."""
    portfolio = _load_state()
    for pos in portfolio.open_positions:
        price = current_prices.get(pos.symbol)
        if not price:
            continue

        # Apply trailing stop ratchet before SL/TP check
        _apply_trailing_stop(pos, price)

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


def _close_position(portfolio: PaperPortfolio, pos: Position, exit_price: float, status: str):
    if pos.direction == "long":
        gross_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
    else:
        gross_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.size_usd

    # Deduct exit fee + slippage
    exit_fee = pos.size_usd * EXIT_FEE_RATE
    net_pnl = gross_pnl - exit_fee

    portfolio.total_fees += exit_fee
    if pos.fee_usd is not None:
        pos.fee_usd = round(pos.fee_usd + exit_fee, 4)   # total round-trip fee on this trade
    else:
        pos.fee_usd = round(exit_fee, 4)

    pos.exit_price = exit_price
    pos.pnl_usd = round(net_pnl, 2)
    pos.closed_at = datetime.now(timezone.utc).isoformat()
    pos.status = status
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
            try:
                _trigger_self_healing(pos.symbol)
                sh_state["last_optimized_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as ex_sh:
                logger.error(f"Failed to trigger self-healing for {pos.symbol}: {ex_sh}")
            
    portfolio.positions.remove(pos)
    portfolio.closed_trades.append(pos)

    emoji = "✅" if net_pnl > 0 else "❌"
    logger.info(
        f"{emoji} PAPER {status.upper()}: {pos.symbol} | "
        f"Gross P&L=${gross_pnl:+,.2f} | Fees=${exit_fee:.2f} | "
        f"Net P&L=${net_pnl:+,.2f} | Total P&L=${portfolio.total_pnl:+,.2f}"
    )

    # Send Telegram notification
    send_message(
        f"{emoji} <b>PAPER Position Closed ({status.upper()})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: {pos.symbol}\n"
        f"💵 Size: ${pos.size_usd:,.2f}\n"
        f"📈 Entry Price: <code>{pos.entry_price:.4f}</code>\n"
        f"📉 Exit Price: <code>{exit_price:.4f}</code>\n"
        f"💰 Net P&L: <b>${net_pnl:+,.2f}</b>\n"
        f"🏷️ Fees paid: ${pos.fee_usd:.2f}\n"
        f"📊 Total Portfolio P&L: <b>${portfolio.total_pnl:+,.2f}</b>"
    )


@locked
def get_status() -> dict:
    portfolio = _load_state()
    open_trades = [asdict(p) for p in portfolio.open_positions]
    closed_trades = [asdict(p) for p in portfolio.closed_trades[-20:]]
    return {
        "account_size": portfolio.account_size,
        "cash": round(portfolio.cash, 2),
        "total_pnl": round(portfolio.total_pnl, 2),
        "total_pnl_pct": round(portfolio.total_pnl / portfolio.account_size * 100, 2),
        "total_fees": round(portfolio.total_fees, 2),
        "total_fees_pct": round(portfolio.total_fees / portfolio.account_size * 100, 3),
        "open_positions": len(portfolio.open_positions),
        "portfolio_heat": round(portfolio.portfolio_heat * 100, 2),
        "win_rate": round(portfolio.win_rate * 100, 1),
        "win_count": portfolio.win_count,
        "loss_count": portfolio.loss_count,
        "trades": open_trades + closed_trades,
        "self_healing_state": portfolio.self_healing_state,
    }


def _trigger_self_healing(symbol: str):
    """Launches the self-healing optimization script in the background for a lost trade symbol."""
    import subprocess
    import sys
    from pathlib import Path
    
    trading_engine_dir = Path(__file__).resolve().parent.parent
    python_bin = sys.executable
    script_path = trading_engine_dir / "run_self_healing.py"
    
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
            cwd=str(trading_engine_dir)
        )
    except Exception as e:
        logger.error(f"Failed to launch self-healing for {symbol}: {e}")


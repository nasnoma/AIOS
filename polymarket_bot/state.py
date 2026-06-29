"""
polymarket_bot/state.py

JSON-backed paper/live portfolio state for Bybit Arbitrage Bot.
Atomic file-lock protected reads/writes.
"""
from __future__ import annotations
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows fallback

_STATE_FILE = Path(__file__).parent / "paper_state.json"
_LOCK_FILE = Path(__file__).parent / "paper_state.lock"
_tls = threading.local()


@contextmanager
def _file_lock():
    """Re-entrant file-level lock for safe concurrent access."""
    if not hasattr(_tls, "depth"):
        _tls.depth = 0
        _tls.fd = None
    if _tls.depth == 0 and _HAS_FCNTL:
        if not _LOCK_FILE.exists():
            _LOCK_FILE.touch()
        _tls.fd = open(_LOCK_FILE, "r+")
        import fcntl as _fcntl
        _fcntl.flock(_tls.fd.fileno(), _fcntl.LOCK_EX)
    _tls.depth += 1
    try:
        yield
    finally:
        _tls.depth -= 1
        if _tls.depth == 0 and _HAS_FCNTL and _tls.fd:
            import fcntl as _fcntl
            _fcntl.flock(_tls.fd.fileno(), _fcntl.LOCK_UN)
            _tls.fd.close()
            _tls.fd = None


@dataclass
class ArbTradeCycle:
    cycle_id: str             # unique ID
    direction: str            # "FORWARD" | "REVERSE"
    started_at: str           # ISO timestamp
    completed_at: str         # ISO timestamp
    est_edge_pct: float       # Expected net edge %
    actual_edge_pct: float    # Actual realized net edge %
    size_usdt: float          # USDT starting size
    pnl_usdt: float           # Net profit/loss in USDT
    status: str               # "completed" | "failed"
    reason: str = ""          # Rejection or execution details
    leg1_price: float = 0.0
    leg2_price: float = 0.0
    leg3_price: float = 0.0


@dataclass
class PortfolioState:
    account_size: float = 500.0
    cash: float = 500.0
    closed_trades: list = field(default_factory=list)    # list of ArbTradeCycle dicts
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_reset_date: str = ""
    win_count: int = 0
    loss_count: int = 0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    cycle_count: int = 0
    route_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        obj = cls(
            account_size=d.get("account_size", 500.0),
            cash=d.get("cash", 500.0),
            total_pnl=d.get("total_pnl", 0.0),
            daily_pnl=d.get("daily_pnl", 0.0),
            daily_reset_date=d.get("daily_reset_date", ""),
            win_count=d.get("win_count", 0),
            loss_count=d.get("loss_count", 0),
            avg_win_usd=d.get("avg_win_usd", 0.0),
            avg_loss_usd=d.get("avg_loss_usd", 0.0),
            cycle_count=d.get("cycle_count", 0),
            route_stats=d.get("route_stats", {}),
        )
        obj.closed_trades = d.get("closed_trades", [])
        if not obj.route_stats:
            obj.route_stats = {
                "BTC-ETH-FORWARD": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
                "BTC-ETH-REVERSE": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
                "BTC-SOL-FORWARD": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
                "BTC-SOL-REVERSE": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
            }
        return obj

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.0


def load_state() -> PortfolioState:
    with _file_lock():
        if _STATE_FILE.exists() and _STATE_FILE.stat().st_size > 0:
            try:
                with open(_STATE_FILE) as f:
                    return PortfolioState.from_dict(json.load(f))
            except Exception as e:
                logger.error(f"Failed to parse paper_state.json: {e} — resetting state.")
        from polymarket_bot.config import settings
        return PortfolioState(account_size=settings.account_size, cash=settings.account_size)


def save_state(state: PortfolioState) -> None:
    with _file_lock():
        with open(_STATE_FILE, "w") as f:
            json.dump(state.to_dict(), f, indent=2)


def get_status() -> dict:
    s = load_state()
    return {
        "account_size": s.account_size,
        "cash": round(s.cash, 2),
        "total_pnl": round(s.total_pnl, 2),
        "total_pnl_pct": round(s.total_pnl / s.account_size * 100, 2) if s.account_size else 0,
        "daily_pnl": round(s.daily_pnl, 2),
        "open_positions": 0,  # Arbitrage cycles close instantly, no open positions overnight
        "win_count": s.win_count,
        "loss_count": s.loss_count,
        "win_rate_pct": round(s.win_rate * 100, 1),
        "cycle_count": s.cycle_count,
        "avg_win_usd": s.avg_win_usd,
        "avg_loss_usd": s.avg_loss_usd,
    }


def reset_daily_pnl_if_new_day(state: PortfolioState) -> PortfolioState:
    """Resets daily_pnl if we've crossed into a new UTC day."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.daily_reset_date != today:
        state.daily_pnl = 0.0
        state.daily_reset_date = today
    return state

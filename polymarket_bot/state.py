"""
polymarket_bot/state.py

JSON-backed paper portfolio state.
Atomic file-lock protected reads/writes.
No external DB dependency.
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
class OpenPosition:
    position_id: str          # unique uuid
    asset: str                # e.g. "BTC"
    token_id_yes: str
    token_id_no: str
    signal_type: str          # SPREAD_ARB | MOMENTUM_LONG | MOMENTUM_SHORT
    side: str                 # "YES" | "NO" | "BOTH"
    entry_price_yes: Optional[float]
    entry_price_no: Optional[float]
    size_usd: float
    window_start: int         # Unix timestamp of window start
    window_end: int           # Unix timestamp of window end
    opened_at: str            # ISO timestamp
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    status: str = "open"      # open | closed | expired


@dataclass
class PortfolioState:
    account_size: float = 1000.0
    cash: float = 1000.0
    positions: list = field(default_factory=list)        # list[OpenPosition dicts]
    closed_trades: list = field(default_factory=list)    # list[OpenPosition dicts]
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_reset_date: str = ""
    win_count: int = 0
    loss_count: int = 0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    cycle_count: int = 0      # completed windows tracked

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        obj = cls(
            account_size=d.get("account_size", 1000.0),
            cash=d.get("cash", 1000.0),
            total_pnl=d.get("total_pnl", 0.0),
            daily_pnl=d.get("daily_pnl", 0.0),
            daily_reset_date=d.get("daily_reset_date", ""),
            win_count=d.get("win_count", 0),
            loss_count=d.get("loss_count", 0),
            avg_win_usd=d.get("avg_win_usd", 0.0),
            avg_loss_usd=d.get("avg_loss_usd", 0.0),
            cycle_count=d.get("cycle_count", 0),
        )
        obj.positions = d.get("positions", [])
        obj.closed_trades = d.get("closed_trades", [])
        # One-time backfill: if avg fields are missing (old state file) but trades exist, compute from history
        if obj.avg_win_usd == 0.0 and obj.avg_loss_usd == 0.0 and obj.closed_trades:
            wins = [t["pnl_usd"] for t in obj.closed_trades if (t.get("pnl_usd") or 0) > 0]
            losses = [t["pnl_usd"] for t in obj.closed_trades if (t.get("pnl_usd") or 0) <= 0]
            if wins:
                obj.avg_win_usd = round(sum(wins) / len(wins), 4)
            if losses:
                obj.avg_loss_usd = round(sum(losses) / len(losses), 4)
        return obj

    @property
    def open_positions(self) -> list[dict]:
        return [p for p in self.positions if p.get("status") == "open"]

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
        "open_positions": len(s.open_positions),
        "win_count": s.win_count,
        "loss_count": s.loss_count,
        "win_rate_pct": round(s.win_rate * 100, 1),
        "cycle_count": s.cycle_count,
    }


def reset_daily_pnl_if_new_day(state: PortfolioState) -> PortfolioState:
    """Resets daily_pnl if we've crossed into a new UTC day."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.daily_reset_date != today:
        state.daily_pnl = 0.0
        state.daily_reset_date = today
    return state

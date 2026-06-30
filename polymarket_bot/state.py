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
    expected_pnl: float = 0.0
    actual_pnl: float = 0.0
    slippage_pct: float = 0.0
    priority_fee_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortfolioState:
    account_size: float = 500.0
    cex_cash: float = 250.0
    dex_cash: float = 250.0
    cex_assets: dict = field(default_factory=lambda: {"SOL": 1.5, "BTC": 0.005, "ETH": 0.05})
    dex_assets: dict = field(default_factory=lambda: {"SOL": 1.5, "BTC": 0.005, "ETH": 0.05})
    closed_trades: list = field(default_factory=list)      # list of ArbTradeCycle dicts
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_reset_date: str = ""
    win_count: int = 0
    loss_count: int = 0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    cycle_count: int = 0
    route_stats: dict = field(default_factory=dict)
    peak_account_size: float = 500.0
    max_drawdown_paused: bool = False
    total_expected_pnl: float = 0.0
    total_actual_pnl: float = 0.0
    total_slippage_usd: float = 0.0
    total_priority_fees_usd: float = 0.0
    total_volume_usdt: float = 0.0

    @property
    def cex_asset(self) -> float:
        return self.cex_assets.get("SOL", 1.5)

    @cex_asset.setter
    def cex_asset(self, value: float) -> None:
        self.cex_assets["SOL"] = value

    @property
    def dex_asset(self) -> float:
        return self.dex_assets.get("SOL", 1.5)

    @dex_asset.setter
    def dex_asset(self, value: float) -> None:
        self.dex_assets["SOL"] = value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        # Handle backward compatibility mapping from cex_asset/dex_asset
        cex_asset_val = d.get("cex_asset", 1.5)
        dex_asset_val = d.get("dex_asset", 1.5)
        cex_assets = d.get("cex_assets") or {"SOL": cex_asset_val, "BTC": 0.005, "ETH": 0.05}
        dex_assets = d.get("dex_assets") or {"SOL": dex_asset_val, "BTC": 0.005, "ETH": 0.05}

        obj = cls(
            account_size=d.get("account_size", 500.0),
            cex_cash=d.get("cex_cash", 250.0),
            dex_cash=d.get("dex_cash", 250.0),
            cex_assets=cex_assets,
            dex_assets=dex_assets,
            total_pnl=d.get("total_pnl", 0.0),
            daily_pnl=d.get("daily_pnl", 0.0),
            daily_reset_date=d.get("daily_reset_date", ""),
            win_count=d.get("win_count", 0),
            loss_count=d.get("loss_count", 0),
            avg_win_usd=d.get("avg_win_usd", 0.0),
            avg_loss_usd=d.get("avg_loss_usd", 0.0),
            cycle_count=d.get("cycle_count", 0),
            route_stats=d.get("route_stats", {}),
            peak_account_size=d.get("peak_account_size", d.get("account_size", 500.0)),
            max_drawdown_paused=d.get("max_drawdown_paused", False),
            total_expected_pnl=d.get("total_expected_pnl", 0.0),
            total_actual_pnl=d.get("total_actual_pnl", 0.0),
            total_slippage_usd=d.get("total_slippage_usd", 0.0),
            total_priority_fees_usd=d.get("total_priority_fees_usd", 0.0),
            total_volume_usdt=d.get("total_volume_usdt", 0.0),
        )
        obj.closed_trades = d.get("closed_trades", [])
        if not obj.route_stats:
            obj.route_stats = {
                "DEX-BUY_CEX-SELL": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
                "CEX-BUY_DEX-SELL": {"win_count": 0, "loss_count": 0, "total_pnl": 0.0},
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
        return PortfolioState(
            account_size=settings.account_size,
            cex_cash=settings.account_size / 2.0,
            cex_assets={"SOL": 1.5, "BTC": 0.005, "ETH": 0.05},
            dex_cash=settings.account_size / 2.0,
            dex_assets={"SOL": 1.5, "BTC": 0.005, "ETH": 0.05}
        )


def save_state(state: PortfolioState) -> None:
    with _file_lock():
        with open(_STATE_FILE, "w") as f:
            json.dump(state.to_dict(), f, indent=2)


def get_status() -> dict:
    s = load_state()
    return {
        "account_size": s.account_size,
        "cex_cash": round(s.cex_cash, 2),
        "cex_asset": round(s.cex_asset, 4),
        "dex_cash": round(s.dex_cash, 2),
        "dex_asset": round(s.dex_asset, 4),
        "total_pnl": round(s.total_pnl, 2),
        "total_pnl_pct": round(s.total_pnl / s.account_size * 100, 2) if s.account_size else 0,
        "daily_pnl": round(s.daily_pnl, 2),
        "open_positions": 0,
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


def rebalance_portfolio(state: PortfolioState, asset: str, direction: str, amount: float) -> tuple[bool, str]:
    """
    Executes a transfer of assets between CEX and DEX wallets, simulating fees.
    Returns (success, message).
    """
    if amount <= 0:
        return False, "Amount must be positive."

    # Fee structures:
    # CEX to DEX (withdrawal fees on Bybit):
    # - SOL: 0.005 SOL
    # - USDT: 1.0 USDT
    # DEX to CEX (on-chain tx fees):
    # - SOL: 0.00005 SOL
    # - USDT: 0.00005 SOL (fees paid in SOL)

    if direction == "CEX_TO_DEX":
        if asset == "USDT":
            fee = 1.0
            if state.cex_cash < amount:
                return False, f"Insufficient CEX USDT. Have {state.cex_cash:.2f}, need {amount:.2f}."
            state.cex_cash -= amount
            state.dex_cash += (amount - fee)
            state.total_priority_fees_usd += fee
            
            # Re-value account size
            state.account_size = state.cex_cash + state.dex_cash + (state.cex_asset + state.dex_asset) * 140.0
            save_state(state)
            return True, f"Rebalanced {amount:.2f} USDT from CEX to DEX. Fee: {fee:.2f} USDT."
        elif asset == "SOL":
            fee = 0.005
            if state.cex_asset < amount:
                return False, f"Insufficient CEX SOL. Have {state.cex_asset:.4f}, need {amount:.4f}."
            state.cex_asset -= amount
            state.dex_asset += (amount - fee)
            fee_usd = fee * 140.0
            state.total_priority_fees_usd += fee_usd
            
            state.account_size = state.cex_cash + state.dex_cash + (state.cex_asset + state.dex_asset) * 140.0
            save_state(state)
            return True, f"Rebalanced {amount:.4f} SOL from CEX to DEX. Fee: {fee:.4f} SOL (~${fee_usd:.2f})."
        else:
            return False, f"Unsupported asset: {asset}."

    elif direction == "DEX_TO_CEX":
        sol_fee = 0.00005
        if state.dex_asset < sol_fee:
            return False, f"Insufficient DEX SOL to pay for transaction gas ({sol_fee:.5f} SOL required)."

        if asset == "USDT":
            if state.dex_cash < amount:
                return False, f"Insufficient DEX USDT. Have {state.dex_cash:.2f}, need {amount:.2f}."
            state.dex_cash -= amount
            state.cex_cash += amount
            state.dex_asset -= sol_fee
            fee_usd = sol_fee * 140.0
            state.total_priority_fees_usd += fee_usd
            
            state.account_size = state.cex_cash + state.dex_cash + (state.cex_asset + state.dex_asset) * 140.0
            save_state(state)
            return True, f"Rebalanced {amount:.2f} USDT from DEX to CEX. Fee: {sol_fee:.5f} SOL (~${fee_usd:.5f})."
        elif asset == "SOL":
            if state.dex_asset < amount + sol_fee:
                return False, f"Insufficient DEX SOL. Have {state.dex_asset:.4f}, need {amount + sol_fee:.4f} SOL (inc. fee)."
            state.dex_asset -= (amount + sol_fee)
            state.cex_asset += amount
            fee_usd = sol_fee * 140.0
            state.total_priority_fees_usd += fee_usd
            
            state.account_size = state.cex_cash + state.dex_cash + (state.cex_asset + state.dex_asset) * 140.0
            save_state(state)
            return True, f"Rebalanced {amount:.4f} SOL from DEX to CEX. Fee: {sol_fee:.5f} SOL (~${fee_usd:.5f})."
        else:
            return False, f"Unsupported asset: {asset}."
    else:
        return False, f"Unsupported direction: {direction}."

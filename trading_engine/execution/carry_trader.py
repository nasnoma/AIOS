"""
trading_engine/execution/carry_trader.py

Delta-Neutral Funding Carry Trader (Paper Mode)
- Opens spot + perp legs that are delta-neutral (net zero BTC exposure)
- Collects funding payments every 8h cycle
- Closes when funding inverts for N consecutive cycles
- Tracks basis P&L, funding collected, and total return

Position structures:
  1. short_perp:  long spot + short perp (same exchange)
     → Collects positive funding (longs pay shorts)
  2. long_perp:   short spot (borrow) + long perp
     → Collects negative funding (shorts pay longs)
  3. cross_basis: short perp on high-funding venue + long perp on low-funding venue
     → Collects the spread between the two funding rates

This is NOT directional trading. Price moves are hedged. The only P&L
source is funding + basis drift - fees.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger
import threading
from contextlib import contextmanager

from trading_engine.config import settings
from trading_engine.alerts.telegram_bot import send_message

STATE_FILE = Path(__file__).parent.parent / "carry_state.json"
LOCK_FILE = Path(__file__).parent.parent / "carry_state.lock"
_lock_state = threading.local()

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


@contextmanager
def state_lock():
    if not hasattr(_lock_state, "depth"):
        _lock_state.depth = 0
        _lock_state.fd = None
    if _lock_state.depth == 0 and HAS_FCNTL:
        if not LOCK_FILE.exists():
            LOCK_FILE.touch()
        _lock_state.fd = open(LOCK_FILE, "r+")
        fcntl.flock(_lock_state.fd.fileno(), fcntl.LOCK_EX)
    _lock_state.depth += 1
    try:
        yield
    finally:
        _lock_state.depth -= 1
        if _lock_state.depth == 0 and HAS_FCNTL and _lock_state.fd:
            fcntl.flock(_lock_state.fd.fileno(), fcntl.LOCK_UN)
            _lock_state.fd.close()
            _lock_state.fd = None


def locked(func):
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        with state_lock():
            return func(*args, **kwargs)
    return wrapper


# Round-trip fees: spot taker 0.1%, perp taker 0.06% (entry + exit)
SPOT_FEE_RATE = 0.001   # 0.1% per side
PERP_FEE_RATE = 0.0006  # 0.06% per side


@dataclass
class CarryPosition:
    symbol: str               # e.g. BTC/USDT
    structure: str            # 'short_perp' | 'long_perp' | 'cross_basis'
    venue_short: str          # exchange where we short perp
    venue_long: str           # exchange where we long spot or perp
    size_usd: float           # total position notional per leg
    entry_spot_price: float   # spot price at entry (0 for cross_basis)
    entry_perp_price_short: float
    entry_perp_price_long: float
    opened_at: str
    status: str = "open"      # 'open' | 'closed'
    closed_at: Optional[str] = None
    funding_collected: float = 0.0
    basis_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_cycles_collected: int = 0
    consecutive_inversions: int = 0
    exit_reason: str = ""
    exit_price_spot: Optional[float] = None
    exit_price_perp_short: Optional[float] = None
    exit_price_perp_long: Optional[float] = None
    net_pnl: float = 0.0


@dataclass
class CarryPortfolio:
    account_size: float = field(default_factory=lambda: settings.account_size)
    cash: float = field(default_factory=lambda: settings.account_size)
    positions: list[CarryPosition] = field(default_factory=list)
    closed_trades: list[CarryPosition] = field(default_factory=list)
    total_funding_collected: float = 0.0
    total_basis_pnl: float = 0.0
    total_fees: float = 0.0

    def to_dict(self):
        return {
            "account_size": self.account_size,
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions],
            "closed_trades": [asdict(p) for p in self.closed_trades],
            "total_funding_collected": self.total_funding_collected,
            "total_basis_pnl": self.total_basis_pnl,
            "total_fees": self.total_fees,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CarryPortfolio":
        p = cls(
            account_size=d.get("account_size", settings.account_size),
            cash=d.get("cash", settings.account_size),
            total_funding_collected=d.get("total_funding_collected", 0.0),
            total_basis_pnl=d.get("total_basis_pnl", 0.0),
            total_fees=d.get("total_fees", 0.0),
        )
        p.positions = [CarryPosition(**pos) for pos in d.get("positions", [])]
        p.closed_trades = [CarryPosition(**pos) for pos in d.get("closed_trades", [])]
        return p


def _load_state() -> CarryPortfolio:
    with state_lock():
        if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
            try:
                with open(STATE_FILE) as f:
                    return CarryPortfolio.from_dict(json.load(f))
            except Exception as e:
                logger.error(f"CRITICAL: Could not parse carry state: {e}")
                raise RuntimeError(f"Failed to load carry state: {e}") from e
        return CarryPortfolio()


def _save_state(portfolio: CarryPortfolio):
    with state_lock():
        with open(STATE_FILE, "w") as f:
            json.dump(portfolio.to_dict(), f, indent=2)


@locked
def open_carry_position(
    symbol: str,
    structure: str,
    venue_short: str,
    venue_long: str,
    size_usd: float,
    spot_price: float,
    perp_price_short: float,
    perp_price_long: float,
) -> Optional[CarryPosition]:
    """
    Open a delta-neutral carry position.
    For 'short_perp': long spot + short perp → collect positive funding
    For 'long_perp':  short spot (borrow) + long perp → collect negative funding
    For 'cross_basis': short perp (high-funding venue) + long perp (low-funding venue)
    """
    portfolio = _load_state()

    # Prevent duplicate positions on same symbol
    if any(p.symbol == symbol and p.status == "open" for p in portfolio.positions):
        logger.info(f"⏭️ Carry: {symbol} already has open position. Skipping.")
        return None

    max_positions = int(getattr(settings, "carry_max_positions", 3))
    if len([p for p in portfolio.positions if p.status == "open"]) >= max_positions:
        logger.warning(f"Carry: max positions ({max_positions}) reached.")
        return None

    # Calculate fees
    # short_perp: spot entry fee + perp entry fee
    # long_perp: spot entry fee (borrow) + perp entry fee
    # cross_basis: 2 perp entry fees
    if structure == "cross_basis":
        entry_fee = size_usd * PERP_FEE_RATE * 2  # two perp legs
    else:
        entry_fee = size_usd * SPOT_FEE_RATE + size_usd * PERP_FEE_RATE

    if portfolio.cash < size_usd + entry_fee:
        logger.warning(f"Carry: insufficient cash (${portfolio.cash:.2f} < ${size_usd + entry_fee:.2f})")
        return None

    portfolio.cash -= (size_usd + entry_fee)
    portfolio.total_fees += entry_fee

    pos = CarryPosition(
        symbol=symbol,
        structure=structure,
        venue_short=venue_short,
        venue_long=venue_long,
        size_usd=size_usd,
        entry_spot_price=spot_price,
        entry_perp_price_short=perp_price_short,
        entry_perp_price_long=perp_price_long,
        opened_at=datetime.now(timezone.utc).isoformat(),
        fees_paid=entry_fee,
    )
    portfolio.positions.append(pos)
    _save_state(portfolio)

    logger.success(
        f"📝 CARRY {structure.upper()} opened: {symbol} | "
        f"Size=${size_usd:,.0f} | Short={venue_short} @ {perp_price_short:.4f} | "
        f"Long={venue_long} @ {perp_price_long if perp_price_long else spot_price:.4f} | "
        f"Fees=${entry_fee:.2f}"
    )

    send_message(
        f"🟢 <b>CARRY {structure.upper()} Opened</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: {symbol}\n"
        f"💵 Size: ${size_usd:,.2f}\n"
        f"⬇️ Short: {venue_short} @ <code>{perp_price_short:.4f}</code>\n"
        f"⬆️ Long: {venue_long} @ <code>{(perp_price_long or spot_price):.4f}</code>\n"
        f"💸 Entry Fees: ${entry_fee:.2f}"
    )
    return pos


@locked
def update_funding_and_manage(current_prices: dict[str, dict]):
    """
    Check funding cycles, collect funding, and manage open positions.
    Called by scheduler every cycle (e.g. every 8h aligned to funding windows).

    current_prices: {symbol: {"spot": float, "perp_short": float, "perp_long": float,
                              "funding_short": float, "funding_long": float}}
    """
    portfolio = _load_state()
    max_inversions = int(getattr(settings, "carry_max_inversions", 2))
    max_hold_cycles = int(getattr(settings, "carry_max_hold_cycles", 90))  # ~30 days

    for pos in list(portfolio.positions):
        if pos.status != "open":
            continue

        prices = current_prices.get(pos.symbol)
        if not prices:
            continue

        # ── Collect funding every cycle ────────────────────────────────
        # Funding is paid on the perp notional. Positive funding = longs pay shorts.
        # For short_perp: we are short perp → collect when funding > 0
        # For long_perp: we are long perp → collect when funding < 0
        # For cross_basis: collect (short_funding - long_funding) on notional
        funding_short = prices.get("funding_short", 0.0)
        funding_long = prices.get("funding_long", 0.0)

        if pos.structure == "short_perp":
            cycle_funding = funding_short * pos.size_usd  # short collects positive funding
            if funding_short < 0:
                pos.consecutive_inversions += 1
            else:
                pos.consecutive_inversions = 0
        elif pos.structure == "long_perp":
            cycle_funding = -funding_long * pos.size_usd  # long collects negative funding
            if funding_long > 0:
                pos.consecutive_inversions += 1
            else:
                pos.consecutive_inversions = 0
        else:  # cross_basis
            cycle_funding = (funding_short - funding_long) * pos.size_usd
            if cycle_funding < 0:
                pos.consecutive_inversions += 1
            else:
                pos.consecutive_inversions = 0

        pos.funding_collected += cycle_funding
        pos.funding_cycles_collected += 1
        portfolio.total_funding_collected += cycle_funding

        # ── Calculate basis P&L (mark-to-market) ───────────────────────
        cur_perp_short = prices.get("perp_short", pos.entry_perp_price_short)
        cur_perp_long = prices.get("perp_long", pos.entry_perp_price_long)
        cur_spot = prices.get("spot", pos.entry_spot_price)

        if pos.structure == "short_perp":
            # Long spot + short perp: P&L from spot + perp + funding
            spot_pnl = (cur_spot - pos.entry_spot_price) / pos.entry_spot_price * pos.size_usd
            perp_pnl = (pos.entry_perp_price_short - cur_perp_short) / pos.entry_perp_price_short * pos.size_usd
            pos.basis_pnl = spot_pnl + perp_pnl
        elif pos.structure == "long_perp":
            # Short spot (borrow) + long perp
            spot_pnl = (pos.entry_spot_price - cur_spot) / pos.entry_spot_price * pos.size_usd
            perp_pnl = (cur_perp_long - pos.entry_perp_price_long) / pos.entry_perp_price_long * pos.size_usd
            pos.basis_pnl = spot_pnl + perp_pnl
        else:  # cross_basis
            # Short perp A + long perp B
            perp_short_pnl = (pos.entry_perp_price_short - cur_perp_short) / pos.entry_perp_price_short * pos.size_usd
            perp_long_pnl = (cur_perp_long - pos.entry_perp_price_long) / pos.entry_perp_price_long * pos.size_usd
            pos.basis_pnl = perp_short_pnl + perp_long_pnl

        # ── Exit conditions ─────────────────────────────────────────────
        should_close = False
        reason = ""

        # 1. Funding inversion: N consecutive cycles of adverse funding
        if pos.consecutive_inversions >= max_inversions:
            should_close = True
            reason = f"Funding inverted {pos.consecutive_inversions} consecutive cycles"

        # 2. Max hold time exceeded
        if pos.funding_cycles_collected >= max_hold_cycles:
            should_close = True
            reason = f"Max hold cycles ({max_hold_cycles}) exceeded"

        # 3. Basis loss exceeds funding collected (stop loss on basis)
        max_basis_loss = float(getattr(settings, "carry_max_basis_loss_pct", 0.05))  # 5% of size
        if pos.basis_pnl < -max_basis_loss * pos.size_usd:
            should_close = True
            reason = f"Basis loss ${pos.basis_pnl:.2f} exceeds ${max_basis_loss * pos.size_usd:.2f} limit"

        # 4. Margin alert (simplified: basis loss > 50% of position)
        if pos.basis_pnl < -0.50 * pos.size_usd:
            should_close = True
            reason = f"CRITICAL: basis loss > 50% of position — margin call risk"

        if should_close:
            _close_carry_position(portfolio, pos, prices, reason)
            continue

        logger.info(
            f"💰 CARRY {pos.symbol}: cycle #{pos.funding_cycles_collected} | "
            f"funding=${cycle_funding:+.2f} (total ${pos.funding_collected:.2f}) | "
            f"basis=${pos.basis_pnl:+.2f} | inversions={pos.consecutive_inversions}"
        )

    _save_state(portfolio)


def _close_carry_position(portfolio: CarryPortfolio, pos: CarryPosition, prices: dict, reason: str):
    """Close a carry position and realize P&L."""
    cur_spot = prices.get("spot", pos.entry_spot_price)
    cur_perp_short = prices.get("perp_short", pos.entry_perp_price_short)
    cur_perp_long = prices.get("perp_long", pos.entry_perp_price_long)

    # Exit fees
    if pos.structure == "cross_basis":
        exit_fee = pos.size_usd * PERP_FEE_RATE * 2
    else:
        exit_fee = pos.size_usd * SPOT_FEE_RATE + pos.size_usd * PERP_FEE_RATE

    portfolio.total_fees += exit_fee
    pos.fees_paid += exit_fee

    # Net P&L = funding collected + basis P&L - all fees
    pos.net_pnl = pos.funding_collected + pos.basis_pnl - pos.fees_paid
    pos.exit_price_spot = cur_spot
    pos.exit_price_perp_short = cur_perp_short
    pos.exit_price_perp_long = cur_perp_long
    pos.closed_at = datetime.now(timezone.utc).isoformat()
    pos.status = "closed"
    pos.exit_reason = reason

    # Return capital + net P&L
    portfolio.cash += pos.size_usd + pos.net_pnl
    portfolio.total_basis_pnl += pos.basis_pnl
    portfolio.positions.remove(pos)
    portfolio.closed_trades.append(pos)

    emoji = "✅" if pos.net_pnl > 0 else "❌"
    logger.info(
        f"{emoji} CARRY CLOSED: {pos.symbol} | {reason} | "
        f"Funding=${pos.funding_collected:.2f} | Basis=${pos.basis_pnl:+.2f} | "
        f"Fees=${pos.fees_paid:.2f} | Net=${pos.net_pnl:+.2f} | "
        f"Cycles={pos.funding_cycles_collected}"
    )

    send_message(
        f"{emoji} <b>CARRY Position Closed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: {pos.symbol}\n"
        f"📊 Structure: {pos.structure}\n"
        f"📝 Reason: {reason}\n"
        f"💰 Funding Collected: ${pos.funding_collected:.2f}\n"
        f"📈 Basis P&L: ${pos.basis_pnl:+.2f}\n"
        f"💸 Fees: ${pos.fees_paid:.2f}\n"
        f"💵 Net P&L: <b>${pos.net_pnl:+,.2f}</b>\n"
        f"🔄 Cycles: {pos.funding_cycles_collected}"
    )


@locked
def get_status() -> dict:
    """Return current carry portfolio status for dashboard/API."""
    portfolio = _load_state()
    open_pos = [asdict(p) for p in portfolio.positions if p.status == "open"]
    closed = [asdict(p) for p in portfolio.closed_trades[-20:]]

    total_funding = sum(p.funding_collected for p in portfolio.closed_trades)
    total_basis = sum(p.basis_pnl for p in portfolio.closed_trades)
    total_fees = portfolio.total_fees
    total_pnl = total_funding + total_basis - total_fees

    wins = sum(1 for p in portfolio.closed_trades if p.net_pnl > 0)
    losses = sum(1 for p in portfolio.closed_trades if p.net_pnl <= 0)
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0.0

    return {
        "account_size": portfolio.account_size,
        "cash": round(portfolio.cash, 2),
        "open_positions": len(open_pos),
        "open_carry": open_pos,
        "closed_trades": closed,
        "total_funding_collected": round(portfolio.total_funding_collected, 2),
        "total_basis_pnl": round(portfolio.total_basis_pnl, 2),
        "total_fees": round(portfolio.total_fees, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / portfolio.account_size * 100, 2) if portfolio.account_size > 0 else 0,
        "win_rate": round(win_rate, 1),
        "win_count": wins,
        "loss_count": losses,
    }

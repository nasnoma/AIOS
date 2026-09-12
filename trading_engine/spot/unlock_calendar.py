"""
Small unlock calendar for ARB and TIA only.

Around known vesting/unlock dates, new buys are paused (sell-only / resting TPs kept).
Unlock ≠ guaranteed dump, but supply overhang raises knife risk for the Spot grid.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

# Buy-pause window: pad days before and after each unlock date (UTC calendar day)
UNLOCK_PAD_DAYS = 4

# Hard floor: no ARB buys until this instant (covers mid/late-Sep 2026 unlock cluster)
ARB_HARD_PAUSE_UNTIL_UTC = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)

# Explicit unlock dates (UTC). ARB: mid-month + 23rd cluster through early 2027.
# TIA: month-end style vesting events (Tokenomics / DefiLlama-style calendars).
_EXPLICIT_UNLOCKS: List[Tuple[str, date]] = [
    # ARB — Sep 2026 cluster (trackers cite ~16 and/or ~23)
    ("ARB/USDT", date(2026, 9, 16)),
    ("ARB/USDT", date(2026, 9, 23)),
    # ARB — remaining monthly-ish team/investor vest window (~through Mar 2027)
    ("ARB/USDT", date(2026, 10, 16)),
    ("ARB/USDT", date(2026, 10, 23)),
    ("ARB/USDT", date(2026, 11, 16)),
    ("ARB/USDT", date(2026, 11, 23)),
    ("ARB/USDT", date(2026, 12, 16)),
    ("ARB/USDT", date(2026, 12, 23)),
    ("ARB/USDT", date(2027, 1, 16)),
    ("ARB/USDT", date(2027, 1, 23)),
    ("ARB/USDT", date(2027, 2, 16)),
    ("ARB/USDT", date(2027, 2, 23)),
    ("ARB/USDT", date(2027, 3, 16)),
    ("ARB/USDT", date(2027, 3, 23)),
    # TIA — month-end unlocks (smaller overhang than ARB; still pause buys near event)
    ("TIA/USDT", date(2026, 9, 30)),
    ("TIA/USDT", date(2026, 10, 31)),
    ("TIA/USDT", date(2026, 11, 30)),
    ("TIA/USDT", date(2026, 12, 31)),
    ("TIA/USDT", date(2027, 1, 31)),
    ("TIA/USDT", date(2027, 2, 28)),
    ("TIA/USDT", date(2027, 3, 31)),
    ("TIA/USDT", date(2027, 4, 30)),
    ("TIA/USDT", date(2027, 5, 31)),
    ("TIA/USDT", date(2027, 6, 30)),
    ("TIA/USDT", date(2027, 7, 31)),
    ("TIA/USDT", date(2027, 8, 31)),
    ("TIA/USDT", date(2027, 9, 30)),
]


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s in ("ARB", "TIA"):
        return f"{s}/USDT"
    return s


def iter_unlock_dates(symbol: Optional[str] = None) -> Iterable[Tuple[str, date]]:
    want = _norm_symbol(symbol) if symbol else None
    for sym, d in _EXPLICIT_UNLOCKS:
        if want is None or sym == want:
            yield sym, d


def upcoming_unlocks(within_days: int = 45, now: Optional[datetime] = None) -> List[Tuple[str, date, int]]:
    """Return (symbol, unlock_date, days_until) for events within the window."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out = []
    for sym, d in _EXPLICIT_UNLOCKS:
        delta = (d - today).days
        if -UNLOCK_PAD_DAYS <= delta <= within_days:
            out.append((sym, d, delta))
    out.sort(key=lambda x: (x[1], x[0]))
    return out


def is_unlock_buy_paused(
    symbol: str,
    now: Optional[datetime] = None,
    pad_days: int = UNLOCK_PAD_DAYS,
) -> Tuple[bool, str]:
    """
    True if new buys should be paused for this symbol due to unlock calendar / hard ARB floor.
    Only ARB and TIA are covered. Returns (paused, reason).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sym = _norm_symbol(symbol)
    if sym not in ("ARB/USDT", "TIA/USDT"):
        return False, ""

    if sym == "ARB/USDT" and now < ARB_HARD_PAUSE_UNTIL_UTC:
        return True, (
            f"ARB hard buy-pause until {ARB_HARD_PAUSE_UNTIL_UTC.date().isoformat()} UTC "
            f"(mid/late-Sep unlock window)"
        )

    today = now.date()
    pad = max(0, int(pad_days))
    for s, d in iter_unlock_dates(sym):
        if abs((d - today).days) <= pad:
            return True, (
                f"{s} unlock buy-pause (±{pad}d around {d.isoformat()} UTC)"
            )
    return False, ""

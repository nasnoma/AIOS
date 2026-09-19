"""
Unlock calendar — TIA vesting pads + temporary ARB hard buy-pause through unlock aftershock.

- TIA: ±UNLOCK_PAD_DAYS around listed month-end unlocks (sells kept).
- ARB: hard buy-only pause until ARB_HARD_PAUSE_UNTIL_UTC (sells kept).
  Covers Sep 23 2026 unlock day + short aftershock; resumes 2026-09-28 00:00 UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Tuple

# Buy-pause window: pad days before and after each unlock date (UTC calendar day)
UNLOCK_PAD_DAYS = 4

# ARB Sep 23 unlock: buy-only pause through end of Sep 27 UTC (resume Sep 28 00:00 UTC).
# Sells / resting TPs stay active. User-approved 2026-09-19.
ARB_HARD_PAUSE_UNTIL_UTC = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
# User uncomfortable holding through unlock: force resting sells to this pin (fee-proof vs cost still enforced).
# Cleared automatically when bag is flat / pause expires — or set to 0 to disable.
ARB_PINNED_SELL_PX = 0.2121  # near-market exit lock-in (~+14.5% vs ~0.1852 cost); buys still paused

# Explicit unlock dates (UTC). TIA month-end vesting; ARB listed for visibility (hard floor above is authoritative until it expires).
_EXPLICIT_UNLOCKS: List[Tuple[str, date]] = [
    ("ARB/USDT", date(2026, 9, 23)),  # informational; hard pause covers through Sep 27
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
    True if new buys should be paused for unlock risk.
    ARB: hard buy-pause until ARB_HARD_PAUSE_UNTIL_UTC (sells unaffected).
    TIA: ±pad around listed unlock dates only.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sym = _norm_symbol(symbol)
    if sym not in ("ARB/USDT", "TIA/USDT"):
        return False, ""

    if sym == "ARB/USDT":
        if now < ARB_HARD_PAUSE_UNTIL_UTC:
            return True, (
                f"ARB hard buy-pause until {ARB_HARD_PAUSE_UNTIL_UTC.date().isoformat()} UTC "
                f"(Sep 23 unlock + aftershock through Sep 27; sells stay)"
            )
        return False, ""

    today = now.date()
    pad = max(0, int(pad_days))
    for s, d in iter_unlock_dates(sym):
        if abs((d - today).days) <= pad:
            return True, (
                f"{s} unlock buy-pause (±{pad}d around {d.isoformat()} UTC)"
            )
    return False, ""

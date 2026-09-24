"""
Unlock calendar — TIA vesting pads + temporary ARB/TIA hard buy-pauses through unlock risk.

- TIA: ±UNLOCK_PAD_DAYS around listed month-end unlocks (sells kept), plus optional
  early hard buy-pause until TIA_HARD_PAUSE_UNTIL_UTC (bridges into the pad window).
- ARB: hard buy-only pause until ARB_HARD_PAUSE_UNTIL_UTC (sells kept).
  Covers Sep 23 2026 unlock day + short aftershock; resumes 2026-09-28 00:00 UTC.

Hard-pause allowlist is ARB/TIA only (Nasir-approved pattern). Additional coins may be
listed as advisory/shadow entries (enabled=False / hard_pause=False by default) for
log + status coverage — never auto-enable hard buy-pause without explicit allowlist.

See: spot/docs/UNLOCK_AUDIT.md and scripts/audit_unlock_coverage.py
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

# Buy-pause window: pad days before and after each unlock date (UTC calendar day)
UNLOCK_PAD_DAYS = 4

# ARB Sep 23 unlock: buy-only pause through end of Sep 27 UTC (resume Sep 28 00:00 UTC).
# Sells / resting TPs stay active. User-approved 2026-09-19.
ARB_HARD_PAUSE_UNTIL_UTC = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
# User uncomfortable holding through unlock: force resting sells to this pin (fee-proof vs cost still enforced).
# Cleared automatically when bag is flat / pause expires — or set to 0 to disable.
ARB_PINNED_SELL_PX = 0.0  # DISABLED — prior 0.2121 undercut FIFO max lots (~0.213); never pin below fee-proof(fifo_max)

# TIA early buy-only pause until normal ±4d pad around Sep 30 unlock begins (resume Sep 26 00:00 UTC).
# Nasir 2026-09-22 early unlock buy-pause until calendar window. Sells / resting TPs stay active.
# On/after 2026-09-26 the ±UNLOCK_PAD_DAYS window around 2026-09-30 takes over continuously.
TIA_HARD_PAUSE_UNTIL_UTC = datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc)

# Only these symbols may hard-pause buys via unlock calendar (Nasir allowlist pattern).
HARD_PAUSE_ALLOWLIST = frozenset({"ARB/USDT", "TIA/USDT"})

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


@dataclass(frozen=True)
class AdvisoryUnlock:
    """Shadow/log calendar row. hard_pause stays False unless Nasir approves + allowlist."""

    symbol: str
    unlock_date: date
    source: str = "unverified"
    note: str = ""
    hard_pause: bool = False  # MUST stay False until allowlisted + Nasir confirm
    enabled: bool = False  # False = shadow only (status/audit); True = advisory soft-log


# Advisory / shadow unlocks for roster coverage. Dates left empty of invented claims —
# add well-sourced rows with enabled=True (soft log) and hard_pause=False until Nasir approves.
# Example shape (DO NOT enable hard pause without parent):
#   AdvisoryUnlock("OP/USDT", date(2026, 10, 15), source="token.unlock", note="...", enabled=True, hard_pause=False)
_ADVISORY_UNLOCKS: List[AdvisoryUnlock] = [
    # Intentionally empty of hard dates — audit script lists coverage gaps for Nasir to fill.
]


_last_unlock_watch_log_ts: float = 0.0
_UNLOCK_WATCH_COOLDOWN_SEC = float(os.environ.get("SPOT_UNLOCK_WATCH_COOLDOWN_SEC", "3600"))


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s in ("ARB", "TIA"):
        return f"{s}/USDT"
    if s and "/" not in s and not s.endswith("USDT"):
        return f"{s}/USDT"
    if s and "/" not in s and s.endswith("USDT") and not s.endswith("/USDT"):
        return s.replace("USDT", "/USDT")
    return s


def iter_unlock_dates(symbol: Optional[str] = None) -> Iterable[Tuple[str, date]]:
    want = _norm_symbol(symbol) if symbol else None
    for sym, d in _EXPLICIT_UNLOCKS:
        if want is None or sym == want:
            yield sym, d


def iter_advisory_unlocks(symbol: Optional[str] = None) -> Iterable[AdvisoryUnlock]:
    want = _norm_symbol(symbol) if symbol else None
    for row in _ADVISORY_UNLOCKS:
        sym = _norm_symbol(row.symbol)
        if want is None or sym == want:
            yield row


def covered_symbols() -> List[str]:
    """Symbols with any explicit (hard/calendar) unlock row."""
    return sorted({sym for sym, _ in _EXPLICIT_UNLOCKS})


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


def upcoming_advisory(
    within_days: int = 45,
    now: Optional[datetime] = None,
    include_disabled: bool = True,
) -> List[Dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out: List[Dict[str, Any]] = []
    for row in _ADVISORY_UNLOCKS:
        if not include_disabled and not row.enabled:
            continue
        delta = (row.unlock_date - today).days
        if -UNLOCK_PAD_DAYS <= delta <= within_days:
            out.append(
                {
                    "symbol": _norm_symbol(row.symbol),
                    "unlock_date": row.unlock_date.isoformat(),
                    "days_until": delta,
                    "source": row.source,
                    "note": row.note,
                    "hard_pause": bool(row.hard_pause),
                    "enabled": bool(row.enabled),
                    "mode": "shadow" if not row.enabled else ("hard" if row.hard_pause else "soft_log"),
                }
            )
    out.sort(key=lambda x: (x["unlock_date"], x["symbol"]))
    return out


def is_unlock_buy_paused(
    symbol: str,
    now: Optional[datetime] = None,
    pad_days: int = UNLOCK_PAD_DAYS,
) -> Tuple[bool, str]:
    """
    True if new buys should be paused for unlock risk.
    ARB: hard buy-pause until ARB_HARD_PAUSE_UNTIL_UTC (sells unaffected).
    TIA: hard buy-pause until TIA_HARD_PAUSE_UNTIL_UTC, then ±pad around unlocks
         (sells unaffected). Early hard floor bridges into the Sep 30 pad window.
    Other symbols: never hard-paused here (advisory only). Optional env
    SPOT_UNLOCK_HARD_EXTRA=SYM1,SYM2 is ignored unless also in HARD_PAUSE_ALLOWLIST —
    safer default: require code allowlist edit + Nasir confirm.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sym = _norm_symbol(symbol)
    if sym not in HARD_PAUSE_ALLOWLIST:
        # Advisory hard_pause rows still require allowlist membership — refuse silently.
        return False, ""

    if sym == "ARB/USDT":
        if now < ARB_HARD_PAUSE_UNTIL_UTC:
            return True, (
                f"ARB hard buy-pause until {ARB_HARD_PAUSE_UNTIL_UTC.date().isoformat()} UTC "
                f"(Sep 23 unlock + aftershock through Sep 27; sells stay)"
            )
        return False, ""

    # TIA early hard floor (Nasir 2026-09-22) then normal ±pad around unlock dates
    if now < TIA_HARD_PAUSE_UNTIL_UTC:
        return True, (
            f"TIA hard buy-pause until {TIA_HARD_PAUSE_UNTIL_UTC.date().isoformat()} UTC "
            f"(Nasir 2026-09-22 early unlock buy-pause until calendar window; sells stay)"
        )

    today = now.date()
    pad = max(0, int(pad_days))
    for s, d in iter_unlock_dates(sym):
        if abs((d - today).days) <= pad:
            return True, (
                f"{s} unlock buy-pause (±{pad}d around {d.isoformat()} UTC)"
            )
    return False, ""


def active_unlock_pauses(
    symbols: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Lightweight list of symbols currently hard-paused for unlock (for /api/spot/status)."""
    now = now or datetime.now(timezone.utc)
    syms = list(symbols) if symbols else list(HARD_PAUSE_ALLOWLIST)
    out: List[Dict[str, Any]] = []
    for raw in syms:
        sym = _norm_symbol(raw)
        paused, reason = is_unlock_buy_paused(sym, now=now)
        if paused:
            out.append({"symbol": sym, "reason": reason, "hard": True})
    return out


def audit_unlock_coverage(
    roster: Sequence[str],
    within_days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compare live/Top-8 roster vs unlock calendar coverage.
    Returns covered, gaps, next_30d from existing calendar sources only
    (explicit + advisory rows in this module — no external fetch).
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    roster_norm = sorted({_norm_symbol(s) for s in roster if s})
    covered = set(covered_symbols())
    advisory_syms = {_norm_symbol(r.symbol) for r in _ADVISORY_UNLOCKS}
    gaps = [s for s in roster_norm if s not in covered and s not in advisory_syms]
    soft_only = [s for s in roster_norm if s in advisory_syms and s not in covered]

    next_30d = []
    for sym, d, delta in upcoming_unlocks(within_days=within_days, now=now):
        next_30d.append(
            {
                "symbol": sym,
                "unlock_date": d.isoformat(),
                "days_until": delta,
                "kind": "explicit",
                "hard_pause_eligible": sym in HARD_PAUSE_ALLOWLIST,
                "currently_paused": is_unlock_buy_paused(sym, now=now)[0],
            }
        )
    for row in upcoming_advisory(within_days=within_days, now=now, include_disabled=True):
        next_30d.append({**row, "kind": "advisory"})

    next_30d.sort(key=lambda x: (x.get("unlock_date") or "", x.get("symbol") or ""))

    return {
        "as_of_utc": now.isoformat(),
        "today_utc": today.isoformat(),
        "roster": roster_norm,
        "covered_explicit": sorted(covered),
        "advisory_symbols": sorted(advisory_syms),
        "gaps": gaps,
        "soft_only": soft_only,
        "hard_pause_allowlist": sorted(HARD_PAUSE_ALLOWLIST),
        "next_30d": next_30d,
        "active_hard_pauses": active_unlock_pauses(roster_norm or list(HARD_PAUSE_ALLOWLIST), now=now),
        "note": (
            "Gaps have no calendar row in-repo. Do NOT invent unlock dates. "
            "Add AdvisoryUnlock(..., enabled=True, hard_pause=False) for soft log; "
            "hard pause only after Nasir confirm + HARD_PAUSE_ALLOWLIST edit."
        ),
    }


def maybe_log_unlock_watch(
    roster: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> None:
    """Rate-limited soft log of upcoming unlocks + coverage gaps for live roster."""
    global _last_unlock_watch_log_ts
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp() if hasattr(now_dt, "timestamp") else time.time()
    if (now_ts - _last_unlock_watch_log_ts) < _UNLOCK_WATCH_COOLDOWN_SEC:
        return
    try:
        roster_list = list(roster) if roster else list(HARD_PAUSE_ALLOWLIST)
        audit = audit_unlock_coverage(roster_list, within_days=30, now=now_dt)
        _last_unlock_watch_log_ts = now_ts
        pauses = audit.get("active_hard_pauses") or []
        next_rows = audit.get("next_30d") or []
        gaps = audit.get("gaps") or []
        if pauses or next_rows:
            next_lbl = [
                "%s@%s" % (r.get("symbol"), r.get("unlock_date"))
                for r in next_rows[:6]
            ]
            logger.info(
                f"[UNLOCK WATCH] active_hard={len(pauses)} next_30d={len(next_rows)} "
                f"gaps={len(gaps)} pauses={[p.get('symbol') for p in pauses]} "
                f"next={next_lbl}"
            )
        if gaps:
            # Soft coverage gap log (no pause) — once per cooldown
            sample = gaps[:12]
            logger.info(
                f"[UNLOCK WATCH] coverage gaps (no calendar row): {sample}"
                + ("…" if len(gaps) > 12 else "")
            )
        # Soft-log enabled advisory rows inside pad window (never hard-pause here)
        for row in _ADVISORY_UNLOCKS:
            if not row.enabled:
                continue
            sym = _norm_symbol(row.symbol)
            delta = (row.unlock_date - now_dt.date()).days
            if abs(delta) <= UNLOCK_PAD_DAYS:
                mode = "HARD(needs allowlist)" if row.hard_pause else "soft_log"
                logger.info(
                    f"[UNLOCK ADVISORY] {sym} unlock {row.unlock_date.isoformat()} "
                    f"(Δ{delta}d) mode={mode} source={row.source} — buys NOT auto-paused"
                )
    except Exception as e:
        logger.debug(f"maybe_log_unlock_watch: {e}")


def unlock_status_summary(
    roster: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compact unlock block for /api/spot/status."""
    now = now or datetime.now(timezone.utc)
    roster_list = list(roster) if roster else list(HARD_PAUSE_ALLOWLIST)
    pauses = active_unlock_pauses(roster_list, now=now)
    return {
        "active_hard_pauses": pauses,
        "pad_days": UNLOCK_PAD_DAYS,
        "hard_pause_allowlist": sorted(HARD_PAUSE_ALLOWLIST),
        "upcoming_30d": [
            {"symbol": s, "unlock_date": d.isoformat(), "days_until": delta}
            for s, d, delta in upcoming_unlocks(30, now=now)
        ],
        "advisory_upcoming_30d": upcoming_advisory(30, now=now, include_disabled=True),
        "arb_hard_until_utc": ARB_HARD_PAUSE_UNTIL_UTC.isoformat(),
        "tia_hard_until_utc": TIA_HARD_PAUSE_UNTIL_UTC.isoformat(),
    }

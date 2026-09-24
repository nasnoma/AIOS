"""
Spot soft alerts — log-only watches (never pause buys).

Nasir 2026-09-24:
  1) Reserve near-floor: free USDT ≈ reserve floor (within ~2% or +small epsilon)
  2) slow_bleed_watch: BTC ≤ −8%/72h OR equity DD from 72h peak ≤ −10%,
     AND cascade latch is NOT active — advisory only (no book-wide pause)

Rate-limited INFO logs + status fields. No Slack spam; no buy cancel.
"""
from __future__ import annotations

import os
import threading
from threading import RLock
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from loguru import logger

# ── Tunables (conservative start) ──
RESERVE_NEAR_PCT = float(os.environ.get("SPOT_SOFT_RESERVE_NEAR_PCT", "0.02"))  # 2%
RESERVE_NEAR_EPS_USD = float(os.environ.get("SPOT_SOFT_RESERVE_EPS_USD", "25.0"))
BTC_SLOW_BLEED_72H_PCT = float(os.environ.get("SPOT_SOFT_BTC_72H_PCT", "-8.0"))
EQUITY_SLOW_BLEED_DD = float(os.environ.get("SPOT_SOFT_EQUITY_DD_72H", "0.10"))  # 10%
SLOW_BLEED_WINDOW_SEC = float(os.environ.get("SPOT_SOFT_BLEED_WINDOW_SEC", str(72 * 3600)))
ALERT_COOLDOWN_SEC = float(os.environ.get("SPOT_SOFT_ALERT_COOLDOWN_SEC", "1800"))  # 30m
_EQUITY_SAMPLE_MAX = 512


@dataclass
class SoftAlertState:
    last_usdt_free: float = 0.0
    last_usdt_reserved: float = 0.0
    last_equity: float = 0.0
    last_btc_72h_pct: float = 0.0
    equity_peak_72h: float = 0.0
    equity_dd_72h: float = 0.0
    reserve_near_floor: bool = False
    slow_bleed_watch: bool = False
    last_reasons: Dict[str, str] = field(default_factory=dict)
    last_fired_ts: Dict[str, float] = field(default_factory=dict)
    fire_counts: Dict[str, int] = field(default_factory=dict)
    # (ts, equity) samples for 72h peak
    _equity_samples: Deque[Tuple[float, float]] = field(default_factory=deque)


_lock = RLock()
_state = SoftAlertState()


def reset_soft_alerts_for_tests() -> None:
    global _state
    with _lock:
        _state = SoftAlertState()


def _rate_ok(key: str, now: float) -> bool:
    last = float(_state.last_fired_ts.get(key, 0.0) or 0.0)
    return (now - last) >= ALERT_COOLDOWN_SEC


def _fire(key: str, msg: str, now: float, level: str = "info") -> None:
    _state.last_fired_ts[key] = now
    _state.fire_counts[key] = int(_state.fire_counts.get(key, 0) or 0) + 1
    _state.last_reasons[key] = msg
    if level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)


def _prune_equity_samples(now: float) -> None:
    cutoff = now - SLOW_BLEED_WINDOW_SEC
    while _state._equity_samples and _state._equity_samples[0][0] < cutoff:
        _state._equity_samples.popleft()


def _update_equity_window(equity: float, now: float) -> Tuple[float, float]:
    """Return (peak_72h, dd_fraction). dd = (peak - equity) / peak when peak>0.

    Equity 0 (full wipe) must report ~100% DD vs peak — not 0%. Negative equity
    is clamped to 0 for DD math; negative samples are not stored.
    """
    eq = float(equity) if equity is not None else 0.0
    # Record non-negative samples so a wipe to $0 remains observable.
    if eq >= 0:
        _state._equity_samples.append((now, eq))
        while len(_state._equity_samples) > _EQUITY_SAMPLE_MAX:
            _state._equity_samples.popleft()
    _prune_equity_samples(now)
    if not _state._equity_samples:
        return 0.0, 0.0
    peak = max(e for _, e in _state._equity_samples)
    if peak > 0:
        dd = (peak - max(eq, 0.0)) / peak
    else:
        dd = 0.0
    return float(peak), float(dd)



def check_reserve_near_floor(
    usdt_free: float,
    usdt_reserved: float,
) -> Tuple[bool, str]:
    free = float(usdt_free or 0.0)
    reserved = float(usdt_reserved or 0.0)
    if reserved <= 0:
        return False, ""
    band = max(reserved * (1.0 + RESERVE_NEAR_PCT), reserved + RESERVE_NEAR_EPS_USD)
    if free <= band:
        pct_above = ((free - reserved) / reserved) * 100.0 if reserved > 0 else 0.0
        return True, (
            f"free USDT ${free:,.2f} near reserve floor ${reserved:,.2f} "
            f"({pct_above:+.1f}% vs floor; band=${band:,.2f})"
        )
    return False, ""


def check_slow_bleed(
    *,
    btc_72h_pct: float,
    equity_dd_72h: float,
    cascade_latch_active: bool,
) -> Tuple[bool, str]:
    """Advisory only. Suppressed while cascade latch is active (that already pauses buys)."""
    if cascade_latch_active:
        return False, "suppressed — cascade latch active"
    parts: List[str] = []
    btc = float(btc_72h_pct or 0.0)
    dd = float(equity_dd_72h or 0.0)
    if btc <= BTC_SLOW_BLEED_72H_PCT:
        parts.append(f"BTC 72h {btc:+.2f}% <= {BTC_SLOW_BLEED_72H_PCT}%")
    if dd >= EQUITY_SLOW_BLEED_DD:
        parts.append(f"equity DD 72h {dd * 100:.1f}% >= {EQUITY_SLOW_BLEED_DD * 100:.0f}%")
    if not parts:
        return False, ""
    return True, "; ".join(parts) + " (alert only — no buy pause)"


def update_soft_alerts(
    *,
    usdt_free: float = 0.0,
    usdt_reserved: float = 0.0,
    equity: float = 0.0,
    btc_72h_pct: float = 0.0,
    cascade_latch_active: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Update watches and emit rate-limited logs. Never pauses buys.
    Call once per Spot tick with live free/reserve/equity/BTC72h.
    """
    now_ts = float(now if now is not None else time.time())
    with _lock:
        try:
            _state.last_usdt_free = float(usdt_free or 0.0)
            _state.last_usdt_reserved = float(usdt_reserved or 0.0)
            _state.last_equity = float(equity or 0.0)
            _state.last_btc_72h_pct = float(btc_72h_pct or 0.0)

            peak, dd = _update_equity_window(_state.last_equity, now_ts)
            _state.equity_peak_72h = peak
            _state.equity_dd_72h = dd

            near, near_why = check_reserve_near_floor(
                _state.last_usdt_free, _state.last_usdt_reserved
            )
            _state.reserve_near_floor = near
            if near and _rate_ok("reserve_near_floor", now_ts):
                _fire(
                    "reserve_near_floor",
                    f"⚠️ [SOFT ALERT] reserve_near_floor — {near_why}",
                    now_ts,
                    level="warning",
                )
            elif not near:
                _state.last_reasons.pop("reserve_near_floor", None)

            bleed, bleed_why = check_slow_bleed(
                btc_72h_pct=_state.last_btc_72h_pct,
                equity_dd_72h=dd,
                cascade_latch_active=bool(cascade_latch_active),
            )
            _state.slow_bleed_watch = bleed
            if bleed and _rate_ok("slow_bleed_watch", now_ts):
                _fire(
                    "slow_bleed_watch",
                    f"⚠️ [SOFT ALERT] slow_bleed_watch — {bleed_why}",
                    now_ts,
                    level="warning",
                )
            elif not bleed:
                # keep last reason if suppressed by cascade for status clarity
                if "cascade latch" not in (bleed_why or ""):
                    _state.last_reasons.pop("slow_bleed_watch", None)
                elif bleed_why:
                    _state.last_reasons["slow_bleed_watch"] = bleed_why
        except Exception as e:
            logger.debug(f"update_soft_alerts: {e}")
        return soft_alerts_summary()


def soft_alerts_summary() -> Dict[str, Any]:
    with _lock:
        return {
            "reserve_near_floor": _state.reserve_near_floor,
            "slow_bleed_watch": _state.slow_bleed_watch,
            "usdt_free": round(_state.last_usdt_free, 2),
            "usdt_reserved": round(_state.last_usdt_reserved, 2),
            "equity": round(_state.last_equity, 2),
            "equity_peak_72h": round(_state.equity_peak_72h, 2),
            "equity_dd_72h_pct": round(_state.equity_dd_72h * 100.0, 2),
            "btc_72h_pct": round(_state.last_btc_72h_pct, 2),
            "last_reasons": dict(_state.last_reasons),
            "fire_counts": dict(_state.fire_counts),
            "thresholds": {
                "RESERVE_NEAR_PCT": RESERVE_NEAR_PCT,
                "RESERVE_NEAR_EPS_USD": RESERVE_NEAR_EPS_USD,
                "BTC_SLOW_BLEED_72H_PCT": BTC_SLOW_BLEED_72H_PCT,
                "EQUITY_SLOW_BLEED_DD_PCT": EQUITY_SLOW_BLEED_DD * 100.0,
                "SLOW_BLEED_WINDOW_SEC": SLOW_BLEED_WINDOW_SEC,
                "ALERT_COOLDOWN_SEC": ALERT_COOLDOWN_SEC,
                "note": "log-only; never pauses buys; slow_bleed suppressed while cascade latch active",
            },
        }

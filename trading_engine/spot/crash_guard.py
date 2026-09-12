"""
Spot true-crash buy halt — cycle-safe by design.

Fires only on rare crash-scale moves, NOT normal RANGE volatility:
- BTC 24h return <= -15%, OR
- Portfolio equity drawdown from in-process peak >= 25%

Clears when both have recovered enough (BTC 24h > -8% AND equity DD < 15%).
Does NOT extend the short BTC flash-dump freeze (that would clip daily cycles).
Does NOT sell below buy / flatten bags — buys only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

# Intentionally high vs day-to-day RANGE noise (flash dump is -1.5%/1h).
BTC_24H_HALT_PCT = -15.0
BTC_24H_CLEAR_PCT = -8.0
EQUITY_DD_HALT = 0.25
EQUITY_DD_CLEAR = 0.15


@dataclass
class CrashHaltState:
    active: bool = False
    reason: str = ""
    equity_peak: float = 0.0
    last_equity: float = 0.0
    last_btc_24h_pct: float = 0.0
    last_equity_dd: float = 0.0


_state = CrashHaltState()


def reset_crash_halt_for_tests() -> None:
    global _state
    _state = CrashHaltState()


def update_crash_halt(*, btc_24h_pct: float, equity: float) -> CrashHaltState:
    """Update peak equity + halt latch. Call once per Spot tick with live numbers."""
    global _state
    try:
        btc = float(btc_24h_pct or 0.0)
        eq = float(equity or 0.0)
        _state.last_btc_24h_pct = btc
        if eq > 0:
            _state.last_equity = eq
            if eq > _state.equity_peak:
                _state.equity_peak = eq
        peak = float(_state.equity_peak or 0.0)
        dd = ((peak - eq) / peak) if (peak > 0 and eq > 0) else 0.0
        _state.last_equity_dd = dd

        btc_trip = btc <= BTC_24H_HALT_PCT
        eq_trip = dd >= EQUITY_DD_HALT
        btc_clear = btc > BTC_24H_CLEAR_PCT
        eq_clear = dd < EQUITY_DD_CLEAR

        if not _state.active:
            if btc_trip or eq_trip:
                parts = []
                if btc_trip:
                    parts.append(f"BTC 24h {btc:+.1f}% <= {BTC_24H_HALT_PCT:.0f}%")
                if eq_trip:
                    parts.append(f"equity DD {dd*100:.1f}% >= {EQUITY_DD_HALT*100:.0f}% from peak")
                _state.active = True
                _state.reason = "; ".join(parts)
                logger.warning(f"🛑 [CRASH HALT] Spot new buys paused — {_state.reason}")
        else:
            # Stay halted until BOTH recovered enough (avoid flicker re-entry buys mid-crash)
            if btc_clear and eq_clear:
                logger.info(
                    f"✅ [CRASH HALT] Cleared — BTC 24h {btc:+.1f}%, equity DD {dd*100:.1f}% "
                    f"(peak ${_state.equity_peak:,.0f})"
                )
                _state.active = False
                _state.reason = ""
            else:
                _state.reason = (
                    f"still active (BTC 24h {btc:+.1f}%, equity DD {dd*100:.1f}% "
                    f"/ peak ${_state.equity_peak:,.0f})"
                )
    except Exception as e:
        logger.debug(f"update_crash_halt: {e}")
    return _state


def is_crash_buy_halted() -> Tuple[bool, str]:
    if _state.active:
        return True, f"crash buy-halt: {_state.reason or 'active'}"
    return False, ""


def crash_halt_summary() -> dict:
    return {
        "active": _state.active,
        "reason": _state.reason,
        "btc_24h_pct": _state.last_btc_24h_pct,
        "equity": _state.last_equity,
        "equity_peak": _state.equity_peak,
        "equity_dd_pct": round(_state.last_equity_dd * 100.0, 2),
        "btc_halt_pct": BTC_24H_HALT_PCT,
        "equity_dd_halt_pct": EQUITY_DD_HALT * 100.0,
    }

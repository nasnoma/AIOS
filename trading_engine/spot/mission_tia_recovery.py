"""
TIA recovery mission (armed 2026-09-19).

Goal: use a capped USDT sleeve to average down so a single fee-proof bag exit
can reach breakeven / tiny profit sooner — without open-ended DCA.

Rules:
- Max sleeve notional (default $1200). Spent fills are tracked on disk.
- May bypass the 10% exposure hard sell-only *only for TIA* while sleeve remains.
- Still subject to crash halt, dump brake, trend veto, exitability, USDT reserve,
  and TIA unlock buy-pauses.
- Resting sells prefer fee-proof bag breakeven (+tiny net) over far grid TPs
  while the mission is active.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Tuple

from loguru import logger

TIA_SYMBOL = "TIA/USDT"
SLEEVE_USD = 0.0  # disabled — no average-down (reverted 2026-09-19)
# Tiny net target on full-bag recovery exit (USD), on top of fee-proof floor
RECOVERY_MIN_NET_USD = 3.0
# Soft ceiling while mission averaging: do not push TIA above this % equity
MISSION_EXPOSURE_SOFT_CAP = 0.10  # no special TIA buy room

_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "tia_recovery_mission.json"
_lock = threading.Lock()


def _load() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text())
    except Exception as e:
        logger.debug(f"tia mission load: {e}")
    return {
        "active": False,
        "sleeve_usd": SLEEVE_USD,
        "spent_usd": 0.0,
        "completed": False,
        "note": "TIA recovery sleeve — average down to fee-proof BE exit",
    }


def _save(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"tia mission save failed: {e}")


def is_tia(symbol: str) -> bool:
    s = (symbol or "").strip().upper()
    return s in ("TIA/USDT", "TIA")


def mission_active(symbol: str | None = None) -> bool:
    if symbol is not None and not is_tia(symbol):
        return False
    st = _load()
    return bool(st.get("active")) and not bool(st.get("completed"))


def sleeve_remaining_usd() -> float:
    st = _load()
    if not st.get("active") or st.get("completed"):
        return 0.0
    sleeve = float(st.get("sleeve_usd") or SLEEVE_USD)
    spent = float(st.get("spent_usd") or 0.0)
    return max(0.0, sleeve - spent)


def allows_exposure_bypass(symbol: str) -> bool:
    """True if TIA may buy despite >=10% equity cap (sleeve still available)."""
    return mission_active(symbol) and sleeve_remaining_usd() >= 35.0


def mission_buy_room_usd(symbol: str, equity: float = 0.0, holding_usd: float = 0.0) -> float:
    """Extra buy room granted by the mission (min of sleeve left and soft cap headroom)."""
    if not allows_exposure_bypass(symbol):
        return 0.0
    room = sleeve_remaining_usd()
    eq = float(equity or 0.0)
    if eq > 0:
        soft_head = max(0.0, eq * MISSION_EXPOSURE_SOFT_CAP - float(holding_usd or 0.0))
        room = min(room, soft_head)
    return max(0.0, room)


def record_buy_fill(symbol: str, notional_usd: float) -> None:
    if not is_tia(symbol):
        return
    with _lock:
        st = _load()
        if not st.get("active") or st.get("completed"):
            return
        spent = float(st.get("spent_usd") or 0.0) + max(0.0, float(notional_usd or 0.0))
        st["spent_usd"] = round(spent, 4)
        sleeve = float(st.get("sleeve_usd") or SLEEVE_USD)
        if spent >= sleeve - 1e-6:
            logger.info(
                f"🎯 [TIA MISSION] Sleeve exhausted (${spent:.2f}/${sleeve:.2f}) — "
                f"no further recovery buys; wait for fee-proof BE exit"
            )
        else:
            logger.info(
                f"🎯 [TIA MISSION] Recovery buy logged ${float(notional_usd):.2f} "
                f"(spent ${spent:.2f}/${sleeve:.2f})"
            )
        _save(st)


def mark_completed(reason: str = "recovery exit filled") -> None:
    with _lock:
        st = _load()
        st["completed"] = True
        st["active"] = False
        st["completed_reason"] = reason
        _save(st)
        logger.info(f"🎯 [TIA MISSION] Completed — {reason}")


def recovery_sell_price(avg_cost: float, qty: float, fee_rate: float = 0.001) -> float:
    """
    Fee-proof price to exit `qty` at avg_cost with ~RECOVERY_MIN_NET_USD net.
    sell*(1-f)*qty - avg*(1+f)*qty >= min_net
    """
    avg = float(avg_cost or 0.0)
    q = float(qty or 0.0)
    f = max(0.0, float(fee_rate or 0.001))
    if avg <= 0 or q <= 0:
        return 0.0
    # fee-proof floor
    floor = avg * (1.0 + f) / max(1e-12, (1.0 - f))
    # add tiny net
    extra = RECOVERY_MIN_NET_USD / (q * max(1e-12, (1.0 - f)))
    return floor + extra


def status() -> dict:
    st = _load()
    st = dict(st)
    st["remaining_usd"] = sleeve_remaining_usd()
    return st

"""
Cascade / falling-knife buy-pause guard (Spec A).

SHADOW-ONLY by default:
  SPOT_CASCADE_SHADOW=1  → log would-pause / triggers; NEVER cancel or block buys
  SPOT_CASCADE_LIVE=1    → would enable live cancel_buys_only path (DEFAULT OFF;
                           do NOT set unless fuller backtest gates pass + Nasir OK)

Observation config (post-sweep 2026-09-23): A_tuned mid-tier used for logging only.
Fuller Top-8 ~21d sweep: NO variant passed net≥baseline AND cycles≥95% AND dump-down.
Live pause must stay OFF. Shadow cannot reduce cycles/profit.

See: spot/docs/DESIGN_buy_pause_and_poc_grid.md
     spot/backtest_results/fuller_cascade_sweep_20260923.json
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

# ── Shadow observation thresholds (A_tuned — best dump reduction; gates FAILED) ──
BTC_CASCADE_1H_PCT = -1.4
BTC_CASCADE_WICK_OL_PCT = -2.0
BTC_CASCADE_4H_PCT = -5.0
CASCADE_CHAIN_1H_PCT = -1.0
FLASH_1H_PCT = -1.5
FLASH_4H_PCT = -3.0
FLASH_PAUSE_SEC = 1800.0
CASCADE_PAUSE_SEC = 5400.0  # 90m
CLEAR_1H_PCT = -0.5


def shadow_enabled() -> bool:
    return os.environ.get("SPOT_CASCADE_SHADOW", "").strip() in ("1", "true", "True", "yes", "YES")


def live_enabled() -> bool:
    """Live buy-cancel. Hard default OFF. Gates did not pass on 2026-09-23 sweep."""
    return os.environ.get("SPOT_CASCADE_LIVE", "").strip() in ("1", "true", "True", "yes", "YES")


def _resolve_data_dir() -> Path:
    env = (os.environ.get("SPOT_CASCADE_SHADOW_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    repo_data = Path(__file__).resolve().parents[1] / "data"
    try:
        repo_data.mkdir(parents=True, exist_ok=True)
        probe = repo_data / ".cascade_shadow_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return repo_data
    except Exception:
        tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "aios_cascade_shadow"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp


_DATA_DIR = _resolve_data_dir()
_JSONL_PATH = _DATA_DIR / "cascade_shadow.jsonl"
_lock = threading.Lock()
_announced = False


@dataclass
class CascadeState:
    flash_until: float = 0.0
    cascade_until: float = 0.0
    paused: bool = False
    last_r1: float = 0.0
    last_r4: float = 0.0
    last_ol: float = 0.0
    last_reason: str = ""
    last_updated: float = 0.0
    triggers: int = 0
    would_pause_ticks: int = 0
    shadow_logs: int = 0


class CascadeGuard:
    """Singleton cascade latch. Shadow logs only unless SPOT_CASCADE_LIVE=1."""

    _instance: Optional["CascadeGuard"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.state = CascadeState()
        global _announced
        _announced = False

    def update_returns(self, r1: float, r4: float, ol: float, now: Optional[float] = None) -> CascadeState:
        """Update latch from BTC 1h close-to-close / 4h / open→low %. Fail-open on errors."""
        global _announced
        try:
            now_ts = float(now if now is not None else time.time())
            st = self.state
            st.last_r1 = float(r1)
            st.last_r4 = float(r4)
            st.last_ol = float(ol)
            st.last_updated = now_ts

            # Flash (mirrors btc_master_filter)
            if r1 < FLASH_1H_PCT or r4 < FLASH_4H_PCT:
                st.flash_until = max(st.flash_until, now_ts + FLASH_PAUSE_SEC)
            flash_active = st.flash_until > now_ts

            triggered = False
            reason = ""
            if r1 <= BTC_CASCADE_1H_PCT:
                triggered = True
                reason = f"btc_1h_close {r1:+.2f}% <= {BTC_CASCADE_1H_PCT}"
            elif ol <= BTC_CASCADE_WICK_OL_PCT:
                triggered = True
                reason = f"btc_wick_ol {ol:+.2f}% <= {BTC_CASCADE_WICK_OL_PCT}"
            elif r4 <= BTC_CASCADE_4H_PCT:
                triggered = True
                reason = f"btc_4h {r4:+.2f}% <= {BTC_CASCADE_4H_PCT}"
            elif flash_active and r1 <= CASCADE_CHAIN_1H_PCT:
                triggered = True
                reason = f"flash_chain r1={r1:+.2f}% <= {CASCADE_CHAIN_1H_PCT}"

            if triggered:
                st.triggers += 1
                st.cascade_until = max(st.cascade_until, now_ts + CASCADE_PAUSE_SEC)
                st.last_reason = reason

            latch_active = st.cascade_until > now_ts
            if latch_active:
                st.paused = True
            elif st.cascade_until > 0:
                if r1 > CLEAR_1H_PCT and not flash_active:
                    st.cascade_until = 0.0
                    st.paused = False
                    st.last_reason = "cleared"
                else:
                    st.paused = True
                    st.last_reason = st.last_reason or "hold_until_clear"
            else:
                st.paused = False

            if st.paused:
                st.would_pause_ticks += 1

            if shadow_enabled() and not _announced:
                _announced = True
                logger.info(
                    "[CASCADE SHADOW] armed — log-only (A_tuned obs thresholds). "
                    "SPOT_CASCADE_LIVE default OFF; sweep gates FAILED — will not cancel buys."
                )

            if shadow_enabled() and st.paused:
                self._shadow_log(
                    {
                        "event": "would_pause",
                        "r1": round(r1, 4),
                        "r4": round(r4, 4),
                        "ol": round(ol, 4),
                        "reason": st.last_reason,
                        "cascade_until": st.cascade_until,
                        "flash_until": st.flash_until,
                        "triggers": st.triggers,
                        "would_pause_ticks": st.would_pause_ticks,
                        "live_enabled": live_enabled(),
                    }
                )
        except Exception as e:
            logger.debug(f"cascade_guard update_returns: {e}")
        return self.state

    def _shadow_log(self, payload: Dict[str, Any]) -> None:
        try:
            with _lock:
                self.state.shadow_logs += 1
                # Throttle identical would_pause to ~1/min in JSONL (counters still tick)
                if self.state.shadow_logs > 1 and (self.state.shadow_logs % 4) != 1:
                    # still emit INFO sparsely
                    if self.state.shadow_logs % 20 == 1:
                        logger.info(
                            f"[CASCADE SHADOW] would_pause ticks={self.state.would_pause_ticks} "
                            f"triggers={self.state.triggers} reason={payload.get('reason')}"
                        )
                    return
                row = {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    **payload,
                }
                with open(_JSONL_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, default=str) + "\n")
                logger.info(
                    f"[CASCADE SHADOW] would_pause r1={payload.get('r1')} ol={payload.get('ol')} "
                    f"reason={payload.get('reason')} (live={live_enabled()})"
                )
        except Exception as e:
            logger.debug(f"cascade_guard shadow_log: {e}")

    def is_cascade_pause(self) -> Tuple[bool, str]:
        """
        Live gate. Returns (should_pause_buys, reason).
        ONLY True when SPOT_CASCADE_LIVE=1 AND latch active.
        Shadow-only never returns True here (orders unchanged).
        """
        if not live_enabled():
            return False, "cascade live off (shadow-only or disabled)"
        st = self.state
        if (time.time() - st.last_updated) > 180.0 and st.last_updated > 0:
            return False, "cascade cache stale — fail open"
        if st.paused:
            rem = max(0, int((st.cascade_until - time.time()) / 60))
            return True, f"cascade pause ({st.last_reason}; ~{rem}m left)"
        return False, "no cascade latch"

    def would_pause(self) -> Tuple[bool, str]:
        """Observability: latch active regardless of live flag."""
        st = self.state
        if st.paused:
            return True, st.last_reason or "cascade latch"
        return False, "no cascade latch"

    def summary(self) -> Dict[str, Any]:
        st = self.state
        return {
            "shadow_enabled": shadow_enabled(),
            "live_enabled": live_enabled(),
            "paused_latch": st.paused,
            "would_pause": st.paused,
            "last_r1": st.last_r1,
            "last_r4": st.last_r4,
            "last_ol": st.last_ol,
            "last_reason": st.last_reason,
            "triggers": st.triggers,
            "would_pause_ticks": st.would_pause_ticks,
            "shadow_logs": st.shadow_logs,
            "cascade_until": st.cascade_until,
            "flash_until": st.flash_until,
            "obs_thresholds": {
                "BTC_CASCADE_1H_PCT": BTC_CASCADE_1H_PCT,
                "BTC_CASCADE_WICK_OL_PCT": BTC_CASCADE_WICK_OL_PCT,
                "BTC_CASCADE_4H_PCT": BTC_CASCADE_4H_PCT,
                "CASCADE_PAUSE_SEC": CASCADE_PAUSE_SEC,
                "note": "A_tuned obs; live gates FAILED on fuller sweep 2026-09-23",
            },
            "jsonl": str(_JSONL_PATH),
        }


cascade_guard = CascadeGuard()

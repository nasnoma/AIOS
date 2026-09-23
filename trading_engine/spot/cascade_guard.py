"""
Cascade / falling-knife buy-pause guard (Spec A — A_tuned LIVE).

Nasir 2026-09-23 accepted A_tuned tradeoff (modest cycles/net haircut for dump cut):
  1h close ≤ −1.4% OR open→low wick ≤ −2.0% (plus A_tuned 4h≤−5% / flash-chain),
  90m latch, extendable while condition holds.
  Do NOT use highbar/flash_cont as live policy (failed to cut Sep23 dump buys).

Flags:
  SPOT_CASCADE_LIVE     → cancel/suppress NEW grid buys only (DEFAULT ON;
                          set 0/false/off to disable). Sells / fee-proof TPs stay.
  SPOT_CASCADE_SHADOW   → JSONL + INFO latch logs (keep on in prod)

Fail-safe: stale/missing BTC → fail OPEN on new buys (match btc_master_filter).
Never sell below buy; MNT hold-only; sticky/hist floors unchanged.

See: spot/docs/DESIGN_buy_pause_and_poc_grid.md
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

# ── A_tuned LIVE thresholds (accepted 2026-09-23) ──
BTC_CASCADE_1H_PCT = -1.4
BTC_CASCADE_WICK_OL_PCT = -2.0
BTC_CASCADE_4H_PCT = -5.0
CASCADE_CHAIN_1H_PCT = -1.0
FLASH_1H_PCT = -1.5
FLASH_4H_PCT = -3.0
FLASH_PAUSE_SEC = 1800.0
CASCADE_PAUSE_SEC = 5400.0  # 90m
CLEAR_1H_PCT = -0.5
_STALE_SEC = 180.0  # fail-open; btc_master_filter uses 120s


def _env_on(name: str, default: str = "") -> bool:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    v = str(raw).strip()
    if v in ("0", "false", "False", "no", "NO", "off", "OFF"):
        return False
    return v in ("1", "true", "True", "yes", "YES")


def shadow_enabled() -> bool:
    return _env_on("SPOT_CASCADE_SHADOW", default="")


def live_enabled() -> bool:
    """Live buy-cancel/suppress. Default ON (Nasir accepted A_tuned). Explicit 0/off disables."""
    return _env_on("SPOT_CASCADE_LIVE", default="1")


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
    live_cancel_ticks: int = 0


class CascadeGuard:
    """Singleton cascade latch. LIVE cancels/suppresses new buys when enabled."""

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

    def reset_for_tests(self) -> None:
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

            if r1 < FLASH_1H_PCT or r4 < FLASH_4H_PCT:
                st.flash_until = max(st.flash_until, now_ts + FLASH_PAUSE_SEC)
            flash_active = st.flash_until > now_ts

            triggered = False
            reason = ""
            # A_tuned OR (not highbar / not flash_cont-only)
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

            if (shadow_enabled() or live_enabled()) and not _announced:
                _announced = True
                logger.info(
                    f"[CASCADE] armed A_tuned — live={live_enabled()} shadow={shadow_enabled()} "
                    f"(1h<={BTC_CASCADE_1H_PCT}% OR wick<={BTC_CASCADE_WICK_OL_PCT}%, "
                    f"latch={int(CASCADE_PAUSE_SEC/60)}m). Buys-only pause; sells stay."
                )

            if shadow_enabled() and st.paused:
                self._shadow_log(
                    {
                        "event": "live_pause" if live_enabled() else "would_pause",
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
            elif live_enabled() and st.paused and (st.would_pause_ticks % 20) == 1:
                logger.info(
                    f"[CASCADE LIVE] pause active reason={st.last_reason} "
                    f"r1={r1:+.2f} ol={ol:+.2f} triggers={st.triggers}"
                )
        except Exception as e:
            logger.debug(f"cascade_guard update_returns: {e}")
        return self.state

    def _shadow_log(self, payload: Dict[str, Any]) -> None:
        try:
            with _lock:
                self.state.shadow_logs += 1
                if self.state.shadow_logs > 1 and (self.state.shadow_logs % 4) != 1:
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
                    f"[CASCADE SHADOW] {payload.get('event')} r1={payload.get('r1')} ol={payload.get('ol')} "
                    f"reason={payload.get('reason')} (live={live_enabled()})"
                )
        except Exception as e:
            logger.debug(f"cascade_guard shadow_log: {e}")

    def is_cascade_pause(self) -> Tuple[bool, str]:
        """
        Live gate. Returns (should_pause_buys, reason).
        ONLY True when SPOT_CASCADE_LIVE on AND latch active.
        Fail-open if BTC never set or stale (match btc_master_filter).
        """
        if not live_enabled():
            return False, "cascade live off"
        st = self.state
        if st.last_updated <= 0:
            return False, "cascade no BTC yet — fail open"
        if (time.time() - st.last_updated) > _STALE_SEC:
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

    def note_live_cancel(self) -> None:
        self.state.live_cancel_ticks += 1

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
            "live_cancel_ticks": st.live_cancel_ticks,
            "shadow_logs": st.shadow_logs,
            "cascade_until": st.cascade_until,
            "flash_until": st.flash_until,
            "obs_thresholds": {
                "BTC_CASCADE_1H_PCT": BTC_CASCADE_1H_PCT,
                "BTC_CASCADE_WICK_OL_PCT": BTC_CASCADE_WICK_OL_PCT,
                "BTC_CASCADE_4H_PCT": BTC_CASCADE_4H_PCT,
                "CASCADE_PAUSE_SEC": CASCADE_PAUSE_SEC,
                "note": "A_tuned LIVE — Nasir accepted 2026-09-23 cycles/net haircut for dump cut",
            },
            "jsonl": str(_JSONL_PATH),
        }


cascade_guard = CascadeGuard()

"""
Spot Jev / TypeSafe System One SHADOW logger.

Observability only:
- Builds a Spot-shaped state from real runner fields (missing -> "unknown")
- Asks Jev (Choice / Score / Noul) with a hard timeout
- Logs answers + Spot tick summary to SQLite (and optional JSONL)
- NEVER places, cancels, or resizes orders
- NEVER feeds answers back into allow_buys / grid / DCA

Enable with: SPOT_JEV_SHADOW=1
Optional:
  TYPESAFE_API_KEY   (required for live ask; prefer env, never CLI)
  TYPESAFE_MODEL     (default jev-1.13.0)
  SPOT_JEV_SHADOW_MIN_INTERVAL_SEC  (default 45)
  SPOT_JEV_SHADOW_TIMEOUT_SEC       (default 12)
  SPOT_JEV_SHADOW_JSONL=1           (also append JSONL beside the DB)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# ── Config ──────────────────────────────────────────────────────────────────
_DEFAULT_MODEL = "jev-1.13.0"
_DEFAULT_MIN_INTERVAL = 45.0
_DEFAULT_TIMEOUT = 12.0

NOUL_BUY_OK = 0.60
NOUL_BUY_BLOCK = 0.40
CHOICE_MIN_PROB = 0.55

def _resolve_data_dir() -> Path:
    """Prefer env, then repo data/, then /tmp (Railway ephemeral FS is fine for shadow)."""
    env = (os.environ.get("SPOT_JEV_SHADOW_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    repo_data = Path(__file__).resolve().parents[1] / "data"
    try:
        repo_data.mkdir(parents=True, exist_ok=True)
        probe = repo_data / ".jev_shadow_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return repo_data
    except Exception:
        tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "aios_jev_shadow"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp


_DATA_DIR = _resolve_data_dir()
_DB_PATH = _DATA_DIR / "jev_shadow.db"
_JSONL_PATH = _DATA_DIR / "jev_shadow.jsonl"

_lock = threading.Lock()
_last_ask_ts: dict[str, float] = {}
_db_ready = False
_shadow_announced = False
_inflight = threading.Semaphore(int(os.environ.get("SPOT_JEV_SHADOW_MAX_INFLIGHT", "4") or 4))
_success_log_counter = 0


def shadow_enabled() -> bool:
    return os.environ.get("SPOT_JEV_SHADOW", "").strip() in ("1", "true", "True", "yes", "YES")


def _model() -> str:
    return (os.environ.get("TYPESAFE_MODEL") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def _min_interval() -> float:
    try:
        return max(5.0, float(os.environ.get("SPOT_JEV_SHADOW_MIN_INTERVAL_SEC", _DEFAULT_MIN_INTERVAL)))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL


def _timeout_sec() -> float:
    try:
        return max(2.0, float(os.environ.get("SPOT_JEV_SHADOW_TIMEOUT_SEC", _DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def _unk(v: Any) -> Any:
    """Normalize empty / missing values to the string 'unknown'."""
    if v is None:
        return "unknown"
    if isinstance(v, float) and (v != v):  # NaN
        return "unknown"
    if isinstance(v, str) and not v.strip():
        return "unknown"
    return v


def _safe_float(v: Any, default: Any = "unknown") -> Any:
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default



def _range_pct(v: Any) -> Any:
    """Normalize 24h range to percent. Accepts fraction (0.05) or percent (5.0)."""
    f = _safe_float(v)
    if not isinstance(f, (int, float)):
        return f
    f = float(f)
    # Fractions from runner tick are typically < 1.5 for crypto 24h ranges;
    # values already in percent are usually >= 1.5 when markets move.
    if abs(f) <= 1.5:
        return round(f * 100.0, 4)
    return round(f, 4)

def build_state(symbol: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Build a compact Spot-shaped state from REAL runner/engine fields.
    Any missing field is marked \"unknown\" (not invented).
    """
    price = _safe_float(ctx.get("price"))
    sma_50 = _safe_float(ctx.get("sma_50"))
    sma_200 = _safe_float(ctx.get("sma_200"))
    adx_1h = _safe_float(ctx.get("adx_1h"))

    price_vs_sma50 = "unknown"
    sma50_vs_sma200 = "unknown"
    if isinstance(price, (int, float)) and isinstance(sma_50, (int, float)) and sma_50 > 0:
        price_vs_sma50 = round((float(price) / float(sma_50) - 1.0) * 100.0, 3)
    if isinstance(sma_50, (int, float)) and isinstance(sma_200, (int, float)) and sma_200 > 0:
        sma50_vs_sma200 = round((float(sma_50) / float(sma_200) - 1.0) * 100.0, 3)

    adx_trend = "unknown"
    if isinstance(adx_1h, (int, float)):
        adx_trend = "strong" if float(adx_1h) >= 25 else "weak"

    btc = ctx.get("btc_filter") if isinstance(ctx.get("btc_filter"), dict) else {}

    state: dict[str, Any] = {
        "fixture": False,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "venue": "bybit_spot",
        "mark_price": price,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "price_vs_sma50_pct": price_vs_sma50,
        "sma50_vs_sma200_pct": sma50_vs_sma200,
        "adx_1h": adx_1h,
        "adx_trend_strength": adx_trend,
        "realized_vol_48h_pct": _unk(ctx.get("realized_vol_48h_pct")),
        "spot_regime": _unk(ctx.get("spot_regime")),
        "btc_master_filter": {
            "regime": _unk(btc.get("regime")),
            "adx": _safe_float(btc.get("adx")),
            "btc_1h_change_pct": _safe_float(btc.get("btc_1h_change_pct")),
            "btc_4h_change_pct": _safe_float(btc.get("btc_4h_change_pct")),
            "btc_24h_change_pct": _safe_float(btc.get("btc_24h_change_pct")),
            "is_safe_for_alt_buys": _unk(btc.get("is_safe_for_alt_buys")),
            "is_flash_dump": _unk(btc.get("is_flash_dump")),
            "safety_reason": _unk(btc.get("safety_reason")),
        }
        if btc
        else "unknown",
        "spot_inventory_usd": _safe_float(ctx.get("inventory_usd")),
        "spot_inventory_units": _safe_float(ctx.get("inventory_units")),
        "avg_cost_basis": _safe_float(ctx.get("avg_cost_basis")),
        "open_buy_notional_usd": _safe_float(ctx.get("open_buy_notional_usd")),
        "open_buy_count": _unk(ctx.get("open_buy_count")),
        "open_sell_count": _unk(ctx.get("open_sell_count")),
        "allocated_usd": _safe_float(ctx.get("allocated_usd")),
        "in_top8": _unk(ctx.get("in_top8")),
        "allow_buys_effective": _unk(ctx.get("allow_buys_effective")),
        "fee_rate_roundtrip": _safe_float(ctx.get("fee_rate_roundtrip")),
        "range_24h_pct": _range_pct(ctx.get("range_24h_pct")),
        "notes": (
            "Judge only from fields present. "
            "Treat unknown as missing evidence, not neutral. "
            "Do not assume order-book depth or funding. "
            "SHADOW ONLY — advisory log, never places/cancels orders."
        ),
    }
    return state


def build_questions(symbol: str) -> dict[str, Any]:
    """Reuse Spot-shaped question design from smoke_typesafe_jev.py (symbol-aware)."""
    from typesafe_sdk import Choice, Noul, Score

    return {
        "regime_4h": Choice(
            instructions=(
                f"Classify the likely {symbol} regime over the next ~4 hours "
                "using only the provided state. Prefer RANGE when trend signals conflict."
            ),
            criteria={
                "BULL": (
                    "Upward drift favored: price supported above key averages "
                    "with rising or firm trend strength"
                ),
                "RANGE": (
                    "Mean-reverting chop or conflicting signals "
                    "(e.g. strong ADX but price below SMA50 while still above SMA200)"
                ),
                "BEAR": (
                    "Downward drift favored: failed supports / averages sloping down "
                    "with sustained selling pressure"
                ),
            },
        ),
        "downside_24h": Score(
            instructions=(
                f"Expected downside severity for {symbol} over the next ~24 hours. "
                "Higher = worse left-tail risk."
            ),
            criteria=[
                "Minimal: support firm, shallow pullbacks only",
                "Low: normal chop, no breakdown setup",
                "Moderate: retesting nearby supports; breakdown possible but not base case",
                "High: elevated odds of a decisive break lower",
                "Extreme: cascading sell-off / air-pocket risk",
            ],
        ),
        "grid_buy_ok": Noul(
            instructions=(
                f"Probability that resting NEW Spot grid BUY limits on {symbol} in the next "
                "few hours is good process for a fee-aware grid. True = cascade risk contained "
                "and chop/mean-revert is plausible. False = buys likely adverse-selected."
            ),
            criteria={
                "true": (
                    "Acceptable to rest new grid buys: cascade risk contained, "
                    "range/bullish lean, or dip looks buyable for grid cycling"
                ),
                "false": (
                    "Poor process to add grid buys now: breakdown / cascade risk, "
                    "or trend-down likely to chew through buy rungs"
                ),
            },
        ),
        "adverse_fill_risk": Score(
            instructions=(
                "If a passive grid BUY fills in the next hour, how bad is immediate "
                "adverse selection (price keeps falling after the fill)?"
            ),
            criteria=[
                "Negligible adverse selection",
                "Mild — normal noise after fill",
                "Moderate — meaningful risk of further slide",
                "Severe — fills likely near local tops of a drop",
                "Toxic — high chance of waterfall after fill",
            ],
        ),
    }


def _probs(ans: Any) -> dict[str, Any]:
    probs = getattr(ans, "probabilities", None) or {}
    if hasattr(probs, "model_dump"):
        probs = probs.model_dump()
    return dict(probs) if probs else {}


def summarize_answers(answers: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, ans in answers.items():
        if hasattr(ans, "choice"):
            out[key] = {
                "type": "choice",
                "choice": ans.choice,
                "confidence": getattr(ans, "confidence", None),
                "probabilities": _probs(ans),
            }
        elif hasattr(ans, "noul") and not hasattr(ans, "score"):
            out[key] = {
                "type": "noul",
                "noul": ans.noul,
                "confidence": getattr(ans, "confidence", None),
            }
        elif hasattr(ans, "score"):
            legend = getattr(ans, "legend", None) or {}
            if hasattr(legend, "model_dump"):
                legend = legend.model_dump()
            out[key] = {
                "type": "score",
                "score": ans.score,
                "confidence": getattr(ans, "confidence", None),
                "probabilities": _probs(ans),
                "legend": legend,
            }
        else:
            out[key] = {"raw": str(ans)}
    return out


def shadow_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """Advisory only — must never be wired into live order paths."""
    regime = summary.get("regime_4h") or {}
    noul = (summary.get("grid_buy_ok") or {}).get("noul")
    downside = (summary.get("downside_24h") or {}).get("score")
    adverse = (summary.get("adverse_fill_risk") or {}).get("score")
    probs = regime.get("probabilities") or {}
    top = regime.get("choice")
    top_p = float(probs.get(top, 0.0) or 0.0) if top else 0.0

    reasons: list[str] = []
    allow_new_buys = True

    if noul is not None and float(noul) < NOUL_BUY_BLOCK:
        allow_new_buys = False
        reasons.append(f"grid_buy_ok noul {float(noul):.2f} < block {NOUL_BUY_BLOCK}")
    elif noul is not None and float(noul) < NOUL_BUY_OK:
        reasons.append(
            f"grid_buy_ok noul {float(noul):.2f} soft zone [{NOUL_BUY_BLOCK}, {NOUL_BUY_OK})"
        )

    if top == "BEAR" and top_p >= CHOICE_MIN_PROB:
        allow_new_buys = False
        reasons.append(f"regime BEAR p={top_p:.2f}")

    if downside is not None and float(downside) >= 3.0:
        allow_new_buys = False
        reasons.append(f"downside_24h score {float(downside):.2f} >= 3")

    if adverse is not None and float(adverse) >= 3.0:
        allow_new_buys = False
        reasons.append(f"adverse_fill_risk {float(adverse):.2f} >= 3")

    if top == "BULL" and noul is not None and float(noul) < 0.35:
        reasons.append("inconsistent: BULL regime but very low grid_buy_ok")

    if not reasons:
        reasons.append("no soft blocks fired")

    return {
        "shadow_allow_new_grid_buys": allow_new_buys,
        "reasons": reasons,
        "note": "advisory only — does not place/cancel orders",
    }


def ask_jev(state: dict[str, Any], timeout_sec: Optional[float] = None) -> dict[str, Any]:
    """
    Call TypeSafe System One. Raises on missing key / SDK / API errors.
    Caller must fail-open.
    """
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")

    from typesafe_sdk import TypeSafeClient

    model = _model()
    questions = build_questions(str(state.get("symbol") or "UNKNOWN"))
    client = TypeSafeClient(api_key=api_key)
    to = float(timeout_sec if timeout_sec is not None else _timeout_sec())

    result_box: dict[str, Any] = {}
    err_box: dict[str, Any] = {}

    def _call() -> None:
        try:
            t0 = time.perf_counter()
            response = client.system_one(state=state, questions=questions, model=model)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            answers = getattr(response, "answers", None) or {}
            summary = summarize_answers(answers)
            usage = getattr(response, "usage", None)
            usage_out = usage.model_dump() if hasattr(usage, "model_dump") else usage
            result_box["payload"] = {
                "ok": True,
                "model": getattr(response, "model", model),
                "latency_ms": latency_ms,
                "usage": usage_out,
                "answers": summary,
                "shadow_gate": shadow_gate(summary),
            }
        except Exception as e:  # noqa: BLE001 — surface to waiter
            err_box["err"] = e

    th = threading.Thread(target=_call, name="jev-shadow-ask", daemon=True)
    th.start()
    th.join(timeout=to)
    if th.is_alive():
        raise TimeoutError(f"Jev ask timed out after {to:.1f}s")
    if err_box.get("err") is not None:
        raise err_box["err"]
    if not result_box.get("payload"):
        raise RuntimeError("Jev ask returned empty payload")
    return result_box["payload"]


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    with _lock:
        if _db_ready:
            return
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(_DB_PATH)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jev_shadow_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    model TEXT,
                    latency_ms REAL,
                    state_json TEXT,
                    answers_json TEXT,
                    shadow_gate_json TEXT,
                    spot_action_json TEXT,
                    error TEXT,
                    ok INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jev_shadow_ts_sym ON jev_shadow_log(ts, symbol)"
            )
            conn.commit()
        _db_ready = True
        logger.info(f"[jev_shadow] db ready at {_DB_PATH}")



def log_row(row: dict[str, Any]) -> None:
    """Persist one shadow observation (SQLite + optional JSONL). Never raises to caller."""
    try:
        _ensure_db()
        ts = row.get("ts") or datetime.now(timezone.utc).isoformat()
        symbol = str(row.get("symbol") or "unknown")
        with sqlite3.connect(str(_DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO jev_shadow_log
                (ts, symbol, model, latency_ms, state_json, answers_json,
                 shadow_gate_json, spot_action_json, error, ok)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    symbol,
                    row.get("model"),
                    row.get("latency_ms"),
                    json.dumps(row.get("state"), default=str),
                    json.dumps(row.get("answers"), default=str),
                    json.dumps(row.get("shadow_gate"), default=str),
                    json.dumps(row.get("spot_action"), default=str),
                    row.get("error"),
                    1 if row.get("ok") else 0,
                ),
            )
            conn.commit()
        if os.environ.get("SPOT_JEV_SHADOW_JSONL", "").strip() in ("1", "true", "True", "yes"):
            with open(_JSONL_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[jev_shadow] log_row failed: {e}")


def _should_skip(symbol: str) -> bool:
    now = time.time()
    with _lock:
        last = _last_ask_ts.get(symbol, 0.0)
        if (now - last) < _min_interval():
            return True
        _last_ask_ts[symbol] = now
    return False


def _collect_ctx(symbol: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Fill gaps from live singletons when caller omitted fields."""
    out = dict(ctx or {})
    try:
        if "btc_filter" not in out or not out.get("btc_filter"):
            from trading_engine.spot.btc_master_filter import btc_master_filter

            out["btc_filter"] = btc_master_filter.summary()
            st = btc_master_filter.state
            # Only copy BTC master SMAs/ADX onto BTC itself — never onto alts.
            _sym = str(symbol or "").replace(" ", "").upper()
            if _sym in ("BTC/USDT", "BTCUSDT", "BTC"):
                if out.get("sma_50") is None and getattr(st, "sma_50", 0):
                    out["sma_50"] = st.sma_50
                if out.get("sma_200") is None and getattr(st, "sma_200", 0):
                    out["sma_200"] = st.sma_200
                if out.get("adx_1h") is None and getattr(st, "adx", 0):
                    out["adx_1h"] = st.adx
    except Exception:
        pass
    return out


def _run_one(symbol: str, ctx: dict[str, Any]) -> None:
    if not _inflight.acquire(blocking=False):
        logger.debug(f"[jev_shadow] drop {symbol}: max inflight reached")
        return
    try:
        _run_one_inner(symbol, ctx)
    finally:
        _inflight.release()


def _run_one_inner(symbol: str, ctx: dict[str, Any]) -> None:
    global _success_log_counter
    ts = datetime.now(timezone.utc).isoformat()
    spot_action = {
        "allow_buys_effective": ctx.get("allow_buys_effective"),
        "in_top8": ctx.get("in_top8"),
        "open_buy_notional_usd": ctx.get("open_buy_notional_usd"),
        "open_buy_count": ctx.get("open_buy_count"),
        "open_sell_count": ctx.get("open_sell_count"),
        "grid_action_summary": ctx.get("grid_action_summary"),
        "tick_events_n": ctx.get("tick_events_n"),
        "allocated_usd": ctx.get("allocated_usd"),
        "spot_regime": ctx.get("spot_regime"),
        "buy_levels": ctx.get("buy_levels"),
    }
    try:
        full_ctx = _collect_ctx(symbol, ctx)
        state = build_state(symbol, full_ctx)
        payload = ask_jev(state)
        log_row(
            {
                "ts": ts,
                "symbol": symbol,
                "ok": True,
                "model": payload.get("model"),
                "latency_ms": payload.get("latency_ms"),
                "state": state,
                "answers": payload.get("answers"),
                "shadow_gate": payload.get("shadow_gate"),
                "spot_action": spot_action,
                "error": None,
            }
        )
        _success_log_counter += 1
        _gate = (payload.get("shadow_gate") or {})
        _msg = (
            f"[jev_shadow] logged {symbol} latency={payload.get('latency_ms')}ms "
            f"gate={_gate.get('shadow_allow_new_grid_buys', _gate.get('shadow_allow_new_grid_buys'))} "
            f"db={_DB_PATH}"
        )
        # Periodic info so Railway log drain proves shadow is alive even if DB is ephemeral
        if _success_log_counter == 1 or _success_log_counter % 25 == 0:
            logger.info(_msg + f" (n={_success_log_counter})")
        else:
            logger.debug(_msg)
    except Exception as e:  # noqa: BLE001 — fail-open
        log_row(
            {
                "ts": ts,
                "symbol": symbol,
                "ok": False,
                "model": _model(),
                "latency_ms": None,
                "state": build_state(symbol, ctx) if ctx else {"symbol": symbol},
                "answers": None,
                "shadow_gate": None,
                "spot_action": spot_action,
                "error": f"{type(e).__name__}: {e}",
            }
        )
        logger.debug(f"[jev_shadow] fail-open {symbol}: {e}")


def maybe_shadow_log(symbol: str, ctx: dict[str, Any], *, async_ok: bool = True) -> None:
    """
    Feature-flagged, throttled, fail-open entry point for the Spot runner.
    Must never block trading; errors are logged and swallowed.
    """
    try:
        if not shadow_enabled():
            return
        if not symbol:
            return
        if _should_skip(symbol):
            return
        global _shadow_announced
        if not _shadow_announced:
            _shadow_announced = True
            logger.info(
                f"[jev_shadow] enabled — model={_model()} db={_DB_PATH} "
                f"min_interval={_min_interval()}s timeout={_timeout_sec()}s"
            )
        if async_ok:
            threading.Thread(
                target=_run_one,
                args=(symbol, dict(ctx or {})),
                name=f"jev-shadow-{symbol.replace('/', '_')}",
                daemon=True,
            ).start()
        else:
            _run_one(symbol, dict(ctx or {}))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[jev_shadow] maybe_shadow_log swallowed: {e}")



def _score_shadow(totals: dict[str, Any], *, avg_latency_ms: float | None = None) -> dict[str, Any]:
    """
    Plain 0–100 smoke score for how Jev shadow is doing (ops + signal, not PnL edge).

    Components (equal weight when data exists):
      reliability  — successful TypeSafe asks (ok_rate)
      coverage     — enough logged calls in the window
      discrimination — not stuck always-ALLOW or always-BLOCK
      speed        — median/avg latency vs timeout (soft)

    Edge vs Spot cycles/net is NOT in this score until we join fills.
    """
    rows = int(totals.get("rows") or 0)
    ok = int(totals.get("ok") or 0)
    allow = int(totals.get("allow") or 0)
    block = int(totals.get("block") or 0)
    decided = allow + block
    ok_rate = (ok / rows) if rows else 0.0
    allow_rate = (allow / decided) if decided else None

    # reliability 0–100
    reliability = round(100.0 * ok_rate, 1)

    # coverage: 0 under 5, ramps to 100 by 40 calls in window
    if rows <= 0:
        coverage = 0.0
    elif rows >= 40:
        coverage = 100.0
    else:
        coverage = round(100.0 * (rows / 40.0), 1)

    # discrimination: best near ~40–70% allow; flat 100% allow or 0% allow scores low
    if decided < 5:
        discrimination = 30.0  # too little to judge
        disc_note = "too few gate decisions yet"
    elif allow_rate is None:
        discrimination = 0.0
        disc_note = "no gate decisions"
    else:
        # distance from 0.55 sweet spot; 0.55 → 100, 0.0 or 1.0 → 0
        discrimination = round(max(0.0, 100.0 * (1.0 - abs(allow_rate - 0.55) / 0.55)), 1)
        if allow_rate >= 0.95:
            disc_note = "almost always ALLOW — little selecting yet"
        elif allow_rate <= 0.05:
            disc_note = "almost always BLOCK — very defensive"
        else:
            disc_note = "mixing ALLOW and BLOCK"

    # speed vs 12s timeout: <2s=100, 2–6s linear, >6s low
    timeout = 12.0
    try:
        timeout = float(_timeout_sec())
    except Exception:
        pass
    if avg_latency_ms is None or avg_latency_ms <= 0:
        speed = 50.0
        speed_note = "latency unknown"
    else:
        sec = float(avg_latency_ms) / 1000.0
        if sec <= 2.0:
            speed = 100.0
        elif sec >= timeout:
            speed = 10.0
        else:
            # 2s→100, timeout→10
            speed = round(100.0 - (sec - 2.0) * (90.0 / max(0.1, timeout - 2.0)), 1)
        speed_note = f"avg {avg_latency_ms:.0f}ms"

    if rows <= 0:
        total = 0
        label = "no data"
        summary = "No shadow rows in this window yet."
        components = {
            "reliability": reliability,
            "coverage": coverage,
            "discrimination": discrimination,
            "speed": speed,
        }
    else:
        # weight: reliability 35%, coverage 25%, discrimination 25%, speed 15%
        total = round(
            0.35 * reliability + 0.25 * coverage + 0.25 * discrimination + 0.15 * speed
        )
        total = int(max(0, min(100, total)))
        if total >= 80:
            label = "strong"
        elif total >= 60:
            label = "ok"
        elif total >= 40:
            label = "weak"
        else:
            label = "poor"
        summary = (
            f"Reliability {reliability:.0f}/100, coverage {coverage:.0f}/100, "
            f"selectivity {discrimination:.0f}/100 ({disc_note}), speed {speed:.0f}/100 ({speed_note}). "
            "This is smoke health — not profit edge vs Spot cycles."
        )
        components = {
            "reliability": reliability,
            "coverage": coverage,
            "discrimination": discrimination,
            "speed": speed,
        }

    return {
        "total": total,
        "label": label,
        "summary": summary,
        "components": components,
        "notes": [
            "0–100 smoke score (call health + whether the gate is choosing).",
            "Does not measure profit edge until shadow decisions are joined to Spot cycles/net.",
            disc_note if rows else "no rows",
        ],
    }



def get_shadow_status(*, recent_n: int = 20, hours: float = 24.0) -> dict[str, Any]:
    """
    Aggregate shadow observability for ops digests /api.
    Advisory only — never influences orders. Safe if DB missing/empty.
    """
    enabled = shadow_enabled()
    out: dict[str, Any] = {
        "enabled": enabled,
        "model": _model(),
        "db_path": str(_DB_PATH),
        "jsonl_path": str(_JSONL_PATH),
        "min_interval_sec": _min_interval(),
        "timeout_sec": _timeout_sec(),
        "window_hours": hours,
        "note": (
            "Advisory only — does not place/cancel orders. "
            "SQLite may reset on Railway redeploy unless SPOT_JEV_SHADOW_DIR points at a volume. "
            "TypeSafe console usage remains the external spend/request proof."
        ),
        "totals": {
            "rows": 0,
            "ok": 0,
            "errors": 0,
            "allow": 0,
            "block": 0,
            "no_gate": 0,
        },
        "by_symbol": {},
        "top_block_reasons": [],
        "recent": [],
        "db_present": _DB_PATH.exists(),
        "error": None,
    }
    if not _DB_PATH.exists():
        out["error"] = "shadow db not created yet (no successful/failed log rows on this instance)"
        return out
    try:
        _ensure_db()
        cutoff = datetime.now(timezone.utc).timestamp() - max(1.0, float(hours)) * 3600.0
        with sqlite3.connect(str(_DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ts, symbol, model, latency_ms, shadow_gate_json, error, ok
                FROM jev_shadow_log
                ORDER BY id DESC
                LIMIT 5000
                """
            ).fetchall()
        # Filter by window when ts parses; keep unparsable rows in totals for visibility
        window_rows = []
        for r in rows:
            ts = r["ts"] or ""
            keep = True
            try:
                # Accept Z / offset ISO
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                keep = t.timestamp() >= cutoff
            except Exception:
                keep = True
            if keep:
                window_rows.append(r)

        reason_counts: dict[str, int] = {}
        by_sym: dict[str, dict[str, Any]] = {}
        for r in window_rows:
            sym = r["symbol"] or "unknown"
            slot = by_sym.setdefault(
                sym,
                {"rows": 0, "ok": 0, "errors": 0, "allow": 0, "block": 0, "avg_latency_ms": None},
            )
            slot["rows"] += 1
            out["totals"]["rows"] += 1
            if int(r["ok"] or 0) == 1:
                slot["ok"] += 1
                out["totals"]["ok"] += 1
            else:
                slot["errors"] += 1
                out["totals"]["errors"] += 1

            gate = None
            raw = r["shadow_gate_json"]
            if raw:
                try:
                    gate = json.loads(raw)
                except Exception:
                    gate = None
            allow = None
            if isinstance(gate, dict):
                allow = gate.get("shadow_allow_new_grid_buys")
                reasons = gate.get("reasons") or []
                if allow is False:
                    for reason in reasons:
                        key = str(reason)
                        reason_counts[key] = reason_counts.get(key, 0) + 1

            if allow is True:
                slot["allow"] += 1
                out["totals"]["allow"] += 1
            elif allow is False:
                slot["block"] += 1
                out["totals"]["block"] += 1
            else:
                out["totals"]["no_gate"] += 1

            lat = r["latency_ms"]
            if lat is not None:
                prev = slot.get("_lat_sum", 0.0)
                n = slot.get("_lat_n", 0)
                slot["_lat_sum"] = prev + float(lat)
                slot["_lat_n"] = n + 1

        for sym, slot in by_sym.items():
            n = slot.pop("_lat_n", 0)
            s = slot.pop("_lat_sum", 0.0)
            slot["avg_latency_ms"] = round(s / n, 1) if n else None
            ok = slot["ok"]
            decided = slot["allow"] + slot["block"]
            slot["allow_rate"] = round(slot["allow"] / decided, 3) if decided else None
            slot["ok_rate"] = round(ok / slot["rows"], 3) if slot["rows"] else None

        out["by_symbol"] = dict(sorted(by_sym.items(), key=lambda kv: (-kv[1]["rows"], kv[0])))
        out["top_block_reasons"] = [
            {"reason": k, "count": v}
            for k, v in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        ]
        # Recent newest-first (already DESC from query; re-filter window)
        recent = []
        for r in window_rows[: max(1, min(int(recent_n), 100))]:
            gate = None
            if r["shadow_gate_json"]:
                try:
                    gate = json.loads(r["shadow_gate_json"])
                except Exception:
                    gate = {"raw": r["shadow_gate_json"][:200]}
            recent.append(
                {
                    "ts": r["ts"],
                    "symbol": r["symbol"],
                    "ok": bool(r["ok"]),
                    "latency_ms": r["latency_ms"],
                    "model": r["model"],
                    "allow": (gate or {}).get("shadow_allow_new_grid_buys") if isinstance(gate, dict) else None,
                    "reasons": (gate or {}).get("reasons") if isinstance(gate, dict) else None,
                    "error": r["error"],
                }
            )
        out["recent"] = recent
        decided = out["totals"]["allow"] + out["totals"]["block"]
        out["totals"]["allow_rate"] = (
            round(out["totals"]["allow"] / decided, 3) if decided else None
        )
        out["totals"]["ok_rate"] = (
            round(out["totals"]["ok"] / out["totals"]["rows"], 3) if out["totals"]["rows"] else None
        )
        # Average latency across per-symbol slots when present
        _lats = [
            float(s["avg_latency_ms"])
            for s in (out.get("by_symbol") or {}).values()
            if isinstance(s, dict) and s.get("avg_latency_ms") is not None
        ]
        _avg_lat = (sum(_lats) / len(_lats)) if _lats else None
        out["score"] = _score_shadow(out["totals"], avg_latency_ms=_avg_lat)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["score"] = {
            "total": 0,
            "label": "error",
            "summary": out["error"],
            "components": {},
            "notes": ["status aggregation failed"],
        }
    if "score" not in out:
        out["score"] = _score_shadow(out.get("totals") or {}, avg_latency_ms=None)
    # Predictive accuracy (after-the-fact vs later marks) — advisory only
    try:
        out["accuracy"] = compute_predictive_accuracy()
    except Exception as e:  # noqa: BLE001
        out["accuracy"] = {
            "hit_rate_pct": None,
            "graded": 0,
            "correct": 0,
            "incorrect": 0,
            "best": None,
            "worst": None,
            "recommendation": {
                "status": "wait",
                "reason": f"accuracy grader failed: {type(e).__name__}",
                "bars_used": _ACCURACY_BARS,
            },
            "digest_lines": [
                "Jev accuracy: unavailable (grader error).",
                "Best/worst: n/a.",
                "What to do: Wait — grader had a problem; try again later.",
            ],
            "note": f"{type(e).__name__}: {e}",
        }
    return out


# ── Predictive accuracy (roadmap step 2) ─────────────────────────────────────
# Grade only after the horizon has elapsed. Prefer later SQLite shadow marks for
# the same symbol (no fill invention). Optional Bybit public kline fallback.
# NEVER wired into allow_buys.

REGIME_HORIZON_SEC = 4 * 3600
ALLOW_HORIZON_SEC = 4 * 3600
REGIME_MOVE_PCT = 0.5  # |ret| <= 0.5% => RANGE; else BULL/BEAR by sign
MARK_MATCH_TOLERANCE_SEC = 45 * 60  # accept a mark within ±45m of target horizon
DEFAULT_FEE_ROUNDTRIP = 0.0015  # 0.15% if state lacks fee_rate_roundtrip
MIN_GRADED_FOR_REC = 30
MIN_SHADOW_DAYS_FOR_REC = 3
HIT_RATE_ADOPT_PCT = 55.0
HIT_RATE_DROP_PCT = 45.0

_ACCURACY_BARS = {
    "regime_horizon_hours": REGIME_HORIZON_SEC / 3600.0,
    "allow_horizon_hours": ALLOW_HORIZON_SEC / 3600.0,
    "regime_move_pct": REGIME_MOVE_PCT,
    "min_graded": MIN_GRADED_FOR_REC,
    "min_shadow_days": MIN_SHADOW_DAYS_FOR_REC,
    "adopt_hit_rate_pct": HIT_RATE_ADOPT_PCT,
    "drop_hit_rate_pct": HIT_RATE_DROP_PCT,
    "price_source": "later_shadow_marks_then_bybit_kline",
    "advisory_only": True,
}


def _parse_iso_ts(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    except Exception:
        return None


def _actual_regime(ret_pct: float, threshold_pct: float = REGIME_MOVE_PCT) -> str:
    if ret_pct > threshold_pct:
        return "BULL"
    if ret_pct < -threshold_pct:
        return "BEAR"
    return "RANGE"


def _bybit_spot_symbol(symbol: str) -> str:
    return str(symbol or "").replace("/", "").replace("-", "").upper()


def _fetch_bybit_mark_near(symbol: str, target: datetime) -> Optional[float]:
    """Public Bybit spot 15m kline close nearest to target. Best-effort; never raises."""
    try:
        import urllib.parse
        import urllib.request

        sym = _bybit_spot_symbol(symbol)
        if not sym.endswith("USDT"):
            return None
        # 15-minute kline window around target
        start_ms = int((target.timestamp() - 30 * 60) * 1000)
        end_ms = int((target.timestamp() + 30 * 60) * 1000)
        q = urllib.parse.urlencode(
            {
                "category": "spot",
                "symbol": sym,
                "interval": "15",
                "start": start_ms,
                "end": end_ms,
                "limit": 10,
            }
        )
        url = f"https://api.bybit.com/v5/market/kline?{q}"
        req = urllib.request.Request(url, headers={"User-Agent": "aios-jev-shadow-grade/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if str(payload.get("retCode")) not in ("0", "0.0"):
            return None
        rows = ((payload.get("result") or {}).get("list")) or []
        # Bybit returns newest-first: [start, open, high, low, close, ...]
        best = None
        best_dt = None
        for row in rows:
            try:
                start = int(row[0]) / 1000.0
                close = float(row[4])
            except Exception:
                continue
            dt = datetime.fromtimestamp(start, tz=timezone.utc)
            if best is None or abs((dt - target).total_seconds()) < abs(
                (best_dt - target).total_seconds()
            ):
                best, best_dt = close, dt
        if best is None or best_dt is None:
            return None
        if abs((best_dt - target).total_seconds()) > MARK_MATCH_TOLERANCE_SEC:
            return None
        return float(best)
    except Exception:
        return None


def _future_mark(
    timeline: dict[str, list[tuple[datetime, float]]],
    symbol: str,
    call_ts: datetime,
    horizon_sec: float,
    *,
    allow_bybit: bool = True,
) -> tuple[Optional[float], Optional[datetime], str]:
    """
    Return (mark, observed_ts, source) near call_ts + horizon.
    Prefer later SQLite shadow marks; optional Bybit public kline fallback.
    """
    target = call_ts.timestamp() + float(horizon_sec)
    cands = timeline.get(symbol) or []
    best: Optional[tuple[float, datetime, float]] = None  # abs_delta, ts, mark
    for ts, mark in cands:
        delta = ts.timestamp() - target
        # Prefer marks at/after horizon, but allow ±tolerance
        if abs(delta) > MARK_MATCH_TOLERANCE_SEC:
            continue
        score = abs(delta) + (0.0 if delta >= -60 else 30.0)  # slight preference for at/after
        if best is None or score < best[0]:
            best = (score, ts, mark)
    if best is not None:
        return best[2], best[1], "shadow_mark"
    if allow_bybit:
        tgt = datetime.fromtimestamp(target, tz=timezone.utc)
        m = _fetch_bybit_mark_near(symbol, tgt)
        if m is not None:
            return m, tgt, "bybit_kline"
    return None, None, "none"


def _call_card(
    *,
    symbol: str,
    call_ts: datetime,
    call: str,
    outcome: str,
    right: bool,
    ret_pct: float,
    source: str,
    kind: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ts": call_ts.isoformat(),
        "kind": kind,
        "call": call,
        "outcome": outcome,
        "right": bool(right),
        "ret_pct": round(float(ret_pct), 4),
        "price_source": source,
    }


def _recommendation(
    *,
    graded: int,
    correct: int,
    incorrect: int,
    hit_rate_pct: Optional[float],
    shadow_days: float,
    false_allow_rate: Optional[float],
    baseline_down_rate: Optional[float],
    block_correct_rate: Optional[float],
    allow_n: int,
    block_n: int,
) -> dict[str, Any]:
    """
    Adopt / wait / drop / keep_shadow_only — advisory only, never auto-wires.
    """
    bars = dict(_ACCURACY_BARS)
    if graded < MIN_GRADED_FOR_REC or shadow_days < MIN_SHADOW_DAYS_FOR_REC:
        return {
            "status": "wait",
            "reason": (
                f"still collecting — {graded} finished calls "
                f"(need {MIN_GRADED_FOR_REC}) over {shadow_days:.1f} days "
                f"(need {MIN_SHADOW_DAYS_FOR_REC})"
            ),
            "bars_used": bars,
        }

    hr = float(hit_rate_pct if hit_rate_pct is not None else 0.0)
    false_allow_hurts = (
        false_allow_rate is not None
        and false_allow_rate >= 0.60
        and allow_n >= 10
    )
    false_allow_ok = True
    if false_allow_rate is not None and baseline_down_rate is not None and allow_n >= 10:
        # Not worse than market base rate of down periods (small slack)
        false_allow_ok = false_allow_rate <= (baseline_down_rate + 0.05)
    block_ok = True
    if block_n >= 5 and block_correct_rate is not None:
        block_ok = block_correct_rate >= 0.50

    if hr < HIT_RATE_DROP_PCT or false_allow_hurts:
        why = (
            f"hit rate {hr:.1f}% < {HIT_RATE_DROP_PCT:.0f}%"
            if hr < HIT_RATE_DROP_PCT
            else f"too many bad 'buy ok' calls ({false_allow_rate:.0%})"
        )
        return {
            "status": "drop",
            "reason": f"{why} — not good enough; drop or retune (still advisory)",
            "bars_used": bars,
        }

    if (
        hr >= HIT_RATE_ADOPT_PCT
        and false_allow_ok
        and block_ok
        and not false_allow_hurts
    ):
        return {
            "status": "adopt",
            "reason": (
                f"right {hr:.0f}% of the time on {graded} calls over {shadow_days:.1f} days; "
                "ready to discuss turning on — still needs your yes"
            ),
            "bars_used": bars,
        }

    return {
        "status": "keep_shadow_only",
        "reason": (
            f"right {hr:.0f}% of the time — mixed so far; keep watching in shadow only"
        ),
        "bars_used": bars,
    }


def _digest_lines(accuracy: dict[str, Any]) -> list[str]:
    """Plain-English lines for the morning briefing (Nasir-facing)."""
    graded = int(accuracy.get("graded") or 0)
    correct = int(accuracy.get("correct") or 0)
    incorrect = int(accuracy.get("incorrect") or 0)
    hr = accuracy.get("hit_rate_pct")
    rec = accuracy.get("recommendation") or {}
    status = str(rec.get("status") or "wait").lower()
    best = accuracy.get("best")
    worst = accuracy.get("worst")

    if graded <= 0 or hr is None:
        line1 = "Right so far: not enough finished calls yet to score."
    else:
        line1 = f"Right so far: {correct} out of {graded} calls ({hr:.0f}%)."

    def _fmt(card: Any, label: str) -> str:
        if not isinstance(card, dict):
            return f"{label}: none yet."
        ok = "right" if card.get("right") else "wrong"
        ret = card.get("ret_pct")
        ret_s = f", price moved {ret:+.1f}%" if isinstance(ret, (int, float)) else ""
        kind = str(card.get("kind") or "call")
        call = str(card.get("call") or "")
        sym = str(card.get("symbol") or "?")
        return f"{label}: {sym} said {call} ({kind}) — {ok}{ret_s}."

    line2 = f"{_fmt(best, 'Best call')} {_fmt(worst, 'Worst call')}"

    what = {
        "wait": "What to do: Wait — still collecting results (need a few days).",
        "keep_shadow_only": "What to do: Looking okay — keep watching in shadow only.",
        "adopt": (
            "What to do: Ready to discuss turning it on "
            "(still needs your yes; nothing changes live until you say so)."
        ),
        "drop": "What to do: Not working well — drop or retune.",
    }.get(status, f"What to do: {status}.")
    line3 = what
    return [line1, line2, line3]


def compute_predictive_accuracy(
    *,
    lookback_hours: float = 24.0 * 21,
    allow_bybit_fallback: bool = True,
    max_bybit_lookups: int = 40,
) -> dict[str, Any]:
    """
    After-the-fact grading of regime_4h + ALLOW/BLOCK vs later marks.

    Rules (honest / conservative):
      • Only grade rows with ok=1, a numeric mark, and horizon fully elapsed.
      • regime_4h: BULL if next ~4h ret > +0.5%; BEAR if < −0.5%; else RANGE.
      • ALLOW: right if mark ret over ~4h >= 0 (flat-or-better vs mark; fee not in verdict).
      • BLOCK: right if mark ret over ~4h < 0 (avoided a red / down period).
      • Prices from later shadow marks for the same symbol; Bybit public kline fallback.
      • Sample size reported; insufficient data → null hit_rate + wait recommendation.
    """
    note_parts = [
        "Grades regime_4h and ALLOW/BLOCK only after horizon elapsed.",
        f"RANGE band = |4h ret| ≤ {REGIME_MOVE_PCT}%.",
        "Prices: later SQLite shadow marks preferred; Bybit public kline fallback.",
        "Mark-vs-mark (not fill-vs-fill); advisory only — never wired into allow_buys.",
    ]
    empty = {
        "hit_rate_pct": None,
        "graded": 0,
        "correct": 0,
        "incorrect": 0,
        "best": None,
        "worst": None,
        "by_kind": {},
        "false_allow_rate": None,
        "baseline_down_rate": None,
        "block_correct_rate": None,
        "shadow_days": 0.0,
        "recommendation": {
            "status": "wait",
            "reason": "too few graded calls",
            "bars_used": dict(_ACCURACY_BARS),
        },
        "digest_lines": [],
        "note": " ".join(note_parts),
        "bars": dict(_ACCURACY_BARS),
    }

    if not _DB_PATH.exists():
        empty["note"] = "shadow db missing — " + empty["note"]
        empty["digest_lines"] = _digest_lines(empty)
        return empty

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - max(1.0, float(lookback_hours)) * 3600.0
    bybit_lookups = 0

    try:
        _ensure_db()
        with sqlite3.connect(str(_DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, ts, symbol, ok, state_json, answers_json, shadow_gate_json
                FROM jev_shadow_log
                WHERE ok = 1
                ORDER BY id ASC
                LIMIT 20000
                """
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        empty["note"] = f"db read failed: {type(e).__name__}: {e}"
        empty["digest_lines"] = _digest_lines(empty)
        return empty

    # Build mark timeline from all ok rows (including those outside lookback)
    timeline: dict[str, list[tuple[datetime, float]]] = {}
    parsed: list[dict[str, Any]] = []
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    for r in rows:
        ts = _parse_iso_ts(r["ts"])
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts
        try:
            state = json.loads(r["state_json"] or "{}")
        except Exception:
            state = {}
        mark = state.get("mark_price")
        try:
            mark_f = float(mark)
            if mark_f != mark_f or mark_f <= 0:
                continue
        except (TypeError, ValueError):
            continue
        sym = str(r["symbol"] or state.get("symbol") or "unknown")
        timeline.setdefault(sym, []).append((ts, mark_f))

        # Only consider calls inside lookback window for grading candidates
        if ts.timestamp() < cutoff:
            continue
        try:
            answers = json.loads(r["answers_json"] or "{}")
        except Exception:
            answers = {}
        try:
            gate = json.loads(r["shadow_gate_json"] or "{}")
        except Exception:
            gate = {}
        fee = state.get("fee_rate_roundtrip")
        try:
            fee_f = float(fee) if fee is not None and fee != "unknown" else DEFAULT_FEE_ROUNDTRIP
        except (TypeError, ValueError):
            fee_f = DEFAULT_FEE_ROUNDTRIP
        parsed.append(
            {
                "id": r["id"],
                "ts": ts,
                "symbol": sym,
                "mark": mark_f,
                "answers": answers if isinstance(answers, dict) else {},
                "gate": gate if isinstance(gate, dict) else {},
                "fee": fee_f,
            }
        )

    for sym in timeline:
        timeline[sym].sort(key=lambda x: x[0])

    shadow_days = 0.0
    if first_ts and last_ts:
        shadow_days = max(0.0, (last_ts - first_ts).total_seconds() / 86400.0)

    graded_cards: list[dict[str, Any]] = []
    down_periods = 0
    down_period_n = 0
    false_allows = 0
    allow_graded = 0
    block_correct = 0
    block_graded = 0
    by_kind: dict[str, dict[str, int]] = {
        "regime_4h": {"correct": 0, "incorrect": 0},
        "allow_block": {"correct": 0, "incorrect": 0},
    }

    def _resolve_future(sym: str, call_ts: datetime, horizon: float) -> tuple[Optional[float], Optional[datetime], str]:
        nonlocal bybit_lookups
        mark, obs, src = _future_mark(
            timeline, sym, call_ts, horizon, allow_bybit=False
        )
        if mark is not None:
            return mark, obs, src
        if allow_bybit_fallback and bybit_lookups < max_bybit_lookups:
            bybit_lookups += 1
            return _future_mark(timeline, sym, call_ts, horizon, allow_bybit=True)
        return None, None, "none"

    for item in parsed:
        call_ts: datetime = item["ts"]
        sym = item["symbol"]
        mark0 = float(item["mark"])
        age = (now - call_ts).total_seconds()

        # ── regime_4h ──────────────────────────────────────────────
        if age >= REGIME_HORIZON_SEC:
            regime_ans = (item["answers"].get("regime_4h") or {})
            choice = regime_ans.get("choice")
            if choice in ("BULL", "BEAR", "RANGE"):
                fut, _, src = _resolve_future(sym, call_ts, REGIME_HORIZON_SEC)
                if fut is not None and mark0 > 0:
                    ret = (fut / mark0 - 1.0) * 100.0
                    actual = _actual_regime(ret)
                    right = choice == actual
                    card = _call_card(
                        symbol=sym,
                        call_ts=call_ts,
                        call=str(choice),
                        outcome=f"actual_{actual}",
                        right=right,
                        ret_pct=ret,
                        source=src,
                        kind="regime_4h",
                    )
                    graded_cards.append(card)
                    bucket = by_kind["regime_4h"]
                    bucket["correct" if right else "incorrect"] += 1

        # ── ALLOW / BLOCK ──────────────────────────────────────────
        if age >= ALLOW_HORIZON_SEC:
            allow = item["gate"].get("shadow_allow_new_grid_buys")
            if allow is True or allow is False:
                fut, _, src = _resolve_future(sym, call_ts, ALLOW_HORIZON_SEC)
                if fut is not None and mark0 > 0:
                    ret = (fut / mark0 - 1.0) * 100.0
                    fee_pct = float(item["fee"]) * 100.0
                    down_period_n += 1
                    if ret < 0:
                        down_periods += 1
                    if allow is True:
                        # Flat-or-better vs mark after horizon (fee noted in limitations)
                        right = ret >= 0.0
                        outcome = "flat_or_better" if right else "red_after_allow"
                        allow_graded += 1
                        if not right:
                            false_allows += 1
                        call_label = "ALLOW"
                    else:
                        right = ret < 0.0
                        outcome = "avoided_red" if right else "missed_up"
                        block_graded += 1
                        if right:
                            block_correct += 1
                        call_label = "BLOCK"
                    card = _call_card(
                        symbol=sym,
                        call_ts=call_ts,
                        call=call_label,
                        outcome=outcome,
                        right=right,
                        ret_pct=ret,
                        source=src,
                        kind="allow_block",
                    )
                    graded_cards.append(card)
                    bucket = by_kind["allow_block"]
                    bucket["correct" if right else "incorrect"] += 1

    correct = sum(1 for c in graded_cards if c.get("right"))
    incorrect = sum(1 for c in graded_cards if not c.get("right"))
    graded = correct + incorrect
    hit_rate = round(100.0 * correct / graded, 1) if graded else None

    def _rank_key(card: dict[str, Any], want_right: bool) -> float:
        # Larger = more extreme confirmation (best) or more extreme miss (worst)
        ret = abs(float(card.get("ret_pct") or 0.0))
        return ret if bool(card.get("right")) == want_right else -1.0

    best = None
    worst = None
    if graded:
        rights = [c for c in graded_cards if c.get("right")]
        wrongs = [c for c in graded_cards if not c.get("right")]
        if rights:
            best = max(rights, key=lambda c: abs(float(c.get("ret_pct") or 0.0)))
        if wrongs:
            worst = max(wrongs, key=lambda c: abs(float(c.get("ret_pct") or 0.0)))
        elif rights:
            # No wrongs — still report least-impressive correct as worst? skip
            worst = None

    false_allow_rate = (
        round(false_allows / allow_graded, 3) if allow_graded else None
    )
    baseline_down_rate = (
        round(down_periods / down_period_n, 3) if down_period_n else None
    )
    block_correct_rate = (
        round(block_correct / block_graded, 3) if block_graded else None
    )

    rec = _recommendation(
        graded=graded,
        correct=correct,
        incorrect=incorrect,
        hit_rate_pct=hit_rate,
        shadow_days=shadow_days,
        false_allow_rate=false_allow_rate,
        baseline_down_rate=baseline_down_rate,
        block_correct_rate=block_correct_rate,
        allow_n=allow_graded,
        block_n=block_graded,
    )

    if bybit_lookups:
        note_parts.append(f"Bybit kline lookups used: {bybit_lookups}.")
    if graded == 0:
        note_parts.append(
            "Insufficient to grade — need ok rows with marks whose 4h horizon has elapsed "
            "and a later mark (or Bybit kline) for the same symbol."
        )

    out = {
        "hit_rate_pct": hit_rate,
        "graded": graded,
        "correct": correct,
        "incorrect": incorrect,
        "best": best,
        "worst": worst,
        "by_kind": by_kind,
        "false_allow_rate": false_allow_rate,
        "baseline_down_rate": baseline_down_rate,
        "block_correct_rate": block_correct_rate,
        "shadow_days": round(shadow_days, 2),
        "recommendation": rec,
        "digest_lines": [],
        "note": " ".join(note_parts),
        "bars": dict(_ACCURACY_BARS),
    }
    out["digest_lines"] = _digest_lines(out)
    return out


def db_path() -> Path:
    return _DB_PATH


def jsonl_path() -> Path:
    return _JSONL_PATH

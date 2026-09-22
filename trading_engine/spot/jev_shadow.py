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
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def db_path() -> Path:
    return _DB_PATH


def jsonl_path() -> Path:
    return _JSONL_PATH

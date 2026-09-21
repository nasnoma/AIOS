#!/usr/bin/env python3
"""Shadow-only TypeSafe System One (Jev) smoke for Spot-shaped judgments.

Hard rules:
- Never influence live orders from this script.
- Never pass TYPESAFE_API_KEY on the command line (shell history leak).
  export TYPESAFE_API_KEY=... then run with the venv python.

Acceptance for any future live gate: cycles + net must be flat-or-better vs control.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")
NOUL_BUY_OK = 0.60
NOUL_BUY_BLOCK = 0.40
CHOICE_MIN_PROB = 0.55


def build_state() -> dict[str, Any]:
    price = 76200.0
    sma_50 = 76500.0
    sma_200 = 74100.0
    adx_1h = 32.5
    return {
        "fixture": True,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": "BTCUSDT",
        "venue": "bybit_spot",
        "mark_price": price,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "price_vs_sma50_pct": round((price / sma_50 - 1.0) * 100.0, 3),
        "sma50_vs_sma200_pct": round((sma_50 / sma_200 - 1.0) * 100.0, 3),
        "adx_1h": adx_1h,
        "adx_trend_strength": "strong" if adx_1h >= 25 else "weak",
        "realized_vol_48h_pct": 4.2,
        "btc_master_filter": "unknown",
        "spot_inventory_usd": "unknown",
        "open_buy_notional_usd": "unknown",
        "fee_rate_roundtrip": "unknown",
        "notes": (
            "Judge only from fields present. "
            "Treat unknown as missing evidence, not neutral. "
            "Do not assume order-book depth or funding."
        ),
    }


def build_questions() -> dict[str, Any]:
    return {
        "regime_4h": Choice(
            instructions=(
                "Classify the likely BTCUSDT regime over the next ~4 hours "
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
                "Expected downside severity for BTCUSDT over the next ~24 hours. "
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
                "Probability that resting NEW Spot grid BUY limits on BTCUSDT in the next "
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


def main() -> int:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print(
            "ERROR: TYPESAFE_API_KEY not set.\n"
            "Fix: export TYPESAFE_API_KEY in the environment (never put it on the CLI).",
            file=sys.stderr,
        )
        return 1

    client = TypeSafeClient(api_key=api_key)
    state = build_state()
    questions = build_questions()

    print(f"Invoking System One model={MODEL} (shadow-only)...")
    t0 = time.perf_counter()
    response = client.system_one(state=state, questions=questions, model=MODEL)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    answers = getattr(response, "answers", None) or {}
    summary = summarize_answers(answers)
    gate = shadow_gate(summary)

    usage = getattr(response, "usage", None)
    usage_out = usage.model_dump() if hasattr(usage, "model_dump") else usage

    payload = {
        "ok": True,
        "model": getattr(response, "model", MODEL),
        "latency_ms": latency_ms,
        "usage": usage_out,
        "state_fixture": True,
        "answers": summary,
        "shadow_gate": gate,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

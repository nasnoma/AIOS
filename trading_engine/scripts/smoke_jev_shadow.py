#!/usr/bin/env python3
"""Unit-level smoke for Spot Jev shadow logger.

- Without TYPESAFE_API_KEY: dry-run state builder + DB write of an error/dry row (no API).
- With TYPESAFE_API_KEY in env: one real ask + one log row.
Never puts the key on the CLI. Never influences orders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_engine.spot.jev_shadow import (  # noqa: E402
    ask_jev,
    build_state,
    db_path,
    log_row,
    shadow_gate,
    summarize_answers,
    _model,
)


def _fixture_ctx() -> dict:
    return {
        "price": 76200.0,
        "sma_50": 76500.0,
        "sma_200": 74100.0,
        "adx_1h": 32.5,
        "realized_vol_48h_pct": 4.2,
        "spot_regime": "RANGE",
        "inventory_usd": 120.0,
        "inventory_units": 0.00157,
        "avg_cost_basis": 75000.0,
        "open_buy_notional_usd": 80.0,
        "open_buy_count": 2,
        "open_sell_count": 1,
        "allocated_usd": 350.0,
        "in_top8": True,
        "allow_buys_effective": True,
        "fee_rate_roundtrip": 0.0015,
        "range_24h_pct": 3.1,
        "btc_filter": {
            "regime": "RANGE",
            "adx": 28.0,
            "btc_1h_change_pct": -0.4,
            "btc_4h_change_pct": 0.8,
            "btc_24h_change_pct": 1.2,
            "is_safe_for_alt_buys": True,
            "is_flash_dump": False,
            "safety_reason": "BTC conditions healthy",
        },
        "grid_action_summary": "smoke fixture — no live grid",
        "tick_events_n": 0,
        "buy_levels": 5,
    }


def main() -> int:
    symbol = "BTC/USDT"
    ctx = _fixture_ctx()
    state = build_state(symbol, ctx)
    assert state["fixture"] is False
    assert state["mark_price"] == 76200.0
    assert "unknown" not in str(state["sma_50"])

    # Unknown marking
    sparse = build_state("ETH/USDT", {"price": 2500.0})
    assert sparse["sma_50"] == "unknown"
    assert sparse["btc_master_filter"] == "unknown" or isinstance(sparse["btc_master_filter"], dict)

    api_key = os.environ.get("TYPESAFE_API_KEY")
    spot_action = {
        "allow_buys_effective": True,
        "in_top8": True,
        "open_buy_notional_usd": 80.0,
        "grid_action_summary": "smoke",
    }

    if not api_key:
        log_row(
            {
                "ts": state["as_of_utc"],
                "symbol": symbol,
                "ok": False,
                "model": _model(),
                "latency_ms": None,
                "state": state,
                "answers": None,
                "shadow_gate": None,
                "spot_action": spot_action,
                "error": "dry_run_no_TYPESAFE_API_KEY",
            }
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry_run",
                    "db": str(db_path()),
                    "state_keys": sorted(state.keys()),
                    "note": "No TYPESAFE_API_KEY — wrote dry-run error row only",
                },
                indent=2,
            )
        )
        return 0

    # Live ask (shadow only)
    payload = ask_jev(state)
    log_row(
        {
            "ts": state["as_of_utc"],
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

    # Confirm last row readable
    with sqlite3.connect(str(db_path())) as conn:
        row = conn.execute(
            "SELECT ts, symbol, model, latency_ms, ok, error FROM jev_shadow_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

    print(
        json.dumps(
            {
                "ok": True,
                "mode": "live_shadow",
                "db": str(db_path()),
                "last_row": {
                    "ts": row[0],
                    "symbol": row[1],
                    "model": row[2],
                    "latency_ms": row[3],
                    "ok": row[4],
                    "error": row[5],
                },
                "shadow_gate": payload.get("shadow_gate"),
                "answers_keys": sorted((payload.get("answers") or {}).keys()),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

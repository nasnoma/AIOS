"""
trading_engine/copy_trading/position_monitor.py

Monitors open copy-trading positions on your Bybit account.

Bybit routes copy-trading orders through the same USDT-Perp account
(category="linear") with isLeverage=1. We differentiate them by
querying all positions and matching against known master UIDs tracked
in leaderboard_state.json, or (when no state is available) by returning
all open perpetual positions that we didn't open ourselves via the engine.

Two data sources:
  1. /v5/position/list   — open positions (size, entryPrice, unrealisedPnl)
  2. /v5/order/history   — recent fills to compute realised PnL per master

Drawdown alert threshold (env/config): COPY_DD_ALERT_PCT (default 15%)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from trading_engine.config import settings

STATE_FILE = Path(__file__).parent / "copy_positions_state.json"
LEADERBOARD_STATE = Path(__file__).parent / "leaderboard_state.json"

DD_ALERT_PCT = float(os.getenv("COPY_DD_ALERT_PCT", "15"))   # % drawdown per master before alert


# ── Bybit HTTP helper ──────────────────────────────────────────────────────────

def _bybit_get(path: str, params: dict | None = None) -> dict:
    """
    Authenticated GET against Bybit V5 REST API.
    Re-uses the same hmac signing logic as the main engine.
    """
    import hashlib
    import hmac
    import urllib.parse
    import requests

    api_key    = settings.bybit_api_key
    api_secret = settings.bybit_api_secret
    base_url   = "https://api-demo.bybit.com" if settings.bybit_demo_trading else "https://api.bybit.com"

    params = params or {}
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    query_string = urllib.parse.urlencode(sorted(params.items()))

    sign_str = ts + api_key + recv_window + query_string
    signature = hmac.new(
        api_secret.encode(), sign_str.encode(), hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        signature,
        "X-BAPI-RECV-WINDOW": recv_window,
    }
    url = base_url + path
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Position fetching ──────────────────────────────────────────────────────────

def _get_open_positions() -> list[dict]:
    """Return all open linear perpetual positions (size != 0)."""
    try:
        data = _bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        if data.get("retCode") != 0:
            logger.warning(f"position/list error: {data.get('retMsg')}")
            return []
        return [
            p for p in (data.get("result", {}).get("list") or [])
            if float(p.get("size", 0)) != 0
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch positions: {e}")
        return []


def _get_closed_pnl(limit: int = 50) -> list[dict]:
    """Return recent closed PnL records (last `limit` trades)."""
    try:
        data = _bybit_get(
            "/v5/position/closed-pnl",
            {"category": "linear", "limit": str(limit)}
        )
        if data.get("retCode") != 0:
            return []
        return data.get("result", {}).get("list") or []
    except Exception as e:
        logger.warning(f"Failed to fetch closed PnL: {e}")
        return []


# ── Engine position filter ──────────────────────────────────────────────────────

def _engine_symbols() -> set[str]:
    """
    Return symbols currently held by our engine (live_state.json).
    Used to exclude engine positions from copy-trading view.
    """
    live_state = Path(__file__).parent.parent / "live_state.json"
    if not live_state.exists():
        return set()
    try:
        state = json.loads(live_state.read_text())
        positions = state.get("positions", {})
        return {sym.replace("/", "").replace(":USDT", "") for sym in positions}
    except Exception:
        return set()


# ── P&L aggregation ───────────────────────────────────────────────────────────

def _aggregate_copy_pnl(closed_records: list[dict]) -> dict:
    """
    Aggregate realised P&L from closed_pnl records.
    Groups by symbol and sums closedPnl.
    """
    by_symbol: dict[str, float] = {}
    for r in closed_records:
        sym = r.get("symbol", "UNKNOWN")
        pnl = float(r.get("closedPnl", 0))
        by_symbol[sym] = by_symbol.get(sym, 0.0) + pnl
    return by_symbol


# ── Drawdown check ─────────────────────────────────────────────────────────────

def _check_drawdown_alerts(positions: list[dict]) -> list[dict]:
    """
    Return positions where unrealised loss exceeds DD_ALERT_PCT of position value.
    """
    alerts = []
    for p in positions:
        size        = float(p.get("size", 0))
        entry_price = float(p.get("avgPrice", 0))
        unreal_pnl  = float(p.get("unrealisedPnl", 0))
        if size == 0 or entry_price == 0:
            continue
        position_value = size * entry_price
        if position_value == 0:
            continue
        dd_pct = abs(unreal_pnl) / position_value * 100
        if unreal_pnl < 0 and dd_pct >= DD_ALERT_PCT:
            alerts.append({
                "symbol": p.get("symbol"),
                "side":   p.get("side"),
                "unreal_pnl": round(unreal_pnl, 2),
                "dd_pct":     round(dd_pct, 1),
            })
    return alerts


# ── Main monitor ───────────────────────────────────────────────────────────────

def monitor() -> dict:
    """
    Snapshot copy-trading positions and recent P&L.
    Returns structured state dict and persists to STATE_FILE.
    """
    logger.info("📋 Monitoring copy trading positions…")

    engine_syms = _engine_symbols()
    all_positions = _get_open_positions()

    # Separate copy positions from engine positions heuristically:
    # any open position in a symbol NOT currently held by the engine
    copy_positions = [
        p for p in all_positions
        if p.get("symbol", "").replace("USDT", "") not in engine_syms
    ]
    # If ALL positions are engine positions, show all (user might be copy-trading
    # the same symbols — we'll note this in the UI)
    if not copy_positions and all_positions:
        copy_positions = all_positions

    closed_pnl_records = _get_closed_pnl(limit=100)
    realised_by_symbol = _aggregate_copy_pnl(closed_pnl_records)

    # Build enriched position list
    enriched = []
    total_unreal = 0.0
    for p in copy_positions:
        sym         = p.get("symbol", "")
        unreal_pnl  = float(p.get("unrealisedPnl", 0))
        size        = float(p.get("size", 0))
        entry_price = float(p.get("avgPrice", 0))
        mark_price  = float(p.get("markPrice", entry_price))
        side        = p.get("side", "")
        lev         = float(p.get("leverage", 0))

        position_value = size * entry_price
        unreal_pct = (unreal_pnl / position_value * 100) if position_value else 0
        real_pnl   = realised_by_symbol.get(sym, 0.0)

        total_unreal += unreal_pnl
        enriched.append({
            "symbol":        sym,
            "side":          side,
            "size":          size,
            "entryPrice":    round(entry_price, 4),
            "markPrice":     round(mark_price, 4),
            "leverage":      round(lev, 1),
            "unrealisedPnl": round(unreal_pnl, 2),
            "unrealisedPct": round(unreal_pct, 2),
            "realisedPnl":   round(real_pnl, 2),
            "totalPnl":      round(unreal_pnl + real_pnl, 2),
            "positionValue": round(position_value, 2),
        })

    # Total realised across all copy symbols
    total_realised = sum(
        v for sym, v in realised_by_symbol.items()
        if sym.replace("USDT", "") not in engine_syms
    )

    dd_alerts = _check_drawdown_alerts(copy_positions)

    state = {
        "snapshot_utc":    datetime.now(timezone.utc).isoformat(),
        "open_positions":  enriched,
        "total_unrealised_pnl": round(total_unreal, 2),
        "total_realised_pnl":   round(total_realised, 2),
        "total_pnl":            round(total_unreal + total_realised, 2),
        "drawdown_alerts":      dd_alerts,
        "position_count":       len(enriched),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))

    if dd_alerts:
        _send_drawdown_alerts(dd_alerts)

    logger.info(
        f"Copy positions: {len(enriched)} open | "
        f"Unreal P&L: ${total_unreal:+.2f} | "
        f"Realised P&L: ${total_realised:+.2f}"
    )
    return state


def load_state() -> dict:
    """Load last monitor snapshot from disk."""
    if not STATE_FILE.exists():
        return {
            "snapshot_utc": None,
            "open_positions": [],
            "total_unrealised_pnl": 0,
            "total_realised_pnl": 0,
            "total_pnl": 0,
            "drawdown_alerts": [],
            "position_count": 0,
        }
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"snapshot_utc": None, "open_positions": [], "total_pnl": 0}


# ── Alerts ─────────────────────────────────────────────────────────────────────

def _send_drawdown_alerts(alerts: list[dict]) -> None:
    """Send drawdown alerts via Telegram. Disabled per user request."""
    return  # alerts disabled



if __name__ == "__main__":
    state = monitor()
    print(json.dumps(state, indent=2))

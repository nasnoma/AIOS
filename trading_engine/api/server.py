"""
trading_engine/api/server.py
FastAPI dashboard with WebSocket live updates.
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import base64
import json
import uvicorn
from loguru import logger

from trading_engine.config import settings
from trading_engine.execution import paper_trader, live_trader
from trading_engine.alerts.telegram_bot import send_message

app = FastAPI(title="Trading Decision Engine", version="1.0.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# In-memory signal history (last 50 signals)
signal_history: list[dict] = []
total_signals_count: int = 0
connected_clients: list[WebSocket] = []
last_scheduler_heartbeat: Optional[datetime] = None


def get_trading_status() -> dict:
    global last_scheduler_heartbeat
    if settings.trading_mode == "live":
        status = live_trader.get_status()
    else:
        status = paper_trader.get_status()
    status["trading_mode"] = settings.trading_mode
    
    # Check if scheduler is online (heartbeat received within last 90 seconds)
    is_sched_online = False
    if last_scheduler_heartbeat:
        diff = (datetime.utcnow() - last_scheduler_heartbeat).total_seconds()
        is_sched_online = diff < 90.0
    status["scheduler_online"] = is_sched_online
    return status


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_file = TEMPLATES_DIR / "dashboard.html"
    return HTMLResponse(content=html_file.read_text())


@app.get("/api/status")
async def get_status():
    return get_trading_status()


@app.get("/api/signals")
async def get_signals():
    return {"signals": signal_history[-50:], "total_count": total_signals_count}


@app.post("/api/signals")
async def add_signal(signal_data: dict):
    global total_signals_count
    total_signals_count += 1
    await broadcast_signal(signal_data)
    return {"status": "ok"}


@app.post("/api/scheduler/heartbeat")
async def scheduler_heartbeat():
    global last_scheduler_heartbeat
    last_scheduler_heartbeat = datetime.utcnow()
    return {"status": "ok"}



@app.get("/api/evolution")
async def get_evolution():
    """Return the autoresearch evolution history from the JSONL log file."""
    history_path = Path(__file__).parent.parent / "autoresearch" / "evolution_history.jsonl"
    history = []
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    history.append(__import__("json").loads(line))
                except Exception:
                    pass
    return {"history": history, "count": len(history)}


@app.get("/api/agent_weights")
async def get_agent_weights():
    """Return current ART agent weights for the dashboard weight panel."""
    import json, os
    from pathlib import Path
    weights_path = Path(__file__).parent.parent / "autoresearch" / "params" / "judge_weights.json"
    try:
        with open(weights_path) as f:
            data = json.load(f)
        mtime = os.path.getmtime(weights_path)
        from datetime import datetime
        return {
            "weights": data.get("weights", {}),
            "last_updated": datetime.utcfromtimestamp(mtime).isoformat() + "Z",
        }
    except Exception as e:
        return {"weights": {}, "last_updated": None, "error": str(e)}


@app.get("/api/copy-trading/leaderboard")
async def get_copy_leaderboard():
    """Return latest copy trading leaderboard scan results.
    Auto-triggers a background scan on first call if no state exists yet."""
    from trading_engine.copy_trading.leaderboard_scanner import load_state, STATE_FILE
    state = load_state()
    # Auto-trigger scan if never run (e.g. fresh Railway deploy with ephemeral fs)
    if not STATE_FILE.exists() or not state.get("top_masters"):
        import threading
        from trading_engine.copy_trading import leaderboard_scanner as _ls
        def _auto_scan():
            try:
                results = _ls.scan()
                if results:
                    _ls.send_leaderboard_alert(results)
            except Exception as _e:
                logger.warning(f"Auto-scan error: {_e}")
        t = threading.Thread(target=_auto_scan, daemon=True)
        t.start()
        state["_auto_scanning"] = True
    return state


@app.get("/api/copy-trading/positions")
async def get_copy_positions():
    """Return latest copy trading position snapshot."""
    from trading_engine.copy_trading.position_monitor import load_state as load_pos_state
    return load_pos_state()


@app.post("/api/copy-trading/scan")
async def trigger_copy_scan():
    """Manually trigger a leaderboard scan (runs in background thread)."""
    import threading
    from trading_engine.copy_trading import leaderboard_scanner
    def _run():
        try:
            results = leaderboard_scanner.scan()
            if results:
                leaderboard_scanner.send_leaderboard_alert(results)
        except Exception as e:
            logger.error(f"Manual copy scan error: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "scan_started", "message": "Leaderboard scan triggered — results in ~2 min"}


@app.get("/api/diagnostics/trades")
async def get_diagnostics_trades():
    from trading_engine.storage import db
    try:
        with db.get_session() as session:
            trades = session.query(db.ClosedTradeLog).order_by(db.ClosedTradeLog.opened_at.desc()).limit(50).all()
            return {
                "status": "ok",
                "trades": [
                    {
                        "id": t.id,
                        "symbol": t.symbol,
                        "direction": t.direction,
                        "entry_price": t.entry_price,
                        "exit_price": t.exit_price,
                        "size_usd": t.size_usd,
                        "pnl_usd": t.pnl_usd,
                        "fee_usd": t.fee_usd,
                        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                        "exit_reason": t.exit_reason
                    }
                    for t in trades
                ]
            }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/diagnostics/orders")
async def get_diagnostics_orders():
    from trading_engine.storage import db
    try:
        with db.get_session() as session:
            orders = session.query(db.OrderAuditLog).order_by(db.OrderAuditLog.id.desc()).limit(100).all()
            return {
                "status": "ok",
                "orders": [
                    {
                        "id": o.id,
                        "timestamp": o.timestamp.isoformat() if o.timestamp else None,
                        "symbol": o.symbol,
                        "side": o.side,
                        "qty": o.qty,
                        "price": o.price,
                        "order_type": o.order_type,
                        "payload": o.payload,
                        "response": o.response,
                        "status": o.status,
                        "error_message": o.error_message
                    }
                    for o in orders
                ]
            }
    except Exception as e:
        return {"status": "error", "error": str(e)}




@app.get("/api/market_status")
async def get_market_status():
    """
    Return current open/closed status for all CFD and crypto markets.
    Used by the dashboard Market Status panel.
    """
    from trading_engine.market_hours import market_status
    from trading_engine.config import settings

    symbols = settings.watchlist_assets
    result = {}
    for sym in symbols:
        ms = market_status(sym)
        next_open_str = None
        if ms.next_open:
            # Format as WAT-friendly string (UTC+1)
            import datetime
            wat = ms.next_open + datetime.timedelta(hours=1)
            next_open_str = wat.strftime("%a %d %b %H:%M WAT")
        result[sym] = {
            "is_open":   ms.is_open,
            "open":      ms.is_open,   # alias for JS compatibility
            "reason":    ms.reason,
            "next_open": next_open_str,
        }
    return result


@app.get("/api/carry/status")
async def get_carry_status():
    """Return delta-neutral carry portfolio status for the dashboard."""
    from trading_engine.execution import carry_trader
    from trading_engine.config import settings
    try:
        status = carry_trader.get_status()
        status["carry_enabled"] = getattr(settings, "carry_enabled", False)
        status["min_apy"] = getattr(settings, "carry_min_apy", 0)
        return status
    except Exception as e:
        return {
            "carry_enabled": getattr(settings, "carry_enabled", False),
            "error": str(e),
            "open_positions": 0,
            "total_pnl": 0,
            "total_return_pct": 0,
        }


@app.post("/api/carry/scan")
async def trigger_carry_scan():
    """Manually trigger a carry funding scan — returns live opportunities."""
    import threading
    import ccxt
    from trading_engine.config import settings
    from trading_engine.agents.carry_agent import _fetch_funding_rates, _apy_from_rate

    watchlist = [s.strip() for s in settings.carry_watchlist.split(",") if s.strip()]
    min_apy = float(getattr(settings, "carry_min_apy", 15.0))
    borrow_cost = float(getattr(settings, "carry_borrow_cost_apy", 5.0))

    def _scan():
        opportunities = []
        for symbol in watchlist:
            try:
                venues = _fetch_funding_rates(symbol)
                if not venues:
                    continue
                binance_data = venues.get("binance", {})
                bybit_data = venues.get("bybit", {})
                binance_funding = binance_data.get("funding_rate", 0)
                bybit_funding = bybit_data.get("funding_rate", 0)
                binance_apy = _apy_from_rate(binance_funding)
                bybit_apy = _apy_from_rate(bybit_funding)
                spread_apy = abs(_apy_from_rate(binance_funding - bybit_funding))
                best_short_apy = max(binance_apy, bybit_apy)
                net_short_carry = best_short_apy - borrow_cost
                cross_basis_apy = spread_apy if len(venues) >= 2 else 0

                structure = None
                apy = 0
                signal = "HOLD"
                if cross_basis_apy > min_apy:
                    structure = "cross_basis"
                    apy = cross_basis_apy
                    signal = "BUY"
                elif net_short_carry > min_apy:
                    structure = "short_perp"
                    apy = net_short_carry
                    signal = "BUY"

                opportunities.append({
                    "symbol": symbol,
                    "structure": structure,
                    "apy": round(apy, 1),
                    "binance_funding": binance_funding,
                    "bybit_funding": bybit_funding,
                    "signal": signal,
                })
            except Exception:
                pass
        return opportunities

    opportunities = _scan()
    return {"opportunities": opportunities, "min_apy": min_apy, "count": len(opportunities)}


@app.get("/api/spot/status")
async def get_spot_status_endpoint():
    """Get status of spot grid portfolio, regimes, active grids, and completed cycles."""
    try:
        from trading_engine.spot.runner import get_spot_status
        return get_spot_status()
    except Exception as e:
        logger.error(f"Error fetching spot status: {e}")
        return {"error": str(e)}


@app.post("/api/spot/tick")
async def trigger_spot_tick_endpoint():
    """Manually trigger a spot grid tick."""
    try:
        from trading_engine.spot.runner import run_spot_grid_tick
        res = run_spot_grid_tick()
        return res
    except Exception as e:
        logger.error(f"Error triggering spot tick: {e}")
        return {"error": str(e)}


@app.post("/api/spot/self-heal")
async def trigger_self_heal_endpoint():
    """Trigger self-healing audit and automated event-driven backtesting."""
    try:
        from trading_engine.spot.runner import run_spot_self_healing_and_optimize
        return run_spot_self_healing_and_optimize()
    except Exception as e:
        logger.error(f"Error triggering self-heal: {e}")
        return {"error": str(e)}



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        # Push immediately on connect (fast first load), then every 10s
        while True:
            current_status = get_trading_status()
            await websocket.send_json({
                "type": "status",
                "data": current_status,
                "timestamp": datetime.utcnow().isoformat()
            })
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast_signal(signal_data: dict):
    """Push new trade signal to all connected dashboard clients."""
    signal_history.append(signal_data)
    dead = []
    for client in connected_clients:
        try:
            await client.send_json({"type": "signal", "data": signal_data})
        except Exception:
            dead.append(client)
    for d in dead:
        connected_clients.remove(d)


# Bamboo webhook endpoints removed for crypto-only system


def start():
    port = int(os.environ.get("PORT", getattr(settings, "api_port", 8000)))
    host = os.environ.get("HOST", getattr(settings, "api_host", "0.0.0.0"))
    logger.info(f"🚀 Starting Uvicorn API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start()

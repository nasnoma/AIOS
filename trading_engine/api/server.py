"""
trading_engine/api/server.py
FastAPI dashboard with WebSocket live updates.
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from loguru import logger

from trading_engine.config import settings
from trading_engine.execution import paper_trader, live_trader

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


@app.get("/api/market_status")
async def get_market_status():
    """
    Return current open/closed status for all CFD and crypto markets.
    Used by the dashboard Market Status panel.
    """
    from trading_engine.market_hours import market_status
    from trading_engine.config import settings

    symbols = ["BTC/USDT"] + settings.all_cfd_assets
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            # Load and push latest status every 5 seconds
            current_status = get_trading_status()
            await websocket.send_json({
                "type": "status",
                "data": current_status,
                "timestamp": datetime.utcnow().isoformat()
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
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


def start():
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="warning")


if __name__ == "__main__":
    start()

"""
trading_engine/api/server.py
FastAPI dashboard with WebSocket live updates.
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

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
connected_clients: list[WebSocket] = []


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_file = TEMPLATES_DIR / "dashboard.html"
    return HTMLResponse(content=html_file.read_text())


@app.get("/api/status")
async def get_status():
    if settings.trading_mode == "live":
        return live_trader.get_status()
    return paper_trader.get_status()


@app.get("/api/signals")
async def get_signals():
    return {"signals": signal_history[-50:]}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        # Send current status on connect
        current_status = live_trader.get_status() if settings.trading_mode == "live" else paper_trader.get_status()
        await websocket.send_json({
            "type": "status",
            "data": current_status
        })
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })
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

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


@app.get("/api/market_status")
async def get_market_status():
    """
    Return current open/closed status for all CFD and crypto markets.
    Used by the dashboard Market Status panel.
    """
    from trading_engine.market_hours import market_status
    from trading_engine.config import settings

    symbols = ["BTC/USDT"] + settings.all_cfd_assets + ["ZENITHBANK/NGX"]
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


@app.get("/deposit/status/{reference}")
async def verify_deposit_status(reference: str, request: Request):
    # Auth check
    auth_token = request.headers.get("X-Bamboo-Webhook-Token")
    if settings.bamboo_webhook_auth_hash:
        if not auth_token or auth_token != settings.bamboo_webhook_auth_hash:
            logger.warning(f"Unauthorized Bamboo deposit status request. Token: {auth_token}")
            raise HTTPException(status_code=401, detail="Unauthorized")
            
    logger.info(f"Verified deposit status endpoint check for reference: {reference}")
    return {"status": "verified", "reference": reference}


@app.post("/api/webhooks/bamboo")
async def bamboo_webhook(request: Request):
    # Auth check
    auth_token = request.headers.get("X-Bamboo-Webhook-Token")
    if settings.bamboo_webhook_auth_hash:
        if not auth_token or auth_token != settings.bamboo_webhook_auth_hash:
            logger.warning(f"Unauthorized Bamboo webhook event. Token: {auth_token}")
            raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    logger.info(f"Received Bamboo webhook: {body}")

    # Decode Pub/Sub envelope if present
    payload = body
    if isinstance(body, dict) and "message" in body and isinstance(body["message"], dict) and "data" in body["message"]:
        try:
            data_str = base64.b64decode(body["message"]["data"]).decode("utf-8")
            payload = json.loads(data_str)
            logger.info(f"Decoded Pub/Sub payload: {payload}")
        except Exception as e:
            logger.error(f"Failed to decode Pub/Sub base64 data: {e}")
            raise HTTPException(status_code=400, detail="Invalid Pub/Sub encoding")

    event_type = payload.get("event_type") or payload.get("event")
    
    if event_type == "deposit_status_update":
        status = payload.get("status")
        reference = payload.get("reference")
        amount = float(payload.get("amount") or 0.0)
        
        logger.info(f"Bamboo deposit status update: ref={reference}, status={status}, amount={amount}")
        
        # On "Settlemented", credit the cash to live portfolio
        if status == "Settlemented":
            portfolio = live_trader._load_state()
            portfolio.cash += amount
            portfolio.account_size += amount
            live_trader._save_state(portfolio)
            logger.success(f"Credited deposit of ${amount:,.2f} (ref: {reference}) to live portfolio.")
            
            # Send Telegram alert
            send_message(
                f"💰 <b>BAMBOO DEPOSIT SETTLED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📂 Reference: <code>{reference}</code>\n"
                f"💵 Amount: <b>${amount:,.2f}</b>\n"
                f"📊 New Portfolio Cash: <b>${portfolio.cash:,.2f}</b>"
            )
            
    elif event_type == "trade_status_update":
        status = payload.get("status")
        side = payload.get("side", "").upper()
        symbol = payload.get("symbol", "").upper()
        order_id = payload.get("order_id")
        
        logger.info(f"Bamboo trade status update: symbol={symbol}, side={side}, status={status}, order_id={order_id}")
        
        # Load state
        portfolio = live_trader._load_state()
        
        if status in ("cancelled", "rejected"):
            # If buy order is rejected/cancelled, revert the reserved cash
            if side == "BUY":
                for pos in list(portfolio.positions):
                    pos_sym_clean = pos.symbol.split("/")[0].split(":")[0].upper()
                    pay_sym_clean = symbol.split("/")[0].split(":")[0].upper()
                    if pos_sym_clean == pay_sym_clean and pos.status == "open":
                        portfolio.cash += pos.size_usd + pos.fee_usd
                        portfolio.positions.remove(pos)
                        live_trader._save_state(portfolio)
                        logger.warning(f"Reverted buy position for {pos.symbol} due to order rejection/cancellation.")
                        send_message(
                            f"❌ <b>BAMBOO ORDER REJECTED/CANCELLED</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Symbol: {pos.symbol}\n"
                            f"💵 Refunded: ${pos.size_usd + pos.fee_usd:,.2f}"
                        )
                        break
        elif status == "filled":
            if side == "BUY":
                # Buy order filled: send Telegram notification of fill confirmation
                for pos in portfolio.positions:
                    pos_sym_clean = pos.symbol.split("/")[0].split(":")[0].upper()
                    pay_sym_clean = symbol.split("/")[0].split(":")[0].upper()
                    if pos_sym_clean == pay_sym_clean and pos.status == "open":
                        send_message(
                            f"✅ <b>BAMBOO BUY ORDER FILLED</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Symbol: {pos.symbol}\n"
                            f"📈 Entry Price: <code>{pos.entry_price:.4f}</code>\n"
                            f"💵 Size: ${pos.size_usd:,.2f}"
                        )
                        break
            elif side == "SELL":
                # Sell order filled: close the position and finalize P&L
                for pos in list(portfolio.positions):
                    pos_sym_clean = pos.symbol.split("/")[0].split(":")[0].upper()
                    pay_sym_clean = symbol.split("/")[0].split(":")[0].upper()
                    if pos_sym_clean == pay_sym_clean and pos.status == "open":
                        fill_price = float(payload.get("price") or payload.get("price_per_share") or pos.entry_price)
                        fee_cost = float(payload.get("fee") or (pos.size_usd * live_trader.EXIT_FEE_RATE))
                        
                        live_trader._finalize_closed_position(
                            portfolio=portfolio,
                            pos=pos,
                            fill_price=fill_price,
                            fee_cost=fee_cost,
                            closed_at=datetime.utcnow().isoformat(),
                            local_close_cash_update=True
                        )
                        live_trader._save_state(portfolio)
                        break
                        
    return {"status": "processed"}


def start():
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="warning")


if __name__ == "__main__":
    start()

"""
polymarket_bot/alerts.py

Telegram alerting for Polymarket Scalping Bot (Globally Paused).
"""
from __future__ import annotations
from typing import Optional

def _is_configured() -> bool:
    return False

async def _send(text: str) -> None:
    return

def send(text: str) -> None:
    return

def startup(mode: str, assets: list[str], account_size: float) -> None:
    return

def trade_opened(asset: str, signal_type: str, side: str, size_usd: float,
                 entry_yes: Optional[float], entry_no: Optional[float], mode: str = "PAPER") -> None:
    return

def circuit_breaker_hit(daily_pnl: float, max_daily_loss: float) -> None:
    return

def daily_summary(status: dict) -> None:
    return

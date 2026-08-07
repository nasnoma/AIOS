"""
trading_engine/alerts/telegram_bot.py
Sends trade signal alerts to Telegram.
"""
from __future__ import annotations
import asyncio
import requests
from loguru import logger
from trading_engine.config import settings


def send_message(text: str):
    """Send a plain text message to Telegram (Globally Paused)."""
    return


def send_signal_alert(signal):
    """Format and send a trade signal alert (Globally Paused)."""
    return


def send_portfolio_update(status: dict):
    """Daily portfolio summary (Globally Paused)."""
    return

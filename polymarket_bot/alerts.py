"""
polymarket_bot/alerts.py

Telegram alerting for Polymarket Scalping Bot.
"""
from __future__ import annotations
import asyncio
from typing import Optional

from loguru import logger

from polymarket_bot.config import settings


def _is_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def _send(text: str) -> None:
    """Send a Telegram HTML message. Non-blocking, swallows errors."""
    if not _is_configured():
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.debug(f"Telegram send failed ({resp.status}): {body[:100]}")
    except Exception as e:
        logger.debug(f"Telegram error (suppressed): {e}")


def send(text: str) -> None:
    """Sync wrapper — schedules the async send on the running event loop."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_send(text))
    except RuntimeError:
        pass  # No running event loop — silently skip


def startup(mode: str, assets: list[str], account_size: float) -> None:
    assets_str = ", ".join(assets)
    send(
        f"🚀 <b>Polymarket Scalping Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 Mode: <b>{mode.upper()}</b>\n"
        f"🪙 Assets: <code>{assets_str}</code>\n"
        f"💰 Initial Capital: ${account_size:,.2f} USDT"
    )


def trade_opened(asset: str, signal_type: str, side: str, size_usd: float,
                 entry_yes: Optional[float], entry_no: Optional[float], mode: str = "PAPER") -> None:
    yes_str = f"{entry_yes:.3f}" if entry_yes else "None"
    no_str = f"{entry_no:.3f}" if entry_no else "None"
    send(
        f"🎯 <b>{mode} Trade Opened</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Asset: <b>{asset}</b>\n"
        f"🔄 Signal: <code>{signal_type}</code>\n"
        f"↕️ Side: {side}\n"
        f"💵 Size: ${size_usd:.2f} USDT\n"
        f"🟢 Entry YES: {yes_str} | Entry NO: {no_str}"
    )


def circuit_breaker_hit(daily_pnl: float, max_daily_loss: float) -> None:
    send(
        f"🚨 <b>Circuit Breaker Triggered!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 Daily Loss: <b>${daily_pnl:+.2f} USDT</b>\n"
        f"🛑 Max Allowed Loss: ${max_daily_loss:.2f} USDT\n"
        f"⚠️ Trading paused until next UTC day."
    )


def daily_summary(status: dict) -> None:
    send(
        f"📊 <b>Daily Summary (Polymarket Scalper)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Daily P&L: <b>${status.get('daily_pnl', 0):+.2f} USDT</b>\n"
        f"📈 Total P&L: <b>${status.get('total_pnl', 0):+.2f} USDT</b>\n"
        f"🎯 Win Rate: {status.get('win_rate_pct', 0):.1f}%\n"
        f"🏆 Wins: {status.get('win_count', 0)} | ❌ Losses: {status.get('loss_count', 0)}\n"
        f"🏦 Current Cash: ${status.get('cash', 0):,.2f} USDT"
    )

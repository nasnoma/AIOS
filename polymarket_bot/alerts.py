"""
polymarket_bot/alerts.py

Self-contained Telegram alerting for Bybit Arbitrage Bot.
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


def arbitrage_executed(cycle_id: str, direction: str, size: float, pnl: float,
                       net_edge: float, mode: str = "PAPER") -> None:
    emoji = "✅" if pnl >= 0 else "❌"
    send(
        f"{emoji} <b>{mode} Arbitrage Executed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{cycle_id}</code>\n"
        f"🔄 Cycle: <b>{direction}</b>\n"
        f"💵 Size: ${size:.2f} USDT\n"
        f"📈 Expected Edge: {net_edge:.4%}\n"
        f"💰 Realized P&L: <b>${pnl:+.4f} USDT</b>"
    )


def arbitrage_failed(cycle_id: str, direction: str, step_failed: str, error_msg: str,
                     mode: str = "PAPER") -> None:
    send(
        f"⚠️ <b>{mode} Arbitrage Execution Failed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{cycle_id}</code>\n"
        f"🔄 Cycle: <b>{direction}</b>\n"
        f"🚫 Step Failed: <code>{step_failed}</code>\n"
        f"🛑 Error: <code>{error_msg}</code>"
    )


def daily_summary(status: dict) -> None:
    send(
        f"📈 <b>Daily Summary (Bybit Arb)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Daily P&L: <b>${status.get('daily_pnl', 0):+.4f} USDT</b>\n"
        f"📊 Total P&L: <b>${status.get('total_pnl', 0):+.4f} USDT</b>\n"
        f"🎯 Win rate: {status.get('win_rate_pct', 0):.1f}%\n"
        f"✅ Wins: {status.get('win_count', 0)} | "
        f"❌ Losses: {status.get('loss_count', 0)}\n"
        f"🔄 Cycles: {status.get('cycle_count', 0)}"
    )


def startup(mode: str, account_size: float) -> None:
    send(
        f"🚀 <b>Bybit Arbitrage Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 Mode: <b>{mode.upper()}</b>\n"
        f"🪙 Whitelist: <code>BTC/USDT, ETH/BTC, ETH/USDT</code>\n"
        f"💰 Initial Capital: ${account_size:,.2f} USDT"
    )

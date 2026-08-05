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
    return  # Telegram alerts globally paused per user request until explicitly re-enabled


def send_signal_alert(signal):
    """Format and send a trade signal alert."""
    action = signal.final_action
    emoji = "🟢" if action == "BUY" else "🔴"
    verdict = signal.verdict
    risk = signal.risk
    total_agents = len(signal.agent_signals) if signal.agent_signals else 2

    text = (
        f"{emoji} <b>{action}: {signal.symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Timeframe: {signal.timeframe}\n"
        f"💰 Entry: <code>{signal.entry_price:.4f}</code>\n"
        f"🛑 Stop Loss: <code>{signal.stop_loss:.4f}</code> ({risk.get('stop_loss_pct', 0)*100:.1f}%)\n"
        f"🎯 Take Profit: <code>{signal.take_profit:.4f}</code> ({risk.get('take_profit_pct', 0)*100:.1f}%)\n"
        f"📊 Risk/Reward: {risk.get('risk_reward', 0):.1f}:1\n"
        f"💼 Size: ${risk.get('position_size_usd', 0):,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Agreement: {verdict.get('agreement', 0)}/{total_agents} agents\n"
        f"📈 Confidence: {verdict.get('confidence', 0):.0f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {signal.reasoning[:200]}..."
    )
    send_message(text)



def send_portfolio_update(status: dict):
    """Daily portfolio summary."""
    pnl = status["total_pnl"]
    pnl_pct = status["total_pnl_pct"]
    emoji = "📈" if pnl >= 0 else "📉"
    text = (
        f"{emoji} <b>Portfolio Update</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Total P&L: <b>${pnl:+,.2f}</b> ({pnl_pct:+.2f}%)\n"
        f"🔥 Portfolio Heat: {status['portfolio_heat']:.1f}%\n"
        f"📊 Open Positions: {status['open_positions']}\n"
        f"✅ Win Rate: {status['win_rate']:.1f}% "
        f"({status['win_count']}W / {status['loss_count']}L)"
    )
    send_message(text)

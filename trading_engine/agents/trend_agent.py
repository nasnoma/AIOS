"""
trading_engine/agents/trend_agent.py

Agent 1: Trend Agent
- Pure quantitative — zero LLM calls
- Analyzes EMA alignment + higher highs / higher lows (market structure)
- Outputs BUY / SELL / HOLD with confidence score
"""
from __future__ import annotations
import numpy as np
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 6
    reasons = []

    # ── EMA Alignment ──────────────────────────────────
    if snap.ema20 > snap.ema50 > snap.ema200:
        score += 3
        reasons.append("Full EMA bullish stack (20>50>200)")
    elif snap.ema20 < snap.ema50 < snap.ema200:
        score -= 3
        reasons.append("Full EMA bearish stack (20<50<200)")
    elif snap.ema20 > snap.ema50:
        score += 1
        reasons.append("Short-term EMA bullish (20>50)")
    elif snap.ema20 < snap.ema50:
        score -= 1
        reasons.append("Short-term EMA bearish (20<50)")

    # ── Price vs EMA200 (primary trend filter) ─────────
    if snap.close > snap.ema200 * 1.005:
        score += 2
        reasons.append(f"Price above EMA200 ({snap.close:.2f} > {snap.ema200:.2f})")
    elif snap.close < snap.ema200 * 0.995:
        score -= 2
        reasons.append(f"Price below EMA200 ({snap.close:.2f} < {snap.ema200:.2f})")

    # ── Higher Highs / Higher Lows (last 20 candles) ───
    df = snap.df.tail(20)
    highs = df["high"].values
    lows = df["low"].values
    hh = all(highs[i] >= highs[i - 1] for i in range(-5, -1))
    hl = all(lows[i] >= lows[i - 1] for i in range(-5, -1))
    lh = all(highs[i] <= highs[i - 1] for i in range(-5, -1))
    ll = all(lows[i] <= lows[i - 1] for i in range(-5, -1))

    if hh and hl:
        score += 1
        reasons.append("Higher highs + higher lows (uptrend)")
    elif lh and ll:
        score -= 1
        reasons.append("Lower highs + lower lows (downtrend)")

    normalized = (score / max_score + 1) / 2   # 0 to 1
    if score >= 3:
        signal = Signal.BUY
        confidence = round(normalized * 100, 1)
    elif score <= -3:
        signal = Signal.SELL
        confidence = round((1 - normalized) * 100, 1)
    else:
        signal = Signal.HOLD
        confidence = round((normalized if normalized >= 0.5 else 1 - normalized) * 100, 1)

    return AgentSignal(
        agent="trend",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No clear trend",
        raw_data={"score": score, "ema20": snap.ema20, "ema50": snap.ema50, "ema200": snap.ema200},
    )

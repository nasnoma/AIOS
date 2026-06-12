"""
trading_engine/agents/momentum_agent.py

Agent 2: Momentum Agent
- Pure quantitative — zero LLM calls
- RSI, StochRSI, Rate of Change
- Detects whether momentum supports or contradicts the trend
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 6
    reasons = []

    # ── RSI ────────────────────────────────────────────
    rsi = snap.rsi
    if rsi > 60:
        score += 2
        reasons.append(f"RSI bullish ({rsi:.1f} > 60)")
    elif rsi < 40:
        score -= 2
        reasons.append(f"RSI bearish ({rsi:.1f} < 40)")
    elif 50 < rsi <= 60:
        score += 1
        reasons.append(f"RSI mildly bullish ({rsi:.1f})")
    elif 40 <= rsi < 50:
        score -= 1
        reasons.append(f"RSI mildly bearish ({rsi:.1f})")

    # Overbought/oversold extremes override
    if rsi >= 80:
        score -= 1
        reasons.append("RSI overbought (≥80) — caution")
    elif rsi <= 20:
        score += 1
        reasons.append("RSI oversold (≤20) — potential reversal")

    # ── Stochastic RSI ─────────────────────────────────
    k, d = snap.stoch_rsi_k, snap.stoch_rsi_d
    if k > 80 and d > 80:
        score -= 1
        reasons.append(f"StochRSI overbought (K={k:.0f}, D={d:.0f})")
    elif k < 20 and d < 20:
        score += 1
        reasons.append(f"StochRSI oversold (K={k:.0f}, D={d:.0f})")
    elif k > d and k > 50:
        score += 1
        reasons.append(f"StochRSI bullish crossover (K>{d:.0f})")
    elif k < d and k < 50:
        score -= 1
        reasons.append(f"StochRSI bearish crossover (K<{d:.0f})")

    # ── Rate of Change ─────────────────────────────────
    roc = snap.roc
    if roc > 3:
        score += 2
        reasons.append(f"ROC strong positive ({roc:.2f}%)")
    elif roc < -3:
        score -= 2
        reasons.append(f"ROC strong negative ({roc:.2f}%)")
    elif roc > 0:
        score += 1
        reasons.append(f"ROC positive ({roc:.2f}%)")
    elif roc < 0:
        score -= 1
        reasons.append(f"ROC negative ({roc:.2f}%)")

    normalized = (score / max_score + 1) / 2
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
        agent="momentum",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "Neutral momentum",
        raw_data={"score": score, "rsi": rsi, "k": k, "d": d, "roc": roc},
    )

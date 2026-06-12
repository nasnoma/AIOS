"""
trading_engine/agents/volume_agent.py

Agent 3: Volume Agent
- Pure quantitative — zero LLM calls
- OBV trend, relative volume, price vs VWAP
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 5
    reasons = []

    # ── Relative Volume ────────────────────────────────
    rel_vol = snap.rel_volume
    if rel_vol >= 1.5:
        score += 2
        reasons.append(f"High relative volume ({rel_vol:.2f}x avg) — confirms move")
    elif rel_vol >= 1.1:
        score += 1
        reasons.append(f"Above-average volume ({rel_vol:.2f}x)")
    elif rel_vol < 0.7:
        score -= 1
        reasons.append(f"Low volume ({rel_vol:.2f}x) — weak conviction")

    # ── OBV Trend (last 10 candles) ────────────────────
    df = snap.df.tail(10)
    if "OBV" in df.columns and df["OBV"].notna().sum() > 5:
        obv_vals = df["OBV"].dropna().values
        if len(obv_vals) >= 5:
            # Simple linear regression slope
            import numpy as np
            x = range(len(obv_vals))
            slope = np.polyfit(x, obv_vals, 1)[0]
            if slope > 0:
                score += 2
                reasons.append("OBV trending up (accumulation)")
            elif slope < 0:
                score -= 2
                reasons.append("OBV trending down (distribution)")

    # ── Price vs VWAP ──────────────────────────────────
    if snap.vwap and snap.close:
        if snap.close > snap.vwap * 1.002:
            score += 1
            reasons.append(f"Price above VWAP ({snap.close:.2f} > {snap.vwap:.2f})")
        elif snap.close < snap.vwap * 0.998:
            score -= 1
            reasons.append(f"Price below VWAP ({snap.close:.2f} < {snap.vwap:.2f})")

    normalized = (score / max_score + 1) / 2
    if score >= 3:
        signal = Signal.BUY
        confidence = round(max(0, min(100, normalized * 100)), 1)
    elif score <= -3:
        signal = Signal.SELL
        confidence = round(max(0, min(100, (1 - normalized) * 100)), 1)
    else:
        signal = Signal.HOLD
        confidence = round(max(0, min(100, (normalized if normalized >= 0.5 else 1 - normalized) * 100)), 1)

    return AgentSignal(
        agent="volume",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "Neutral volume",
        raw_data={"score": score, "rel_volume": rel_vol, "obv": snap.obv, "vwap": snap.vwap},
    )

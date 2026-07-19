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

    # ── Higher Timeframe Filter ────────────────────────
    htf_bearish = False
    htf_bullish = False
    
    # ── Multi-Timeframe Trend Filter (Daily + 4H + 1H) ──
    mtf_bearish_count = 0
    mtf_bullish_count = 0
    mtf_details = []

    for tf_name, tf_snap in [("1H", snap.htf_1h_snap), ("4H", snap.htf_4h_snap), ("Daily", snap.htf_1d_snap)]:
        if tf_snap:
            is_tf_bullish = tf_snap.close > tf_snap.ema200 and tf_snap.ema20 > tf_snap.ema50
            is_tf_bearish = tf_snap.close < tf_snap.ema200 and tf_snap.ema20 < tf_snap.ema50
            if is_tf_bullish:
                mtf_bullish_count += 1
                mtf_details.append(f"{tf_name}: Bullish")
            elif is_tf_bearish:
                mtf_bearish_count += 1
                mtf_details.append(f"{tf_name}: Bearish")
            else:
                mtf_details.append(f"{tf_name}: Neutral")

    if mtf_details:
        reasons.append(f"MTF Trends ({', '.join(mtf_details)})")

        # ── Supportive MTF Consensus Filter ───────────────────────
        # We allow trading as long as there is supportive majority alignment
        # and no strong conflict with the macro trend (e.g. buying when majority is bearish).
        htf_count = sum(1 for _, s in [("1H", snap.htf_1h_snap), ("4H", snap.htf_4h_snap), ("Daily", snap.htf_1d_snap)] if s is not None)
        if htf_count >= 2:
            if mtf_bullish_count >= 2:
                # Majority bullish — suppress any bearish score
                if score < 0:
                    score = 0
                    reasons.append("MTF Support: Majority of HTFs are BULLISH — bearish signal suppressed.")
            elif mtf_bearish_count >= 2:
                # Majority bearish — suppress any bullish score
                if score > 0:
                    score = 0
                    reasons.append("MTF Support: Majority of HTFs are BEARISH — bullish signal suppressed.")
            else:
                # Split trend without a clear majority — discount score slightly for caution
                if score > 0 and mtf_bearish_count >= 1:
                    score = max(0, score - 1)
                    reasons.append("MTF Caution: Trend is split — bullish score discounted.")
                elif score < 0 and mtf_bullish_count >= 1:
                    score = min(0, score + 1)
                    reasons.append("MTF Caution: Trend is split — bearish score discounted.")
    else:
        # Fallback to single htf_snap
        if snap.htf_snap:
            htf = snap.htf_snap
            if htf.close < htf.ema200 or (htf.ema20 < htf.ema50 < htf.ema200):
                htf_bearish = True
            elif htf.close > htf.ema200 and htf.ema20 > htf.ema50:
                htf_bullish = True

        if htf_bearish and score > 0:
            score = min(0, score - 5)
            reasons.append(f"HTF Trend Filter active: Macro trend ({snap.htf_snap.timeframe}) is BEARISH (price below EMA200). Long signals vetoed.")
        elif htf_bullish and score < 0:
            score = max(0, score + 5)
            reasons.append(f"HTF Trend Filter active: Macro trend ({snap.htf_snap.timeframe}) is BULLISH. Short signals vetoed.")

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

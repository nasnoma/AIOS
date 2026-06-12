"""
trading_engine/agents/structure_agent.py

Agent 6: Market Structure Agent
- Pure quantitative — zero LLM calls
- Detects: support/resistance zones, Break of Structure (BOS), Fair Value Gaps (FVG)
- Key SMC (Smart Money Concepts) logic
"""
from __future__ import annotations
import numpy as np
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def _find_support_resistance(df, lookback=50):
    """Identify key S/R levels from pivot highs/lows."""
    highs = df["high"].values[-lookback:]
    lows = df["low"].values[-lookback:]
    close = df["close"].values[-1]

    # Swing highs/lows: pivot points
    resistance_levels = []
    support_levels = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])

    nearest_resistance = min((r for r in resistance_levels if r > close), default=None)
    nearest_support = max((s for s in support_levels if s < close), default=None)

    return nearest_support, nearest_resistance


def _detect_bos(df, lookback=20):
    """Break of Structure: close above last swing high = bullish BOS."""
    recent = df.iloc[-lookback-1:-1]
    recent_high = recent["high"].max()
    recent_low = recent["low"].min()
    last_close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]

    bullish_bos = prev_close <= recent_high and last_close > recent_high * 1.001
    bearish_bos = prev_close >= recent_low and last_close < recent_low * 0.999
    return bullish_bos, bearish_bos


def _detect_fvg(df):
    """Fair Value Gap: gap between candle[i-2].high and candle[i].low (bullish FVG)."""
    c = df.tail(5)
    fvg_bullish = False
    fvg_bearish = False
    for i in range(2, len(c)):
        if c["low"].iloc[i] > c["high"].iloc[i - 2]:
            fvg_bullish = True
        if c["high"].iloc[i] < c["low"].iloc[i - 2]:
            fvg_bearish = True
    return fvg_bullish, fvg_bearish


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 6
    reasons = []
    df = snap.df

    if len(df) < 50:
        return AgentSignal(
            agent="structure",
            signal=Signal.HOLD,
            confidence=40.0,
            reason="Insufficient data for structure analysis",
        )

    # ── Support / Resistance proximity ─────────────────
    support, resistance = _find_support_resistance(df)
    close = snap.close

    if resistance and support:
        rr_range = resistance - support
        dist_to_res = (resistance - close) / rr_range if rr_range > 0 else 0.5
        dist_to_sup = (close - support) / rr_range if rr_range > 0 else 0.5

        if dist_to_res < 0.1:    # Very close to resistance
            score -= 2
            reasons.append(f"Price near resistance ({resistance:.2f}) — take-profit zone")
        elif dist_to_sup < 0.15: # Close to support = good buy zone
            score += 2
            reasons.append(f"Price near support ({support:.2f}) — buy zone")
        else:
            score += 1
            reasons.append(f"Price in mid-range (S:{support:.2f} R:{resistance:.2f})")
    elif resistance and close < resistance * 0.95:
        score += 1
        reasons.append(f"Below resistance ({resistance:.2f}) with room to run")
    elif support and close > support * 1.05:
        score += 1
        reasons.append(f"Above support ({support:.2f})")

    # ── Break of Structure ─────────────────────────────
    bullish_bos, bearish_bos = _detect_bos(df)
    if bullish_bos:
        score += 3
        reasons.append("Bullish Break of Structure (BOS) — strong momentum signal")
    elif bearish_bos:
        score -= 3
        reasons.append("Bearish Break of Structure (BOS) — breakdown confirmed")

    # ── Fair Value Gaps ────────────────────────────────
    fvg_bull, fvg_bear = _detect_fvg(df)
    if fvg_bull:
        score += 1
        reasons.append("Bullish Fair Value Gap present")
    if fvg_bear:
        score -= 1
        reasons.append("Bearish Fair Value Gap present")

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
        agent="structure",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No clear structure",
        raw_data={"score": score, "support": support, "resistance": resistance,
                  "bullish_bos": bullish_bos, "bearish_bos": bearish_bos},
    )

"""
trading_engine/agents/vwap_agent.py

VWAP Agent — Volume Weighted Average Price bounce/reversion
Strategy source: institutional benchmark, 59-65% WR with RSI confirmation
- VWAP = the fair value institutional traders use for intraday entries
- BUY when price is below VWAP by > 1 stddev (oversold → bounce to VWAP)
- SELL when price is above VWAP by > 1 stddev (overbought → revert to VWAP)
- RSI confirmation: RSI < 35 for BUY, RSI > 65 for SELL
- Exit target: return to VWAP (mean reversion anchor)
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 5
    reasons = []

    vwap = snap.vwap
    close = snap.close
    rsi = snap.rsi
    atr = snap.atr

    if vwap == 0 or close == 0:
        return AgentSignal(
            agent="vwap",
            signal=Signal.HOLD,
            confidence=50.0,
            reason="No VWAP data available.",
            raw_data={},
        )

    # ── Deviation from VWAP ──────────────────────────────────────
    deviation_pct = (close - vwap) / vwap * 100
    # Use ATR as the dynamic band — if no ATR, use 0.5% fallback
    band_pct = (atr / close * 100) if atr > 0 else 0.5

    if deviation_pct < -band_pct:
        # Price below VWAP by more than 1 ATR-band → oversold → BUY (bounce)
        score += 2
        reasons.append(f"Price {deviation_pct:.2f}% below VWAP (band={band_pct:.2f}%) — oversold bounce")
    elif deviation_pct > band_pct:
        # Price above VWAP by more than 1 ATR-band → overbought → SELL (revert)
        score -= 2
        reasons.append(f"Price {deviation_pct:.2f}% above VWAP (band={band_pct:.2f}%) — overbought revert")
    else:
        reasons.append(f"Price near VWAP ({deviation_pct:+.2f}%, band={band_pct:.2f}%) — fair value")

    # ── RSI confirmation ──────────────────────────────────────────
    if rsi < 35:
        score += 1
        reasons.append(f"RSI={rsi:.0f} oversold — confirms VWAP bounce")
    elif rsi > 65:
        score -= 1
        reasons.append(f"RSI={rsi:.0f} overbought — confirms VWAP revert")
    elif rsi < 45:
        score += 0
        reasons.append(f"RSI={rsi:.0f} mildly bearish")
    elif rsi > 55:
        score -= 0
        reasons.append(f"RSI={rsi:.0f} mildly bullish")

    # ── Volume confirmation (institutional participation) ─────────
    if snap.rel_volume >= 1.3:
        if deviation_pct < 0:
            score += 1
            reasons.append(f"Volume {snap.rel_volume:.1f}× — institutional buying below VWAP")
        elif deviation_pct > 0:
            score -= 1
            reasons.append(f"Volume {snap.rel_volume:.1f}× — institutional selling above VWAP")

    # ── MFI confirmation (volume-weighted momentum) ───────────────
    mfi = snap.mfi
    if mfi < 30:
        score += 1
        reasons.append(f"MFI={mfi:.0f} oversold — money flowing out, reversal due")
    elif mfi > 70:
        score -= 1
        reasons.append(f"MFI={mfi:.0f} overbought — money flowing in, exhaustion risk")

    normalized = (score / max_score + 1) / 2
    normalized = max(0.0, min(1.0, normalized))
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
        agent="vwap",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No VWAP signal",
        raw_data={"score": score, "vwap": vwap, "deviation_pct": deviation_pct, "rsi": rsi, "mfi": mfi},
    )

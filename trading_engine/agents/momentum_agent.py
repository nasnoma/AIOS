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
    from trading_engine.config import settings
    
    is_scalping = getattr(settings, "scalping_mode", True) and snap.timeframe in ("5m", "15m")
    
    if is_scalping:
        sma100 = snap.sma100
        macd = snap.macd
        macd_signal = snap.macd_signal
        
        # Bullish confluence: price above SMA100 AND MACD above signal line
        bullish_sma = sma100 and snap.close > sma100
        bullish_macd = macd is not None and macd_signal is not None and macd > macd_signal
        
        # Bearish confluence: price below SMA100 AND MACD below signal line
        bearish_sma = sma100 and snap.close < sma100
        bearish_macd = macd is not None and macd_signal is not None and macd < macd_signal
        
        if bullish_sma and bullish_macd:
            return AgentSignal(
                agent="momentum",
                signal=Signal.BUY,
                confidence=90.0,
                reason=f"Bullish momentum alignment: price above SMA100 ({sma100:.2f}) & MACD > Signal",
                raw_data={"sma100": sma100, "macd": macd, "macd_signal": macd_signal}
            )
        elif bearish_sma and bearish_macd:
            return AgentSignal(
                agent="momentum",
                signal=Signal.SELL,
                confidence=90.0,
                reason=f"Bearish momentum alignment: price below SMA100 ({sma100:.2f}) & MACD < Signal",
                raw_data={"sma100": sma100, "macd": macd, "macd_signal": macd_signal}
            )
        # Partial alignment — fall through to standard RSI/StochRSI scoring below

    score = 0
    max_score = 6
    reasons = []

    # Check trend using EMA200 if scalping mode is enabled
    is_uptrend = False
    is_downtrend = False
    if getattr(settings, "scalping_mode", True) and snap.ema200 and snap.ema200 > 0:
        is_uptrend = snap.close > snap.ema200 * 1.005
        is_downtrend = snap.close < snap.ema200 * 0.995

    # ── RSI ────────────────────────────────────────────
    rsi = snap.rsi
    if is_uptrend and rsi < 40:
        score += 2
        reasons.append(f"RSI oversold pullback in uptrend ({rsi:.1f} < 40) — BUY DIP")
    elif is_downtrend and rsi > 60:
        score -= 2
        reasons.append(f"RSI overbought rally in downtrend ({rsi:.1f} > 60) — SELL RALLY")
    else:
        # Standard trend-following momentum
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

    # Overbought/oversold extremes override (only in standard trend-following mode)
    if not (is_uptrend and rsi < 40) and not (is_downtrend and rsi > 60):
        if rsi >= 80:
            score -= 1
            reasons.append("RSI overbought (≥80) — caution")
        elif rsi <= 20:
            score += 1
            reasons.append("RSI oversold (≤20) — potential reversal")

    # ── Stochastic RSI ─────────────────────────────────
    k, d = snap.stoch_rsi_k, snap.stoch_rsi_d
    if is_uptrend and k < 30:
        score += 2
        reasons.append(f"StochRSI oversold pullback in uptrend (K={k:.0f}, D={d:.0f}) — BUY DIP")
        if k > d:
            score += 1
            reasons.append("StochRSI bullish crossover")
    elif is_downtrend and k > 70:
        score -= 2
        reasons.append(f"StochRSI overbought rally in downtrend (K={k:.0f}, D={d:.0f}) — SELL RALLY")
        if k < d:
            score -= 1
            reasons.append("StochRSI bearish crossover")
    else:
        # Standard trend-following momentum
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
    if is_uptrend and rsi < 40:
        # Do not penalize negative ROC in pullback zones; reward positive rebounds
        if roc > 0:
            score += 1
            reasons.append(f"ROC positive rebound ({roc:.2f}%)")
    elif is_downtrend and rsi > 60:
        # Do not penalize positive ROC in rally zones; reward negative rollovers
        if roc < 0:
            score -= 1
            reasons.append(f"ROC negative rollover ({roc:.2f}%)")
    else:
        # Standard ROC
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
        agent="momentum",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "Neutral momentum",
        raw_data={"score": score, "rsi": rsi, "k": k, "d": d, "roc": roc},
    )

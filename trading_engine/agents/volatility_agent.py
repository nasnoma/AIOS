"""
trading_engine/agents/volatility_agent.py

Agent 5: Volatility Agent
- Detects market regimes: trending / choppy / extreme
- ATR % (normalized), Bollinger Band Width, realized vol
- Can recommend avoiding trade entirely
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 4
    reasons = []
    avoid_trade = False

    # ── ATR % (ATR relative to price) ─────────────────
    atr_pct = (snap.atr / snap.close * 100) if snap.close else 0

    if atr_pct > 8:
        avoid_trade = True
        score -= 3
        reasons.append(f"EXTREME volatility: ATR={atr_pct:.1f}% of price — avoid trade")
    elif atr_pct > 4:
        score -= 1
        reasons.append(f"High volatility: ATR={atr_pct:.1f}% — widen stops")
    elif 1 <= atr_pct <= 3:
        score += 2
        reasons.append(f"Healthy volatility: ATR={atr_pct:.1f}% — good for swing")
    elif atr_pct < 0.5:
        score -= 1
        reasons.append(f"Very low ATR ({atr_pct:.1f}%) — consolidation/choppy")

    # ── Bollinger Band Width ────────────────────────────
    bb_w = snap.bb_width
    if bb_w > 0.15:
        score -= 1
        reasons.append(f"BBands very wide ({bb_w:.3f}) — volatile expansion")
    elif bb_w < 0.03:
        score -= 1
        reasons.append(f"BBands very narrow ({bb_w:.3f}) — squeeze pending (high uncertainty)")
    else:
        score += 1
        reasons.append(f"BBand width normal ({bb_w:.3f})")

    # ── Realized Volatility ─────────────────────────────
    rv = snap.realized_vol
    if rv is not None and rv > 0:
        if rv > 1.5:        # >150% annualized vol
            avoid_trade = True
            score -= 2
            reasons.append(f"Realized vol extreme: {rv:.2f} annualized")
        elif rv > 0.8:
            score -= 1
            reasons.append(f"Realized vol elevated: {rv:.2f}")
        else:
            score += 1
            reasons.append(f"Realized vol manageable: {rv:.2f}")

    # ── Market regime ──────────────────────────────────
    regime = "extreme" if avoid_trade else ("choppy" if score <= 0 else "trending")

    # Volatility agent: HOLD = good conditions to trade; SELL = avoid
    normalized = (score / max_score + 1) / 2
    confidence = round(max(0, min(100, normalized * 100)), 1)

    if avoid_trade:
        signal = Signal.SELL  # "SELL" here means "avoid trading"
        confidence = 85.0
    elif score >= 2:
        signal = Signal.BUY   # "BUY" here means "conditions are good to trade"
    else:
        signal = Signal.HOLD

    return AgentSignal(
        agent="volatility",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "Normal volatility",
        raw_data={"score": score, "atr_pct": atr_pct, "bb_width": bb_w, "regime": regime},
    )

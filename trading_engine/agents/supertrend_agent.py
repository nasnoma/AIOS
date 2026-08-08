"""
trading_engine/agents/supertrend_agent.py

Supertrend Agent — ATR-based trailing trend indicator
Strategy source: 87% win rate backtest (daviddtech, TradingView)
- Uses Supertrend line (ATR × 3.0 multiplier) for trend direction
- Volume filter: only fires when rel_volume > 1.2 (confirms breakout)
- MFI confirmation: MFI > 50 = buying pressure, MFI < 50 = selling pressure
- BUY when Supertrend flips to uptrend (dir=+1) AND volume confirms AND MFI > 50
- SELL when Supertrend flips to downtrend (dir=-1) AND volume confirms AND MFI < 50
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 6
    reasons = []

    st = snap.supertrend
    st_dir = snap.supertrend_dir
    mfi = snap.mfi
    rel_vol = snap.rel_volume

    # ── Supertrend direction ─────────────────────────────────────
    if st_dir == 1 and st > 0:
        score += 2
        reasons.append(f"Supertrend UPTREND (line={st:.2f}, dir=+1)")
    elif st_dir == -1 and st > 0:
        score -= 2
        reasons.append(f"Supertrend DOWNTREND (line={st:.2f}, dir=-1)")
    else:
        reasons.append("Supertrend neutral/undefined")

    # ── Volume filter (confirms trend strength) ──────────────────
    # 87% WR strategy requires volume confirmation on the breakout
    if rel_vol >= 1.5:
        if st_dir == 1:
            score += 2
            reasons.append(f"Volume surge {rel_vol:.1f}× confirms bullish breakout")
        elif st_dir == -1:
            score -= 2
            reasons.append(f"Volume surge {rel_vol:.1f}× confirms bearish breakout")
    elif rel_vol >= 1.2:
        if st_dir == 1:
            score += 1
            reasons.append(f"Above-average volume ({rel_vol:.1f}×) supports uptrend")
        elif st_dir == -1:
            score -= 1
            reasons.append(f"Above-average volume ({rel_vol:.1f}×) supports downtrend")
    else:
        reasons.append(f"Low volume ({rel_vol:.1f}×) — no breakout confirmation")

    # ── MFI confirmation (volume-weighted RSI — better for crypto) ─
    if mfi > 70:
        score += 1
        reasons.append(f"MFI={mfi:.0f} — strong buying pressure")
    elif mfi < 30:
        score -= 1
        reasons.append(f"MFI={mfi:.0f} — strong selling pressure")
    elif mfi > 50:
        score += 0
        reasons.append(f"MFI={mfi:.0f} — mild buying pressure")
    elif mfi < 50:
        score -= 0
        reasons.append(f"MFI={mfi:.0f} — mild selling pressure")

    # ── Price vs Supertrend line (distance = trend strength) ──────
    if st > 0 and snap.close > 0:
        if st_dir == 1:
            # Distance above supertrend = trend strength
            dist_pct = (snap.close - st) / snap.close * 100
            if dist_pct > 1.0:
                score += 1
                reasons.append(f"Price {dist_pct:.1f}% above Supertrend — strong uptrend")
        elif st_dir == -1:
            dist_pct = (st - snap.close) / snap.close * 100
            if dist_pct > 1.0:
                score -= 1
                reasons.append(f"Price {dist_pct:.1f}% below Supertrend — strong downtrend")

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
        agent="supertrend",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No Supertrend signal",
        raw_data={"score": score, "supertrend": st, "st_dir": st_dir, "mfi": mfi, "rel_vol": rel_vol},
    )

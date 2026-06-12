"""
trading_engine/agents/orderflow_agent.py

Agent 4: Order Flow Agent (Crypto-specific)
- Funding rate: positive = longs paying (bearish pressure), negative = shorts paying (bullish)
- Open interest: rising OI in uptrend confirms move; rising OI in downtrend confirms sell-off
- Liquidations: heavy long liqs can be floor; heavy short liqs = squeeze
- For stocks: falls back to volume/options proxy
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 5
    reasons = []

    if snap.asset_type == "stock":
        # For stocks: use relative volume as proxy for institutional flow
        if snap.rel_volume >= 2.0:
            score += 2
            reasons.append(f"Unusual volume spike ({snap.rel_volume:.1f}x) — institutional activity")
        elif snap.rel_volume >= 1.5:
            score += 1
            reasons.append(f"Above-average volume ({snap.rel_volume:.1f}x)")
        else:
            reasons.append("Normal stock volume — no clear institutional signal")
    else:
        # ── Funding Rate ───────────────────────────────────
        fr = snap.funding_rate
        if fr is not None:
            if fr > 0.001:     # > 0.1%
                score -= 1
                reasons.append(f"High positive funding ({fr:.4f}) — overleveraged longs, bearish")
            elif fr < -0.0005: # < -0.05%
                score += 2
                reasons.append(f"Negative funding ({fr:.4f}) — shorts paying, squeeze risk (bullish)")
            elif -0.0003 <= fr <= 0.0003:
                score += 1
                reasons.append(f"Neutral funding ({fr:.4f}) — healthy market")

        # ── Open Interest ──────────────────────────────────
        oi = snap.open_interest
        if oi is not None:
            # We compare to previous OI in the DataFrame if available
            if "open_interest" in snap.df.columns:
                prev_oi = snap.df["open_interest"].iloc[-5]
                oi_change = (oi - prev_oi) / prev_oi if prev_oi else 0
                if oi_change > 0.05:
                    # Rising OI: direction depends on price
                    if snap.close > snap.ema20:
                        score += 2
                        reasons.append(f"Rising OI +{oi_change:.1%} with price up — trend continuation")
                    else:
                        score -= 1
                        reasons.append(f"Rising OI +{oi_change:.1%} with price down — capitulation risk")
            else:
                reasons.append(f"OI data available: {oi:,.0f}")

        # ── Liquidations ───────────────────────────────────
        long_liq = snap.long_liq_24h
        short_liq = snap.short_liq_24h
        if long_liq is not None and short_liq is not None and (long_liq + short_liq) > 0:
            liq_ratio = long_liq / (long_liq + short_liq + 1e-9)
            if liq_ratio > 0.7:
                score += 1
                reasons.append(f"Heavy long liquidations — capitulation floor forming")
            elif liq_ratio < 0.3:
                score -= 1
                reasons.append(f"Heavy short liquidations — short squeeze exhausting")

    normalized = (score / max_score + 1) / 2
    if score >= 2:
        signal = Signal.BUY
        confidence = round(max(0, min(100, normalized * 100)), 1)
    elif score <= -2:
        signal = Signal.SELL
        confidence = round(max(0, min(100, (1 - normalized) * 100)), 1)
    else:
        signal = Signal.HOLD
        confidence = round(max(0, min(100, (normalized if normalized >= 0.5 else 1 - normalized) * 100)), 1)

    return AgentSignal(
        agent="orderflow",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No clear order flow signal",
        raw_data={
            "score": score,
            "funding_rate": snap.funding_rate,
            "open_interest": snap.open_interest,
        },
    )

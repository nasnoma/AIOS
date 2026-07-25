"""
trading_engine/agents/mean_reversion_agent.py

Agent 9: Mean Reversion Agent (Regime-Adaptive)
- Pure quantitative — zero LLM calls
- Only fires in RANGING markets (ADX < 25)
- Buys oversold extremes at lower Bollinger Band
- Sells overbought extremes at upper Bollinger Band
- Returns HOLD in trending markets (ADX >= 25) — lets trend_agent lead

Rationale: trend-following agents bleed in ranges. This agent captures
the ~60% of bars where ADX < 25 by fading BB extremes with RSI confirmation.
"""
from __future__ import annotations
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot


def analyze(snap: MarketSnapshot) -> AgentSignal:
    from trading_engine.config import settings

    adx = snap.adx if isinstance(snap.adx, (int, float)) else 0.0
    ranging_threshold = float(getattr(settings, "mean_reversion_adx_threshold", 25.0))

    # ── Regime gate: only fire in ranging markets ─────────────
    if adx <= 0 or adx >= ranging_threshold:
        return AgentSignal(
            agent="mean_reversion",
            signal=Signal.HOLD,
            confidence=50.0,
            reason=f"ADX={adx:.1f} >= {ranging_threshold:.0f} (trending) — mean-reversion inactive.",
            raw_data={"adx": adx, "regime": "trending"},
        )

    # ── Pull Bollinger Bands from df ──────────────────────────
    try:
        latest = snap.df.iloc[-1]
        bb_upper = float(latest.get("BBU_20_2.0", snap.close * 1.02))
        bb_lower = float(latest.get("BBL_20_2.0", snap.close * 0.98))
    except Exception:
        bb_upper = snap.close * 1.02
        bb_lower = snap.close * 0.98

    bb_mid = (bb_upper + bb_lower) / 2.0
    bb_range = bb_upper - bb_lower
    if bb_range <= 0 or snap.close <= 0:
        return AgentSignal(
            agent="mean_reversion",
            signal=Signal.HOLD,
            confidence=50.0,
            reason="BB range degenerate — no mean-reversion signal.",
            raw_data={"adx": adx, "regime": "ranging"},
        )

    # Distance from mid as % of band half-width (1.0 = at band, >1 = outside)
    half_band = bb_range / 2.0
    dist_from_mid = (snap.close - bb_mid) / half_band if half_band > 0 else 0.0

    rsi = snap.rsi
    rsi_oversold = float(getattr(settings, "mean_reversion_rsi_oversold", 35.0))
    rsi_overbought = float(getattr(settings, "mean_reversion_rsi_overbought", 65.0))

    # ── BUY: price at/below lower BB AND RSI oversold ─────────
    if snap.close <= bb_lower and rsi < rsi_oversold:
        # Confidence scales with how far below the band we are
        stretch = (bb_lower - snap.close) / half_band + 1.0  # 1.0 at band, >1 outside
        confidence = min(95.0, 65.0 + stretch * 15.0)
        return AgentSignal(
            agent="mean_reversion",
            signal=Signal.BUY,
            confidence=round(confidence, 1),
            reason=(
                f"Mean-reversion LONG: price ({snap.close:.4f}) ≤ lower BB ({bb_lower:.4f}) "
                f"with RSI={rsi:.1f} < {rsi_oversold:.0f} in ranging regime (ADX={adx:.1f}). "
                f"Target BB mid={bb_mid:.4f}."
            ),
            raw_data={
                "adx": adx, "regime": "ranging", "bb_upper": bb_upper,
                "bb_lower": bb_lower, "bb_mid": bb_mid, "rsi": rsi,
                "dist_from_mid": dist_from_mid,
            },
        )

    # ── SELL: price at/above upper BB AND RSI overbought ──────
    if snap.close >= bb_upper and rsi > rsi_overbought:
        stretch = (snap.close - bb_upper) / half_band + 1.0
        confidence = min(95.0, 65.0 + stretch * 15.0)
        return AgentSignal(
            agent="mean_reversion",
            signal=Signal.SELL,
            confidence=round(confidence, 1),
            reason=(
                f"Mean-reversion SHORT: price ({snap.close:.4f}) ≥ upper BB ({bb_upper:.4f}) "
                f"with RSI={rsi:.1f} > {rsi_overbought:.0f} in ranging regime (ADX={adx:.1f}). "
                f"Target BB mid={bb_mid:.4f}."
            ),
            raw_data={
                "adx": adx, "regime": "ranging", "bb_upper": bb_upper,
                "bb_lower": bb_lower, "bb_mid": bb_mid, "rsi": rsi,
                "dist_from_mid": dist_from_mid,
            },
        )

    # ── Inside the bands — no edge ────────────────────────────
    # Closer to a band = stronger conviction that reversal is near
    if dist_from_mid > 0.7:
        # Near a band but not extreme enough — soft HOLD leaning toward reversal
        lean = "long" if dist_from_mid < 0 else "short"
        confidence = 55.0 + abs(dist_from_mid) * 10.0
        return AgentSignal(
            agent="mean_reversion",
            signal=Signal.HOLD,
            confidence=round(min(70.0, confidence), 1),
            reason=(
                f"Price near {'lower' if lean == 'long' else 'upper'} BB "
                f"(dist={dist_from_mid:+.2f}) but RSI={rsi:.1f} not extreme enough. Waiting."
            ),
            raw_data={
                "adx": adx, "regime": "ranging", "bb_upper": bb_upper,
                "bb_lower": bb_lower, "bb_mid": bb_mid, "rsi": rsi,
                "dist_from_mid": dist_from_mid,
            },
        )

    return AgentSignal(
        agent="mean_reversion",
        signal=Signal.HOLD,
        confidence=50.0,
        reason=f"Price mid-range (dist={dist_from_mid:+.2f}) — no mean-reversion edge.",
        raw_data={"adx": adx, "regime": "ranging", "bb_mid": bb_mid, "dist_from_mid": dist_from_mid},
    )

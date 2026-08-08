"""
trading_engine/agents/orb_agent.py

Opening Range Breakout (ORB) Agent
Strategy source: 74.6% WR backtested (tradethatswing.com)
- Crypto has no "market open" but uses 00:00 UTC as daily reset
- Opening Range = first 15 minutes of UTC day (configurable)
- BUY when 5m candle close > OR high (bullish breakout)
- SELL when 5m candle close < OR low (bearish breakout)
- Filter: skip if OR range > 1.5% of price (too much already moved)
- Only fires once per day after the OR is established
"""
from __future__ import annotations
from datetime import datetime, timezone
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot
from loguru import logger


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 5
    reasons = []

    df = snap.df
    if df is None or len(df) < 20:
        return AgentSignal(
            agent="orb",
            signal=Signal.HOLD,
            confidence=50.0,
            reason="Insufficient data for ORB.",
            raw_data={},
        )

    # ── Determine the opening range (first 15 min of UTC day) ──────
    # For 5m timeframe: first 3 candles of the UTC day = 15 min
    # For 15m: first 1 candle = 15 min
    # For 1h: first 1 candle = 60 min (wider OR)
    now = datetime.now(timezone.utc)
    today = now.date()

    # Find today's candles
    if df.index.tz is None:
        df_idx_utc = df.index.tz_localize("UTC")
    else:
        df_idx_utc = df.index.tz_convert("UTC")

    today_mask = df_idx_utc.date == today
    today_candles = df[today_mask]

    if len(today_candles) < 2:
        return AgentSignal(
            agent="orb",
            signal=Signal.HOLD,
            confidence=50.0,
            reason=f"ORB: only {len(today_candles)} candle(s) today — OR not established.",
            raw_data={"candles_today": len(today_candles)},
        )

    # Opening range = first 3 candles (15 min on 5m, 45 min on 15m)
    or_candles = today_candles.head(3)
    or_high = float(or_candles["high"].max())
    or_low = float(or_candles["low"].min())
    or_range_pct = (or_high - or_low) / or_low * 100 if or_low > 0 else 0

    # ── Filter: skip if OR too wide (move already happened) ────────
    max_or_range = 1.5  # 1.5% max opening range
    if or_range_pct > max_or_range:
        return AgentSignal(
            agent="orb",
            signal=Signal.HOLD,
            confidence=40.0,
            reason=f"ORB: range {or_range_pct:.2f}% > {max_or_range}% max — too much already moved.",
            raw_data={"or_high": or_high, "or_low": or_low, "or_range_pct": or_range_pct},
        )

    # ── Current candle (last of today) ────────────────────────────
    current_close = snap.close
    current_high = float(today_candles["high"].iloc[-1])
    current_low = float(today_candles["low"].iloc[-1])

    # ── Breakout detection ────────────────────────────────────────
    if current_close > or_high:
        score += 3
        reasons.append(f"Bullish ORB: close {current_close:.4f} > OR high {or_high:.4f}")
    elif current_close < or_low:
        score -= 3
        reasons.append(f"Bearish ORB: close {current_close:.4f} < OR low {or_low:.4f}")
    else:
        reasons.append(f"Inside OR: {or_low:.4f} - {or_high:.4f} (range {or_range_pct:.2f}%)")

    # ── Volume confirmation on breakout ───────────────────────────
    if snap.rel_volume >= 1.3:
        if current_close > or_high:
            score += 1
            reasons.append(f"Volume {snap.rel_volume:.1f}× confirms bullish breakout")
        elif current_close < or_low:
            score -= 1
            reasons.append(f"Volume {snap.rel_volume:.1f}× confirms bearish breakout")
    else:
        reasons.append(f"Volume {snap.rel_volume:.1f}× — no breakout confirmation")

    # ── MFI confirmation ──────────────────────────────────────────
    if snap.mfi > 55 and current_close > or_high:
        score += 1
        reasons.append(f"MFI={snap.mfi:.0f} — buying pressure confirms breakout up")
    elif snap.mfi < 45 and current_close < or_low:
        score -= 1
        reasons.append(f"MFI={snap.mfi:.0f} — selling pressure confirms breakout down")

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
        agent="orb",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons) or "No ORB signal",
        raw_data={
            "score": score,
            "or_high": or_high,
            "or_low": or_low,
            "or_range_pct": or_range_pct,
            "candles_today": len(today_candles),
        },
    )

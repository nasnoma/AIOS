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
    close = df["close"].values[-1]

    if "is_pivot_high" in df.columns and "is_pivot_low" in df.columns:
        # Vectorized precomputed path using numpy (much faster, used in backtests)
        high_vals = df["high"].values
        low_vals = df["low"].values
        piv_high_vals = df["is_pivot_high"].values
        piv_low_vals = df["is_pivot_low"].values

        resistance_levels = high_vals[-lookback:-2][piv_high_vals[-lookback:-2]]
        support_levels = low_vals[-lookback:-2][piv_low_vals[-lookback:-2]]

    else:
        # Fallback original python loop (used in live scans where lookback is small anyway)
        highs = df["high"].values[-lookback:]
        lows = df["low"].values[-lookback:]
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


def _get_ny_daily_range_bounds(df_4h) -> tuple[float, float] | None:
    """
    Finds the high and low of the first 4-hour candle of the current day (New York time).
    """
    import pytz
    from datetime import datetime, timezone
    try:
        df_ny = df_4h.copy()
        if df_ny.index.tz is None:
            df_ny.index = df_ny.index.tz_localize("UTC")
        df_ny.index = df_ny.index.tz_convert("America/New_York")
        
        ny_tz = pytz.timezone("America/New_York")
        current_time_ny = datetime.now(timezone.utc).astimezone(ny_tz)
        current_date_ny = current_time_ny.date()
        
        df_today = df_ny[df_ny.index.date == current_date_ny]
        if len(df_today) == 0:
            # Fallback to the latest date in the dataframe
            latest_date = df_ny.index[-1].date()
            df_today = df_ny[df_ny.index.date == latest_date]
            
        if len(df_today) > 0:
            df_today = df_today.sort_index()
            first_candle = df_today.iloc[0]
            return float(first_candle["high"]), float(first_candle["low"])
    except Exception as e:
        import loguru
        loguru.logger.warning(f"Error computing NY daily range bounds: {e}")
    return None


def analyze(snap: MarketSnapshot) -> AgentSignal:
    from trading_engine.config import settings
    
    # Check if we are in scalping mode and timeframe is short (5m or 15m)
    is_scalping = getattr(settings, "scalping_mode", True) and snap.timeframe in ("5m", "15m")
    
    if is_scalping and snap.htf_4h_snap:
        bounds = _get_ny_daily_range_bounds(snap.htf_4h_snap.df)
        if bounds:
            range_high, range_low = bounds
            df_5m = snap.df
            
            # Check the recent 3 candles (including the current incomplete/closed one) for sweeps
            if len(df_5m) >= 4:
                recent = df_5m.tail(4)
                # Bullish Sweep: low of any of the last 3 candles went below range_low, and current close is above range_low
                swept_low = any(recent["low"].iloc[i] < range_low for i in range(3))
                closed_above_low = recent["close"].iloc[-1] > range_low
                
                # Bearish Sweep: high of any of the last 3 candles went above range_high, and current close is below range_high
                swept_high = any(recent["high"].iloc[i] > range_high for i in range(3))
                closed_below_high = recent["close"].iloc[-1] < range_high
                
                if swept_low and closed_above_low:
                    return AgentSignal(
                        agent="structure",
                        signal=Signal.BUY,
                        confidence=90.0,
                        reason=f"Bullish range sweep: swept daily low ({range_low:.4f}) and closed back inside",
                        raw_data={"range_high": range_high, "range_low": range_low, "sweep": "bullish"}
                    )
                elif swept_high and closed_below_high:
                    return AgentSignal(
                        agent="structure",
                        signal=Signal.SELL,
                        confidence=90.0,
                        reason=f"Bearish range sweep: swept daily high ({range_high:.4f}) and closed back inside",
                        raw_data={"range_high": range_high, "range_low": range_low, "sweep": "bearish"}
                    )
                # No sweep detected — fall through to normal structure analysis
                # (range sweep is a high-conviction entry boost, not a hard gate)

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

    # ── Higher Timeframe S/R Filter ────────────────────
    if snap.htf_snap:
        htf_support, htf_resistance = _find_support_resistance(snap.htf_snap.df)
        if htf_resistance:
            dist_res_pct = (htf_resistance - close) / close
            if 0 <= dist_res_pct <= 0.015:
                score = min(0, score - 4)  # Prevent BUY signal, force HOLD/SELL
                reasons.append(f"HTF Resistance nearby: Major HTF resistance at {htf_resistance:.2f} is within {dist_res_pct*100:.1f}%. Long signals vetoed.")
        if htf_support:
            dist_sup_pct = (close - htf_support) / htf_support
            if 0 <= dist_sup_pct <= 0.02:
                score = max(0, score + 3)  # Boost BUY signal confidence
                reasons.append(f"HTF Support nearby: Price is resting near major HTF support at {htf_support:.2f} (within {dist_sup_pct*100:.1f}%). High-probability entry area.")

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

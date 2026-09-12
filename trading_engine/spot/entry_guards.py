"""
Entry protection helpers for the Spot engine.

Goals: fewer falling knives and capital traps, without killing RANGE cycle velocity.
- Higher-TF (4h) trend veto: pause new buys when the slower trend is bearish
  even if 1h still says RANGE.
- Soft dump brake: pause buys after a fast downside impulse (price + volume).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from loguru import logger

# In-memory dump-brake state (per process / Railway instance)
_DUMP_PAUSE_UNTIL: Dict[str, datetime] = {}
_TREND_CACHE = {}  # symbol -> (epoch_ts, EntryGate)
TREND_CACHE_SEC = 1800


DUMP_LOOKBACK_BARS = 6          # ~6h on 1h candles
DUMP_DROP_PCT = 0.045           # -4.5% over lookback
DUMP_VOL_MULT = 1.6             # vs median volume
DUMP_PAUSE_HOURS = 6.0


@dataclass
class EntryGate:
    allow_buys: bool
    reason: str = ""


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def is_dump_buy_paused(symbol: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sym = _norm(symbol)
    until = _DUMP_PAUSE_UNTIL.get(sym)
    if until and now < until:
        mins = max(1, int((until - now).total_seconds() // 60))
        return True, f"dump-brake pause (~{mins}m left)"
    return False, ""


def arm_dump_brake(symbol: str, hours: float = DUMP_PAUSE_HOURS, reason: str = "") -> None:
    sym = _norm(symbol)
    until = datetime.now(timezone.utc).timestamp() + float(hours) * 3600.0
    _DUMP_PAUSE_UNTIL[sym] = datetime.fromtimestamp(until, tz=timezone.utc)
    logger.warning(f"🛑 [{sym}] Dump brake armed for {hours:.1f}h {('— ' + reason) if reason else ''}")


def check_4h_trend_veto(symbol: str, exchange) -> EntryGate:
    """
    Bearish 4h structure => block new buys (sells/TPs untouched).
    Uses SMA50/SMA200 + ADX/DI on 4h — same language as 1h regime, slower clock.
    """
    import time
    sym = _norm(symbol)
    now_ts = time.time()
    cached = _TREND_CACHE.get(sym)
    if cached and (now_ts - cached[0]) < TREND_CACHE_SEC:
        return cached[1]
    try:
        import pandas as pd
        import pandas_ta as ta

        ohlcv = exchange.fetch_ohlcv(symbol, "4h", limit=220)
        if not ohlcv or len(ohlcv) < 210:
            return EntryGate(True, "")
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df.ta.sma(length=50, append=True)
        df.ta.sma(length=200, append=True)
        df.ta.adx(length=14, append=True)
        latest = df.iloc[-1]
        price = float(latest["close"])
        sma50 = float(latest.get("SMA_50") or 0)
        sma200 = float(latest.get("SMA_200") or 0)
        adx = float(latest.get("ADX_14") or 0)
        plus_di = float(latest.get("DMP_14") or 0)
        minus_di = float(latest.get("DMN_14") or 0)
        if sma50 <= 0 or price <= 0:
            return EntryGate(True, "")
        # Soft bear: below SMA50, -DI dominant, ADX showing trend
        if price < sma50 and minus_di > plus_di and adx >= 22:
            # Stronger if also below SMA200
            strength = "hard" if (sma200 > 0 and price < sma200) else "soft"
            gate = EntryGate(
                False,
                f"4h {strength} trend veto (px<{('SMA200' if strength=='hard' else 'SMA50')}, -DI>+DI, ADX={adx:.1f})",
            )
            _TREND_CACHE[sym] = (now_ts, gate)
            return gate
        gate = EntryGate(True, "")
        _TREND_CACHE[sym] = (now_ts, gate)
        return gate
    except Exception as e:
        logger.debug(f"4h trend veto skipped [{symbol}]: {e}")
        return EntryGate(True, "")


def check_dump_brake(symbol: str, exchange) -> EntryGate:
    """Arm / honor soft dump brake from recent 1h impulse."""
    paused, reason = is_dump_buy_paused(symbol)
    if paused:
        return EntryGate(False, reason)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, "1h", limit=max(40, DUMP_LOOKBACK_BARS + 5))
        if not ohlcv or len(ohlcv) < DUMP_LOOKBACK_BARS + 2:
            return EntryGate(True, "")
        window = ohlcv[-DUMP_LOOKBACK_BARS:]
        highs = [float(c[2]) for c in window]
        closes = [float(c[4]) for c in window]
        vols = [float(c[5] or 0) for c in window]
        peak = max(highs) if highs else 0.0
        last = closes[-1] if closes else 0.0
        if peak <= 0 or last <= 0:
            return EntryGate(True, "")
        drop = (peak - last) / peak
        # median volume of prior bars (exclude last)
        prior = vols[:-1] or vols
        prior_sorted = sorted(v for v in prior if v > 0) or [0.0]
        med = prior_sorted[len(prior_sorted) // 2]
        vol_ok = med <= 0 or vols[-1] >= med * DUMP_VOL_MULT
        if drop >= DUMP_DROP_PCT and vol_ok:
            arm_dump_brake(symbol, DUMP_PAUSE_HOURS, reason=f"−{drop*100:.1f}% / {DUMP_LOOKBACK_BARS}h w/ volume")
            return EntryGate(False, f"dump-brake armed (−{drop*100:.1f}% impulse)")
        return EntryGate(True, "")
    except Exception as e:
        logger.debug(f"dump brake skipped [{symbol}]: {e}")
        return EntryGate(True, "")


def entry_buys_allowed(symbol: str, exchange) -> EntryGate:
    """Combined gate used before placing/building new buys."""
    from trading_engine.spot.asset_guards import is_fee_buffer_asset, is_never_buy_symbol

    if is_fee_buffer_asset(symbol) or is_never_buy_symbol(symbol):
        return EntryGate(False, "fee-buffer hold-only")
    dump = check_dump_brake(symbol, exchange)
    if not dump.allow_buys:
        return EntryGate(False, dump.reason)
    trend = check_4h_trend_veto(symbol, exchange)
    if not trend.allow_buys:
        return EntryGate(False, trend.reason)
    return EntryGate(True, "")


def exitability_score(symbol: str, exchange=None, portfolio=None) -> float:
    """
    Higher = easier to recycle capital via fee-proof limit sells.
    Uses FIFO open-lot age/spread vs last price when available; else neutral 0.
    """
    try:
        from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis

        units = None
        last = 0.0
        if portfolio is not None and hasattr(portfolio, "holdings"):
            h = (portfolio.holdings or {}).get(symbol)
            if h is not None:
                units = float(getattr(h, "units_held", 0) or (h.get("units_held") if isinstance(h, dict) else 0) or 0)
                last = float(getattr(h, "last_price", 0) or (h.get("last_price") if isinstance(h, dict) else 0) or 0)
        if exchange is not None and last <= 0:
            try:
                t = exchange.fetch_ticker(symbol)
                last = float(t.get("last") or 0)
            except Exception:
                pass
        fb = get_fifo_cost_basis(symbol, units_held=units if units and units > 0 else None) or {}
        avg = float(fb.get("avg_cost") or 0)
        age_h = float(fb.get("oldest_buy_age_hours") or 0)
        open_u = float(fb.get("units_open") or 0)
        if avg <= 0 or last <= 0:
            # No inventory drag — mildly positive (fresh capital)
            return 5.0
        edge = (last - avg) / avg  # positive if above cost
        # Prefer above-cost, recently cycling names; penalize aged underwater bags
        score = 10.0 * edge
        if edge >= 0.005:
            score += 8.0
        elif edge >= 0:
            score += 3.0
        else:
            score -= min(15.0, abs(edge) * 40.0)
        if age_h >= 48:
            score -= 6.0
        elif age_h >= 24:
            score -= 3.0
        elif age_h >= 12:
            score -= 1.0
        if open_u <= 0:
            score += 2.0
        return float(score)
    except Exception as e:
        logger.debug(f"exitability_score [{symbol}]: {e}")
        return 0.0

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
_EXIT_CACHE = {}  # symbol -> (epoch_ts, EntryGate)
EXIT_CACHE_SEC = 900


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



# Buy-time exitability: don't open a bag we can't reasonably recycle
EXIT_NEED_PCT_CAP = 0.018          # hard cap: fee-proof TP must be <= 1.8% above buy
EXIT_ATR_MULT = 0.55               # or <= 0.55 * ATR%
EXIT_LOOKBACK_HOURS = 36           # recent high must have tagged TP zone
EXIT_MIN_BUY_USD = 75.0            # typical grid slice for min-net math


def check_exitability_at_buy(
    symbol: str,
    exchange,
    buy_price: float = 0.0,
    buy_usd: float = EXIT_MIN_BUY_USD,
) -> EntryGate:
    """
    Allow a new buy only if a fee-proof take-profit looks reachable soon:
    - premium to fee-proof floor is small vs ATR / capped at ~1.8%, AND
    - recent highs have already traded up through that floor (mean-reversion tape),
      OR the required premium is tiny (<= 0.6%).
    """
    import time
    sym = _norm(symbol)
    # Cache only the no-override price path (roster scans)
    if float(buy_price or 0.0) <= 0:
        now_ts = time.time()
        cached = _EXIT_CACHE.get(sym)
        if cached and (now_ts - cached[0]) < EXIT_CACHE_SEC:
            return cached[1]
    try:
        from trading_engine.spot.sell_guard import min_fee_proof_sell_price, get_fee_factor

        px = float(buy_price or 0.0)
        if px <= 0 and exchange is not None:
            t = exchange.fetch_ticker(symbol)
            px = float(t.get("last") or t.get("bid") or 0.0)
        if px <= 0:
            return EntryGate(True, "")  # fail-open if no price

        notional = max(float(buy_usd or EXIT_MIN_BUY_USD), EXIT_MIN_BUY_USD)
        qty = notional / px
        floor = float(
            min_fee_proof_sell_price(px, qty, fee_factor=get_fee_factor(), min_net_usd=0.60)
        )
        if floor <= px:
            return EntryGate(True, "exitability ok (floor <= buy)")

        need_pct = (floor - px) / px

        atr_pct = 0.0
        recent_high = 0.0
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, "1h", limit=max(48, EXIT_LOOKBACK_HOURS + 2))
            if ohlcv and len(ohlcv) >= 20:
                # simple ATR% proxy: mean true range / price over last 14
                trs = []
                for i in range(1, min(15, len(ohlcv))):
                    h, l, c_prev = float(ohlcv[-i][2]), float(ohlcv[-i][3]), float(ohlcv[-i - 1][4])
                    trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
                if trs:
                    atr_pct = (sum(trs) / len(trs)) / px
                window = ohlcv[-EXIT_LOOKBACK_HOURS:]
                recent_high = max(float(c[2]) for c in window)
        except Exception as e_atr:
            logger.debug(f"exitability ATR/high [{symbol}]: {e_atr}")

        # Recent range already reached our TP → recyclable mean-reversion name
        tagged = recent_high >= floor * 0.998 if recent_high > 0 else False
        atr_ok = atr_pct > 0 and need_pct <= max(EXIT_NEED_PCT_CAP, EXIT_ATR_MULT * atr_pct)
        tiny = need_pct <= 0.006

        if tiny or (tagged and (atr_ok or need_pct <= EXIT_NEED_PCT_CAP)):
            gate = EntryGate(
                True,
                f"exitability ok (need {need_pct*100:.2f}%, atr {atr_pct*100:.2f}%, tagged={tagged})",
            )
        elif tagged and need_pct <= EXIT_NEED_PCT_CAP * 1.25:
            gate = EntryGate(True, f"exitability ok soft (need {need_pct*100:.2f}%, tagged)")
        else:
            gate = EntryGate(
                False,
                (
                    f"exitability veto: fee-proof TP +{need_pct*100:.2f}% "
                    f"(cap {EXIT_NEED_PCT_CAP*100:.1f}% / ATR {atr_pct*100:.2f}%, "
                    f"36h high tagged={tagged})"
                ),
            )
        if float(buy_price or 0.0) <= 0:
            _EXIT_CACHE[sym] = (time.time(), gate)
        return gate
    except Exception as e:
        logger.debug(f"exitability_at_buy [{symbol}]: {e}")
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
    ex = check_exitability_at_buy(symbol, exchange)
    if not ex.allow_buys:
        return EntryGate(False, ex.reason)
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

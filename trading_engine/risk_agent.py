"""
trading_engine/risk_agent.py

Risk Agent — The most important component.
Can VETO any trade regardless of judge verdict.

Features:
- ATR-based stop loss (not fixed %)
- Fractional Kelly Criterion position sizing
- Portfolio heat check (max total open risk)
- Asset correlation filter (prevents double-exposure on correlated assets)
- Hard veto conditions
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from loguru import logger
import math
import numpy as np

from trading_engine.agents.base import Signal
from trading_engine.judge import JudgeVerdict
from trading_engine.data.market_data import MarketSnapshot
from trading_engine.config import settings

# ── Param file (editable by autoresearch optimizer) ────────────────────────────
from pathlib import Path
import json as _json

_RISK_PARAM_PATH = Path(__file__).parent / "autoresearch" / "params" / "risk_thresholds.json"

_RISK_DEFAULTS = {
    "kelly_fraction":       0.25,
    "max_portfolio_heat":   0.10,   # tightened from 0.15 — conservative fallback
    "atr_stop_multiplier":  2.0,
    "corr_soft_threshold":  0.75,
    "corr_hard_threshold":  0.90,
    "max_position_pct":     0.10,
}


def _load_risk_params() -> dict:
    """Load risk thresholds from param file, falling back to hardcoded defaults."""
    if _RISK_PARAM_PATH.exists():
        try:
            data = _json.loads(_RISK_PARAM_PATH.read_text())
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.warning(f"risk_thresholds.json load failed: {e}. Using defaults.")
    return _RISK_DEFAULTS


# ── Correlation groups ─────────────────────────────────────────────────────────
# Assets in the same group are treated as correlated.
# Used to detect double-exposure risk.
CORRELATION_GROUPS = [
    # Major crypto (high beta to BTC)
    {"BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"},
    # US tech equities
    {"AAPL", "MSFT", "NVDA", "TSLA"},
    # US broad market ETFs
    {"SPY", "QQQ", "IWM"},
    # DeFi mid-caps — frequently appear together in oversold scans
    {"NEAR/USDT", "JUP/USDT", "LIT/USDT", "CRV/USDT", "EIGEN/USDT", "AAVE/USDT", "UNI/USDT"},
    # L2 / infrastructure alts
    {"HYPE/USDT", "RUNE/USDT", "APEX/USDT", "OP/USDT", "ARB/USDT", "MANTA/USDT", "STRK/USDT"},
    # L1 alternative chains
    {"AVAX/USDT", "SUI/USDT", "APT/USDT", "SEI/USDT", "INJ/USDT", "TIA/USDT"},
    # Meme / high-volatility tokens
    {"DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "FLOKI/USDT", "WIF/USDT", "BONK/USDT"},
]


# Correlation thresholds — loaded live from param file
# (module-level names kept for backward compatibility)
CORR_SOFT_THRESHOLD = _RISK_DEFAULTS["corr_soft_threshold"]
CORR_HARD_THRESHOLD = _RISK_DEFAULTS["corr_hard_threshold"]


def _get_correlation_group(symbol: str) -> Optional[set]:
    """Return the correlation group a symbol belongs to, or None."""
    for group in CORRELATION_GROUPS:
        if symbol in group:
            return group
    return None


def _compute_price_correlation(
    snap_new: MarketSnapshot,
    existing_snap: MarketSnapshot,
    lookback: int = 30,
) -> float:
    """
    Compute Pearson correlation of log returns between two assets
    over the last `lookback` candles using their OHLCV DataFrames.
    Falls back to 0.85 if data is insufficient (conservative assumption
    for assets in the same group).
    """
    try:
        df_new = snap_new.df["close"].iloc[-lookback:]
        df_existing = existing_snap.df["close"].iloc[-lookback:]

        if len(df_new) < 10 or len(df_existing) < 10:
            return 0.85  # Conservative fallback

        ret_new = np.log(df_new / df_new.shift(1)).dropna()
        ret_existing = np.log(df_existing / df_existing.shift(1)).dropna()

        # Align to same length
        min_len = min(len(ret_new), len(ret_existing))
        ret_new = ret_new.iloc[-min_len:].values
        ret_existing = ret_existing.iloc[-min_len:].values

        corr = float(np.corrcoef(ret_new, ret_existing)[0, 1])
        return corr if not np.isnan(corr) else 0.85
    except Exception as e:
        logger.warning(f"Correlation computation failed: {e}. Using conservative default 0.85.")
        return 0.85


def check_correlation(
    new_symbol: str,
    new_snap: MarketSnapshot,
    open_position_snaps: dict[str, MarketSnapshot],
) -> tuple[float, str]:
    """
    Check correlation of the new signal asset against all open positions.
    Returns (size_multiplier, reason_string).
    - size_multiplier = 1.0  → no adjustment
    - size_multiplier = 0.5  → soft: reduce position size by 50%
    - size_multiplier = 0.0  → hard veto: do not trade
    """
    if not open_position_snaps:
        return 1.0, "No open positions — no correlation adjustment"

    max_corr = 0.0
    most_correlated_symbol = None

    for open_symbol, open_snap in open_position_snaps.items():
        if open_symbol == new_symbol:
            continue

        # Compute rolling log-returns correlation directly for all asset pairs
        corr = _compute_price_correlation(new_snap, open_snap)
        logger.info(
            f"  📊 Correlation {new_symbol} ↔ {open_symbol}: {corr:.3f}"
        )
        if corr > max_corr:
            max_corr = corr
            most_correlated_symbol = open_symbol

    _params = _load_risk_params()
    _soft = _params.get("corr_soft_threshold", CORR_SOFT_THRESHOLD)
    _hard = _params.get("corr_hard_threshold", CORR_HARD_THRESHOLD)

    if max_corr >= _hard:
        return 0.0, (
            f"Correlation veto: {new_symbol} ↔ {most_correlated_symbol} "
            f"correlation={max_corr:.3f} ≥ hard threshold {_hard:.2f}. "
            f"Prevents double-exposure on correlated pair."
        )
    elif max_corr >= _soft:
        return 0.5, (
            f"Correlation soft adjustment: {new_symbol} ↔ {most_correlated_symbol} "
            f"correlation={max_corr:.3f} ≥ soft threshold {_soft:.2f}. "
            f"Position size reduced by 50%."
        )
    else:
        return 1.0, (
            f"Correlation OK: max={max_corr:.3f} below thresholds. No size adjustment."
        )


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size_pct: float    # % of account to use
    position_size_usd: float    # dollar amount
    entry_price: float
    stop_loss: float            # absolute price
    take_profit: float          # absolute price (3:1 R:R default)
    stop_loss_pct: float        # % from entry
    take_profit_pct: float
    risk_reward: float
    max_loss_usd: float         # worst case loss in USD
    atr: float


def _kelly_fraction(win_rate: float, rr_ratio: float, kelly_fraction: float = 0.25) -> float:
    """
    Fractional Kelly Criterion.
    kelly_fraction=0.25 means quarter-kelly (much safer than full kelly).
    """
    if rr_ratio <= 0 or win_rate <= 0:
        return 0.01
    kelly = (win_rate * rr_ratio - (1 - win_rate)) / rr_ratio
    kelly = max(0, kelly)
    return kelly * kelly_fraction


def _get_5m_atr(snap: MarketSnapshot) -> tuple[float, float]:
    """
    Returns (entry_price, atr) on 5m timeframe if available and configured, 
    otherwise falls back to (snap.close, snap.atr).
    """
    from trading_engine.market_hours import classify_symbol, AssetClass
    if not settings.crypto_use_5m_atr:
        return snap.close, snap.atr

    if classify_symbol(snap.symbol) != AssetClass.CRYPTO:
        logger.info(f"   [Stage 3] Asset class is not CRYPTO. Using native timeframe ATR for {snap.symbol} ({snap.timeframe})")
        return snap.close, snap.atr

    if snap.timeframe == "5m":
        return snap.close, snap.atr
        
    try:
        from trading_engine.data.market_data import build_snapshot
        # Fetch the 5m snapshot for the symbol. Set is_htf=True to avoid recursive fetching.
        snap_5m = build_snapshot(snap.symbol, timeframe="5m", is_htf=True)
        if snap_5m and snap_5m.atr > 0:
            logger.info(
                f"   [Stage 3] Precise 5m entry={snap_5m.close:.4f} and ATR={snap_5m.atr:.4f} "
                f"(original tf={snap.timeframe} entry={snap.close:.4f}, ATR={snap.atr:.4f})"
            )
            return snap_5m.close, snap_5m.atr
    except Exception as e:
        logger.warning(f"Failed to fetch precise 5m ATR for {snap.symbol}: {e}. Falling back to default timeframe.")
        
    return snap.close, snap.atr


def _get_btc_regime_fallback() -> tuple[float, float]:
    """
    Fallback to fetch BTC/USDT candles from public Binance/Bybit CCXT to check regime.
    Returns (btc_close, btc_ma).
    Raises Exception if both fail.
    """
    import ccxt
    tf = getattr(settings, "regime_btc_timeframe", "1d")
    ma_period = getattr(settings, "regime_btc_ma_period", 50)
    
    # Try Binance public first, then Bybit public
    for ex_id in ["binance", "bybit"]:
        try:
            ex = getattr(ccxt, ex_id)()
            # Fetch enough candles to compute MA
            limit = ma_period + 10
            ohlcv = ex.fetch_ohlcv("BTC/USDT", timeframe=tf, limit=limit)
            if ohlcv and len(ohlcv) >= ma_period:
                closes = [c[4] for c in ohlcv]
                btc_close = closes[-1]
                btc_ma = sum(closes[-ma_period:]) / ma_period
                logger.info(f"  🚦 Regime filter fallback ({ex_id}): BTC close={btc_close:.2f}, MA={btc_ma:.2f}")
                return btc_close, btc_ma
        except Exception as e:
            logger.warning(f"Regime filter fallback failed on {ex_id}: {e}")
    raise RuntimeError("All public fallback exchanges failed to fetch BTC history")


def evaluate(
    verdict: JudgeVerdict,
    snap: MarketSnapshot,
    current_portfolio_heat: float = 0.0,   # existing open risk as % of account
    historical_win_rate: float = 0.50,      # default until we have real track record
    open_positions: int = 0,
    open_position_snaps: dict[str, MarketSnapshot] = None,  # symbol → snapshot for correlation check
    daily_pnl_usd: float = 0.0,            # today's realized PnL (negative = loss)
    account_size: Optional[float] = None,
) -> RiskDecision:
    """
    Run full risk assessment. Returns RiskDecision with approved=True/False.
    """
    # ── Stage 3: Precise 5M Entry & ATR stop loss calculation ───────
    entry, atr = _get_5m_atr(snap)
    account = account_size if account_size is not None else settings.account_size
    max_risk_per_trade = settings.max_risk_per_trade


    # Load risk thresholds live from param file (optimizer can update without restart)
    _rp = _load_risk_params()
    max_heat = _rp.get("max_portfolio_heat", settings.max_portfolio_heat)
    kelly_frac = _rp.get("kelly_fraction", settings.kelly_fraction)

    # Crypto peak session check
    from trading_engine.market_hours import classify_symbol, AssetClass, is_crypto_peak_session
    import datetime
    
    asset_class = classify_symbol(snap.symbol)
    is_crypto = asset_class == AssetClass.CRYPTO
    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
    in_peak = is_crypto_peak_session(now_utc) if is_crypto else True

    # ── Hard Veto Conditions ───────────────────────────
    if not verdict.approved:
        return RiskDecision(
            approved=False,
            reason=f"Judge did not approve trade (agreement={verdict.agreement}, conf={verdict.confidence:.0f}%)",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    if atr == 0:
        return RiskDecision(
            approved=False, reason="ATR is zero — cannot calculate stop loss (bad data)",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=0,
        )

    if snap.bb_width and snap.bb_width > 0.12:
        return RiskDecision(
            approved=False, reason=f"Market too volatile: BBand width={snap.bb_width:.3f} > 0.12",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    # Volatility Check: Low Volatility Filter
    atr_pct = (atr / entry * 100) if entry > 0 else 0
    min_atr = getattr(settings, "min_atr_pct", 0.15)
    if atr_pct < min_atr:
        return RiskDecision(
            approved=False,
            reason=f"Volatility filter veto: ATR% ({atr_pct:.2f}%) is below minimum threshold ({min_atr:.2f}%) — market too flat/ranging.",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    min_bb = getattr(settings, "min_bb_width", 0.015)
    if snap.bb_width is not None and snap.bb_width < min_bb:
        return RiskDecision(
            approved=False,
            reason=f"Volatility filter veto: BBand width ({snap.bb_width:.3f}) is below minimum threshold ({min_bb:.3f}) — market too flat/ranging.",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    # Session Check: Crypto Peak Sessions Veto
    if is_crypto and not in_peak and getattr(settings, "crypto_peak_sessions_only", False):
        return RiskDecision(
            approved=False,
            reason=f"Crypto session veto: Current time ({now_utc.strftime('%H:%M')} UTC) is outside configured peak sessions.",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    if current_portfolio_heat >= max_heat:
        return RiskDecision(
            approved=False,
            reason=f"Portfolio heat maxed: {current_portfolio_heat:.1%} >= limit {max_heat:.1%}",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    max_positions = settings.max_concurrent_positions
    if open_positions >= max_positions:
        return RiskDecision(
            approved=False, reason=f"Max concurrent positions reached ({open_positions} >= {max_positions})",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    # ── Circuit Breaker ─────────────────────────────────────
    # Halt all new entries once today's realized loss breaches the limit.
    daily_loss_limit = getattr(settings, "daily_loss_limit_usd", 300.0)
    if daily_loss_limit > 0 and daily_pnl_usd < -abs(daily_loss_limit):
        return RiskDecision(
            approved=False,
            reason=(
                f"Circuit breaker: daily realized loss ${daily_pnl_usd:,.2f} ≤ -${daily_loss_limit:,.0f} limit. "
                "No new entries until tomorrow."
            ),
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    # ── Market Regime Filter ─────────────────────────────────
    # Block LONG crypto trades when BTC is below its 50-period MA.
    # This prevents blindly buying into a bear-market downtrend.
    if (
        is_crypto
        and verdict.decision == Signal.BUY
        and getattr(settings, "regime_filter_enabled", True)
    ):
        try:
            from trading_engine.data.market_data import build_snapshot as _bs
            ma_period = getattr(settings, "regime_btc_ma_period", 50)
            btc_close, btc_ma = None, None
            try:
                tf = getattr(settings, "regime_btc_timeframe", "1d")
                btc_snap = _bs("BTC/USDT", timeframe=tf)
                btc_close = btc_snap.close
                # Use the EMA50 already computed on the snapshot if available,
                # otherwise fall back to computing a simple MA from the OHLCV df.
                btc_ma = getattr(btc_snap, "ema50", None)
                if not btc_ma or btc_ma == 0:
                    btc_ma = btc_snap.df["close"].iloc[-ma_period:].mean()
            except Exception as e_bs:
                logger.warning(f"  Regime filter primary snapshot failed ({e_bs}); attempting fallback query...")
                btc_close, btc_ma = _get_btc_regime_fallback()

            if btc_close is not None and btc_ma is not None:
                if btc_close < btc_ma:
                    logger.warning(
                        f"  🚦 Regime filter: BTC/USDT {btc_close:.2f} < {ma_period}-MA {btc_ma:.2f} — blocking LONG for {snap.symbol}"
                    )
                    return RiskDecision(
                        approved=False,
                        reason=(
                            f"Market regime filter: BTC ({btc_close:.2f}) is below its {ma_period}-period MA "
                            f"({btc_ma:.2f}). Longs paused during bear trend."
                        ),
                        position_size_pct=0, position_size_usd=0,
                        entry_price=entry, stop_loss=0, take_profit=0,
                        stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
                        max_loss_usd=0, atr=atr,
                    )
                else:
                    logger.info(
                        f"  ✅ Regime filter: BTC {btc_close:.2f} > {ma_period}-MA {btc_ma:.2f} — bull regime OK"
                    )
        except Exception as _re:
            logger.warning(f"  Regime filter check failed ({_re}); allowing trade to proceed.")

    # ── Asset Correlation Filter ───────────────────────
    corr_multiplier = 1.0
    corr_reason = "No correlation check (no existing positions)"
    if open_position_snaps:
        corr_multiplier, corr_reason = check_correlation(
            snap.symbol, snap, open_position_snaps
        )
        logger.info(f"  🔗 Correlation filter: {corr_reason}")

    if corr_multiplier == 0.0:
        return RiskDecision(
            approved=False,
            reason=corr_reason,
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    # ── ATR-Based Stop Loss ────────────────────────────
    # Stop = 1.5x ATR below entry (long), above entry (short)
    atr_multiplier = _rp.get("atr_stop_multiplier", settings.atr_multiplier)
    stop_distance = atr * atr_multiplier

    # Enforce minimum stop distance of 1% of entry price.
    # ATR on 5m bars can be as low as 0.07%, which puts SL inside the spread
    # and makes TP unreachably small. 1% floor gives meaningful SL/TP levels.
    min_stop_pct = _rp.get("min_stop_pct", 0.01)  # default 1%
    min_stop_distance = entry * min_stop_pct
    if stop_distance < min_stop_distance:
        logger.info(
            f"ATR stop ({stop_distance:.4f}) below 1% minimum ({min_stop_distance:.4f}). "
            f"Widening stop to {min_stop_pct:.1%} of entry."
        )
        stop_distance = min_stop_distance

    if verdict.decision == Signal.BUY:
        stop_loss = entry - stop_distance
        stop_loss_pct = stop_distance / entry
        rr_ratio = settings.rr_ratio  # target risk/reward
        take_profit = entry + (stop_distance * rr_ratio)
        take_profit_pct = (take_profit - entry) / entry
    else:  # SELL (short)
        stop_loss = entry + stop_distance
        stop_loss_pct = stop_distance / entry
        rr_ratio = settings.rr_ratio
        take_profit = entry - (stop_distance * rr_ratio)
        take_profit_pct = (entry - take_profit) / entry

    # Hard cap: if stop > max allowed pct away, reject (too risky)
    if stop_loss_pct > settings.stop_loss_pct_max:
        return RiskDecision(
            approved=False,
            reason=f"Stop loss too wide: {stop_loss_pct:.1%} > {settings.stop_loss_pct_max:.1%} max (ATR={atr:.2f})",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=stop_loss, take_profit=take_profit,
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            risk_reward=rr_ratio, max_loss_usd=0, atr=atr,
        )

    # ── Kelly / Volatility Position Sizing ────────────
    kelly = _kelly_fraction(historical_win_rate, rr_ratio, kelly_frac)
    # Risk-based position size: never risk more than max_risk_per_trade
    risk_based_size = max_risk_per_trade / stop_loss_pct
    kelly_size = kelly
    max_pos_pct = _rp.get("max_position_pct", 0.25)
    # Take the minimum of kelly and risk-based cap
    position_size_pct = min(kelly_size, risk_based_size, max_pos_pct)

    # ── Confidence-Weighted Position Sizing ────────────────
    # Scale size by judge confidence: high conviction → larger, borderline → smaller.
    confidence_weight = 1.0
    if getattr(settings, "confidence_sizing_enabled", True):
        conf = float(verdict.confidence)  # range [0, 100]
        min_c = float(getattr(settings, "min_avg_confidence", 48.0))
        max_c = 100.0
        min_w = float(getattr(settings, "position_size_min_weight", 0.6))
        max_w = float(getattr(settings, "position_size_max_weight", 1.4))
        # Linear interpolation: min_w at min_c, max_w at max_c
        span = max_c - min_c
        if span > 0:
            confidence_weight = min_w + (max_w - min_w) * (conf - min_c) / span
        confidence_weight = max(min_w, min(max_w, confidence_weight))
        logger.info(
            f"  💡 Confidence-weight: conf={conf:.0f} → size×{confidence_weight:.3f}"
            f" (range [{min_w}×–{max_w}×])"
        )

    # Apply all sizing multipliers in order: confidence → correlation
    position_size_pct *= confidence_weight
    position_size_pct *= corr_multiplier

    # Apply short multiplier for short positions (Signal.SELL) to protect capital against altcoin short squeezes
    short_multiplier = 1.0
    if verdict.decision == Signal.SELL:
        short_multiplier = getattr(settings, "short_position_multiplier", 0.75)
        position_size_pct *= short_multiplier

    position_size_usd = account * position_size_pct
    max_loss_usd = position_size_usd * stop_loss_pct

    # Remaining heat check
    remaining_heat = max_heat - current_portfolio_heat
    risk_per_trade = position_size_pct * stop_loss_pct
    if risk_per_trade > remaining_heat:
        position_size_pct = min(position_size_pct, max(0.0, remaining_heat) / stop_loss_pct)
        position_size_usd = account * position_size_pct
        max_loss_usd = position_size_usd * stop_loss_pct

    # Enforce a minimum size of $100.0 so that wins at 3:1 R:R produce ~$3 per trade
    min_size_usd = 100.0
    if position_size_usd < min_size_usd:
        position_size_usd = min_size_usd
        position_size_pct = position_size_usd / account if account > 0 else 0.0
        max_loss_usd = position_size_usd * stop_loss_pct

    # Session Sizing Check: Crypto Peak Sessions Size Reduction
    session_multiplier = 1.0
    if is_crypto and not in_peak and getattr(settings, "crypto_peak_sessions_reduce_size", True):
        session_multiplier = 0.5
        position_size_pct *= session_multiplier
        position_size_usd *= session_multiplier
        max_loss_usd *= session_multiplier

    session_tag = f" | Session×{session_multiplier:.1f}" if session_multiplier < 1.0 else ""
    corr_tag = f" | Corr×{corr_multiplier:.1f}" if corr_multiplier < 1.0 else ""
    conf_tag = f" | Conf×{confidence_weight:.2f}" if abs(confidence_weight - 1.0) > 0.01 else ""
    short_tag = f" | Short×{short_multiplier:.2f}" if short_multiplier < 1.0 else ""
    logger.success(
        f"Risk APPROVED | {verdict.decision.value} {snap.symbol} | "
        f"Size={position_size_pct:.1%} (${position_size_usd:,.0f}){conf_tag}{corr_tag}{session_tag}{short_tag} | "
        f"SL={stop_loss_pct:.1%} | TP={take_profit_pct:.1%} | R:R={rr_ratio:.1f} | "
        f"Max loss=${max_loss_usd:,.0f}"
    )

    return RiskDecision(
        approved=True,
        reason=f"Trade approved. Kelly sizing: {position_size_pct:.1%} of account.",
        position_size_pct=round(position_size_pct, 4),
        position_size_usd=round(position_size_usd, 2),
        entry_price=entry,
        stop_loss=round(stop_loss, 4),
        take_profit=round(take_profit, 4),
        stop_loss_pct=round(stop_loss_pct, 4),
        take_profit_pct=round(take_profit_pct, 4),
        risk_reward=rr_ratio,
        max_loss_usd=round(max_loss_usd, 2),
        atr=atr,
    )

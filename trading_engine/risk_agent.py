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

# ── Correlation groups ─────────────────────────────────────────────────────────
# Assets in the same group are treated as correlated.
# Used to detect double-exposure risk.
CORRELATION_GROUPS = [
    {"BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"},  # Major crypto (high beta)
    {"AAPL", "MSFT", "NVDA", "TSLA"},                    # US tech equities
    {"SPY", "QQQ", "IWM"},                               # US broad market ETFs
]

# Correlation thresholds
CORR_SOFT_THRESHOLD = 0.75   # Reduce position size by 50%
CORR_HARD_THRESHOLD = 0.90   # Veto trade entirely


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

    new_group = _get_correlation_group(new_symbol)
    max_corr = 0.0
    most_correlated_symbol = None

    for open_symbol, open_snap in open_position_snaps.items():
        if open_symbol == new_symbol:
            continue

        # First check group membership (fast path)
        open_group = _get_correlation_group(open_symbol)
        if new_group and open_group and new_group == open_group:
            # They are in the same group — compute actual rolling correlation
            corr = _compute_price_correlation(new_snap, open_snap)
            logger.info(
                f"  📊 Correlation {new_symbol} ↔ {open_symbol}: {corr:.3f}"
            )
            if corr > max_corr:
                max_corr = corr
                most_correlated_symbol = open_symbol
        else:
            # Different groups — assume negligible correlation
            logger.debug(
                f"  📊 {new_symbol} ↔ {open_symbol}: different groups, skipping correlation check"
            )

    if max_corr >= CORR_HARD_THRESHOLD:
        return 0.0, (
            f"Correlation veto: {new_symbol} ↔ {most_correlated_symbol} "
            f"correlation={max_corr:.3f} ≥ hard threshold {CORR_HARD_THRESHOLD:.2f}. "
            f"Prevents double-exposure on correlated pair."
        )
    elif max_corr >= CORR_SOFT_THRESHOLD:
        return 0.5, (
            f"Correlation soft adjustment: {new_symbol} ↔ {most_correlated_symbol} "
            f"correlation={max_corr:.3f} ≥ soft threshold {CORR_SOFT_THRESHOLD:.2f}. "
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


def evaluate(
    verdict: JudgeVerdict,
    snap: MarketSnapshot,
    current_portfolio_heat: float = 0.0,   # existing open risk as % of account
    historical_win_rate: float = 0.50,      # default until we have real track record
    open_positions: int = 0,
    open_position_snaps: dict[str, MarketSnapshot] = None,  # symbol → snapshot for correlation check
) -> RiskDecision:
    """
    Run full risk assessment. Returns RiskDecision with approved=True/False.
    """
    entry = snap.close
    atr = snap.atr
    account = settings.account_size
    max_risk_per_trade = settings.max_risk_per_trade
    max_heat = settings.max_portfolio_heat
    kelly_frac = settings.kelly_fraction

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

    if current_portfolio_heat >= max_heat:
        return RiskDecision(
            approved=False,
            reason=f"Portfolio heat maxed: {current_portfolio_heat:.1%} >= limit {max_heat:.1%}",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

    if open_positions >= 5:
        return RiskDecision(
            approved=False, reason=f"Max concurrent positions reached ({open_positions})",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=0, take_profit=0,
            stop_loss_pct=0, take_profit_pct=0, risk_reward=0,
            max_loss_usd=0, atr=atr,
        )

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
    atr_multiplier = 1.5
    stop_distance = atr * atr_multiplier

    if verdict.decision == Signal.BUY:
        stop_loss = entry - stop_distance
        stop_loss_pct = stop_distance / entry
        rr_ratio = 3.0  # target 3:1 risk/reward
        take_profit = entry + (stop_distance * rr_ratio)
        take_profit_pct = (take_profit - entry) / entry
    else:  # SELL (short)
        stop_loss = entry + stop_distance
        stop_loss_pct = stop_distance / entry
        rr_ratio = 3.0
        take_profit = entry - (stop_distance * rr_ratio)
        take_profit_pct = (entry - take_profit) / entry

    # Hard cap: if stop > 8% away, reject (too risky)
    if stop_loss_pct > 0.08:
        return RiskDecision(
            approved=False,
            reason=f"Stop loss too wide: {stop_loss_pct:.1%} > 8% max (ATR={atr:.2f})",
            position_size_pct=0, position_size_usd=0,
            entry_price=entry, stop_loss=stop_loss, take_profit=take_profit,
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            risk_reward=rr_ratio, max_loss_usd=0, atr=atr,
        )

    # ── Kelly Position Sizing ─────────────────────────
    kelly = _kelly_fraction(historical_win_rate, rr_ratio, kelly_frac)

    # Risk-based position size: never risk more than max_risk_per_trade
    risk_based_size = max_risk_per_trade / stop_loss_pct
    kelly_size = kelly

    # Take the minimum of kelly and risk-based cap
    position_size_pct = min(kelly_size, risk_based_size, 0.10)  # hard cap 10% of account

    # Apply correlation multiplier (1.0 = full size, 0.5 = half size due to high correlation)
    position_size_pct *= corr_multiplier

    position_size_usd = account * position_size_pct
    max_loss_usd = position_size_usd * stop_loss_pct

    # Remaining heat check
    remaining_heat = max_heat - current_portfolio_heat
    if max_risk_per_trade > remaining_heat:
        position_size_pct = (remaining_heat / stop_loss_pct)
        position_size_usd = account * position_size_pct
        max_loss_usd = position_size_usd * stop_loss_pct

    corr_tag = f" | Corr×{corr_multiplier:.1f}" if corr_multiplier < 1.0 else ""
    logger.success(
        f"Risk APPROVED | {verdict.decision.value} {snap.symbol} | "
        f"Size={position_size_pct:.1%} (${position_size_usd:,.0f}){corr_tag} | "
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

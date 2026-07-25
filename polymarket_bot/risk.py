"""
polymarket_bot/risk.py

Risk controls — pure functions, no I/O.
Applied before every trade execution.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from polymarket_bot.config import Settings
from polymarket_bot.strategy import Signal, SignalType


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_usd_yes: float = 0.0   # USDC to spend on YES leg
    size_usd_no: float = 0.0    # USDC to spend on NO leg
    total_size_usd: float = 0.0


def compute_position_size(
    account_size: float,
    cash: float,
    max_risk_pct: float,
    entry_price: float,
    n_legs: int = 1,
) -> float:
    """
    Compute position size in USDC for a single leg.
    Caps at max_risk_pct of account_size and available cash.

    Args:
        account_size:   Total virtual/real account size in USD.
        cash:           Currently available cash.
        max_risk_pct:   Maximum fraction of account to risk per trade.
        entry_price:    Share price (0–1). Max loss = entry_price per share.
        n_legs:         1 for single-leg, 2 for both legs (budget is split).
    Returns:
        Size in USDC to allocate to one leg.
    """
    max_by_risk = account_size * max_risk_pct
    # For spread_arb, split budget equally across both legs
    leg_budget = max_by_risk / n_legs
    # Don't exceed available cash
    leg_budget = min(leg_budget, cash / n_legs)
    # Minimum trade guard
    return round(max(leg_budget, 0.0), 2)


def check_daily_loss_limit(daily_pnl: float, limit_usd: float) -> bool:
    """Returns True if the circuit breaker should HALT trading."""
    if limit_usd <= 0:
        return False
    return daily_pnl <= -abs(limit_usd)


def evaluate(
    signal: Signal,
    account_size: float,
    cash: float,
    daily_pnl: float,
    open_position_count: int,
    cfg: Settings,
) -> RiskDecision:
    """
    Run all risk checks. Returns RiskDecision with approved=True/False.

    Priority of checks:
      1. Signal must not be NO_SIGNAL
      2. Circuit breaker: daily loss limit
      3. Max concurrent positions
      4. Sufficient cash
    """
    _deny = lambda reason: RiskDecision(approved=False, reason=reason)

    # ── 1. Valid signal ─────────────────────────────────────────────────────
    if signal.signal_type == SignalType.NO_SIGNAL:
        return _deny("No signal")

    # ── 2. Circuit breaker ──────────────────────────────────────────────────
    if check_daily_loss_limit(daily_pnl, cfg.max_daily_loss_usd):
        return _deny(
            f"Circuit breaker: daily PnL ${daily_pnl:,.2f} ≤ "
            f"-${cfg.max_daily_loss_usd:,.2f}. No new trades until tomorrow."
        )

    # Temporary live guardrail: Pause if daily/session PnL is <= -$20.00
    if daily_pnl <= -20.0:
        return _deny(
            f"Temporary session guardrail: daily/session PnL ${daily_pnl:,.2f} <= -$20.00. Trading paused."
        )

    # ── 3. Max concurrent positions ─────────────────────────────────────────
    if open_position_count >= cfg.max_concurrent_positions:
        return _deny(
            f"Max concurrent positions reached ({open_position_count}/{cfg.max_concurrent_positions})"
        )

    # ── 4. Position sizing ──────────────────────────────────────────────────
    n_legs = 2 if signal.signal_type == SignalType.SPREAD_ARB else 1
    min_trade_usd = 1.10  # Hard floor to avoid dust positions (Polymarket min is $1.00)

    if signal.signal_type == SignalType.SPREAD_ARB:
        yes_price = signal.entry_price_yes or 0.5
        no_price = signal.entry_price_no or 0.5
        size_yes = compute_position_size(account_size, cash, cfg.max_risk_per_trade_pct, yes_price, n_legs=2)
        size_no = compute_position_size(account_size, cash, cfg.max_risk_per_trade_pct, no_price, n_legs=2)
        total = size_yes + size_no

        if total < min_trade_usd * 2 or cash < total:
            return _deny(f"Insufficient cash for spread arb: need ${total:.2f}, have ${cash:.2f}")

        return RiskDecision(
            approved=True,
            reason=f"SPREAD_ARB approved | size_yes=${size_yes:.2f} size_no=${size_no:.2f}",
            size_usd_yes=size_yes,
            size_usd_no=size_no,
            total_size_usd=total,
        )

    else:
        # Single-leg momentum trade
        entry_price = signal.entry_price_yes or signal.entry_price_no or 0.5
        size = compute_position_size(account_size, cash, cfg.max_risk_per_trade_pct, entry_price, n_legs=1)

        # Cap base size at $25.00 for live guardrail (strong signals max $25.00)
        size = min(size, 25.00)

        # Size Management: scale size down by 50% if confidence < 45%, and by 50% if daily_pnl is negative (drawdown protection)
        size_factor = 1.0
        if signal.confidence < 0.45:
            size_factor *= 0.5
        if daily_pnl < 0:
            size_factor *= 0.5

        if size_factor < 1.0:
            size = max(min_trade_usd, round(size * size_factor, 2))

        if size < min_trade_usd or cash < size:
            return _deny(f"Insufficient cash for momentum trade: need ${size:.2f}, have ${cash:.2f}")

        size_yes = size if signal.buy_yes else 0.0
        size_no = size if signal.buy_no else 0.0

        factor_str = f" (scaled by {size_factor:.2f}x)" if size_factor < 1.0 else ""
        return RiskDecision(
            approved=True,
            reason=f"{signal.signal_type.value} approved | size=${size:.2f}{factor_str}",
            size_usd_yes=size_yes,
            size_usd_no=size_no,
            total_size_usd=size,
        )

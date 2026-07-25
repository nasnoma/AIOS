"""
tests/test_risk.py
Unit tests for risk evaluation — no I/O.
"""
import pytest
from polymarket_bot.config import Settings
from polymarket_bot.risk import (
    RiskDecision, check_daily_loss_limit, compute_position_size, evaluate
)
from polymarket_bot.strategy import Signal, SignalType


def _cfg(**overrides) -> Settings:
    defaults = dict(
        polymarket_private_key="",
        openrouter_api_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        spread_arb_threshold=0.98,
        momentum_threshold_usd=15.0,
        min_confidence=0.70,
        max_entry_price=0.95,
        entry_window_min_s=45,
        entry_window_max_s=270,
        momentum_lookback_s=30,
        max_risk_per_trade_pct=0.01,
        max_concurrent_positions=3,
        max_daily_loss_usd=100.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _signal(signal_type: SignalType = SignalType.MOMENTUM_LONG, **kwargs) -> Signal:
    defaults = dict(
        asset="BTC",
        buy_yes=True,
        buy_no=False,
        entry_price_yes=0.75,
        entry_price_no=None,
        implied_prob=0.75,
        momentum_usd=20.0,
        confidence=0.80,
        elapsed_s=120,
        remaining_s=180,
    )
    defaults.update(kwargs)
    return Signal(signal_type=signal_type, **defaults)


# ── compute_position_size ─────────────────────────────────────────────────────

def test_position_size_bounded_by_risk_pct():
    # 1% of $1000 = $10
    size = compute_position_size(1000, 1000, 0.01, 0.75)
    assert size == pytest.approx(10.0, abs=0.01)


def test_position_size_bounded_by_cash():
    # Only $5 available → size = $5 / 1 leg = $5
    size = compute_position_size(1000, 5, 0.01, 0.75)
    assert size <= 5.0


def test_position_size_split_across_two_legs():
    # 2 legs → each gets $10 / 2 = $5
    size = compute_position_size(1000, 1000, 0.01, 0.75, n_legs=2)
    assert size == pytest.approx(5.0, abs=0.01)


# ── check_daily_loss_limit ────────────────────────────────────────────────────

def test_circuit_breaker_triggered_on_large_loss():
    assert check_daily_loss_limit(-150.0, 100.0) is True


def test_circuit_breaker_not_triggered_on_small_loss():
    assert check_daily_loss_limit(-50.0, 100.0) is False


def test_circuit_breaker_disabled_when_limit_zero():
    assert check_daily_loss_limit(-999.0, 0.0) is False


# ── evaluate ─────────────────────────────────────────────────────────────────

def test_risk_approved_for_valid_signal():
    cfg = _cfg()
    sig = _signal()
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=0, open_position_count=0, cfg=cfg)
    assert dec.approved is True
    assert dec.total_size_usd > 0


def test_risk_denied_no_signal():
    cfg = _cfg()
    sig = _signal(SignalType.NO_SIGNAL)
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=0, open_position_count=0, cfg=cfg)
    assert dec.approved is False


def test_risk_denied_circuit_breaker():
    cfg = _cfg(max_daily_loss_usd=100.0)
    sig = _signal()
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=-150.0, open_position_count=0, cfg=cfg)
    assert dec.approved is False
    assert "Circuit breaker" in dec.reason


def test_risk_denied_max_positions():
    cfg = _cfg(max_concurrent_positions=2)
    sig = _signal()
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=0, open_position_count=2, cfg=cfg)
    assert dec.approved is False
    assert "Max concurrent" in dec.reason


def test_risk_denied_insufficient_cash():
    cfg = _cfg()
    sig = _signal()
    dec = evaluate(sig, account_size=1000, cash=0.5, daily_pnl=0, open_position_count=0, cfg=cfg)
    assert dec.approved is False


def test_spread_arb_approved_and_splits_size():
    cfg = _cfg()
    sig = Signal(
        signal_type=SignalType.SPREAD_ARB,
        asset="BTC",
        buy_yes=True,
        buy_no=True,
        entry_price_yes=0.47,
        entry_price_no=0.48,
        yes_no_sum=0.95,
        confidence=0.90,
        elapsed_s=100,
        remaining_s=200,
    )
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=0, open_position_count=0, cfg=cfg)
    assert dec.approved is True
    assert dec.size_usd_yes > 0
    assert dec.size_usd_no > 0


def test_risk_size_scaled_on_low_confidence():
    cfg = _cfg()
    sig = _signal(confidence=0.38)  # < 0.45
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=0, open_position_count=0, cfg=cfg)
    assert dec.approved is True
    # Base size = $10 (not capped by $25.00 limit). Low confidence scales to $5.00.
    assert dec.total_size_usd == pytest.approx(5.00, abs=0.01)


def test_risk_size_scaled_on_negative_pnl():
    cfg = _cfg()
    sig = _signal(confidence=0.80)  # > 0.45
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=-10.0, open_position_count=0, cfg=cfg)
    assert dec.approved is True
    # Base size = $10 (not capped by $25.00 limit). Negative daily P&L scales to $5.00.
    assert dec.total_size_usd == pytest.approx(5.00, abs=0.01)


def test_risk_size_scaled_on_both():
    cfg = _cfg()
    sig = _signal(confidence=0.38)  # < 0.45
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=-10.0, open_position_count=0, cfg=cfg)
    assert dec.approved is True
    # Base size = $10 (not capped by $25.00 limit). Both scale size down to $2.50 (25%).
    assert dec.total_size_usd == pytest.approx(2.50, abs=0.01)


def test_risk_denied_session_drawdown_limit():
    cfg = _cfg()
    sig = _signal()
    dec = evaluate(sig, account_size=1000, cash=1000, daily_pnl=-25.0, open_position_count=0, cfg=cfg)
    assert dec.approved is False
    assert "Temporary session guardrail" in dec.reason


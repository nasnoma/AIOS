"""
tests/test_strategy.py
Unit tests for signal evaluation — pure functions, no I/O.
"""
import pytest
from unittest.mock import MagicMock
from polymarket_bot.clob_client import OrderBook, OrderBookLevel
from polymarket_bot.strategy import SignalType, evaluate_signals
from polymarket_bot.config import Settings


def _make_book(mid: float, token_id: str = "tok") -> OrderBook:
    """Create a mock order book with a given mid price."""
    spread = 0.005
    bid = mid - spread / 2
    ask = mid + spread / 2
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=bid, size=100)],
        asks=[OrderBookLevel(price=ask, size=100)],
    )


def _cfg(**overrides) -> Settings:
    """Build a Settings object with defaults suitable for testing."""
    defaults = dict(
        polymarket_private_key="",
        openrouter_api_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        spread_arb_threshold=0.98,
        momentum_threshold_usd=15.0,
        min_confidence=0.70,
        min_signal_confidence=0.0,   # tests override explicit filters themselves
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



# ── Entry window guard ────────────────────────────────────────────────────────

def test_no_signal_before_entry_window():
    cfg = _cfg()
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.55),
        book_no=_make_book(0.55),
        momentum_usd=20.0,
        elapsed_s=10,       # < 45s min
        remaining_s=290,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_no_signal_after_entry_window():
    cfg = _cfg()
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.55),
        book_no=_make_book(0.55),
        momentum_usd=20.0,
        elapsed_s=280,      # > 270s max
        remaining_s=20,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


# ── Spread Arbitrage ──────────────────────────────────────────────────────────

def test_spread_arb_triggered_when_sum_below_threshold():
    cfg = _cfg(spread_arb_threshold=0.98)
    # YES=0.47, NO=0.48 → sum=0.95 < 0.98 → SPREAD_ARB
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.47),
        book_no=_make_book(0.48),
        momentum_usd=0,
        elapsed_s=100,
        remaining_s=200,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.SPREAD_ARB
    assert signal.buy_yes is True
    assert signal.buy_no is True
    assert signal.yes_no_sum < 0.98


def test_spread_arb_not_triggered_when_sum_at_threshold():
    cfg = _cfg(spread_arb_threshold=0.98)
    # YES=0.50, NO=0.50 → sum=1.00 → NO_SIGNAL
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.50),
        book_no=_make_book(0.50),
        momentum_usd=0,
        elapsed_s=100,
        remaining_s=200,
        cfg=cfg,
    )
    assert signal.signal_type != SignalType.SPREAD_ARB


def test_spread_arb_blocked_if_yes_above_max_entry():
    cfg = _cfg(spread_arb_threshold=0.98, max_entry_price=0.95)
    # YES=0.97 (above max), NO=0.00 → blocked
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.97),
        book_no=_make_book(0.00),
        momentum_usd=0,
        elapsed_s=100,
        remaining_s=200,
        cfg=cfg,
    )
    assert signal.signal_type != SignalType.SPREAD_ARB


# ── Momentum Sniping ──────────────────────────────────────────────────────────

def test_momentum_long_triggered_on_rising_btc():
    cfg = _cfg(momentum_threshold_usd=15.0, min_confidence=0.65, max_entry_price=0.95)
    # BTC up $20 → YES (UP) leg should be favoured
    # YES mid=0.75 → above min_confidence (0.65) and below max_entry (0.95)
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.75),
        book_no=_make_book(0.28),
        momentum_usd=+20.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.MOMENTUM_LONG
    assert signal.buy_yes is True
    assert signal.buy_no is False


def test_momentum_short_triggered_on_falling_btc():
    cfg = _cfg(momentum_threshold_usd=15.0, min_confidence=0.65, max_entry_price=0.95)
    # BTC down $25 → NO (DOWN) leg should be favoured
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.22),
        book_no=_make_book(0.78),
        momentum_usd=-25.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.MOMENTUM_SHORT
    assert signal.buy_no is True
    assert signal.buy_yes is False


def test_momentum_not_triggered_below_threshold():
    cfg = _cfg(momentum_threshold_usd=15.0)
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.75),
        book_no=_make_book(0.28),
        momentum_usd=+5.0,   # below 15 USD threshold
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_momentum_not_triggered_low_confidence():
    cfg = _cfg(momentum_threshold_usd=15.0, min_confidence=0.70)
    # YES=0.60 but min_confidence=0.70 → blocked
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.60),
        book_no=_make_book(0.42),
        momentum_usd=+20.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_no_signal_when_books_missing():
    cfg = _cfg()
    signal = evaluate_signals(
        asset="BTC",
        book_yes=None,
        book_no=None,
        momentum_usd=+20.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_strong_momentum_override_no_longer_bypasses_prob_check():
    cfg = _cfg(momentum_threshold_usd=15.0, min_confidence=0.70)
    # YES=0.60 (below min_confidence 0.70) but momentum=+35.0 (>= 2x threshold of 30.0) -> now blocked!
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.60),
        book_no=_make_book(0.40),
        momentum_usd=+35.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_strong_momentum_override_fails_if_momentum_below_double_threshold():
    cfg = _cfg(momentum_threshold_usd=15.0, min_confidence=0.70)
    # YES=0.60 (below min_confidence 0.70) and momentum=+25.0 (< 2x threshold of 30.0) -> blocked
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.60),
        book_no=_make_book(0.40),
        momentum_usd=+25.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_min_signal_confidence_blocks_trade():
    cfg = _cfg(momentum_threshold_usd=10.0, min_confidence=0.55, min_signal_confidence=0.35)
    # YES=0.56 (slightly above min_confidence 0.55 -> prob_confidence = 0.02)
    # momentum=+11.0 (slightly above threshold 10.0 -> momentum_confidence = 11/30 = 0.36)
    # combined_confidence = (0.02 + 0.36)/2 = 0.19 < 0.35 -> blocked!
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.56),
        book_no=_make_book(0.44),
        momentum_usd=+11.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL


def test_min_signal_confidence_enforced_strictly_under_strong_momentum():
    cfg = _cfg(momentum_threshold_usd=10.0, min_confidence=0.55, min_signal_confidence=0.50)
    # YES=0.56, momentum is +25.0 (strong momentum!)
    # combined_confidence = 0.4278 (< min_signal_confidence 0.50) -> blocked!
    signal = evaluate_signals(
        asset="BTC",
        book_yes=_make_book(0.56),
        book_no=_make_book(0.44),
        momentum_usd=+25.0,
        elapsed_s=120,
        remaining_s=180,
        cfg=cfg,
    )
    assert signal.signal_type == SignalType.NO_SIGNAL

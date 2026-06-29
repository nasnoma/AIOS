"""
tests/test_market.py
Unit tests for window timing logic — no network calls.
"""
import time
import pytest
from polymarket_bot.market import get_current_window, WindowInfo


def test_window_start_is_divisible_by_300():
    w = get_current_window()
    assert w.window_start % 300 == 0


def test_window_end_is_300s_after_start():
    w = get_current_window()
    assert w.window_end == w.window_start + 300


def test_elapsed_is_within_range():
    w = get_current_window()
    assert 0 <= w.elapsed_s < 300


def test_remaining_is_within_range():
    w = get_current_window()
    assert 0 < w.remaining_s <= 300


def test_elapsed_plus_remaining_approx_300():
    w = get_current_window()
    assert abs(w.elapsed_s + w.remaining_s - 300) < 2  # allow 2s clock drift


def test_progress_pct_range():
    w = get_current_window()
    assert 0.0 <= w.progress_pct <= 100.0


def test_window_determinism():
    """Two calls within the same 5-min window return the same window_start."""
    w1 = get_current_window()
    w2 = get_current_window()
    assert w1.window_start == w2.window_start

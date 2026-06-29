"""
tests/test_state.py
Unit tests for paper portfolio state persistence.
"""
import json
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.state import (
    PortfolioState, load_state, save_state, get_status, reset_daily_pnl_if_new_day
)


@pytest.fixture(autouse=True)
def tmp_state_file(tmp_path):
    """Redirect state I/O to a temp file for each test."""
    state_path = tmp_path / "paper_state.json"
    lock_path = tmp_path / "paper_state.lock"
    with patch("polymarket_bot.state._STATE_FILE", state_path), \
         patch("polymarket_bot.state._LOCK_FILE", lock_path):
        yield state_path


def test_load_state_returns_default_when_no_file():
    s = load_state()
    assert s.account_size > 0
    assert s.cash > 0
    assert s.total_pnl == 0.0
    assert s.win_count == 0


def test_save_and_load_roundtrip():
    s = load_state()
    s.cash = 750.0
    s.total_pnl = 50.0
    s.win_count = 5
    s.loss_count = 3
    save_state(s)

    loaded = load_state()
    assert loaded.cash == pytest.approx(750.0)
    assert loaded.total_pnl == pytest.approx(50.0)
    assert loaded.win_count == 5
    assert loaded.loss_count == 3


def test_open_positions_property():
    s = load_state()
    s.positions = [
        {"status": "open", "asset": "BTC"},
        {"status": "closed", "asset": "ETH"},
    ]
    assert len(s.open_positions) == 1
    assert s.open_positions[0]["asset"] == "BTC"


def test_win_rate_with_no_trades():
    s = PortfolioState()
    assert s.win_rate == 0.0


def test_win_rate_with_trades():
    s = PortfolioState(win_count=7, loss_count=3)
    assert s.win_rate == pytest.approx(0.70)


def test_get_status_dict_keys():
    status = get_status()
    required_keys = ["account_size", "cash", "total_pnl", "daily_pnl", "win_rate_pct", "cycle_count"]
    for key in required_keys:
        assert key in status, f"Missing key: {key}"


def test_reset_daily_pnl_on_new_day():
    s = load_state()
    s.daily_pnl = -80.0
    s.daily_reset_date = "2020-01-01"  # Old date
    s = reset_daily_pnl_if_new_day(s)
    assert s.daily_pnl == 0.0
    assert s.daily_reset_date != "2020-01-01"


def test_no_reset_daily_pnl_same_day():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    s = load_state()
    s.daily_pnl = -50.0
    s.daily_reset_date = today
    s = reset_daily_pnl_if_new_day(s)
    assert s.daily_pnl == pytest.approx(-50.0)  # unchanged

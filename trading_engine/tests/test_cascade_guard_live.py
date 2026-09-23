"""A_tuned cascade LIVE: latch, buy cancel, sells remain; fail-open on stale/missing BTC."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from trading_engine.spot.cascade_guard import (
    CASCADE_PAUSE_SEC,
    CascadeGuard,
    cascade_guard,
    live_enabled,
    shadow_enabled,
)
from trading_engine.spot.grid_engine import GridEngine, GridLevel


@pytest.fixture(autouse=True)
def _reset_cascade(monkeypatch):
    monkeypatch.setenv("SPOT_CASCADE_LIVE", "1")
    monkeypatch.setenv("SPOT_CASCADE_SHADOW", "1")
    cascade_guard.reset_for_tests()
    yield
    cascade_guard.reset_for_tests()


def test_live_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SPOT_CASCADE_LIVE", raising=False)
    # re-read via function (default="1")
    assert live_enabled() is True
    monkeypatch.setenv("SPOT_CASCADE_LIVE", "0")
    assert live_enabled() is False


def test_latch_triggers_on_1h_close_and_extends():
    now = time.time()
    cascade_guard.update_returns(-1.5, 0.0, -0.5, now=now)
    paused, reason = cascade_guard.is_cascade_pause()
    assert paused is True
    assert "btc_1h_close" in reason
    until1 = cascade_guard.state.cascade_until
    assert until1 >= now + CASCADE_PAUSE_SEC - 1

    # still weak → extend latch
    cascade_guard.update_returns(-1.6, 0.0, -0.5, now=now + 60)
    until2 = cascade_guard.state.cascade_until
    assert until2 >= until1
    assert cascade_guard.state.paused is True


def test_latch_triggers_on_wick():
    now = time.time()
    cascade_guard.update_returns(-0.5, 0.0, -2.1, now=now)
    paused, reason = cascade_guard.is_cascade_pause()
    assert paused is True
    assert "wick" in reason


def test_fail_open_when_stale_or_missing():
    # never updated
    cascade_guard.reset_for_tests()
    paused, reason = cascade_guard.is_cascade_pause()
    assert paused is False
    assert "fail open" in reason or "no BTC" in reason

    now = time.time()
    cascade_guard.update_returns(-2.0, 0.0, 0.0, now=now)
    assert cascade_guard.is_cascade_pause()[0] is True
    # stale
    cascade_guard.state.last_updated = now - 500
    paused, reason = cascade_guard.is_cascade_pause()
    assert paused is False
    assert "stale" in reason


def test_live_off_never_pauses(monkeypatch):
    monkeypatch.setenv("SPOT_CASCADE_LIVE", "0")
    cascade_guard.update_returns(-3.0, -6.0, -3.0, now=time.time())
    assert cascade_guard.would_pause()[0] is True
    assert cascade_guard.is_cascade_pause()[0] is False


class _FakeExchange:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, oid, symbol, params=None):
        self.cancelled.append((oid, symbol))
        return {"id": oid, "status": "canceled"}


def test_cancel_buys_only_keeps_sells():
    eng = GridEngine(symbol="ETH/USDT", allocated_usd=1000.0, paper_mode=True)
    buy = GridLevel(price=100.0, side="buy", qty=1.0, size_usd=100.0, order_id="B1", status="open")
    sell = GridLevel(
        price=110.0, side="sell", qty=1.0, size_usd=110.0, order_id="S1", status="open",
        linked_buy_price=100.0,
    )
    eng.grid_levels = [buy, sell]
    exch = _FakeExchange()
    eng.cancel_buys_only(exch)
    assert all(l.side == "sell" for l in eng.grid_levels)
    assert len(eng.grid_levels) == 1
    assert eng.grid_levels[0].order_id == "S1"
    assert eng.grid_levels[0].status == "open"
    # paper mode cancels without exchange call for buys; sell untouched
    assert buy.status == "cancelled"


def test_runner_helper_wires_live_pause(monkeypatch):
    from trading_engine.spot import runner as spot_runner
    cascade_guard.update_returns(-2.0, 0.0, 0.0, now=time.time())
    on, why = spot_runner.is_cascade_buy_paused()
    assert on is True
    assert "cascade" in why.lower()
    monkeypatch.setenv("SPOT_CASCADE_LIVE", "0")
    on2, _ = spot_runner.is_cascade_buy_paused()
    assert on2 is False

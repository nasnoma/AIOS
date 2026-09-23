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

def test_place_grid_orders_cascade_skips_buys_not_sells(monkeypatch):
    """Regression: cascade continue must be buy-gated — sells stay placable during latch."""
    from types import SimpleNamespace

    cascade_guard.update_returns(-2.0, 0.0, 0.0, now=time.time())
    assert cascade_guard.is_cascade_pause()[0] is True

    eng = GridEngine(symbol="ETH/USDT", allocated_usd=1000.0, paper_mode=False)
    eng._last_portfolio_avg_cost = 100.0

    class _Ex:
        markets = {
            "ETH/USDT": {
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
            }
        }

        def __init__(self):
            self.created = []

        def market(self, s):
            return self.markets[s]

        def amount_to_precision(self, s, q):
            return float(q)

        def price_to_precision(self, s, p):
            return float(p)

        def create_limit_buy_order(self, symbol, amount, price, params=None):
            self.created.append({"side": "buy", "amount": amount, "price": price})
            return {"id": f"OID-{len(self.created)}", "status": "open"}

        def create_limit_sell_order(self, symbol, amount, price, params=None):
            self.created.append({"side": "sell", "amount": amount, "price": price})
            return {"id": f"OID-{len(self.created)}", "status": "open"}

        def cancel_order(self, oid, symbol=None, params=None):
            return {"id": oid}

        def fetch_balance(self, params=None):
            return {"free": {"ETH": 1.0, "USDT": 100000.0}}

    ex = _Ex()
    eng.exchange = ex
    buy = GridLevel(price=90.0, side="buy", qty=1.0, size_usd=90.0, status="pending")
    sell = GridLevel(
        price=120.0, side="sell", qty=1.0, size_usd=120.0, status="pending",
        linked_buy_price=100.0,
    )
    eng.grid_levels = [buy, sell]

    holding = SimpleNamespace(units_held=1.0, avg_cost_basis=100.0)

    class _P:
        holdings = {"ETH/USDT": holding}
        usdt_reserved = 0.0
        usdt_available = 100000.0
        total_open_buy_usd = 0.0
        total_unified_equity = 10000.0
        total_capital = 10000.0

    import trading_engine.spot.grid_engine as ge
    monkeypatch.setattr(
        ge, "entry_buys_allowed",
        lambda *a, **k: SimpleNamespace(allow_buys=True, reason="ok"),
    )
    # Fee-proof bag gate must not block this regression focus
    monkeypatch.setattr(
        ge, "assert_sell_clears_bag_max",
        lambda *a, **k: (True, "ok"),
    )

    eng.place_grid_orders(_P(), ex)

    assert any(c["side"] == "sell" for c in ex.created), "cascade must still place sells"
    assert not any(c["side"] == "buy" for c in ex.created), "cascade must not place buys"
    assert buy.status == "pending"
    assert sell.status == "open"

"""FIFO unknown: refuse NEW sells, but do not wipe resting / missing_sells-loop.

1e6d586 fail-closed made resting_sell_is_safe return False when CostRef=0, which
made cancel_unsafe wipe exits and runner missing_sells cancel_all every tick.
"""
from __future__ import annotations

from types import SimpleNamespace

import trading_engine.spot.sell_guard as sg
from trading_engine.spot.grid_engine import GridEngine, GridLevel


def _patch_fifo_empty(monkeypatch):
    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(fr, "get_fifo_cost_basis", lambda *a, **k: {})
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", lambda *a, **k: {})


def test_resting_safe_reason_is_fail_closed_not_generic(monkeypatch):
    _patch_fifo_empty(monkeypatch)
    ok, why, floor = sg.resting_sell_is_safe(
        "DOT/USDT", 1.25, 100.0, units_held=500.0, portfolio_avg_cost=1.176
    )
    assert ok is False
    assert floor == 0.0
    assert "fail-closed" in why


def test_cancel_unsafe_does_not_wipe_when_fifo_unknown(monkeypatch):
    _patch_fifo_empty(monkeypatch)
    eng = GridEngine(symbol="DOT/USDT", allocated_usd=1000.0, paper_mode=True)
    lvl = GridLevel(price=1.25, side="sell", qty=100.0, size_usd=125.0)
    lvl.status = "open"
    lvl.order_id = "PAPER_x"
    eng.grid_levels = [lvl]
    n = eng.cancel_unsafe_resting_sells(None, units_held=500.0, portfolio_avg_cost=1.176)
    assert n == 0
    assert eng.grid_levels[0].status == "open"


def test_cancel_unsafe_still_cancels_proven_under_floor(monkeypatch):
    """When FIFO max is known, under-floor sells must still be cancelled."""

    def fake_basis(symbol, units_held=None):
        return {"avg_cost": 1.21, "max_buy_price": 1.21}

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {"avg_cost": 1.21, "max_buy_price": 1.21}

    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_basis)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot)

    eng = GridEngine(symbol="DOT/USDT", allocated_usd=1000.0, paper_mode=True)
    # Clearly under fee-proof(1.21)
    lvl = GridLevel(price=1.18, side="sell", qty=100.0, size_usd=118.0)
    lvl.status = "open"
    lvl.order_id = "PAPER_y"
    eng.grid_levels = [lvl]
    n = eng.cancel_unsafe_resting_sells(None, units_held=500.0, portfolio_avg_cost=1.21)
    assert n == 1
    assert eng.grid_levels[0].status == "cancelled"

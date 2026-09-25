"""Fail-closed: holding + FIFO max unknown → refuse sells (no portfolio-avg CostRef).

DOT 2026-09-25: after restart FIFO lagged; Aged-Compress used portfolio avg (~1.176)
instead of bag-max lot (~1.21) and filled under true lots.
"""
from __future__ import annotations

from types import SimpleNamespace

import trading_engine.spot.sell_guard as sg
from trading_engine.spot.grid_engine import GridEngine


def test_holding_fifo_unknown_refuses_cost_ref(monkeypatch):
    """units_held > 0 and no fifo max/lot → resolve returns 0 (not portfolio avg)."""

    def fake_basis(symbol, units_held=None):
        return {}  # empty / not ready

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {}

    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_basis)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot)

    cref = sg.resolve_sell_cost_ref(
        "DOT/USDT",
        portfolio_avg_cost=1.176,  # cheap avg — must NOT become CostRef
        units_held=500.0,
        sell_qty=250.0,
        hist_cost=1.10,
        current_price=1.18,
    )
    assert cref == 0.0, f"expected refuse (0.0), got {cref}"


def test_fifo_ready_floor_uses_max_lot_not_cheap_avg(monkeypatch):
    """When FIFO max lot is ready, CostRef / fee-proof floor uses max not cheap avg."""

    def fake_basis(symbol, units_held=None):
        return {"avg_cost": 1.176, "max_buy_price": 1.21}

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {"avg_cost": 1.176, "max_buy_price": 1.176}

    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_basis)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot)

    cref = sg.resolve_sell_cost_ref(
        "DOT/USDT",
        portfolio_avg_cost=1.176,
        units_held=500.0,
        sell_qty=250.0,
        current_price=1.18,
    )
    assert cref >= 1.21 - 1e-12, f"expected bag-max 1.21, got {cref}"

    floor = sg.min_fee_proof_sell_price(cref, 250.0, fee_factor=0.0011, min_net_usd=0.50)
    assert floor >= 1.21
    # Cheap avg-based floor (~1.1808 style) must be below true fee-proof(max)
    cheap_floor = sg.min_fee_proof_sell_price(1.176, 250.0, fee_factor=0.0011, min_net_usd=0.50)
    assert cheap_floor < floor
    ok, why, _ = sg.resting_sell_is_safe(
        "DOT/USDT", cheap_floor, 250.0, units_held=500.0, portfolio_avg_cost=1.176
    )
    assert ok is False


def test_build_grid_refuses_sells_when_fifo_unknown(monkeypatch):
    """grid build_grid: holding > 0, fifo max unknown → no sell GridLevels placed."""
    eng = GridEngine(symbol="DOT/USDT", allocated_usd=1000.0, paper_mode=True)

    holding = SimpleNamespace(units_held=500.0, avg_cost_basis=1.176)
    portfolio = SimpleNamespace(
        holdings={"DOT": holding},
        total_unified_equity=10000.0,
        total_capital=10000.0,
    )

    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(
        fr, "get_fifo_cost_basis",
        lambda symbol, units_held=None: {},
    )

    eng.build_grid(current_price=1.18, portfolio=portfolio, force=True)
    sells = [lv for lv in eng.grid_levels if getattr(lv, "side", "") == "sell"]
    assert sells == [], f"expected no sells when FIFO unknown, got {len(sells)}: {sells}"

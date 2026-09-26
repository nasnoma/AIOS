"""ATOM pin CostRef must use bag-max/hist — not portfolio avg alone.

Cosmetic ATOM 2026-09-25: pin logged cost_ref=$1.7900 avg while place @1.825
from bag-max/hist 1.8158.
"""
from __future__ import annotations

import trading_engine.spot.sell_guard as sg
from trading_engine.spot.atom_exit_pin import atom_fee_proof_pin


def test_atom_pin_prefers_caller_cost_ref_not_avg(monkeypatch):
    portfolio_avg = 1.7900
    bag_max = 1.8158
    qty = 80.0

    def boom(*a, **k):
        raise AssertionError("resolve should not be needed when cost_ref provided")

    monkeypatch.setattr(sg, "resolve_sell_cost_ref", boom)

    pin = atom_fee_proof_pin(
        units_held=qty,
        portfolio_avg_cost=portfolio_avg,
        sell_qty=qty,
        cost_ref=bag_max,
        hist_cost=bag_max,
        min_net_usd=0.50,
    )
    expect = sg.min_fee_proof_sell_price(bag_max, qty, min_net_usd=0.50)
    assert pin > 0
    assert abs(pin - expect) < 1e-9
    cheap = sg.min_fee_proof_sell_price(portfolio_avg, qty, min_net_usd=0.50)
    assert pin > cheap


def test_atom_pin_hist_raises_above_avg(monkeypatch):
    portfolio_avg = 1.7900
    bag_max = 1.8158
    qty = 80.0

    def fake_resolve(symbol, **kwargs):
        # Simulate resolve with hist raise-only (as sell_guard does)
        hist = float(kwargs.get("hist_cost") or 0.0)
        avg = float(kwargs.get("portfolio_avg_cost") or 0.0)
        return max(avg, hist) if hist or avg else 0.0

    monkeypatch.setattr(sg, "resolve_sell_cost_ref", fake_resolve)

    pin = atom_fee_proof_pin(
        units_held=qty,
        portfolio_avg_cost=portfolio_avg,
        sell_qty=qty,
        hist_cost=bag_max,  # place-path parity
        min_net_usd=0.50,
    )
    expect = sg.min_fee_proof_sell_price(bag_max, qty, min_net_usd=0.50)
    assert abs(pin - expect) < 1e-9

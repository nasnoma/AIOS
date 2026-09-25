"""Re-align / legacy compress must aim from bag-max FIFO CostRef — not portfolio avg.

ATOM 2026-09-25: portfolio avg ~1.7900 → aim ~1.7981 while bag-max/hist CostRef
~1.8158 → place @~1.825, then cancel_all every ~40s (target still below floor).
"""
from __future__ import annotations

import trading_engine.spot.sell_guard as sg


def test_legacy_compress_uses_bag_max_not_cheap_avg():
    """portfolio avg < bag-max → target/place floor is bag-max fee-proof, not avg."""
    portfolio_avg = 1.7900
    bag_max = 1.8158
    qty = 80.0  # ~$145 notional
    fee = 0.0011
    live = 1.7800

    cheap_target_floor = sg.min_fee_proof_sell_price(
        portfolio_avg, qty, fee_factor=fee, min_net_usd=0.60
    )
    bag_floor = sg.min_fee_proof_sell_price(bag_max, qty, fee_factor=fee, min_net_usd=0.60)
    assert cheap_target_floor < bag_max
    assert bag_floor >= bag_max
    # Resting sell already at fee-proof(bag-max) — must NOT cancel
    resting = bag_floor
    do_cancel, target, why = sg.should_cancel_for_legacy_compress(
        min_resting_sell_px=resting,
        cost_ref=bag_max,
        qty=qty,
        live_price=live,
        fee_factor=fee,
    )
    assert do_cancel is False, f"expected no cancel when resting >= bag-max floor, got {why} target={target}"
    assert target + 1e-12 >= bag_floor * 0.999  # aim near bag-max fee-proof, not cheap avg
    assert target > cheap_target_floor


def test_legacy_compress_skips_when_cost_ref_fail_closed():
    """FIFO unknown (cost_ref=0) → refuse avg-based re-align (no thrash)."""
    do_cancel, target, why = sg.should_cancel_for_legacy_compress(
        min_resting_sell_px=1.825,
        cost_ref=0.0,
        qty=80.0,
        live_price=1.78,
        fee_factor=0.0011,
    )
    assert do_cancel is False
    assert target == 0.0
    assert "fail-closed" in why


def test_legacy_compress_still_cancels_when_resting_far_above_bag_max():
    """Fat resting sell well above bag-max quick-exit → cancel to compress (once)."""
    bag_max = 1.8158
    qty = 80.0
    fee = 0.0011
    bag_floor = sg.min_fee_proof_sell_price(bag_max, qty, fee_factor=fee, min_net_usd=0.60)
    fat = bag_floor * 1.05  # 5% above fee-proof target
    do_cancel, target, why = sg.should_cancel_for_legacy_compress(
        min_resting_sell_px=fat,
        cost_ref=bag_max,
        qty=qty,
        live_price=1.78,
        fee_factor=fee,
    )
    assert do_cancel is True, why
    assert target + 1e-12 >= bag_floor * 0.999
    assert fat > target * 1.005


def test_atom_avg_below_bag_max_no_cancel_loop(monkeypatch):
    """End-to-end CostRef: avg 1.79 + FIFO/hist bag-max 1.8158 → resting @fee-proof safe."""
    portfolio_avg = 1.7900
    bag_max = 1.8158
    qty = 80.0
    units = 80.0

    def fake_basis(symbol, units_held=None):
        return {"avg_cost": portfolio_avg, "max_buy_price": bag_max}

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {"avg_cost": portfolio_avg, "max_buy_price": bag_max}

    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_basis)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot)

    cref = sg.resolve_sell_cost_ref(
        "ATOM/USDT",
        portfolio_avg_cost=portfolio_avg,
        units_held=units,
        sell_qty=qty,
        hist_cost=bag_max,  # place path parity
        current_price=1.78,
    )
    assert cref >= bag_max - 1e-12

    place_px = sg.min_fee_proof_sell_price(cref, qty, fee_factor=0.0011, min_net_usd=0.50)
    assert place_px >= bag_max

    # Cheap avg aim would want ~fee_proof(1.79) and wrongly cancel place_px
    cheap = sg.min_fee_proof_sell_price(portfolio_avg, qty, fee_factor=0.0011, min_net_usd=0.60)
    assert place_px > cheap * 1.005  # would have thrashed under old logic

    do_cancel, target, why = sg.should_cancel_for_legacy_compress(
        min_resting_sell_px=place_px,
        cost_ref=cref,
        qty=qty,
        live_price=1.78,
        fee_factor=0.0011,
    )
    assert do_cancel is False, f"no cancel-loop expected, got cancel={do_cancel} why={why} target={target}"

"""Stuck-bag recycle: step fat sells toward bag-max fee-proof floor — never under.

Unit proofs (no cycle backtest needed): capital-freeing + fee-proof + no thrash at floor.
"""
from __future__ import annotations

import trading_engine.spot.sell_guard as sg


def test_stuck_step_down_never_below_bag_max_fee_proof_floor():
    bag_max = 1.8158
    qty = 80.0
    fee = 0.0011
    live = 1.7800
    bag_floor = sg.min_fee_proof_sell_price(bag_max, qty, fee_factor=fee, min_net_usd=0.60)
    fat = bag_floor * 1.08  # clearly parked above floor

    do_cancel, target, why = sg.should_step_down_stuck_sell(
        min_resting_sell_px=fat,
        cost_ref=bag_max,
        qty=qty,
        live_price=live,
        fee_factor=fee,
    )
    assert do_cancel is True, why
    assert target + 1e-12 >= bag_floor * 0.999
    assert target + 1e-12 >= bag_max  # never under bag-max gross


def test_stuck_no_thrash_when_already_at_floor():
    bag_max = 1.8158
    qty = 80.0
    fee = 0.0011
    live = 1.7800
    bag_floor = sg.min_fee_proof_sell_price(bag_max, qty, fee_factor=fee, min_net_usd=0.60)

    do_cancel, target, why = sg.should_step_down_stuck_sell(
        min_resting_sell_px=bag_floor,
        cost_ref=bag_max,
        qty=qty,
        live_price=live,
        fee_factor=fee,
    )
    assert do_cancel is False, f"no thrash at floor, got cancel why={why} target={target}"
    assert target + 1e-12 >= bag_floor * 0.999


def test_stuck_fail_closed_cost_ref_skips():
    do_cancel, target, why = sg.should_step_down_stuck_sell(
        min_resting_sell_px=2.0,
        cost_ref=0.0,
        qty=80.0,
        live_price=1.78,
        fee_factor=0.0011,
    )
    assert do_cancel is False
    assert target == 0.0
    assert "fail-closed" in why

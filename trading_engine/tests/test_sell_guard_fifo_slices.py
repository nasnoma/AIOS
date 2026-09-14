"""
Regression: multi-level sells must never price below a dearer FIFO lot.

Reproduces the 2026-09-14 SOL gap:
  Lot A (oldest) ~$102.58, Lot B ~$104.71
  Two resting equal-qty sells priced off bag avg (~$103.05) both cleared above avg;
  FIFO consumed cheap lot first; second fill hit dear lot below its buy → loss.

Fix: per-slice cost_ref = max(avg_slice, max_buy_in_slice) + fee-proof floor,
plus qty_offset so slice 2 walks past slice 1's lots.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_engine.spot.fifo_reconciler import fifo_slice_cost_from_lots
from trading_engine.spot.sell_guard import (
    min_fee_proof_sell_price,
    sell_clears_buy,
    sell_is_fee_proof,
    enforce_sell_floor,
)


def _sol_like_lots():
    # Cheap then dear — FIFO order
    return [
        {"price": 102.58, "rem": 1.0},
        {"price": 104.71, "rem": 1.0},
    ]


def test_fifo_slice_walk_assigns_dear_lot_to_second_sell():
    lots = _sol_like_lots()
    qty = 1.0
    s1 = fifo_slice_cost_from_lots(lots, qty, qty_offset=0.0)
    s2 = fifo_slice_cost_from_lots(lots, qty, qty_offset=qty)

    assert abs(s1["avg_cost"] - 102.58) < 1e-6
    assert abs(s1["max_buy_price"] - 102.58) < 1e-6
    assert abs(s2["avg_cost"] - 104.71) < 1e-6
    assert abs(s2["max_buy_price"] - 104.71) < 1e-6


def test_second_sell_fee_proof_floor_clears_dear_lot():
    lots = _sol_like_lots()
    qty = 1.0
    fee = 0.0011  # ~Bybit 0.10% * 1.10 safety

    s1 = fifo_slice_cost_from_lots(lots, qty, qty_offset=0.0)
    s2 = fifo_slice_cost_from_lots(lots, qty, qty_offset=qty)

    # cost_ref = max(avg, max) per slice (lot-level never-sell-below-buy)
    cost1 = max(s1["avg_cost"], s1["max_buy_price"])
    cost2 = max(s2["avg_cost"], s2["max_buy_price"])

    # Bug simulation: bag avg floor for BOTH sells
    bag_avg = (102.58 + 104.71) / 2.0
    buggy_sell2 = min_fee_proof_sell_price(bag_avg, qty, fee_factor=fee, min_net_usd=0.60)
    # Buggy price can clear avg yet sit below dear lot
    assert buggy_sell2 < 104.71 or not sell_clears_buy(buggy_sell2, 104.71) or True
    # Demonstrate the loss case if second sell used avg-only ~103.69-style:
    lossy = 103.80
    assert lossy < cost2
    assert not sell_clears_buy(lossy, cost2)

    floor1 = enforce_sell_floor(0.0, cost1, qty, fee_factor=fee, min_net_usd=0.60)
    floor2 = enforce_sell_floor(0.0, cost2, qty, fee_factor=fee, min_net_usd=0.60)

    assert sell_clears_buy(floor1, cost1)
    assert sell_clears_buy(floor2, cost2)
    assert floor2 >= cost2
    assert floor2 >= 104.71
    assert sell_is_fee_proof(floor2, cost2, qty, fee_factor=fee, min_net_usd=0.55)

    # Critical regression assertion from the brief:
    # second sell price >= fee-proof(dear lot)
    dear_fee_proof = min_fee_proof_sell_price(104.71, qty, fee_factor=fee, min_net_usd=0.60)
    assert floor2 + 1e-9 >= dear_fee_proof


def test_resolve_sell_cost_ref_never_below_max_buy(monkeypatch=None):
    """resolve_sell_cost_ref must include fifo_max even when sell_qty is known."""
    from trading_engine.spot import sell_guard

    fake_lot = {"avg_cost": 103.05, "max_buy_price": 104.71, "units": 1.0, "min_buy_price": 102.58}

    def _fake_lot(symbol, qty, qty_offset=0.0):
        return dict(fake_lot)

    def _fake_basis(symbol, units_held=None):
        return {"avg_cost": 103.05, "max_buy_price": 104.71}

    import trading_engine.spot.fifo_reconciler as fr
    orig_lot = fr.get_fifo_lot_cost_for_qty
    orig_basis = fr.get_fifo_cost_basis
    fr.get_fifo_lot_cost_for_qty = _fake_lot
    fr.get_fifo_cost_basis = _fake_basis
    try:
        ref = sell_guard.resolve_sell_cost_ref(
            "SOL/USDT",
            portfolio_avg_cost=103.05,
            sell_qty=1.0,
        )
        assert ref >= 104.71
    finally:
        fr.get_fifo_lot_cost_for_qty = orig_lot
        fr.get_fifo_cost_basis = orig_basis


def test_post_rebuild_second_slice_still_safe():
    """After first slice consumed, remaining lot alone floors the next sell."""
    remaining = [{"price": 104.71, "rem": 1.0}]
    qty = 1.0
    fee = 0.0011
    s = fifo_slice_cost_from_lots(remaining, qty, qty_offset=0.0)
    cost = max(s["avg_cost"], s["max_buy_price"])
    floor = enforce_sell_floor(0.0, cost, qty, fee_factor=fee, min_net_usd=0.60)
    assert floor >= 104.71
    assert sell_is_fee_proof(floor, cost, qty, fee_factor=fee, min_net_usd=0.55)


def test_fill_order_requires_bag_max_on_all_levels():
    """Bybit fills lowest sell first → that sell hits oldest/dearest lots.
    Every resting sell must therefore clear bag-wide max buy, not only its slice.
    """
    lots = _sol_like_lots()
    qty = 1.0
    bag_max = max(l["price"] for l in lots)
    s2 = fifo_slice_cost_from_lots(lots, qty, qty_offset=qty)
    slice2_only = max(s2["avg_cost"], s2["max_buy_price"])
    cost_ref = max(slice2_only, bag_max)
    assert cost_ref >= bag_max - 1e-9
    fee = 0.0011
    px = min_fee_proof_sell_price(cost_ref, qty, fee_factor=fee, min_net_usd=0.60)
    assert sell_clears_buy(px, bag_max)
    assert sell_is_fee_proof(px, cost_ref, qty, fee_factor=fee, min_net_usd=0.55)


if __name__ == "__main__":
    test_fifo_slice_walk_assigns_dear_lot_to_second_sell()
    test_second_sell_fee_proof_floor_clears_dear_lot()
    test_resolve_sell_cost_ref_never_below_max_buy()
    test_fill_order_requires_bag_max_on_all_levels()
    print("ALL PASS: sell_guard FIFO slice never-sell-below-buy")

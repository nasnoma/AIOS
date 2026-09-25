"""Regression: batch-net thin-cycle accounting + bag-max sell gate."""
from trading_engine.spot.sell_guard import (
    HARD_MIN_NET_USD,
    batch_completed_cycles_by_fill,
    min_fee_proof_sell_price,
    resting_sell_is_safe,
    assert_sell_clears_bag_max,
    resolve_sell_cost_ref,
)


def test_hard_min_is_fifty_cents():
    assert float(HARD_MIN_NET_USD) == 0.50


def test_batch_net_not_lot_rows():
    """APT-style: three lot-rows under $0.50 but batch totals >= $0.50."""
    cycles = [
        {"symbol": "APT/USDT", "sell_price": 0.7459, "timestamp": "2026-09-21T01:06:10Z", "net_pnl": 0.168, "fee": 0.06},
        {"symbol": "APT/USDT", "sell_price": 0.7459, "timestamp": "2026-09-21T01:06:10Z", "net_pnl": 0.261, "fee": 0.07},
        {"symbol": "APT/USDT", "sell_price": 0.7459, "timestamp": "2026-09-21T01:06:11Z", "net_pnl": 0.244, "fee": 0.07},
    ]
    # same minute bucket 01:06
    batches = batch_completed_cycles_by_fill(cycles)
    assert len(batches) == 1
    b = batches[0]
    assert b["lots"] == 3
    assert b["thin_lot_rows"] == 3
    assert b["net_pnl"] >= 0.50
    assert b["under_floor"] is False


def test_render_style_underfloor_batch_flagged():
    cycles = [
        {"symbol": "RENDER/USDT", "sell_price": 1.764, "timestamp": "2026-09-21T08:23:01Z", "net_pnl": 0.055, "fee": 0.045},
        {"symbol": "RENDER/USDT", "sell_price": 1.764, "timestamp": "2026-09-21T08:23:01Z", "net_pnl": 0.099, "fee": 0.100},
    ]
    batches = batch_completed_cycles_by_fill(cycles)
    assert len(batches) == 1
    assert batches[0]["under_floor"] is True
    assert batches[0]["net_pnl"] < 0.50


def test_linked_buy_cannot_undercut_bag_max(monkeypatch):
    import trading_engine.spot.sell_guard as sg

    def fake_resolve(symbol, **kwargs):
        # Simulate: linked cheap 1.75 but bag max 1.757 folded in by real resolve —
        # here we assert resting_sell_is_safe rejects RENDER @1.764 vs cost 1.757
        return 1.757

    monkeypatch.setattr(sg, "resolve_sell_cost_ref", fake_resolve)
    ok, why, floor = sg.resting_sell_is_safe("RENDER/USDT", 1.764, 41.39, units_held=41.39, portfolio_avg_cost=1.757)
    assert ok is False
    assert floor > 1.764
    ok2, _ = sg.assert_sell_clears_bag_max("RENDER/USDT", 1.764, 41.39, units_held=41.39, portfolio_avg_cost=1.757)
    assert ok2 is False
    # Fee-proof+0.50 floor should clear
    safe_px = sg.min_fee_proof_sell_price(1.757, 41.39, min_net_usd=0.50)
    ok3, _ = sg.assert_sell_clears_bag_max("RENDER/USDT", safe_px, 41.39, units_held=41.39, portfolio_avg_cost=1.757)
    assert ok3 is True


def test_resolve_folds_linked_into_max_not_early_return(monkeypatch):
    import trading_engine.spot.sell_guard as sg

    def fake_fifo_basis(symbol, units_held=None):
        return {"avg_cost": 1.75, "max_buy_price": 1.80}

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {"avg_cost": 1.70, "max_buy_price": 1.70}

    monkeypatch.setattr("trading_engine.spot.fifo_reconciler.get_fifo_cost_basis", fake_fifo_basis, raising=False)
    monkeypatch.setattr("trading_engine.spot.fifo_reconciler.get_fifo_lot_cost_for_qty", fake_lot, raising=False)
    # Also patch the import path used inside resolve
    import trading_engine.spot.fifo_reconciler as fr
    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_fifo_basis, raising=False)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot, raising=False)

    cref = sg.resolve_sell_cost_ref(
        "RENDER/USDT",
        portfolio_avg_cost=1.74,
        units_held=40.0,
        sell_qty=10.0,
        linked_buy_price=1.70,  # cheap linked must not win alone
    )
    assert cref >= 1.80


if __name__ == "__main__":
    test_hard_min_is_fifty_cents()
    test_batch_net_not_lot_rows()
    test_render_style_underfloor_batch_flagged()
    print("basic ok (monkeypatch tests via pytest)")

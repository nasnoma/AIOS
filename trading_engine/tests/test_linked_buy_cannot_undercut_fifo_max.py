
"""linked buy price must not undercut bag-wide FIFO max lot floor."""
from trading_engine.spot.sell_guard import resolve_sell_cost_ref

class _FakeLot:
    pass

def test_linked_cheaper_than_fifo_max_still_floors(monkeypatch):
    import trading_engine.spot.sell_guard as sg

    def fake_basis(symbol, units_held=None):
        return {"avg_cost": 8.92, "max_buy_price": 8.979944}

    def fake_lot(symbol, qty, qty_offset=0.0, units_held=None):
        return {"avg_cost": 8.873843, "max_buy_price": 8.873843}

    monkeypatch.setattr(
        "trading_engine.spot.fifo_reconciler.get_fifo_cost_basis", fake_basis, raising=False
    )
    # patch at import site used inside resolve
    import trading_engine.spot.fifo_reconciler as fr
    monkeypatch.setattr(fr, "get_fifo_cost_basis", fake_basis)
    monkeypatch.setattr(fr, "get_fifo_lot_cost_for_qty", fake_lot)

    cref = resolve_sell_cost_ref(
        "UNI/USDT",
        portfolio_avg_cost=8.92,
        units_held=98.4,
        sell_qty=49.2,
        linked_buy_price=8.873843,
    )
    assert cref >= 8.979944 - 1e-9, cref

"""ARKM 2026-09-23 hole: after a dearer buy fill, resting sells under the new
fee-proof floor (bag/linked cost + fees + $0.50) must be cancelled immediately
— not wait for FIFO sync / next runner tick.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_engine.spot.grid_engine import GridEngine, GridLevel
from trading_engine.spot.sell_guard import min_fee_proof_sell_price, get_fee_factor


class _FakeExchange:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, oid, symbol, params=None):
        self.cancelled.append((oid, symbol))
        return {"id": oid, "status": "canceled"}


def _lag_fifo(monkeypatch, avg=0.1320, mx=0.1325):
    import trading_engine.spot.fifo_reconciler as fr

    monkeypatch.setattr(
        fr, "get_fifo_cost_basis",
        lambda symbol, units_held=None: {"avg_cost": avg, "max_buy_price": mx},
    )
    monkeypatch.setattr(
        fr, "get_fifo_lot_cost_for_qty",
        lambda symbol, qty, qty_offset=0.0, units_held=None: {
            "avg_cost": avg, "max_buy_price": mx
        },
    )


def test_post_buy_fill_cancels_resting_sell_under_new_lot(monkeypatch):
    """Resting sell @0.1341 must die when buy @0.1343 floors cancel (FIFO lag)."""
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=True)
    eng._last_base_qty_held = 1000.0
    eng._last_portfolio_avg_cost = 0.1320

    stale = GridLevel(
        price=0.1341,
        side="sell",
        qty=1512.13,
        size_usd=0.1341 * 1512.13,
        order_id="RESTING_OLD",
        status="open",
        linked_buy_price=0.1320,
    )
    eng.grid_levels = [stale]
    _lag_fifo(monkeypatch)

    exch = _FakeExchange()
    buy_px = 0.1343
    buy_qty = 3009.25
    held = float(eng._last_base_qty_held) + buy_qty
    avg_floor = max(float(eng._last_portfolio_avg_cost), buy_px)
    eng._last_base_qty_held = held
    eng._last_portfolio_avg_cost = avg_floor
    n = eng.cancel_unsafe_resting_sells(
        exch, units_held=held, portfolio_avg_cost=avg_floor
    )

    floor = min_fee_proof_sell_price(
        buy_px, stale.qty, fee_factor=get_fee_factor(), min_net_usd=0.50
    )
    assert floor > 0.1341, f"expected fee-proof floor > sell pin, got {floor}"
    assert n == 1, f"expected 1 cancel, got {n}"
    assert stale.status == "cancelled"
    assert exch.cancelled and exch.cancelled[0][0] == "RESTING_OLD"


def test_post_buy_keeps_sell_above_new_lot_fee_proof(monkeypatch):
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=True)
    buy_px = 0.1343
    qty = 874.97
    floor = min_fee_proof_sell_price(
        buy_px, qty, fee_factor=get_fee_factor(), min_net_usd=0.50
    )
    safe = GridLevel(
        price=floor + 0.0001,
        side="sell",
        qty=qty,
        size_usd=(floor + 0.0001) * qty,
        order_id="SAFE_SELL",
        status="open",
        linked_buy_price=buy_px,
    )
    eng.grid_levels = [safe]
    _lag_fifo(monkeypatch, avg=buy_px, mx=buy_px)

    exch = _FakeExchange()
    n = eng.cancel_unsafe_resting_sells(
        exch, units_held=2000.0, portfolio_avg_cost=buy_px
    )
    assert n == 0
    assert safe.status == "open"
    assert exch.cancelled == []


def test_tick_buy_fill_invokes_post_buy_cancel(monkeypatch):
    """Paper tick: buy fill must cancel a resting under-lot sell in the same pass."""
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=True)
    eng._last_base_qty_held = 500.0
    eng._last_portfolio_avg_cost = 0.1319
    eng.current_spacing = 0.0078

    buy = GridLevel(
        price=0.1343, side="buy", qty=300.0, size_usd=0.1343 * 300.0,
        order_id="PAPER_BUY", status="open",
    )
    stale = GridLevel(
        price=0.1341, side="sell", qty=1512.13, size_usd=0.1341 * 1512.13,
        order_id="PAPER_STALE", status="open", linked_buy_price=0.1319,
    )
    # Buy first so post-buy cancel runs before the sell level is visited.
    eng.grid_levels = [buy, stale]
    _lag_fifo(monkeypatch, avg=0.1319, mx=0.1320)

    fills = eng.tick(0.1343, portfolio=None, exchange=_FakeExchange())
    assert any(f.get("side") == "buy" for f in fills)
    assert stale.status == "cancelled", f"stale sell status={stale.status}"
    new_sells = [
        l for l in eng.grid_levels
        if l.side == "sell" and l.status in ("open", "pending")
    ]
    assert new_sells, "expected replacement sell after buy"
    assert all(l.price + 1e-12 >= 0.1343 for l in new_sells)


def test_rebuild_with_lagged_fifo_respects_sticky_post_buy_floor(monkeypatch):
    """Buy raises sticky; lagged FIFO/portfolio must not undercut fee-proof sells on rebuild."""
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=True)
    buy_px = 0.1343
    buy_qty = 3009.25
    prior_held = 1000.0
    held = prior_held + buy_qty
    eng._last_base_qty_held = held
    # Post-buy sticky raised to the new fill (FIFO still lagging below).
    eng._last_portfolio_avg_cost = buy_px
    _lag_fifo(monkeypatch, avg=0.1320, mx=0.1325)

    portfolio = SimpleNamespace(
        holdings={
            "ARKM/USDT": SimpleNamespace(
                units_held=held,
                avg_cost_basis=0.1320,  # lagged portfolio avg
            )
        }
    )
    eng.grid_levels = []
    eng.build_grid(buy_px, portfolio=portfolio, force=True)

    sells = [
        l for l in eng.grid_levels
        if l.side == "sell" and l.status in ("open", "pending")
    ]
    assert sells, "expected sell levels after rebuild with holdings"
    # Sticky must survive lagged overwrite path.
    assert float(eng._last_portfolio_avg_cost) + 1e-12 >= buy_px

    for s in sells:
        floor = min_fee_proof_sell_price(
            buy_px, s.qty, fee_factor=get_fee_factor(), min_net_usd=0.50
        )
        assert s.price + 1e-12 >= floor, (
            f"rebuild sell @{s.price} under fee-proof(sticky={buy_px})+$0.50 "
            f"floor {floor} qty={s.qty}"
        )


def test_place_grid_orders_raise_only_sticky(monkeypatch):
    """place_grid_orders must not overwrite sticky downward while holding."""
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=True)
    eng._last_portfolio_avg_cost = 0.1343
    eng._last_base_qty_held = 4000.0
    eng.grid_levels = []
    _lag_fifo(monkeypatch, avg=0.1320, mx=0.1325)

    portfolio = SimpleNamespace(
        holdings={
            "ARKM/USDT": SimpleNamespace(
                units_held=4000.0,
                avg_cost_basis=0.1320,
            )
        }
    )
    eng.place_grid_orders(portfolio, _FakeExchange())
    assert float(eng._last_portfolio_avg_cost) + 1e-12 >= 0.1343


def test_cancel_unsafe_leaves_open_when_live_exchange_cancel_fails(monkeypatch):
    """Live (non-paper) cancel failure must not pretend the sell was cancelled."""
    eng = GridEngine(symbol="ARKM/USDT", allocated_usd=1000.0, paper_mode=False)
    eng._last_base_qty_held = 4000.0
    eng._last_portfolio_avg_cost = 0.1343
    stale = GridLevel(
        price=0.1341,
        side="sell",
        qty=1512.13,
        size_usd=0.1341 * 1512.13,
        order_id="LIVE_STALE",
        status="open",
        linked_buy_price=0.1320,
    )
    eng.grid_levels = [stale]
    _lag_fifo(monkeypatch)

    class _FailExchange:
        def cancel_order(self, oid, symbol, params=None):
            raise RuntimeError("exchange down")

    n = eng.cancel_unsafe_resting_sells(
        _FailExchange(), units_held=4000.0, portfolio_avg_cost=0.1343
    )
    assert n == 0
    assert stale.status == "open"

"""
Settled fifo_cycles must be append-only.

Reproduces the 2026-09-25 dashboard bug:
  daily_realised_pnl fell 417.84 → 386.44 while cycles_today rose 144 → 155.
  Root cause: run_fifo_match DELETEd all fifo_cycles and rematched from fills,
  so a fill resync revised already-booked nets.

Contract:
  - daily / all-time net = SQL sum of booked fifo_cycles nets for that scope
  - a new cycle may ADD to the sum; it must not erase or rewrite prior nets
  - refreshing / re-running match must not shrink an existing sell_fill_id row
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trading_engine.spot.fifo_reconciler as fr


def _seed_fill(db: sqlite3.Connection, *, fid, symbol, side, price, qty, fee, ts_ms, ts):
    db.execute(
        """
        INSERT OR IGNORE INTO fills
            (id, symbol, side, price, qty, cost, fee, ts_ms, timestamp, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fid,
            symbol,
            side,
            price,
            qty,
            price * qty,
            fee,
            ts_ms,
            ts,
            ts,
        ),
    )


@pytest.fixture()
def fifo_db(tmp_path, monkeypatch):
    db_path = tmp_path / "spot_trades_test.db"
    monkeypatch.setattr(fr, "DB_PATH", db_path)
    conn = fr._conn()
    conn.close()
    return db_path


def test_rerun_match_does_not_rewrite_booked_net(fifo_db):
    """Late cheap buy must not revise an already-booked sell's net on rematch."""
    day = "2026-09-25"
    conn = sqlite3.connect(fifo_db)
    # First pass: expensive buy then sell → modest net booked
    _seed_fill(
        conn,
        fid="b_dear",
        symbol="SEI/USDT",
        side="buy",
        price=0.0700,
        qty=1000.0,
        fee=0.05,
        ts_ms=1_000,
        ts=f"{day}T10:00:00.000Z",
    )
    _seed_fill(
        conn,
        fid="s1",
        symbol="SEI/USDT",
        side="sell",
        price=0.0720,
        qty=1000.0,
        fee=0.05,
        ts_ms=2_000,
        ts=f"{day}T11:00:00.000Z",
    )
    conn.commit()
    conn.close()

    n1 = fr.run_fifo_match(fee_rate=0.001)
    assert n1 == 1
    daily1 = fr.get_daily_pnl(day)
    all1 = fr.get_alltime_pnl()
    assert daily1["cycles"] == 1
    booked_net = daily1["net_pnl"]
    assert booked_net > 0

    conn = sqlite3.connect(fifo_db)
    row = conn.execute(
        "SELECT net_pnl, buy_price, gross_pnl FROM fifo_cycles WHERE sell_fill_id=?",
        ("s1",),
    ).fetchone()
    assert row is not None
    net_s1, buy_px_s1, gross_s1 = row
    conn.close()

    # Second pass: insert an EARLIER cheap buy that would rematch s1 cheaper
    # (and inflate gross/net) if DELETE+rebuild were still in place.
    conn = sqlite3.connect(fifo_db)
    _seed_fill(
        conn,
        fid="b_cheap_early",
        symbol="SEI/USDT",
        side="buy",
        price=0.0500,
        qty=1000.0,
        fee=0.05,
        ts_ms=500,
        ts=f"{day}T09:00:00.000Z",
    )
    # Plus a brand-new sell that may add a new cycle
    _seed_fill(
        conn,
        fid="b2",
        symbol="SEI/USDT",
        side="buy",
        price=0.0710,
        qty=500.0,
        fee=0.03,
        ts_ms=3_000,
        ts=f"{day}T12:00:00.000Z",
    )
    _seed_fill(
        conn,
        fid="s2",
        symbol="SEI/USDT",
        side="sell",
        price=0.0730,
        qty=500.0,
        fee=0.03,
        ts_ms=4_000,
        ts=f"{day}T13:00:00.000Z",
    )
    conn.commit()
    conn.close()

    n2 = fr.run_fifo_match(fee_rate=0.001)
    # Only the new sell should insert; s1 must be IGNORE'd
    assert n2 == 1

    conn = sqlite3.connect(fifo_db)
    row2 = conn.execute(
        "SELECT net_pnl, buy_price, gross_pnl FROM fifo_cycles WHERE sell_fill_id=?",
        ("s1",),
    ).fetchone()
    assert row2 is not None
    assert row2[0] == net_s1
    assert row2[1] == buy_px_s1
    assert row2[2] == gross_s1
    # Prove a full rematch WOULD have changed buy_price if we had rewritten
    assert buy_px_s1 == pytest.approx(0.0700, abs=1e-6)
    assert abs(buy_px_s1 - 0.0500) > 1e-6

    s2 = conn.execute(
        "SELECT net_pnl FROM fifo_cycles WHERE sell_fill_id=?",
        ("s2",),
    ).fetchone()
    assert s2 is not None
    conn.close()

    daily2 = fr.get_daily_pnl(day)
    all2 = fr.get_alltime_pnl()
    assert daily2["cycles"] == 2
    # Prior booked net preserved; daily grows only by the new cycle
    assert daily2["net_pnl"] == pytest.approx(booked_net + s2[0], abs=0.02)
    assert all2["net_pnl"] == pytest.approx(all1["net_pnl"] + s2[0], abs=0.02)
    # Must not shrink relative to the previously booked single-cycle sum
    assert daily2["net_pnl"] >= booked_net - 1e-9


def test_get_daily_pnl_is_sql_sum_of_booked_nets(fifo_db):
    day = "2026-09-25"
    conn = sqlite3.connect(fifo_db)
    for i, (bp, sp, qty) in enumerate(
        [(0.10, 0.11, 100.0), (0.20, 0.21, 50.0), (0.50, 0.52, 10.0)], start=1
    ):
        _seed_fill(
            conn,
            fid=f"b{i}",
            symbol="OP/USDT",
            side="buy",
            price=bp,
            qty=qty,
            fee=0.01,
            ts_ms=i * 1000,
            ts=f"{day}T0{i}:00:00.000Z",
        )
        _seed_fill(
            conn,
            fid=f"s{i}",
            symbol="OP/USDT",
            side="sell",
            price=sp,
            qty=qty,
            fee=0.01,
            ts_ms=i * 1000 + 500,
            ts=f"{day}T0{i}:30:00.000Z",
        )
    conn.commit()
    conn.close()

    assert fr.run_fifo_match(fee_rate=0.001) == 3
    daily = fr.get_daily_pnl(day)
    conn = sqlite3.connect(fifo_db)
    nets = [r[0] for r in conn.execute(
        "SELECT net_pnl FROM fifo_cycles WHERE sell_timestamp LIKE ?",
        (f"{day}%",),
    ).fetchall()]
    conn.close()
    assert daily["cycles"] == len(nets) == 3
    assert daily["net_pnl"] == pytest.approx(round(sum(nets), 2), abs=0.01)

    # Refresh path: rematch with zero new fills must not change row or sum
    assert fr.run_fifo_match(fee_rate=0.001) == 0
    daily_again = fr.get_daily_pnl(day)
    assert daily_again == daily

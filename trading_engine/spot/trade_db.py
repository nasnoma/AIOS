"""
trading_engine/spot/trade_db.py

Permanent SQLite Database for Spot Engine Trade Recording.
Stores exact exchange fills, fees, and closed cycles permanently on disk.
Zero reliance on ephemeral in-memory state or exchange 100-trade rolling buffers.
"""

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger
from typing import List, Dict, Any, Optional

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "spot_trades.db"
DB_PATH = _DEFAULT_DB_PATH  # tests may monkeypatch


def get_db_path() -> Path:
    """Same ledger file as fifo_reconciler. Env SPOT_TRADES_DB_PATH wins."""
    env = (os.environ.get("SPOT_TRADES_DB_PATH") or "").strip()
    if env:
        return Path(env)
    return Path(DB_PATH)


def init_db():
    path = get_db_path()
    os.makedirs(path.parent, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                qty REAL NOT NULL,
                cost REAL NOT NULL,
                fee REAL NOT NULL,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS completed_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                buy_order_id TEXT,
                sell_order_id TEXT,
                buy_price REAL NOT NULL,
                sell_price REAL NOT NULL,
                qty REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                fee REAL NOT NULL,
                net_pnl REAL NOT NULL,
                timestamp TEXT NOT NULL,
                UNIQUE(symbol, timestamp, qty)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                date TEXT PRIMARY KEY,
                starting_equity REAL NOT NULL,
                ending_equity REAL,
                total_realised_pnl REAL DEFAULT 0.0,
                cycles_count INTEGER DEFAULT 0,
                fees_paid REAL DEFAULT 0.0
            )
        """)
        conn.commit()

def record_fill(fill: Dict[str, Any]):
    init_db()
    try:
        with sqlite3.connect(str(get_db_path())) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO fills (id, symbol, side, price, qty, cost, fee, timestamp, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(fill.get('id', '')),
                str(fill.get('symbol', '')),
                str(fill.get('side', '')).lower(),
                float(fill.get('price', 0.0)),
                float(fill.get('qty', fill.get('amount', 0.0))),
                float(fill.get('cost', 0.0)),
                float(fill.get('fee', 0.0)),
                str(fill.get('timestamp', fill.get('datetime', ''))),
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()
    except Exception as e:
        logger.debug(f"record_fill DB error: {e}")

def record_completed_cycle(cycle: Dict[str, Any]):
    init_db()
    try:
        with sqlite3.connect(str(get_db_path())) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO completed_cycles (symbol, buy_order_id, sell_order_id, buy_price, sell_price, qty, gross_pnl, fee, net_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(cycle.get('symbol', '')),
                str(cycle.get('buy_order_id', '')),
                str(cycle.get('sell_order_id', '')),
                float(cycle.get('buy_price', 0.0)),
                float(cycle.get('sell_price', 0.0)),
                float(cycle.get('qty', 0.0)),
                float(cycle.get('gross_pnl', 0.0)),
                float(cycle.get('fee', 0.0)),
                float(cycle.get('net_pnl', 0.0)),
                str(cycle.get('timestamp', datetime.now(timezone.utc).isoformat()))
            ))
            conn.commit()
    except Exception as e:
        logger.debug(f"record_completed_cycle DB error: {e}")

def get_completed_cycles(limit: int = 100) -> List[Dict[str, Any]]:
    init_db()
    try:
        with sqlite3.connect(str(get_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM completed_cycles 
                WHERE (sell_order_id IS NULL OR NOT sell_order_id LIKE 'PAPER%')
                  AND (buy_order_id IS NULL OR NOT buy_order_id LIKE 'PAPER%')
                  AND NOT timestamp LIKE '%+00:00%'
                ORDER BY id DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"get_completed_cycles error: {e}")
        return []

def get_today_metrics() -> Dict[str, Any]:
    init_db()
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(str(get_db_path())) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*), COALESCE(SUM(gross_pnl), 0.0), COALESCE(SUM(fee), 0.0), COALESCE(SUM(net_pnl), 0.0)
                FROM completed_cycles
                WHERE timestamp LIKE ?
            """, (f"{today_utc}%",))
            row = cursor.fetchone()
            return {
                'cycles_today': int(row[0] or 0),
                'gross_today': round(float(row[1] or 0.0), 2),
                'fees_today': round(float(row[2] or 0.0), 2),
                'pnl_today': round(float(row[3] or 0.0), 2)
            }
    except Exception as e:
        logger.debug(f"get_today_metrics error: {e}")
        return {'cycles_today': 0, 'gross_today': 0.0, 'fees_today': 0.0, 'pnl_today': 0.0}



def upsert_daily_snapshot(
    *,
    date: str,
    net_pnl: float = 0.0,
    gross_pnl: float = 0.0,
    fees: float = 0.0,
    cycles: int = 0,
    ending_equity: float | None = None,
    starting_equity: float | None = None,
) -> None:
    """Observability-only UTC day snapshot. Never touches the trade path."""
    init_db()
    try:
        with sqlite3.connect(str(get_db_path())) as conn:
            cur = conn.cursor()
            # Preserve starting_equity if row already exists and caller omitted it
            existing = cur.execute(
                "SELECT starting_equity FROM daily_snapshots WHERE date = ?", (date,)
            ).fetchone()
            start = float(starting_equity) if starting_equity is not None else (
                float(existing[0]) if existing else float(ending_equity or 0.0)
            )
            cur.execute(
                """
                INSERT INTO daily_snapshots (date, starting_equity, ending_equity, total_realised_pnl, cycles_count, fees_paid)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    ending_equity = excluded.ending_equity,
                    total_realised_pnl = excluded.total_realised_pnl,
                    cycles_count = excluded.cycles_count,
                    fees_paid = excluded.fees_paid
                """,
                (
                    str(date),
                    float(start or 0.0),
                    float(ending_equity) if ending_equity is not None else None,
                    float(net_pnl or 0.0),
                    int(cycles or 0),
                    float(fees or 0.0),
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"upsert_daily_snapshot skipped: {e}")


_last_snapshot_date: str | None = None


def maybe_persist_daily_snapshot(*, equity: float = 0.0) -> None:
    """When UTC day rolls, persist (date, net, gross, fees, cycles). Fail-open."""
    global _last_snapshot_date
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _last_snapshot_date == today:
            return
        m = get_today_metrics()
        upsert_daily_snapshot(
            date=today,
            net_pnl=float(m.get("pnl_today") or 0.0),
            gross_pnl=float(m.get("gross_today") or 0.0),
            fees=float(m.get("fees_today") or 0.0),
            cycles=int(m.get("cycles_today") or 0),
            ending_equity=float(equity or 0.0) or None,
        )
        _last_snapshot_date = today
    except Exception as e:
        logger.debug(f"maybe_persist_daily_snapshot: {e}")


init_db()

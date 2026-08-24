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

DB_PATH = Path(__file__).parent.parent.parent / "data" / "spot_trades.db"

def init_db():
    os.makedirs(DB_PATH.parent, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
                timestamp TEXT NOT NULL
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO completed_cycles (symbol, buy_order_id, sell_order_id, buy_price, sell_price, qty, gross_pnl, fee, net_pnl, timestamp)
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
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM completed_cycles ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"get_completed_cycles error: {e}")
        return []

def get_today_metrics() -> Dict[str, Any]:
    init_db()
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_PATH) as conn:
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

init_db()

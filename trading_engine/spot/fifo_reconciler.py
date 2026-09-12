"""
trading_engine/spot/fifo_reconciler.py

Authoritative FIFO P&L reconciler.

Design:
  1. Pulls ALL historical fills from Bybit per-symbol with full pagination.
     Results are persisted in `fills` table — incremental on subsequent runs.
  2. Runs strict FIFO matching: oldest unmatched buy -> each sell in time order.
  3. Matched cycles are written to `fifo_cycles` table (idempotent insert).
  4. Exposes get_daily_pnl() / get_alltime_pnl() for the dashboard.

No estimates. No fallback costs. No stale historical dict lookups.
If a sell has no matching buy (edge case: inventory pre-dates bot), it is
skipped and will be matched when the corresponding buy fill is synced.
"""

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque
from typing import Optional, Dict, Any, List

from loguru import logger

DB_PATH = Path(__file__).parent.parent.parent / "data" / "spot_trades.db"

# Every symbol the bot has ever traded
ALL_SYMBOLS = [
    "INJ/USDT", "FET/USDT", "NEAR/USDT", "SUI/USDT", "ARKM/USDT",
    "TIA/USDT", "ARB/USDT", "OP/USDT", "APT/USDT", "AVAX/USDT",
    "SEI/USDT", "SOL/USDT",
    # Legacy bags
    "UNI/USDT", "RENDER/USDT", "ADA/USDT", "ICP/USDT", "LINK/USDT",
    "ETH/USDT", "ATOM/USDT", "DOT/USDT", "ALGO/USDT", "XAUT/USDT",
    "BTC/USDT", "MNT/USDT",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_db(conn: sqlite3.Connection):
    # Step 1: migrate fills table — add ts_ms if column is missing from pre-existing DB
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)").fetchall()}
    if "ts_ms" not in existing_cols and existing_cols:
        conn.execute("ALTER TABLE fills ADD COLUMN ts_ms INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            UPDATE fills SET ts_ms = CAST(
                (julianday(timestamp) - julianday('1970-01-01')) * 86400000 AS INTEGER
            ) WHERE ts_ms = 0 AND timestamp != ''
        """)
        conn.commit()

    # Step 2: create all tables and indexes
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            id          TEXT PRIMARY KEY,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            price       REAL NOT NULL,
            qty         REAL NOT NULL,
            cost        REAL NOT NULL,
            fee         REAL NOT NULL,
            ts_ms       INTEGER NOT NULL DEFAULT 0,
            timestamp   TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fills_sym_ts ON fills (symbol, ts_ms);

        CREATE TABLE IF NOT EXISTS fifo_cycles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            buy_fill_ids    TEXT,
            sell_fill_id    TEXT NOT NULL,
            buy_price       REAL NOT NULL,
            sell_price      REAL NOT NULL,
            qty             REAL NOT NULL,
            gross_pnl       REAL NOT NULL,
            fee             REAL NOT NULL,
            net_pnl         REAL NOT NULL,
            sell_ts_ms      INTEGER NOT NULL,
            sell_timestamp  TEXT NOT NULL,
            UNIQUE(sell_fill_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fifo_ts  ON fifo_cycles (sell_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_fifo_sym ON fifo_cycles (symbol, sell_ts_ms);

        CREATE TABLE IF NOT EXISTS sync_state (
            symbol      TEXT PRIMARY KEY,
            last_ts_ms  INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()




def _conn() -> sqlite3.Connection:
    os.makedirs(DB_PATH.parent, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    _init_db(c)
    return c


# ---------------------------------------------------------------------------
# Bybit fill fetcher — paginated per symbol
# ---------------------------------------------------------------------------

def _fetch_fills_window(exchange, since_ms: int, until_ms: int) -> List[Dict]:
    """
    Fetch all fills between since_ms and until_ms across all tracked symbols.
    Querying per-symbol guarantees no symbols are dropped by Bybit's global 100-limit.
    """
    results = []
    seen = set()
    for sym in ALL_SYMBOLS:
        cursor = since_ms
        while cursor < until_ms:
            try:
                batch = exchange.fetch_my_trades(
                    sym,
                    params={"category": "spot", "startTime": cursor,
                            "endTime": until_ms, "limit": 100}
                )
            except Exception as e:
                logger.debug(f"[FIFO] fetch window {sym} {cursor}-{until_ms}: {e}")
                break
            if not batch:
                break
            new = [t for t in batch if t["id"] not in seen]
            if not new:
                break
            results.extend(new)
            seen.update(t["id"] for t in new)
            if len(batch) < 100:
                break
            cursor = batch[-1]["timestamp"] + 1
    return results


def _fetch_all_fills_since(exchange, since_ms: int) -> List[Dict]:
    """
    Fetch ALL fills from since_ms to now using 7-day sliding windows.
    Bybit only returns up to 7 days of history per window reliably.
    """
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    WINDOW   = 7 * 24 * 60 * 60 * 1000   # 7 days in ms
    all_fills: Dict[str, Dict] = {}

    start = since_ms
    while start < now_ms:
        end   = min(start + WINDOW, now_ms)
        batch = _fetch_fills_window(exchange, start, end)
        for t in batch:
            all_fills[t["id"]] = t
        start = end + 1

    return list(all_fills.values())


# ---------------------------------------------------------------------------
# Sync fills from Bybit → SQLite (incremental)
# ---------------------------------------------------------------------------

def sync_fills(exchange) -> int:
    """
    Incrementally fetch all fills from Bybit using 7-day sliding windows.
    Bybit's global fetch_my_trades (no symbol) + startTime/endTime returns all
    spot fills across all coins up to 100 per call. We walk forward in 7-day
    windows from last_synced to now, persisting every fill we get.
    Returns total new rows inserted.
    """
    db      = _conn()
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Find the actual latest fill timestamp in the SQLite database
    max_fill_row = db.execute("SELECT MAX(ts_ms) FROM fills").fetchone()
    max_fill_ts  = int(max_fill_row[0]) if (max_fill_row and max_fill_row[0]) else 0

    if max_fill_ts < 1_000_000_000_000:
        # First run — start from Aug 1, 2026 (covers all bot history from inception)
        since_ms = 1785542400000
    else:
        since_ms = max(1785542400000, max_fill_ts - 300_000)   # 5-minute overlap before latest fill


    WINDOW = 5 * 24 * 60 * 60 * 1000  # 5 days (Bybit rejects windows >= 7 days)
    max_ts  = since_ms
    total_new = 0

    start = since_ms
    while start < now_ms:
        end   = min(start + WINDOW, now_ms)

        batch = _fetch_fills_window(exchange, start, end)

        if batch:
            inserted_window = 0
            for t in batch:
                ts_ms    = int(t.get("timestamp") or 0)
                fee_obj  = t.get("fee") or {}
                fee_cost = float(fee_obj.get("cost") or 0.0)
                sym      = str(t.get("symbol") or "")
                # Only store symbols we track
                if sym not in ALL_SYMBOLS:
                    continue

                try:
                    db.execute("""
                        INSERT OR IGNORE INTO fills
                            (id, symbol, side, price, qty, cost, fee, ts_ms, timestamp, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(t["id"]),
                        sym,
                        str(t.get("side", "")).lower(),
                        float(t.get("price") or 0.0),
                        float(t.get("amount") or 0.0),
                        float(t.get("cost") or 0.0),
                        fee_cost,
                        ts_ms,
                        str(t.get("datetime") or ""),
                        now_iso,
                    ))
                    inserted_window += db.execute("SELECT changes()").fetchone()[0]
                except Exception as e:
                    logger.debug(f"[FIFO] insert fill {t.get('id')}: {e}")
                if ts_ms > max_ts:
                    max_ts = ts_ms

            db.commit()
            total_new += inserted_window
            if inserted_window:
                dt_str = datetime.fromtimestamp(end / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                logger.info(f"[FIFO] window ending {dt_str}: +{inserted_window} new fills")

        start = end + 1

    # Update global watermark
    db.execute("""
        INSERT INTO sync_state (symbol, last_ts_ms) VALUES ('_all', ?)
        ON CONFLICT(symbol) DO UPDATE SET last_ts_ms = excluded.last_ts_ms
    """, (max_ts,))
    db.commit()
    db.close()
    return total_new


# ---------------------------------------------------------------------------
# FIFO matcher
# ---------------------------------------------------------------------------

def run_fifo_match(fee_rate: float = 0.00075) -> int:
    """
    Match sell fills against prior buy fills in strict chronological order.
    A sell fill can ONLY match against buy inventory that was filled BEFORE that sell (ts_ms_buy <= ts_ms_sell).
    """
    db = _conn()

    # Clear previous fifo_cycles table to ensure a completely clean, authoritative replay
    db.execute("DELETE FROM fifo_cycles")
    db.commit()

    # Load every fill in strict chronological order
    all_rows = db.execute(
        "SELECT id, symbol, side, price, qty, fee, ts_ms, timestamp "
        "FROM fills ORDER BY ts_ms ASC, id ASC"
    ).fetchall()

    buy_q: Dict[str, deque] = defaultdict(deque)
    inserted = 0

    for r in all_rows:
        sym   = r["symbol"]
        side  = r["side"]
        price = float(r["price"])
        qty   = float(r["qty"])
        fee   = float(r["fee"])
        fid   = r["id"]
        ts_ms = int(r["ts_ms"])
        ts    = r["timestamp"]

        if side == "buy":
            buy_q[sym].append({
                "id":    fid,
                "price": price,
                "rem":   qty,
                "ts_ms": ts_ms,
            })
        elif side == "sell":
            needed       = qty
            matched_cost = 0.0
            matched_qty  = 0.0
            buy_ids      = []

            q = buy_q.get(sym)
            if q:
                while q and needed > 1e-8:
                    b    = q[0]
                    take = min(needed, b["rem"])
                    matched_cost += take * b["price"]
                    matched_qty  += take
                    needed       -= take
                    b["rem"]     -= take
                    buy_ids.append(b["id"])
                    if b["rem"] <= 1e-8:
                        q.popleft()

            if matched_qty < 1e-8:
                # Sell had no preceding buy fills in our window — skip
                continue

            avg_bp    = matched_cost / matched_qty
            buy_fee   = avg_bp * matched_qty * fee_rate   # maker buy fee
            total_fee = fee + buy_fee                     # actual sell fee + maker buy fee
            gross     = (price - avg_bp) * matched_qty
            net       = gross - total_fee

            try:
                db.execute("""
                    INSERT OR IGNORE INTO fifo_cycles
                        (symbol, buy_fill_ids, sell_fill_id, buy_price, sell_price,
                         qty, gross_pnl, fee, net_pnl, sell_ts_ms, sell_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sym,
                    ",".join(buy_ids[:10]),
                    fid,
                    round(avg_bp, 6),
                    round(price, 6),
                    round(matched_qty, 6),
                    round(gross, 4),
                    round(total_fee, 4),
                    round(net, 4),
                    ts_ms,
                    ts,
                ))
                inserted += db.execute("SELECT changes()").fetchone()[0]
            except Exception as e:
                logger.debug(f"[FIFO] insert cycle {fid}: {e}")

    db.commit()
    db.close()
    return inserted



# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_daily_pnl(date_utc: Optional[str] = None) -> Dict[str, Any]:
    if date_utc is None:
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db  = _conn()
    row = db.execute("""
        SELECT COUNT(*), COALESCE(SUM(gross_pnl),0),
               COALESCE(SUM(fee),0), COALESCE(SUM(net_pnl),0)
        FROM fifo_cycles WHERE sell_timestamp LIKE ?
    """, (f"{date_utc}%",)).fetchone()
    db.close()
    cnt, gross, fee, net = row
    return {
        "date":      date_utc,
        "cycles":    int(cnt   or 0),
        "gross_pnl": round(float(gross or 0), 2),
        "fees":      round(float(fee   or 0), 2),
        "net_pnl":   round(float(net   or 0), 2),
    }


def get_alltime_pnl() -> Dict[str, Any]:
    db  = _conn()
    row = db.execute("""
        SELECT COUNT(*), COALESCE(SUM(gross_pnl),0),
               COALESCE(SUM(fee),0), COALESCE(SUM(net_pnl),0)
        FROM fifo_cycles
    """).fetchone()
    db.close()
    cnt, gross, fee, net = row
    return {
        "cycles_total": int(cnt   or 0),
        "gross_pnl":    round(float(gross or 0), 2),
        "fees":         round(float(fee   or 0), 2),
        "net_pnl":      round(float(net   or 0), 2),
    }


def get_recent_cycles(limit: int = 50) -> List[Dict[str, Any]]:
    db   = _conn()
    rows = db.execute("""
        SELECT symbol, buy_price, sell_price, qty,
               gross_pnl, fee, net_pnl, sell_timestamp
        FROM fifo_cycles ORDER BY sell_ts_ms DESC LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_symbol_pnl_breakdown(date_utc: Optional[str] = None) -> List[Dict[str, Any]]:
    if date_utc is None:
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db   = _conn()
    rows = db.execute("""
        SELECT symbol,
               COUNT(*)                   AS cycles,
               COALESCE(SUM(gross_pnl),0) AS gross,
               COALESCE(SUM(fee),0)       AS fee,
               COALESCE(SUM(net_pnl),0)   AS net
        FROM fifo_cycles WHERE sell_timestamp LIKE ?
        GROUP BY symbol ORDER BY net DESC
    """, (f"{date_utc}%",)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_fifo_cost_basis(symbol: str, units_held: Optional[float] = None) -> Dict[str, float]:
    """
    Query the real-time FIFO open buy inventory for a symbol from SQLite fills.
    If units_held is provided, it matches the exact quantity currently in the wallet
    taking the most recent active buy lots (protecting against missing historical fills/ghost lots).
    Returns:
      {
        'units_open': float,
        'avg_cost': float,
        'min_buy_price': float,
        'max_buy_price': float (high-water mark of open lots),
        'oldest_buy_age_hours': float,
        'newest_buy_age_hours': float
      }
    """
    db = _conn()
    rows = db.execute(
        "SELECT side, price, qty, ts_ms FROM fills WHERE symbol = ? ORDER BY ts_ms ASC, id ASC",
        (symbol,)
    ).fetchall()
    db.close()

    buy_lots = deque()
    for r in rows:
        side = r["side"]
        price = float(r["price"])
        qty = float(r["qty"])
        ts_ms = int(r["ts_ms"]) if "ts_ms" in r.keys() and r["ts_ms"] else 0
        if side == "buy":
            buy_lots.append({"price": price, "rem": qty, "ts_ms": ts_ms})
        elif side == "sell":
            needed = qty
            while buy_lots and needed > 1e-8:
                take = min(needed, buy_lots[0]["rem"])
                needed -= take
                buy_lots[0]["rem"] -= take
                if buy_lots[0]["rem"] <= 1e-8:
                    buy_lots.popleft()

    active_lots = [b for b in buy_lots if b["rem"] > 1e-6]
    if not active_lots:
        return {
            'units_open': 0.0,
            'avg_cost': 0.0,
            'min_buy_price': 0.0,
            'max_buy_price': 0.0,
            'oldest_buy_age_hours': 0.0,
            'newest_buy_age_hours': 0.0,
        }

    # If actual units_held is known and smaller than ghost lots remaining in SQLite,
    # take the most recent lots corresponding to units_held (reverse chronological)
    if units_held is not None and units_held > 1e-6:
        needed_h = units_held
        wallet_lots = []
        for b in reversed(active_lots):
            take_q = min(needed_h, b["rem"])
            wallet_lots.append({"price": b["price"], "rem": take_q, "ts_ms": b["ts_ms"]})
            needed_h -= take_q
            if needed_h <= 1e-6:
                break
        active_lots = wallet_lots

    tot_qty = sum(b["rem"] for b in active_lots)
    tot_val = sum(b["rem"] * b["price"] for b in active_lots)
    avg_cost = tot_val / tot_qty if tot_qty > 0 else 0.0
    min_p = min(b["price"] for b in active_lots) if active_lots else 0.0
    max_p = max(b["price"] for b in active_lots) if active_lots else 0.0

    import time
    now_ms = time.time() * 1000.0
    valid_ts = [b["ts_ms"] for b in active_lots if b.get("ts_ms") and b["ts_ms"] > 0]
    oldest_age_h = round(max(0.0, (now_ms - min(valid_ts)) / 3600000.0), 1) if valid_ts else 0.0
    newest_age_h = round(max(0.0, (now_ms - max(valid_ts)) / 3600000.0), 1) if valid_ts else 0.0

    return {
        'units_open': round(tot_qty, 6),
        'avg_cost': round(avg_cost, 6),
        'min_buy_price': round(min_p, 6),
        'max_buy_price': round(max_p, 6),
        'oldest_buy_age_hours': oldest_age_h,
        'newest_buy_age_hours': newest_age_h,
    }


# ---------------------------------------------------------------------------
# Top-level entry point (called from scheduler)
# ---------------------------------------------------------------------------



def get_fifo_lot_cost_for_qty(symbol: str, qty: float) -> Dict[str, float]:
    """
    Cost basis for selling `qty` under strict FIFO (oldest open lots first).

    This is the correct floor for a partial sell: never below the buy prices of
    the lots that FIFO would actually consume — without inflating to the max
    lot across the entire book (which stalls cycles when one expensive lot exists).
    """
    qty = float(qty or 0.0)
    if qty <= 1e-12:
        return {
            'units': 0.0,
            'avg_cost': 0.0,
            'min_buy_price': 0.0,
            'max_buy_price': 0.0,
        }

    db = _conn()
    rows = db.execute(
        "SELECT side, price, qty, ts_ms FROM fills WHERE symbol = ? ORDER BY ts_ms ASC, id ASC",
        (symbol,)
    ).fetchall()
    db.close()

    buy_lots = deque()
    for r in rows:
        side = r["side"]
        price = float(r["price"])
        q = float(r["qty"])
        ts_ms = int(r["ts_ms"]) if "ts_ms" in r.keys() and r["ts_ms"] else 0
        if side == "buy":
            buy_lots.append({"price": price, "rem": q, "ts_ms": ts_ms})
        elif side == "sell":
            needed = q
            while buy_lots and needed > 1e-8:
                take = min(needed, buy_lots[0]["rem"])
                needed -= take
                buy_lots[0]["rem"] -= take
                if buy_lots[0]["rem"] <= 1e-8:
                    buy_lots.popleft()

    needed = qty
    taken = []
    for b in buy_lots:
        if needed <= 1e-8:
            break
        if b["rem"] <= 1e-8:
            continue
        take = min(needed, b["rem"])
        taken.append({"price": b["price"], "qty": take})
        needed -= take

    if not taken:
        return {
            'units': 0.0,
            'avg_cost': 0.0,
            'min_buy_price': 0.0,
            'max_buy_price': 0.0,
        }

    tot_qty = sum(t["qty"] for t in taken)
    tot_val = sum(t["qty"] * t["price"] for t in taken)
    avg_cost = tot_val / tot_qty if tot_qty > 0 else 0.0
    return {
        'units': round(tot_qty, 6),
        'avg_cost': round(avg_cost, 6),
        'min_buy_price': round(min(t["price"] for t in taken), 6),
        'max_buy_price': round(max(t["price"] for t in taken), 6),
    }


def reconcile(exchange, fee_rate: float = 0.00075) -> Dict[str, Any]:
    """
    Sync new fills from Bybit, run FIFO match, return today's and all-time P&L.
    Safe to call frequently — fully incremental.
    """
    new_fills  = sync_fills(exchange)
    new_cycles = run_fifo_match(fee_rate=fee_rate)
    daily      = get_daily_pnl()
    alltime    = get_alltime_pnl()
    if new_fills or new_cycles:
        logger.info(
            f"[FIFO] +{new_fills} fills, +{new_cycles} cycles | "
            f"Today: ${daily['net_pnl']:+.2f} net ({daily['cycles']} cycles) | "
            f"All-time: ${alltime['net_pnl']:+.2f}"
        )
    return {"daily": daily, "alltime": alltime}

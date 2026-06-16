#!/usr/bin/env python3
"""
trading_engine/scratch/download_bybit_history.py

Downloads 5m OHLCV historical data for all required symbols from the
Bybit V5 public REST API (api.bybit.com) - no API key, no geo-blocking.

Paginated backwards from 'now', saving to /data/historical_5m_<SYMBOL>.csv
"""
from __future__ import annotations
import time
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
from loguru import logger

# ── Config ──────────────────────────────────────────────────────────────────
DATA_DIR      = Path("/Users/nasir.noma/claude_projects/AIOS/data")
BYBIT_API     = "https://api.bybit.com/v5/market/kline"
INTERVAL      = "5"           # 5-minute
LIMIT_PER_REQ = 200           # Bybit max per request
DAYS_BACK     = 270           # 180 train + 90 val
MIN_ROWS      = (180 + 90) * 288   # ~77,760 minimum candles needed
SLEEP_BETWEEN = 0.5           # seconds between requests (rate-limit guard)

SYMBOLS_NEEDED = [
    "LINK/USDT", "AVAX/USDT", "LTC/USDT", "XLM/USDT",
    "POL/USDT", "UNI/USDT", "DOT/USDT", "HBAR/USDT",
    "BNB/USDT", "IOTA/USDT", "XTZ/USDT", "ATOM/USDT",
    "ETC/USDT",
]

# Map ccxt-style symbols → Bybit symbol names
# Try linear perp first (most liquid), fall back to spot
SYMBOL_MAP: dict[str, list[tuple[str, str]]] = {
    "SOL/USDT":  [("linear", "SOLUSDT"),  ("spot", "SOLUSDT")],
    "XRP/USDT":  [("linear", "XRPUSDT"),  ("spot", "XRPUSDT")],
    "ADA/USDT":  [("linear", "ADAUSDT"),  ("spot", "ADAUSDT")],
    "ALGO/USDT": [("linear", "ALGOUSDT"), ("spot", "ALGOUSDT")],
    "LINK/USDT": [("linear", "LINKUSDT"), ("spot", "LINKUSDT")],
    "AVAX/USDT": [("linear", "AVAXUSDT"), ("spot", "AVAXUSDT")],
    "LTC/USDT":  [("linear", "LTCUSDT"),  ("spot", "LTCUSDT")],
    "XLM/USDT":  [("linear", "XLMUSDT"),  ("spot", "XLMUSDT")],
    "POL/USDT":  [("linear", "POLUSDT"),  ("spot", "POLUSDT")],
    "UNI/USDT":  [("linear", "UNIUSDT"),  ("spot", "UNIUSDT")],
    "DOT/USDT":  [("linear", "DOTUSDT"),  ("spot", "DOTUSDT")],
    "HBAR/USDT": [("linear", "HBARUSDT"), ("spot", "HBARUSDT")],
    "BNB/USDT":  [("linear", "BNBUSDT"),  ("spot", "BNBUSDT")],
    "IOTA/USDT": [("linear", "IOTAUSDT"), ("spot", "IOTAUSDT")],
    "XTZ/USDT":  [("linear", "XTZUSDT"),  ("spot", "XTZUSDT")],
    "ATOM/USDT": [("linear", "ATOMUSDT"), ("spot", "ATOMUSDT")],
    "ETC/USDT":  [("linear", "ETCUSDT"),  ("spot", "ETCUSDT")],
}


def fetch_symbol(category: str, bybit_sym: str, days: int = DAYS_BACK) -> pd.DataFrame | None:
    """
    Paginate backwards from now, collecting all 5m candles for `days` days.
    Returns a sorted DataFrame or None on failure.
    """
    now_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms  = now_ms - days * 24 * 3600 * 1000
    candle_ms = 5 * 60 * 1000  # 5 minutes in ms

    all_rows: list[list] = []
    end_ms = now_ms
    req_count = 0

    logger.info(f"  [{category}] {bybit_sym} — paginating {days}d of 5m candles...")

    while end_ms > start_ms:
        params = {
            "category": category,
            "symbol":   bybit_sym,
            "interval": INTERVAL,
            "end":      str(end_ms),
            "limit":    str(LIMIT_PER_REQ),
        }
        try:
            resp = requests.get(BYBIT_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("retCode") != 0:
                logger.warning(f"    retCode={data.get('retCode')} msg={data.get('retMsg')}")
                return None

            batch = data.get("result", {}).get("list", [])
            if not batch:
                break

            all_rows.extend(batch)
            req_count += 1

            # Bybit returns newest-first; oldest candle is last in list
            oldest_ts = int(batch[-1][0])
            if oldest_ts <= start_ms:
                break

            # Move end pointer before the oldest candle we just got
            end_ms = oldest_ts - candle_ms
            time.sleep(SLEEP_BETWEEN)

        except requests.exceptions.RequestException as e:
            logger.error(f"    Request failed: {e}")
            return None

    if not all_rows:
        return None

    logger.info(f"    {req_count} requests → {len(all_rows)} raw rows")

    # Bybit kline format: [startTime, open, high, low, close, volume, turnover]
    df = pd.DataFrame(all_rows, columns=["timestamp","open","high","low","close","volume","turnover"])
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[["open","high","low","close","volume"]].astype(float)
    df = df.drop_duplicates()

    # Trim to exactly the requested range
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    df = df[df.index >= cutoff]

    return df


def download_symbol(sym: str) -> bool:
    """Try each category/name variant until one works."""
    cache_name = sym.replace("/", "_").replace(":", "_")
    cache_path = DATA_DIR / f"historical_5m_{cache_name}.csv"

    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if len(existing) >= MIN_ROWS:
                logger.success(f"  ✅ {sym}: already cached ({len(existing):,} rows) — skipping")
                return True
            else:
                logger.info(f"  ♻️  {sym}: cache too small ({len(existing):,} < {MIN_ROWS:,}) — refreshing")
        except Exception:
            pass

    candidates = SYMBOL_MAP.get(sym, [])
    for category, bybit_sym in candidates:
        logger.info(f"\n{'─'*60}")
        logger.info(f"  Downloading {sym}  →  [{category}] {bybit_sym}")
        df = fetch_symbol(category, bybit_sym)

        if df is not None and len(df) >= 100:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path)
            logger.success(
                f"  ✅ {sym} saved: {len(df):,} rows  "
                f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}  "
                f"({cache_path.stat().st_size/1024/1024:.1f}MB)"
            )
            return True
        else:
            logger.warning(f"  ⚠️  {bybit_sym} [{category}] returned insufficient data — trying next variant")
            time.sleep(5)  # cool-down after rate-limit before next attempt

    logger.error(f"  ❌ {sym}: all variants failed")
    return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Download Bybit historical 5m candles data")
    parser.add_argument("--symbol", help="Single symbol to download (e.g. BTC/USDT or AAPL/USDT:USDT)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.symbol:
        sym = args.symbol.upper().strip()
        # Ensure it is mapped in SYMBOL_MAP
        if sym not in SYMBOL_MAP:
            # E.g., AAPL/USDT:USDT -> AAPLUSDT, SOL/USDT -> SOLUSDT
            clean_sym = sym.replace("/", "").split(":")[0]
            SYMBOL_MAP[sym] = [("linear", clean_sym), ("spot", clean_sym)]
        logger.info(f"Downloading single symbol: {sym}")
        ok = download_symbol(sym)
        sys.exit(0 if ok else 1)

    logger.info("=" * 65)
    logger.info("   BYBIT 5m HISTORICAL DATA DOWNLOADER")
    logger.info(f"   Period: {DAYS_BACK} days  |  Interval: 5m  |  Symbols: {len(SYMBOLS_NEEDED)}")
    logger.info("=" * 65)

    results = {"ok": [], "fail": []}
    for i, sym in enumerate(SYMBOLS_NEEDED, 1):
        logger.info(f"\n[{i}/{len(SYMBOLS_NEEDED)}] Processing {sym}")
        ok = download_symbol(sym)
        (results["ok"] if ok else results["fail"]).append(sym)

    logger.info("\n" + "=" * 65)
    logger.success(f"  DONE — {len(results['ok'])}/{len(SYMBOLS_NEEDED)} succeeded")
    if results["ok"]:
        logger.success(f"  Downloaded: {results['ok']}")
    if results["fail"]:
        logger.error(f"  Failed:     {results['fail']}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()

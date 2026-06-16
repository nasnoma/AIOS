#!/usr/bin/env python3
import time
import sys
from pathlib import Path
from datetime import datetime, timezone
import requests
import pandas as pd
from loguru import logger

DATA_DIR = Path("/Users/nasir.noma/claude_projects/AIOS/data")
BYBIT_API = "https://api.bybit.com/v5/market/kline"
INTERVAL = "5"
LIMIT_PER_REQ = 200
DAYS_BACK = 270
MIN_ROWS = (180 + 90) * 288
SLEEP_BETWEEN = 1.0  # Safe 1.0s sleep to avoid rate limits

def fetch_etc(category: str, bybit_sym: str) -> pd.DataFrame | None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - DAYS_BACK * 24 * 3600 * 1000
    candle_ms = 5 * 60 * 1000

    all_rows = []
    end_ms = now_ms
    req_count = 0

    logger.info(f"Downloading ETC/USDT [{category}] {bybit_sym}...")

    while end_ms > start_ms:
        params = {
            "category": category,
            "symbol": bybit_sym,
            "interval": INTERVAL,
            "end": str(end_ms),
            "limit": str(LIMIT_PER_REQ),
        }
        
        # Retry loop for rate limits or timeouts
        success = False
        for attempt in range(5):
            try:
                resp = requests.get(BYBIT_API, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("retCode") == 0:
                    batch = data.get("result", {}).get("list", [])
                    if not batch:
                        end_ms = 0  # Stop
                    else:
                        all_rows.extend(batch)
                        req_count += 1
                        oldest_ts = int(batch[-1][0])
                        end_ms = oldest_ts - candle_ms
                    success = True
                    break
                elif data.get("retCode") == 10006:
                    logger.warning(f"Rate limited (retCode=10006). Sleeping 5s (attempt {attempt+1}/5)...")
                    time.sleep(5)
                else:
                    logger.error(f"Error response: {data.get('retMsg')}")
                    return None
            except Exception as e:
                logger.warning(f"Request failed: {e}. Sleeping 5s (attempt {attempt+1}/5)...")
                time.sleep(5)
                
        if not success:
            logger.error("Failed to complete request after 5 attempts.")
            return None

        time.sleep(SLEEP_BETWEEN)

    if not all_rows:
        return None

    logger.info(f"Completed: {req_count} requests, {len(all_rows)} rows.")
    df = pd.DataFrame(all_rows, columns=["timestamp","open","high","low","close","volume","turnover"])
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[["open","high","low","close","volume"]].astype(float)
    df = df.drop_duplicates()
    
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=DAYS_BACK)
    df = df[df.index >= cutoff]
    return df

def main():
    # Try linear perp first, then spot
    for cat, sym in [("linear", "ETCUSDT"), ("spot", "ETCUSDT")]:
        df = fetch_etc(cat, sym)
        if df is not None and len(df) >= 100:
            cache_path = DATA_DIR / "historical_5m_ETC_USDT.csv"
            df.to_csv(cache_path)
            logger.success(f"Successfully cached {len(df):,} rows to {cache_path} ({cache_path.stat().st_size/1024/1024:.1f}MB)")
            sys.exit(0)
            
    logger.error("All ETC download attempts failed.")
    sys.exit(1)

if __name__ == "__main__":
    main()

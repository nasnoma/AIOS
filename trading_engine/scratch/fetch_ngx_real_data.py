"""
scratch/fetch_ngx_real_data.py

Fetches REAL historical OHLCV data from NGX Pulse API for all 19 tickers.
Uses 19 API calls (well within 100/day Personal limit).
Synthesizes High/Low from Open/Close since NGX Pulse only provides O+C+Vol.

Usage:
    cd /Users/nasir.noma/claude_projects/AIOS
    python -m trading_engine.scratch.fetch_ngx_real_data
"""
import sys
import time
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

API_KEY = "ngxpulse_q5mjduuebre6pr0x"
BASE_URL = "https://ngxpulse.ng/api/ngxdata/prices"
FROM_DATE = "2024-01-01"   # ~18 months of real data
OUT_DIR = PROJECT_ROOT / "data" / "ngx"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TICKERS = [
    "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
    "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
    "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
    "NASCON", "FLOURMILL", "NB", "MEYER",
]

HEADERS = {"X-API-Key": API_KEY}


def fetch_ticker(ticker: str) -> pd.DataFrame | None:
    url = f"{BASE_URL}/{ticker}?from={FROM_DATE}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [ERROR] {ticker}: {e}")
        return None

    prices = data.get("prices", [])
    if not prices:
        print(f"  [WARN]  {ticker}: no price data returned")
        return None

    df = pd.DataFrame(prices)
    df["timestamp"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("timestamp")
    df = df.sort_index()

    # Use open_price as open, close_price as close
    df["open"] = pd.to_numeric(df["open_price"], errors="coerce")
    df["close"] = pd.to_numeric(df["close_price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    # Synthesize High / Low (NGX Pulse doesn't provide them)
    # High = max(open, close) * (1 + abs(N(0, 0.005)))
    # Low  = min(open, close) * (1 - abs(N(0, 0.005)))
    np.random.seed(42)
    n = len(df)
    buf = np.abs(np.random.normal(0, 0.005, n))
    df["high"] = np.maximum(df["open"], df["close"]) * (1 + buf)
    buf2 = np.abs(np.random.normal(0, 0.005, n))
    df["low"] = np.minimum(df["open"], df["close"]) * (1 - buf2)

    # Drop rows where close is NaN or zero
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]

    return df[["open", "high", "low", "close", "volume"]]


def main():
    print("=" * 62)
    print("   FETCHING REAL NGX DATA FROM NGX PULSE API")
    print(f"   Period: {FROM_DATE} → today")
    print(f"   Tickers: {len(TICKERS)}")
    print("=" * 62)

    success = 0
    failed = []

    for ticker in TICKERS:
        print(f"\n  ▸ {ticker:12s} ...", end=" ", flush=True)
        df = fetch_ticker(ticker)

        if df is None or len(df) < 10:
            print(f"SKIPPED (insufficient data: {len(df) if df is not None else 0} rows)")
            failed.append(ticker)
            time.sleep(1)
            continue

        out_path = OUT_DIR / f"{ticker}.csv"
        df.to_csv(out_path)
        first = df.index[0].date()
        last = df.index[-1].date()
        print(f"{len(df)} days  |  {first} → {last}  |  close ₦{df['close'].iloc[-1]:,.1f}")
        success += 1
        time.sleep(6.5)  # ~9 req/min  →  stays under 10/min limit

    print("\n" + "=" * 62)
    print(f"   Done: {success}/{len(TICKERS)} tickers fetched successfully")
    if failed:
        print(f"   Failed: {', '.join(failed)}")
    print("=" * 62)


if __name__ == "__main__":
    main()

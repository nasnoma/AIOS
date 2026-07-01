import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.append(str(PROJECT_ROOT))

from trading_engine.utils.bamboo_client import bamboo_client
from trading_engine.config import settings

# Predefined realistic fallback prices in NGN (Nigerian Naira)
FALLBACK_PRICES = {
    "ARADEL": 500.0,
    "AIRTELAFRI": 2200.0,
    "BUACEMENT": 100.0,
    "BUAFOODS": 380.0,
    "CAP": 35.0,
    "DANGCEM": 500.0,
    "JAIZBANK": 2.2,
    "WAPCO": 35.0,
    "MTNN": 220.0,
    "OANDO": 75.0,
    "SEPLAT": 3400.0,
    "PRESCO": 350.0,
    "OKOMUOIL": 360.0,
    "UNILEVER": 16.0,
    "CADBURY": 16.0,
    "NASCON": 30.0,
    "FLOURMILL": 32.0,
    "NB": 28.0,
    "MEYER": 6.0,
}

TICKERS = list(FALLBACK_PRICES.keys())

def generate_history_for_ticker(ticker: str, current_price: float, limit: int = 252) -> pd.DataFrame:
    """Generates a realistic daily price history for a given ticker."""
    np.random.seed(hash(ticker) % 2**32)
    
    # 252 trading days (approx 1 year)
    end_date = datetime.now(timezone.utc)
    dates = []
    curr = end_date
    while len(dates) < limit:
        # Exclude weekends
        if curr.weekday() < 5:
            dates.append(curr)
        curr -= timedelta(days=1)
    dates.reverse()

    # Geometric random walk with daily drift/volatility
    # Volatility of ~1.5% daily
    vol = 0.015
    prices = [current_price]
    for _ in range(limit - 1):
        # Generate backward
        change = np.random.normal(0.0002, vol)
        prev_price = prices[-1] / (1.0 + change)
        prices.append(max(0.1, prev_price))
    prices.reverse()

    # Generate OHLCV
    opens = []
    highs = []
    lows = []
    volumes = []
    
    for p in prices:
        op = p * (1.0 + np.random.normal(0, 0.005))
        hi = max(op, p) * (1.0 + abs(np.random.normal(0, 0.008)))
        lo = min(op, p) * (1.0 - abs(np.random.normal(0, 0.008)))
        vol_val = float(np.random.randint(50000, 5000000))
        opens.append(op)
        highs.append(hi)
        lows.append(lo)
        volumes.append(vol_val)
        
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes
    }, index=dates)
    df.index.name = "timestamp"
    return df

def main():
    print("="*60)
    print("   GENERATING LOCAL DATABASE DUMPS FOR NIGERIAN STOCKS")
    print("="*60)
    
    out_dir = PROJECT_ROOT / "data" / "ngx"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Try to authenticate/login with Bamboo if credentials present
    bamboo_active = False
    if settings.bamboo_username and settings.bamboo_password:
        try:
            bamboo_client.get_client_token()
            bamboo_active = True
            print("Successfully authenticated with Bamboo API for current quote validation.")
        except Exception as e:
            print(f"Warning: Bamboo login failed ({e}). Using predefined fallback prices.")

    for ticker in TICKERS:
        price = FALLBACK_PRICES[ticker]
        if bamboo_active:
            try:
                stock_info = bamboo_client.get_stock(ticker)
                quote_price = float(stock_info.get("market_price") or stock_info.get("close_price") or price)
                if quote_price > 0:
                    price = quote_price
                    print(f"[{ticker}] Fetched live quote: ₦{price:,.2f}")
            except Exception as e:
                print(f"[{ticker}] Quote fetch failed ({e}). Using fallback: ₦{price:,.2f}")
        else:
            print(f"[{ticker}] Using fallback: ₦{price:,.2f}")
            
        # Generate and save CSV
        df = generate_history_for_ticker(ticker, price)
        csv_path = out_dir / f"{ticker}.csv"
        df.to_csv(csv_path)
        print(f"[{ticker}] Saved {len(df)} daily candles to {csv_path.relative_to(PROJECT_ROOT)}")

    print("\n" + "="*60)
    print("   ALL HISTORICAL DUMPS GENERATED SUCCESSFULLY!")
    print("="*60)

if __name__ == "__main__":
    main()

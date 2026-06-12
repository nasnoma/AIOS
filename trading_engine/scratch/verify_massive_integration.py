import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from unittest.mock import patch
from trading_engine.data.market_data import build_snapshot, StockDataFetcher
from trading_engine.config import settings

def test_stock_data():
    print("=== Testing Stock Data & Metrics Fetching ===")
    try:
        snap = build_snapshot("AAPL", timeframe="1d")
        print("Stock snapshot built successfully!")
        print(f"Symbol: {snap.symbol}")
        print(f"Close Price: {snap.close}")
        print(f"Market Cap: {snap.market_cap}")
        print(f"Shares Outstanding: {snap.shares_outstanding}")
        print(f"Net Income: {snap.net_income}")
        print(f"Revenue: {snap.revenue}")
    except Exception as e:
        print(f"Stock build_snapshot failed: {e}")

def test_crypto_fallback():
    print("\n=== Testing Crypto Fallback Mechanism ===")
    # Patch the exchange fetch_ohlcv to raise an Exception, forcing the fallback path
    with patch("trading_engine.data.market_data.ccxt.bybit.fetch_ohlcv") as mock_fetch:
        mock_fetch.side_effect = Exception("Simulated Exchange Outage")
        try:
            snap = build_snapshot("BTC/USDT", timeframe="1d")
            print("Crypto snapshot built successfully via fallback!")
            print(f"Symbol: {snap.symbol}")
            print(f"Close Price: {snap.close}")
            print(f"RSI: {snap.rsi}")
        except Exception as e:
            print(f"Crypto fallback failed: {e}")

if __name__ == "__main__":
    test_stock_data()
    test_crypto_fallback()

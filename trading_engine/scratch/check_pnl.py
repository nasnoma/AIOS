import json
import os
import sys

# Add parent directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from trading_engine.config import settings
from trading_engine.data.market_data import CryptoDataFetcher, StockDataFetcher

def main():
    # Load live state
    state_path = "trading_engine/live_state.json"
    if not os.path.exists(state_path):
        print(f"Error: {state_path} does not exist.")
        return

    with open(state_path) as f:
        state = json.load(f)

    # Initialize fetchers
    stock_fetcher = StockDataFetcher()
    crypto_fetcher = CryptoDataFetcher()

    prices = {}
    print("Fetching current prices...")

    for pos in state.get("positions", []):
        symbol = pos["symbol"]
        if symbol in prices:
            continue
            
        if "/" in symbol or symbol.endswith("USDT"):
            try:
                ticker = crypto_fetcher.exchange.fetch_ticker(symbol)
                prices[symbol] = float(ticker["last"])
            except Exception as e:
                print(f"Error fetching crypto price for {symbol} via fetch_ticker: {e}")
                # Fallback to fetch_ohlcv last close
                try:
                    df = crypto_fetcher.fetch_ohlcv(symbol, "1m", 5)
                    prices[symbol] = float(df["close"].iloc[-1])
                except Exception as e2:
                    print(f"Fallback failed for {symbol}: {e2}")
        else:
            try:
                price = stock_fetcher.fetch_latest_price(symbol)
                if price > 0:
                    prices[symbol] = price
                else:
                    print(f"Received 0.0 price for {symbol}, trying fallback...")
                    df = stock_fetcher.fetch_ohlcv(symbol, "1m", 5)
                    prices[symbol] = float(df["close"].iloc[-1])
            except Exception as e:
                print(f"Error fetching stock price for {symbol}: {e}")

    print("\n--- Current Prices ---")
    for symbol, price in prices.items():
        print(f"{symbol}: {price}")

    print("\n--- Unrealized P&L Report ---")
    total_unrealized_pnl = 0.0
    for i, pos in enumerate(state.get("positions", [])):
        symbol = pos["symbol"]
        entry = pos["entry_price"]
        size = pos["size_usd"]
        current = prices.get(symbol)
        
        if current is None:
            print(f"Position #{i+1}: {symbol} | Unable to fetch current price (using entry price {entry})")
            current = entry

        qty = size / entry
        current_val = qty * current
        pnl = current_val - size
        total_unrealized_pnl += pnl
        pct = (current / entry - 1) * 100
        
        print(f"Position #{i+1}: {symbol} | Entry: {entry:.4f} | Current: {current:.4f} | Size: ${size:.2f} | P&L: ${pnl:+.4f} ({pct:+.2f}%)")

    print(f"\nTotal Unrealized P&L: ${total_unrealized_pnl:+.4f}")

if __name__ == "__main__":
    main()

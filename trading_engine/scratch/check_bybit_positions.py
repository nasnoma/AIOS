import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.config import settings
import ccxt

def check_positions():
    print("Checking Bybit derivatives positions...")
    params = {
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {"defaultType": "linear"}
    }
    exchange = ccxt.bybit(params)
    exchange.enable_demo_trading(True)
    try:
        exchange.load_markets()
        positions = exchange.fetch_positions()
        open_positions = [p for p in positions if float(p.get("size", 0)) > 0]
        print(f"\nFound {len(open_positions)} active linear perpetual positions:")
        for pos in open_positions:
            print(f"- {pos['symbol']}: side={pos['side']}, size={pos['size']}, entry={pos['entryPrice']}, pnl={pos['unrealizedPnl']}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_positions()

import sys
import ccxt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from trading_engine.config import settings

def main():
    exchange = ccxt.bybit({
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {
            "defaultType": "linear",
        }
    })
    if settings.bybit_demo_trading:
        exchange.enable_demo_trading(True)
        
    print("Test 1: fetch_my_trades WITHOUT load_markets")
    try:
        trades = exchange.fetch_my_trades(symbol="NVDA/USDT:USDT", limit=5)
        print(f"Success! Found {len(trades)} trades.")
    except Exception as e:
        print(f"Failed: {e}")
        
    print("\nTest 2: fetch_my_trades WITH load_markets")
    try:
        exchange.load_markets()
        trades = exchange.fetch_my_trades(symbol="NVDA/USDT:USDT", limit=5)
        print(f"Success! Found {len(trades)} trades.")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    main()

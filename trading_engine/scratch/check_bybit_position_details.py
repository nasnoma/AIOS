import sys
import ccxt
import json
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
        print("Using Bybit Demo Trading Mode (enable_demo_trading=True).")
    else:
        print("Using Bybit Live Mode.")
        
    try:
        exchange.load_markets()
        print("Connected to Bybit.")
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    symbols = ["NVDA/USDT:USDT", "TSLA/USDT:USDT", "AAPL/USDT:USDT"]
    
    for symbol in symbols:
        print("="*80)
        print(f"SYMBOL: {symbol}")
        print("\n--- FETCHING TRADE HISTORY ---")
        try:
            trades = exchange.fetch_my_trades(symbol=symbol, limit=20)
            print(f"Found {len(trades)} trades for {symbol}:")
            for t in trades:
                print(f"ID={t['id']} | OrderID={t['order']} | Time={t['datetime']} | Side={t['side']} | Price={t['price']} | Qty={t['amount']}")
        except Exception as e:
            print(f"Error fetching trades: {e}")

        print("\n--- FETCHING POSITION INFO ---")
        try:
            positions = exchange.fetch_positions(symbols=[symbol])
            for p in positions:
                print(f"Symbol={p['symbol']} | Side={p['side']} | Size={p['contracts']} | Entry={p['entryPrice']} | Mark={p['markPrice']} | UnPnL={p['unrealizedPnl']}")
        except Exception as e:
            print(f"Error fetching positions: {e}")

if __name__ == "__main__":
    main()

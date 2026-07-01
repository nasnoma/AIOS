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
        try:
            # Fetch closed orders
            closed_orders = exchange.fetch_closed_orders(symbol=symbol, limit=20)
            print(f"Found {len(closed_orders)} closed orders:")
            for o in closed_orders:
                print(f"ID={o['id']} | Time={o['datetime']} | Side={o['side']} | Type={o['type']} | Price={o['price']} | AvgPrice={o.get('average')} | Qty={o['amount']} | Trigger={o.get('stopPrice')} | Status={o['status']}")
                # If it's a stop loss or take profit, check its stop price
                if o['info'].get('stopLoss'):
                    print(f"  StopLoss Info: {o['info'].get('stopLoss')} | TakeProfit: {o['info'].get('takeProfit')}")
        except Exception as e:
            print(f"Error fetching closed orders: {e}")

if __name__ == "__main__":
    main()

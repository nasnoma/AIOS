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

    order_ids = [
        "4a73742f-136f-4b27-83a6-877f2a84a781",
        "be07bd04-e3f5-4f1b-989f-ef8f7108b808",
        "a1afc927-6c51-419c-ba71-251dc0a47b6e",
        "421edf44-ca4a-431d-a03c-86da0ca755ad",
        "b950a758-2a66-48d4-ad94-d02996a702e7",
        "c2941331-6fa7-456b-98e5-c2f2edb3c2d6"
    ]
    
    print("\n--- FETCHING SPECIFIC ORDERS ---")
    for o_id in order_ids:
        try:
            # We try fetch_order first
            order = exchange.fetch_order(id=o_id, symbol="NVDA/USDT:USDT" if "NVDA" in o_id or o_id in ("4a73742f-136f-4b27-83a6-877f2a84a781", "be07bd04-e3f5-4f1b-989f-ef8f7108b808") else ("TSLA/USDT:USDT" if o_id in ("a1afc927-6c51-419c-ba71-251dc0a47b6e", "421edf44-ca4a-431d-a03c-86da0ca755ad") else "AAPL/USDT:USDT"))
            print(f"ID={order['id']} | Symbol={order['symbol']} | Side={order['side']} | Status={order['status']} | Price={order['price']} | AvgPrice={order.get('average')} | Qty={order['amount']} | TriggerPrice={order.get('stopPrice') or order.get('triggerPrice')}")
            print(f"  Raw Info: {json.dumps(order['info'], indent=2)}")
        except Exception as e:
            # Try fetching closed/canceled orders of that symbol to find it
            print(f"Failed to fetch order {o_id}: {e}")

if __name__ == "__main__":
    main()

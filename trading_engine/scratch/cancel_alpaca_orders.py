import sys
from pathlib import Path

# Add parent directory to sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from alpaca.trading.client import TradingClient
from trading_engine.config import settings

def main():
    client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        url_override=settings.alpaca_base_url
    )
    
    print("\n" + "=" * 80)
    print("                    ❌ CANCELLING ALL OPEN ORDERS  ❌")
    print("=" * 80)
    
    try:
        res = client.cancel_orders()
        print("Cancel requests sent. Response status:")
        for r in res:
            print(f"Order ID: {r.id} | Status: {r.status}")
    except Exception as e:
        print(f"Error cancelling orders: {e}")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()

import sys
from pathlib import Path

# Add parent directory to sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from trading_engine.config import settings

def main():
    client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        url_override=settings.alpaca_base_url
    )
    
    print("\n" + "=" * 80)
    print("                    📋 ALPACA OPEN ORDERS  📋")
    print("=" * 80)
    
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        if not orders:
            print("No open orders found.")
        else:
            for o in orders:
                print(f"ID: {o.id} | Symbol: {o.symbol} | Side: {o.side} | Qty: {o.qty} | Status: {o.status} | Type: {o.order_type}")
                # Optional: client.cancel_order_by_id(o.id)
    except Exception as e:
        print(f"Error fetching orders: {e}")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()

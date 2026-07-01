import urllib.request
import json

def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def main():
    base_url = "https://aios-trading-engine-production.up.railway.app"
    
    print("\n--- FETCHING CLOSED TRADES FROM DIAGNOSTICS ---")
    trades_data = fetch_url(f"{base_url}/api/diagnostics/trades")
    if trades_data and trades_data.get("status") == "ok":
        trades = trades_data.get("trades", [])
        print(f"Fetched {len(trades)} closed trades:")
        target_symbols = ("NVDA", "TSLA", "AAPL")
        matching_trades = [t for t in trades if any(ts in t.get("symbol", "").upper() for ts in target_symbols)]
        for t in matching_trades:
            print(f"Symbol={t['symbol']} | Dir={t['direction']} | Entry={t['entry_price']} | Exit={t['exit_price']} | Size={t['size_usd']} | PnL={t['pnl_usd']} | Fee={t['fee_usd']} | Opened={t['opened_at']} | Closed={t['closed_at']} | Reason={t['exit_reason']}")
    else:
        print("Failed to fetch trades or status not ok:", trades_data)

    print("\n--- FETCHING ORDER AUDIT LOGS FROM DIAGNOSTICS ---")
    orders_data = fetch_url(f"{base_url}/api/diagnostics/orders")
    if orders_data and orders_data.get("status") == "ok":
        orders = orders_data.get("orders", [])
        print(f"Fetched {len(orders)} order audit logs:")
        target_symbols = ("NVDA", "TSLA", "AAPL")
        matching_orders = [o for o in orders if any(ts in o.get("symbol", "").upper() for ts in target_symbols)]
        for o in matching_orders[:30]:
            print(f"Timestamp={o['timestamp']} | Symbol={o['symbol']} | Side={o['side']} | Qty={o['qty']} | Price={o['price']} | Type={o['order_type']} | Status={o['status']} | Err={o['error_message']}")
    else:
        print("Failed to fetch orders or status not ok:", orders_data)

if __name__ == "__main__":
    main()


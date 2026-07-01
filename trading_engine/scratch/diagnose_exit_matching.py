import sys
import ccxt
from datetime import datetime, timezone
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
        print("Using Bybit Demo Trading Mode.")
    else:
        print("Using Bybit Live Mode.")
        
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    # Check for NVDA/USDT:USDT
    nvda_opened_at_str = "2026-06-23T15:40:14.652849+00:00"
    nvda_opened_dt = datetime.fromisoformat(nvda_opened_at_str)
    
    print("\n--- NVDA / Bybit Trades ---")
    trades = exchange.fetch_my_trades(symbol="NVDA/USDT:USDT", limit=20)
    trades.sort(key=lambda x: x["timestamp"], reverse=True)
    
    target_side = "buy"  # NVDA was short, so target_side is buy
    
    for t in trades:
        t_time = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        print(f"Trade ID={t['id']} | Side={t['side']} | Price={t['price']} | Qty={t['amount']} | Time={t_time} | TargetSideMatch={t['side'].lower() == target_side} | TimeMatch={t_time >= nvda_opened_dt}")
        
    # Check for TSLA/USDT:USDT
    tsla_opened_at_str = "2026-06-23T15:39:52.082370+00:00"
    tsla_opened_dt = datetime.fromisoformat(tsla_opened_at_str)
    
    print("\n--- TSLA / Bybit Trades ---")
    trades = exchange.fetch_my_trades(symbol="TSLA/USDT:USDT", limit=20)
    trades.sort(key=lambda x: x["timestamp"], reverse=True)
    
    target_side = "buy"  # TSLA was short, so target_side is buy
    
    for t in trades:
        t_time = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        print(f"Trade ID={t['id']} | Side={t['side']} | Price={t['price']} | Qty={t['amount']} | Time={t_time} | TargetSideMatch={t['side'].lower() == target_side} | TimeMatch={t_time >= tsla_opened_dt}")

if __name__ == "__main__":
    main()

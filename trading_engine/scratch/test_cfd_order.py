import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.config import settings
import ccxt

def test_cfd():
    print("Testing CFD linear perpetuals client setup...")
    
    # 1. Using set_sandbox_mode(True) - as in place_bybit_linear_order
    print("\n--- 1. Testing sandbox mode (set_sandbox_mode(True)) ---")
    exchange_sb = ccxt.bybit({
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {"defaultType": "linear"}
    })
    exchange_sb.set_sandbox_mode(True)
    try:
        exchange_sb.load_markets()
        bal = exchange_sb.fetch_balance()
        print(f"🟢 Sandbox Success! Balance: {bal.get('USDT', {}).get('free', 0.0)} USDT")
    except Exception as e:
        print(f"❌ Sandbox Failed: {e}")
        
    # 2. Using enable_demo_trading(True) - as we did in get_bybit_exchange
    print("\n--- 2. Testing demo trading (enable_demo_trading(True)) ---")
    exchange_demo = ccxt.bybit({
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {"defaultType": "linear"}
    })
    # Since we want demo trading, let's enable it
    exchange_demo.enable_demo_trading(True)
    try:
        exchange_demo.load_markets()
        bal = exchange_demo.fetch_balance()
        print(f"🟢 Demo Success! Balance: {bal.get('USDT', {}).get('free', 0.0)} USDT")
    except Exception as e:
        print(f"❌ Demo Failed: {e}")

if __name__ == "__main__":
    test_cfd()

import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.config import settings
import ccxt

def check_bybit():
    print("Checking Bybit connection...")
    print(f"API Key: {settings.bybit_api_key}")
    print(f"Testnet Setting: {settings.crypto_testnet}")
    print(f"Demo Trading Setting: {settings.bybit_demo_trading}")
    
    params = {
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "options": {
            "defaultType": "spot",
        }
    }
    
    # Check Spot Mainnet vs Testnet vs Demo
    for demo in [True, False]:
        for testnet in [True, False]:
            print(f"\nTrying combination: demo={demo}, testnet={testnet}...")
            exchange = ccxt.bybit(params)
            if testnet:
                if demo:
                    exchange.enable_demo_trading(True)
                else:
                    exchange.set_sandbox_mode(True)
            try:
                balance = exchange.fetch_balance()
                print(f"🟢 SUCCESS! Balance: {balance.get('USDT', {}).get('free', 0.0)} USDT free")
                # print some exchange properties
                print(f"Exchange URLs: {exchange.urls}")
            except Exception as e:
                print(f"❌ Failed: {e}")

if __name__ == "__main__":
    check_bybit()

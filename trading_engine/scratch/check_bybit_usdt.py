import sys
import os
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.config import settings
import ccxt

def main():
    print("Checking Bybit live balance...")
    params = {
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
    }
    
    exchange = ccxt.bybit(params)
    if settings.crypto_testnet:
        exchange.set_sandbox_mode(True)
        
    try:
        balance = exchange.fetch_balance()
        print("\nUSDT balance structure:")
        print(balance.get('USDT'))
        print("\nUSDC balance structure:")
        print(balance.get('USDC'))
        print("\nFull balance keys:")
        print(list(balance.get('total', {}).keys()))
    except Exception as e:
        print(f"Failed to fetch: {e}")

if __name__ == "__main__":
    main()

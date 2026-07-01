import sys
import ccxt
from datetime import datetime, timezone
sys.path.insert(0, "/Users/nasir.noma/claude_projects/AIOS")
from trading_engine.config import settings

def main():
    print("Connecting to Bybit...")
    params = {
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "enableRateLimit": True,
    }
    exchange = ccxt.bybit(params)
    if settings.crypto_testnet:
        if settings.bybit_demo_trading:
            print("Enabling Demo Trading mode...")
            exchange.enable_demo_trading(True)
        else:
            print("Setting Sandbox mode...")
            exchange.set_sandbox_mode(True)
            
    symbols = ["UNI/USDT", "GRASS/USDT", "AVAX/USDT", "AAVE/USDT", "POPCAT/USDT", "JTO/USDT"]
    print("\nRecent broker-side fills (last 10 trades per symbol):")
    for symbol in symbols:
        try:
            trades = exchange.fetch_my_trades(symbol=symbol, limit=10)
            if trades:
                print(f"\n--- {symbol} ---")
                trades.sort(key=lambda x: x["timestamp"], reverse=True)
                for t in trades:
                    t_time = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                    fee_info = ""
                    if t.get("fee"):
                        fee_cost = t["fee"].get("cost", 0)
                        fee_curr = t["fee"].get("currency", "")
                        fee_info = f" | Fee: {fee_cost} {fee_curr}"
                    print(f"  [{t_time}] {t['side'].upper()} {t['amount']} @ {t['price']}{fee_info}")
            else:
                print(f"\nNo trades found for {symbol}")
        except Exception as e:
            print(f"\nError fetching trades for {symbol}: {e}")

if __name__ == "__main__":
    main()

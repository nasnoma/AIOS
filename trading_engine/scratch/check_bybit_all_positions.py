import sys
import ccxt
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
        positions = exchange.fetch_positions()
        print(f"Total positions returned by fetch_positions(): {len(positions)}")
        
        # Filter for non-zero or target symbols
        for p in positions:
            size = float(p.get('contracts') or p.get('size') or (p.get('info') and p['info'].get('size')) or 0)
            sym = p.get('symbol')
            if size > 0 or any(t in sym for t in ("NVDA", "TSLA", "AAPL")):
                print(f"Symbol={p['symbol']} | Side={p['side']} | Size={size} | Contracts={p.get('contracts')} | InfoSize={p.get('info', {}).get('size') if p.get('info') else None}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()

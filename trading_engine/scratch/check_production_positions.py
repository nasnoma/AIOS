import requests
import json
import ccxt
from datetime import datetime, timezone

def main():
    status_url = "https://aios-trading-engine-production.up.railway.app/api/status"
    try:
        resp = requests.get(status_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Error fetching status from Railway: {e}")
        return

    positions = data.get("trades", [])
    open_positions = [p for p in positions if p.get("status") == "open" or p.get("closed_at") is None]
    
    if not open_positions:
        print("No open positions found in the production dashboard.")
        return

    print(f"Found {len(open_positions)} open positions on Railway. Fetching current prices...")
    
    # Initialize exchange
    exchange = ccxt.bybit()
    
    # Fetch ticker for each unique symbol
    symbols = list(set([p["symbol"] for p in open_positions]))
    prices = {}
    for sym in symbols:
        try:
            # check if it's a Bybit CFD or plain crypto ticker
            ticker_symbol = sym
            if ":" in sym:
                # E.g. AAPL/USDT:USDT -> AAPLUSDT
                ticker_symbol = sym.replace("/", "").split(":")[0]
            
            ticker = exchange.fetch_ticker(ticker_symbol)
            prices[sym] = float(ticker["last"])
            print(f"Fetched price for {sym}: {prices[sym]}")
        except Exception as e:
            print(f"Error fetching ticker for {sym}: {e}")
            # Try a simple fallback or mock if needed
            prices[sym] = None

    print("\n" + "="*80)
    print(f"{'SYMBOL':<15} | {'DIRECTION':<9} | {'ENTRY':<10} | {'CURRENT':<10} | {'SL':<10} | {'TP':<10} | {'STATUS / ACTION':<15}")
    print("="*80)
    
    close_candidates = 0
    for pos in open_positions:
        sym = pos["symbol"]
        direction = pos["direction"]
        entry = pos["entry_price"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]
        current = prices.get(sym)
        
        if current is None:
            action = "No Price Data"
        else:
            pnl_pct = (current - entry) / entry * 100 if direction == "long" else (entry - current) / entry * 100
            
            # Check if SL or TP is hit
            if direction == "long":
                if current <= sl:
                    action = f"❌ HIT SL (-{abs(pnl_pct):.1f}%)"
                    close_candidates += 1
                elif current >= tp:
                    action = f"✅ HIT TP (+{pnl_pct:.1f}%)"
                    close_candidates += 1
                else:
                    action = f"Hold ({pnl_pct:+.1f}%)"
            else: # short
                if current >= sl:
                    action = f"❌ HIT SL (-{abs(pnl_pct):.1f}%)"
                    close_candidates += 1
                elif current <= tp:
                    action = f"✅ HIT TP (+{pnl_pct:.1f}%)"
                    close_candidates += 1
                else:
                    action = f"Hold ({pnl_pct:+.1f}%)"
                    
        print(f"{sym:<15} | {direction:<9} | {entry:<10.4f} | {current:<10.4f} | {sl:<10.4f} | {tp:<10.4f} | {action}")

    print("="*80)
    print(f"Total positions that should be closed: {close_candidates}")

if __name__ == "__main__":
    main()

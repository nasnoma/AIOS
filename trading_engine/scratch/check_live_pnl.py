import os
import sys
import json
from pathlib import Path
from loguru import logger

# Add parent directory to sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

import ccxt
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from trading_engine.config import settings

def main():
    state_file = Path(__file__).resolve().parents[1] / "live_state.json"
    if not state_file.exists():
        print("No live state file found.")
        return

    with open(state_file) as f:
        state = json.load(f)

    positions = state.get("positions", [])
    if not positions:
        print("No open positions found.")
        return

    print("\n" + "=" * 80)
    print("                    📊 LIVE POSITIONS MONITOR  📊")
    print("=" * 80)

    # 1. Initialize Clients
    bybit = ccxt.bybit({'options': {'defaultType': 'spot'}})
    alpaca = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

    # 2. Group and Fetch Latest Prices
    current_prices = {}
    
    # Fetch Bybit Crypto Prices
    crypto_symbols = list(set([p["symbol"] for p in positions if "/" in p["symbol"]]))
    for sym in crypto_symbols:
        try:
            ticker = bybit.fetch_ticker(sym)
            current_prices[sym] = ticker.get("last") or ticker.get("close")
        except Exception as e:
            logger.warning(f"Failed to fetch CCXT price for {sym}: {e}")

    # Fetch Alpaca Stock Prices
    stock_symbols = list(set([p["symbol"] for p in positions if "/" not in p["symbol"]]))
    if stock_symbols:
        try:
            res = alpaca.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=stock_symbols))
            for sym in stock_symbols:
                if sym in res:
                    current_prices[sym] = float(res[sym].price)
        except Exception as e:
            logger.warning(f"Failed to fetch Alpaca price for {stock_symbols}: {e}")

    # 3. Calculate and display P&L
    total_unrealized_pnl = 0.0
    for p in positions:
        sym = p["symbol"]
        direction = p["direction"]
        entry = p["entry_price"]
        size_usd = p["size_usd"]
        
        current_price = current_prices.get(sym)
        if current_price is None:
            print(f"❌ {sym} — Current price could not be retrieved.")
            continue

        # For demo stocks, size_usd is $15, but actual shares was 1, so the actual exposure is entry * qty
        # Let's handle the actual exposure (since for non-fractionable stocks we buy 1 share)
        is_crypto = "/" in sym
        if not is_crypto and sym == "SMCL":
            # SMCL was purchased as 1 share, so entry was 374.80 and exposure is 374.80
            exposure = entry  # 1 share
            pnl_pct = (current_price - entry) / entry
            pnl_usd = pnl_pct * exposure
        elif not is_crypto and sym == "AAPL":
            # AAPL is fractionable, so size_usd is $15
            qty = size_usd / entry
            pnl_usd = (current_price - entry) * qty
            pnl_pct = (current_price - entry) / entry
        else:
            # Crypto
            qty = size_usd / entry
            pnl_usd = (current_price - entry) * qty
            pnl_pct = (current_price - entry) / entry

        total_unrealized_pnl += pnl_usd
        emoji = "🟢" if pnl_usd >= 0 else "🔴"
        
        print(f"\n{emoji} {sym} ({direction.upper()})")
        print(f"   Entry: ${entry:,.4f} | Current: ${current_price:,.4f}")
        print(f"   Size: ${size_usd:,.2f} | P&L: ${pnl_usd:+,.2f} ({pnl_pct:+.2%})")
        print(f"   Stop Loss: ${p["stop_loss"]:,.4f} | Take Profit: ${p["take_profit"]:,.4f}")

    print("\n" + "=" * 80)
    print(f"💵 Total Unrealized P&L: ${total_unrealized_pnl:+,.2f}")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()

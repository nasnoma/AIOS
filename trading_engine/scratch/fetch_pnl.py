import requests
import ccxt
import sys
from pathlib import Path

def get_live_prices(symbols):
    prices = {}
    
    # 1. Try Bybit Spot
    bybit = ccxt.bybit({'options': {'defaultType': 'spot'}})
    for symbol in symbols:
        try:
            ticker = bybit.fetch_ticker(symbol)
            prices[symbol] = ticker.get('last') or ticker.get('close')
        except Exception as e:
            # Try Bybit linear perp if spot fails
            try:
                bybit_perp = ccxt.bybit({'options': {'defaultType': 'linear'}})
                ticker = bybit_perp.fetch_ticker(symbol)
                prices[symbol] = ticker.get('last') or ticker.get('close')
            except Exception as e2:
                # Try Yahoo Finance as a backup
                try:
                    import yfinance as yf
                    yf_sym = symbol.replace("/USDT", "-USD")
                    ticker = yf.Ticker(yf_sym)
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        prices[symbol] = float(hist["Close"].iloc[-1])
                except Exception:
                    pass
    return prices

def main():
    status_url = "https://aios-trading-engine-production.up.railway.app/api/status"
    try:
        resp = requests.get(status_url, timeout=10)
        resp.raise_for_status()
        status_data = resp.json()
    except Exception as e:
        print(f"Error fetching status from {status_url}: {e}")
        return

    trades = status_data.get("trades", [])
    open_trades = [t for t in trades if t.get("status") == "open"]
    
    if not open_trades:
        print("No open positions found in the status response.")
        return

    # Extract unique symbols
    symbols = list(set([t["symbol"] for t in open_trades]))
    
    # Fetch current prices
    print(f"Fetching current prices for: {', '.join(symbols)}...")
    prices = get_live_prices(symbols)
    
    # Calculate P&L
    total_entry_val = 0.0
    total_current_val = 0.0
    total_unrealized_pnl = 0.0

    header_fmt = "| {:<2} | {:<10} | {:<4} | {:<12} | {:<12} | {:<10} | {:<20} | {:<12} | {:<12} |"
    row_fmt = "| {:<2d} | {:<10} | {:<4} | {:<12} | {:<12} | {:<10} | {:<20} | {:<12} | {:<12} |"
    
    print("\n" + "=" * 115)
    print("                                      📊 LIVE UNREALIZED P&L REPORT 📊")
    print("=" * 115)
    print(header_fmt.format("#", "Symbol", "Dir", "Entry", "Current", "Size USD", "Unrealized P&L", "SL", "TP"))
    print("-" * 115)

    for idx, pos in enumerate(open_trades, 1):
        sym = pos["symbol"]
        direction = pos["direction"]
        entry = pos["entry_price"]
        size_usd = pos["size_usd"]
        stop_loss = pos["stop_loss"]
        take_profit = pos["take_profit"]
        opened_at = pos["opened_at"]
        
        current_price = prices.get(sym)
        if current_price is None:
            # Fallback for display if we can't fetch it
            current_price_str = "N/A"
            pnl_usd = 0.0
            pnl_pct = 0.0
            pnl_str = "N/A"
        else:
            current_price_str = f"${current_price:,.6f}" if current_price < 1.0 else f"${current_price:,.4f}"
            qty = size_usd / entry
            if direction == "long":
                pnl_usd = (current_price - entry) * qty
                pnl_pct = (current_price - entry) / entry * 100
            else:
                pnl_usd = (entry - current_price) * qty
                pnl_pct = (entry - current_price) / entry * 100
            
            pnl_str = f"${pnl_usd:+,.2f} ({pnl_pct:+.2f}%)"
            total_unrealized_pnl += pnl_usd
            total_entry_val += size_usd
            total_current_val += (size_usd + pnl_usd)

        entry_str = f"${entry:,.6f}" if entry < 1.0 else f"${entry:,.4f}"
        sl_str = f"${stop_loss:,.4f}" if stop_loss >= 1.0 else f"${stop_loss:,.6f}"
        tp_str = f"${take_profit:,.4f}" if take_profit >= 1.0 else f"${take_profit:,.6f}"
        
        print(row_fmt.format(
            idx,
            sym,
            direction.upper()[:4],
            entry_str,
            current_price_str,
            f"${size_usd:,.2f}",
            pnl_str,
            sl_str,
            tp_str
        ))

    print("=" * 115)
    print(f"Total Invested Size: ${total_entry_val:,.2f}")
    print(f"Total Current Value: ${total_current_val:,.2f}")
    print(f"Total Unrealized P&L: ${total_unrealized_pnl:+,.2f} ({total_unrealized_pnl/total_entry_val*100:+.2f}% of exposure)" if total_entry_val > 0 else "")
    print("=" * 115 + "\n")

if __name__ == "__main__":
    main()

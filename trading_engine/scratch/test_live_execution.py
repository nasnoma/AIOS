import sys
import os
from loguru import logger

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from trading_engine.execution import live_trader
from trading_engine.config import settings

def run_test():
    print("=" * 60)
    print("         LIVE EXECUTION INTEGRATION TEST (SPOT TRADING)")
    print("=" * 60)
    
    # Check live state file starting point
    print(f"Live State File Location: {live_trader.STATE_FILE}")
    if live_trader.STATE_FILE.exists():
        try:
            live_trader.STATE_FILE.unlink()
            print("Cleaned up existing live state file for a fresh test.")
        except Exception:
            pass
            
    print("\n--- 1. Testing Bybit Spot Demo Execution ---")
    try:
        # We will attempt to open a small $5 BTC/USDT trade on Bybit demo
        print("Opening $5 BTC/USDT trade on Bybit demo...")
        pos_crypto = live_trader.open_trade(
            symbol="BTC/USDT",
            direction="long",
            entry=68000.0,
            size_usd=5.0,
            stop_loss=60000.0,
            take_profit=80000.0
        )
        if pos_crypto:
            print("Successfully opened BTC/USDT position on Bybit!")
            print(f"Position Details: {pos_crypto}")
            
            # Immediately close the position to exit the market
            print("\nClosing BTC/USDT position on Bybit...")
            portfolio = live_trader._load_state()
            pos = next((p for p in portfolio.positions if p.symbol == "BTC/USDT" and p.status == "open"), None)
            if pos:
                live_trader._close_position(portfolio, pos, pos.entry_price, "closed")
                live_trader._save_state(portfolio)
                print("Successfully closed BTC/USDT position!")
        else:
            print("Failed to open BTC/USDT position.")
    except Exception as e:
        print(f"Error testing Bybit: {e}")
        
    print("\n--- 2. Testing Alpaca Stock Paper Execution ---")
    try:
        # We will attempt to open a small AAPL trade on Alpaca paper
        # AAPL price is around $170, so let's buy $15 worth of AAPL
        print("Opening $15 AAPL trade on Alpaca paper...")
        pos_stock = live_trader.open_trade(
            symbol="AAPL",
            direction="long",
            entry=175.0,
            size_usd=15.0,
            stop_loss=150.0,
            take_profit=200.0
        )
        if pos_stock:
            print("Successfully opened AAPL position on Alpaca!")
            print(f"Position Details: {pos_stock}")
            
            # Immediately close the position
            print("\nClosing AAPL position on Alpaca...")
            portfolio = live_trader._load_state()
            pos = next((p for p in portfolio.positions if p.symbol == "AAPL" and p.status == "open"), None)
            if pos:
                live_trader._close_position(portfolio, pos, pos.entry_price, "closed")
                live_trader._save_state(portfolio)
                print("Successfully closed AAPL position!")
        else:
            print("Failed to open AAPL position.")
    except Exception as e:
        print(f"Error testing Alpaca: {e}")

    # Print final status
    status = live_trader.get_status()
    print("\n" + "=" * 60)
    print("                  TEST RUN SUMMARY")
    print("=" * 60)
    print(f"Account Size:  ${status['account_size']:,.2f}")
    print(f"Cash Balance:  ${status['cash']:,.2f}")
    print(f"Total PnL:     ${status['total_pnl']:+,.2f}")
    print(f"Total Fees:    ${status['total_fees']:,.4f}")
    print(f"Closed Trades: {len(status['trades'])}")
    for t in status["trades"]:
        print(f" - {t['symbol']}: Entry=${t['entry_price']:.2f}, Exit=${t['exit_price']:.2f}, PnL=${t['pnl_usd']:.2f}")

if __name__ == "__main__":
    # Force settings to live mode for the test
    settings.trading_mode = "live"
    run_test()

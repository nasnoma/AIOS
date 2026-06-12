#!/usr/bin/env python
"""
run_multi_year_backtest.py
Runs the vectorized backtester over 3 years (1095 days) of historical data
for BTC/USDT and SOL/USDT to verify configuration parameter robustness (atr_multiplier=2.2).
"""
import sys
import os
from loguru import logger

# Add project root to python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from trading_engine.backtest.engine import run_backtest
from trading_engine.config import settings

def main():
    print("=" * 70)
    print("        RUNNING MULTI-YEAR BACKTEST (3 YEARS / 1095 DAYS)")
    print("=" * 70)
    
    # Configure backtest settings
    settings.crypto_testnet = False
    settings.llm_provider = "mock"  # Ensure no real LLM calls
    
    symbols = ["BTC/USDT", "SOL/USDT"]
    days = 1095  # 3 Years
    timeframe = "4h"
    
    for symbol in symbols:
        print(f"\n--- Backtesting {symbol} ({days} days on {timeframe} timeframe) ---")
        try:
            results = run_backtest(
                symbol=symbol,
                timeframe=timeframe,
                days=days,
                initial_capital=10000.0,
                use_multi_agent=True
            )
            
            print(f"Results for {symbol}:")
            print(f"  Total Trades:     {results.get('total_trades')}")
            print(f"  Win Rate:         {results.get('win_rate')}%")
            print(f"  Profit Factor:    {results.get('profit_factor')}")
            print(f"  Total Return:     {results.get('total_return_pct')}%")
            print(f"  Max Drawdown:     {results.get('max_drawdown_pct')}%")
            print(f"  Initial Capital:  ${results.get('initial_capital'):,.2f}")
            print(f"  Final Capital:    ${results.get('final_capital'):,.2f}")
            
            # Check if drawdown exceeded a critical threshold
            max_dd = results.get('max_drawdown_pct', 0.0)
            if max_dd > 35.0:
                print(f"⚠️ WARNING: {symbol} experienced high max drawdown ({max_dd}%).")
            else:
                print(f"✅ Robustness check passed: max drawdown is safe ({max_dd}%).")
                
        except Exception as e:
            print(f"❌ Failed to run backtest for {symbol}: {e}")
            
    print("\n" + "=" * 70)
    print("                 MULTI-YEAR BACKTEST COMPLETE")
    print("=" * 70)

if __name__ == "__main__":
    main()

import sys
import json
from pathlib import Path

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.backtest.engine import run_walk_forward_optimization

top_3 = ["DANGCEM/NGX", "SEPLAT/NGX", "BUAFOODS/NGX"]
results = {}

print("=== Running Single-Stock WFO on Top 3 NGX Tickers ===")
for symbol in top_3:
    print(f"\nRunning WFO for {symbol}...")
    try:
        # Run WFO with standard 90 days lookback and 45 days forward windows
        res = run_walk_forward_optimization(
            symbol=symbol,
            timeframe="1d",
            days=400,
            lookback_days=90,
            forward_days=45,
            initial_capital=10000
        )
        results[symbol] = res
        print(f"WFO Results for {symbol}:")
        if "error" in res:
            print(f"  Error: {res['error']}")
        else:
            print(f"  OOS Return: {res.get('total_return_pct'):+.2f}%")
            print(f"  Total Trades: {res.get('total_trades')}")
            print(f"  Win Rate: {res.get('win_rate')}%")
            print(f"  Profit Factor: {res.get('profit_factor')}")
            print(f"  Baseline Return: {res.get('baseline_return_pct'):+.2f}%")
            print(f"  WFO Edge vs Baseline: {res.get('wfo_edge_pct'):+.2f}%")
    except Exception as e:
        print(f"  Exception: {e}")

# Save results
output_file = PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx" / "top3_wfo_results.json"
output_file.parent.mkdir(parents=True, exist_ok=True)
with open(output_file, "w") as f:
    json.dump(results, f, indent=2, default=str)

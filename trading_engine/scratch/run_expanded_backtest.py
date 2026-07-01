import sys
import json
from pathlib import Path

# Add project root to python path
PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

# Setup dummy loguru logger if needed or just use trading_engine's
from loguru import logger
from trading_engine.backtest.engine import run_ngx_native_backtest, _ngx_composite_score

# Load expanded tickers
ngx_dir = PROJECT_ROOT / "data" / "ngx"
with open(ngx_dir / "expanded_tickers.json", "r") as f:
    tickers = json.load(f)

print(f"Running backtest on {len(tickers)} expanded tickers...")
results = []
failed = []

for ticker in tickers:
    symbol = f"{ticker}/NGX"
    try:
        res = run_ngx_native_backtest(
            symbol=symbol,
            days=400,
            initial_capital=10000,
            strategy="auto",
            output_dir=str(PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx")
        )
        if "error" in res:
            print(f"  {ticker:15s}: FAILED - {res['error']}")
            failed.append((ticker, res['error']))
        else:
            strategy_used = res.get("strategy_used")
            score = _ngx_composite_score(res)
            results.append({
                "ticker": ticker,
                "strategy": strategy_used,
                "result": {
                    "total_trades": res.get("total_trades", 0),
                    "win_rate": res.get("win_rate", 0.0),
                    "profit_factor": res.get("profit_factor", 0.0),
                    "total_return_pct": res.get("total_return_pct", 0.0),
                    "max_drawdown_pct": res.get("max_drawdown_pct", 0.0),
                },
                "score": score
            })
            print(f"  {ticker:15s}: [{strategy_used:12s}] trades={res.get('total_trades'):3d} WR={res.get('win_rate'):5.1f}% ret={res.get('total_return_pct'):+7.2f}% PF={res.get('profit_factor'):5.2f}")
    except Exception as e:
        print(f"  {ticker:15s}: EXCEPTION - {e}")
        failed.append((ticker, str(e)))

# Sort by score descending
results.sort(key=lambda x: x["score"], reverse=True)

# Write results
output_file = PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx" / "expanded_universe_results.json"
output_file.parent.mkdir(parents=True, exist_ok=True)
with open(output_file, "w") as f:
    json.dump({"results": results, "failed": failed}, f, indent=2)

print("\n" + "=" * 70)
print("TOP 10 NEW STOCKS:")
for idx, r in enumerate(results[:10]):
    br = r["result"]
    print(f"  #{idx+1} {r['ticker']:15s} [{r['strategy']:12s}] ret={br['total_return_pct']:+.2f}% WR={br['win_rate']:.1f}% PF={br['profit_factor']:.2f} score={r['score']:.4f}")

print("\nFAILED/INSUFFICIENT DATA:")
for tkr, err in failed:
    print(f"  {tkr:15s}: {err}")

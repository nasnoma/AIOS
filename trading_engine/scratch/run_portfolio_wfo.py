import sys
import json
from pathlib import Path

# Add project root to python path
PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.backtest.engine import run_ngx_portfolio_wfo

# 1. Run on original 19 stocks
print("=== Running Portfolio WFO on Original 19 Stocks ===")
res_orig = run_ngx_portfolio_wfo(
    days=400,
    lookback_days=90,
    forward_days=45,
    output_dir=str(PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx")
)

# 2. Run on all expanded tickers (including original 19)
print("\n=== Running Portfolio WFO on Combined Universe (Original + Expanded) ===")
# Load expanded tickers
with open(PROJECT_ROOT / "data" / "ngx" / "expanded_tickers.json", "r") as f:
    expanded = json.load(f)

# Original 19 list
original_19 = [
    "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
    "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
    "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
    "NASCON", "FLOURMILL", "NB", "MEYER"
]

all_tickers = list(set(original_19 + expanded))
print(f"Total tickers in pool: {len(all_tickers)}")

res_all = run_ngx_portfolio_wfo(
    tickers=all_tickers,
    days=400,
    lookback_days=90,
    forward_days=45,
    output_dir=str(PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx")
)

# Save combined result separately
with open(PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx" / "ngx_portfolio_wfo_combined.json", "w") as f:
    json.dump(res_all, f, indent=2, default=str)

import sys
import json
from pathlib import Path

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.backtest.engine import run_ngx_dynamic_portfolio_wfo

# Run dynamic portfolio WFO with:
# - Concentrated universe: Top 10 new + original 19 (29 tickers total)
# - Position size: 15% of current portfolio capital per trade
# - Stop loss: 8% (Max risk per trade is 1.2% of total portfolio value)
# - Take profit: 20%
print("=== Running Dynamic Cash Sharing Portfolio WFO ===")
res = run_ngx_dynamic_portfolio_wfo(
    days=400,
    lookback_days=90,
    forward_days=45,
    initial_capital=10_000,
    stop_loss_pct=0.08,
    take_profit_pct=0.20,
    position_fraction=0.15,
    output_dir=str(PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx")
)

print("\n=== Dynamic Cash Sharing WFO Results ===")
if "error" in res:
    print(f"Error: {res['error']}")
else:
    print(f"Total Tickers Loaded & Tested: {res.get('tickers_tested')}")
    print(f"OOS Total Return: {res.get('total_return_pct'):+.2f}%")
    print(f"Final Capital: ₦{res.get('final_capital'):,.2f}")
    print(f"Total Trades Taken: {res.get('total_trades')}")
    print(f"Win Rate: {res.get('win_rate')}% ({res.get('wins')} Wins, {res.get('losses')} Losses)")
    print(f"Profit Factor: {res.get('profit_factor')}")
    print(f"Total Net PnL: ₦{res.get('total_pnl'):,.2f}")
    
    print("\nPer-Ticker Performance (Top 10 by PnL):")
    per_ticker = res.get("per_ticker", {})
    sorted_tickers = sorted(per_ticker.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for ticker, stats in sorted_tickers[:10]:
        print(f"  {ticker:15s}: trades={stats['trades']:3d} | wins={stats['wins']:2d} | Net PnL=₦{stats['pnl']:+8.2f}")

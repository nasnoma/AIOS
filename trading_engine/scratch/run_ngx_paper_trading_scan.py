import sys
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.config import settings
from trading_engine.orchestrator import run as run_pipeline, _get_daily_pnl
from trading_engine.execution import paper_trader
from trading_engine.market_hours import classify_symbol

def run_ngx_paper_scan():
    logger.info("🇳🇬 Starting Manual NGX Paper Trading Run for Top 10 Ranked Stocks...")
    
    # Verify watchlist
    ngx_watchlist = settings.ngx_assets
    logger.info(f"Top 10 Watchlist: {ngx_watchlist}")
    
    # Load paper portfolio state
    portfolio = paper_trader._load_state()
    logger.info(f"Current Account size: ₦{portfolio.account_size:,.2f}")
    logger.info(f"Current Cash balance: ₦{portfolio.cash:,.2f}")
    logger.info(f"Open Positions: {[p.symbol for p in portfolio.open_positions]}")
    
    # Calculate daily realized PnL
    daily_pnl = _get_daily_pnl(paper_trader)
    
    results = []
    
    for symbol in ngx_watchlist:
        logger.info(f"\nProcessing {symbol}...")
        try:
            # Prevent duplicate concurrent positions
            if any(p.symbol == symbol for p in portfolio.open_positions):
                logger.info(f"⏭️ Skipping {symbol}: position already open.")
                continue
                
            # Run pipeline (bypassing market open hours filter to allow immediate dry run/paper trade execution)
            sig = run_pipeline(
                symbol=symbol,
                timeframe="1d",
                portfolio_heat=portfolio.portfolio_heat,
                open_positions=len(portfolio.open_positions),
                win_rate=portfolio.win_rate,
                open_position_snaps={},
                daily_pnl_usd=daily_pnl
            )
            
            results.append(sig)
            
            # Execute approved trades
            if sig.final_action == "BUY":
                size_needed = sig.position_size_usd or 0.0
                if portfolio.cash < size_needed:
                    logger.warning(f"❌ blocked: Insufficient cash (cash=₦{portfolio.cash:,.2f}, needed=₦{size_needed:,.2f})")
                    continue
                    
                logger.info(f"🚀 Execution triggered for {symbol}: BUY size=₦{size_needed:,.2f} entry=₦{sig.entry_price:.2f}")
                pos = paper_trader.open_trade(
                    symbol=sig.symbol,
                    direction="long",
                    entry=sig.entry_price,
                    size_usd=size_needed,
                    stop_loss=sig.stop_loss or 0.0,
                    take_profit=sig.take_profit or 0.0,
                    atr=float((sig.risk or {}).get("atr", 0.0))
                )
                if pos:
                    # Reload portfolio to reflect new position
                    portfolio = paper_trader._load_state()
            else:
                logger.info(f"No trade executed for {symbol}. Action: {sig.final_action}")
        except Exception as e:
            logger.error(f"Pipeline failed for {symbol}: {e}")
            
    # Post run summary
    portfolio = paper_trader._load_state()
    logger.info("\n============================================================")
    logger.info("🇳🇬 NGX PAPER TRADING CYCLE COMPLETED")
    logger.info("============================================================")
    logger.info(f"Remaining Cash: ₦{portfolio.cash:,.2f}")
    logger.info(f"Open Positions count: {len(portfolio.open_positions)}")
    for p in portfolio.open_positions:
        logger.info(f"  - {p.symbol}: entry=₦{p.entry_price:,.2f} | size=₦{p.size_usd:,.2f} | stop=₦{p.stop_loss:,.2f} | tp=₦{p.take_profit:,.2f}")

if __name__ == "__main__":
    run_ngx_paper_scan()

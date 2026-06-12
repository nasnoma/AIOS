"""
trading_engine/scheduler.py

APScheduler-based runner.
Runs the full pipeline on configured assets at regular intervals.
Also handles position monitoring between signal cycles.
"""
from __future__ import annotations
import signal as os_signal
import sys
from datetime import datetime
from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from trading_engine.config import settings
from trading_engine.orchestrator import run_all_assets
from trading_engine.execution import paper_trader, live_trader
from trading_engine.alerts.telegram_bot import send_signal_alert


scheduler = BlockingScheduler(timezone="UTC")


def run_signal_cycle():
    """Main cycle: analyze all assets and execute if signal found."""
    logger.info(f"\n🔄 Signal cycle started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    if settings.trading_mode == "live":
        trader = live_trader
    else:
        trader = paper_trader

    # Get portfolio state for context
    status = trader.get_status()
    portfolio_heat = status["portfolio_heat"] / 100
    open_positions = status["open_positions"]
    win_rate = status.get("win_rate", 50) / 100

    # Run full analysis
    signals = run_all_assets()

    for sig in signals:
        if sig.final_action in ("BUY", "SELL") and settings.trading_mode != "signal_only":
            direction = "long" if sig.final_action == "BUY" else "short"
            trader.open_trade(
                symbol=sig.symbol,
                direction=direction,
                entry=sig.entry_price,
                size_usd=sig.position_size_usd or 0,
                stop_loss=sig.stop_loss or 0,
                take_profit=sig.take_profit or 0,
            )

        # Send Telegram alert for any actionable signal
        if sig.final_action in ("BUY", "SELL"):
            try:
                send_signal_alert(sig)
            except Exception as e:
                logger.warning(f"Telegram alert failed: {e}")

    logger.info(f"✅ Cycle complete. {sum(1 for s in signals if s.final_action != 'NO_TRADE')} actionable signals.")


def monitor_positions():
    """Check open positions against current prices every 15 minutes."""
    if settings.trading_mode == "live":
        trader = live_trader
    else:
        trader = paper_trader

    logger.info("🔍 Monitoring open positions...")
    portfolio = trader._load_state()
    open_positions = portfolio.open_positions
    if not open_positions:
        return

    # Fetch current prices for all open symbols
    from trading_engine.data.market_data import CryptoDataFetcher, StockDataFetcher
    import ccxt

    current_prices = {}
    
    # 1. Fetch crypto prices via exchange
    crypto_symbols = [pos.symbol for pos in open_positions if "/" in pos.symbol or pos.symbol.endswith("USDT") or pos.symbol.endswith("USD")]
    if crypto_symbols:
        try:
            exchange_class = getattr(ccxt, settings.crypto_exchange)
            exchange = exchange_class()
            for asset in crypto_symbols:
                try:
                    ticker = exchange.fetch_ticker(asset)
                    current_prices[asset] = ticker["last"]
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Price monitor fetch error for crypto: {e}")

    # 2. Fetch stock prices via Massive
    stock_symbols = [pos.symbol for pos in open_positions if pos.symbol not in current_prices]
    if stock_symbols and settings.get_massive_api_key:
        try:
            fetcher = StockDataFetcher()
            for asset in stock_symbols:
                try:
                    price = fetcher.fetch_latest_price(asset)
                    if price > 0:
                        current_prices[asset] = price
                except Exception as e:
                    logger.warning(f"Price monitor fetch error for stock {asset}: {e}")
        except Exception as e:
            logger.warning(f"Price monitor fetch error for stocks: {e}")

    if current_prices:
        trader.update_prices(current_prices)


def shutdown(signum, frame):
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()
    sys.exit(0)


def main():
    os_signal.signal(os_signal.SIGINT, shutdown)
    os_signal.signal(os_signal.SIGTERM, shutdown)

    interval = settings.signal_interval_minutes
    logger.info(f"🚀 Trading Engine Scheduler starting")
    logger.info(f"   Mode: {settings.trading_mode.upper()}")
    logger.info(f"   Assets: {settings.default_assets}")
    logger.info(f"   Interval: every {interval} minutes")
    logger.info(f"   Timeframe: {settings.timeframe}")

    # Run once immediately
    run_signal_cycle()

    # Schedule recurring runs
    scheduler.add_job(
        run_signal_cycle,
        trigger=IntervalTrigger(minutes=interval),
        id="signal_cycle",
        name="Trading Signal Cycle",
    )

    # Position monitor every 15 minutes
    scheduler.add_job(
        monitor_positions,
        trigger=IntervalTrigger(minutes=15),
        id="position_monitor",
        name="Position Monitor",
    )

    scheduler.start()


if __name__ == "__main__":
    main()

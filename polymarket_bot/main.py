"""
polymarket_bot/main.py

Main orchestrator for the Bybit-Solana CEX-DEX Arbitrage Bot.
Runs three concurrent tasks:
  1. price_feed_loop    — WebSocket connection to Bybit Spot SOL/USDT orderbook.
  2. arb_scan_loop      — 3-second loop fetching Jupiter quotes and scanning for spreads.
  3. daily_summary_loop — Periodic loop to reset daily PnL and send summary.
"""
from __future__ import annotations
import asyncio
import signal
from datetime import datetime, timezone
from loguru import logger

from polymarket_bot import alerts
from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed
from polymarket_bot.scanner import CexDexArbitrageScanner
from polymarket_bot.execution import execute_arbitrage, close_bybit_client
from polymarket_bot.state import get_status, load_state
from polymarket_bot.dashboard import start_dashboard

_SCAN_INTERVAL_S = 3.0        # Poll Jupiter API every 3s to respect rate limits
_DAILY_SUMMARY_HOUR = 22      # UTC hour to send daily Telegram summary
_shutdown_event = asyncio.Event()
_last_daily_summary_date = ""


async def arb_scan_loop(scanner: CexDexArbitrageScanner) -> None:
    """Core scanning loop checking Bybit bids/asks vs Jupiter quotes."""
    logger.info("CEX-DEX Arbitrage scanning loop started")
    execution_cooldown = 0.0

    while not _shutdown_event.is_set():
        try:
            now = asyncio.get_event_loop().time()
            if now < execution_cooldown:
                await asyncio.sleep(0.5)
                continue

            state = load_state()
            opportunity = await scanner.scan(state)

            if opportunity:
                # Execute simultaneous CEX-DEX trades
                await execute_arbitrage(opportunity, feed, scanner)
                # Set a cooldown to allow balances to settle
                execution_cooldown = now + 5.0

        except Exception as e:
            logger.error(f"Error in arbitrage scanning loop: {e}")
            await asyncio.sleep(2.0)

        await asyncio.sleep(_SCAN_INTERVAL_S)


async def daily_summary_loop() -> None:
    """Manages daily resets and reports to Telegram."""
    global _last_daily_summary_date
    logger.info("Daily summary loop started")

    while not _shutdown_event.is_set():
        try:
            now_utc = datetime.now(timezone.utc)
            today_str = now_utc.date().isoformat()

            if now_utc.hour == _DAILY_SUMMARY_HOUR and _last_daily_summary_date != today_str:
                status = get_status()
                alerts.daily_summary(status)
                _last_daily_summary_date = today_str
                logger.info("Daily Telegram summary sent.")

        except Exception as e:
            logger.error(f"Error in daily summary loop: {e}")

        await asyncio.sleep(60)


async def main() -> None:
    """Bootstrap and run all tasks."""
    logger.info(f"Starting Bybit-Solana CEX-DEX Arbitrage Bot (mode: {settings.trading_mode})")

    # Initialize components
    feed = BybitPriceFeed()
    scanner = CexDexArbitrageScanner(feed)
    
    # 1. Startup alert
    alerts.startup(settings.trading_mode, settings.account_size)

    # 2. Register Signal Handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    def handle_shutdown():
        logger.warning("Shutdown signal received. Stopping tasks...")
        _shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown)
        except NotImplementedError:
            pass  # OS fallback

    # 3. Start tasks
    feed_task = asyncio.create_task(feed.connect_and_listen())
    scan_task = asyncio.create_task(arb_scan_loop(scanner))
    summary_task = asyncio.create_task(daily_summary_loop())
    
    # Start web dashboard
    runner = await start_dashboard(feed, scanner)

    # Keep running until shutdown event is set
    try:
        # Wait for shutdown signal
        while not _shutdown_event.is_set():
            await asyncio.sleep(1)
    finally:
        # 4. Graceful Cleanup
        logger.info("Cleaning up tasks...")
        feed_task.cancel()
        scan_task.cancel()
        summary_task.cancel()
        
        # Shut down dashboard
        await runner.cleanup()
        
        # Close HTTP and Bybit clients
        await scanner.close_session()
        await close_bybit_client()
        
        # Wait for tasks to cancel
        await asyncio.gather(feed_task, scan_task, summary_task, return_exceptions=True)
        logger.info("All tasks stopped. Exit complete.")


if __name__ == "__main__":
    asyncio.run(main())

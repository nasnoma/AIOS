"""
polymarket_bot/main.py

Main orchestrator for the Bybit Spot Triangular Arbitrage Bot.
Runs three concurrent tasks:
  1. price_feed_loop    — Websocket connection to Bybit Spot orderbook feed
  2. arb_scan_loop      — 20Hz loop scanning orderbook for profitable triangles
  3. daily_summary_loop — Periodic loop to reset daily PnL and send summary

Run:
    python -m polymarket_bot.main
"""
from __future__ import annotations
import asyncio
import signal
from datetime import datetime, timezone
from loguru import logger

from polymarket_bot import alerts
from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed
from polymarket_bot.scanner import ArbitrageScanner
from polymarket_bot.execution import execute_arbitrage, close_bybit_client
from polymarket_bot.state import get_status, load_state
from polymarket_bot.dashboard import start_dashboard

_SCAN_INTERVAL_S = 0.05       # Scan 20 times per second (50ms)
_DAILY_SUMMARY_HOUR = 22      # UTC hour to send daily Telegram summary
_shutdown_event = asyncio.Event()
_last_daily_summary_date = ""


async def arb_scan_loop(scanner: ArbitrageScanner) -> None:
    """Core hot-path scanning loop."""
    logger.info("Arbitrage scanning loop started")
    execution_cooldown = 0.0

    while not _shutdown_event.is_set():
        try:
            # Enforce execution cooldown after a trade to allow balances to settle
            now = asyncio.get_event_loop().time()
            if now < execution_cooldown:
                await asyncio.sleep(_SCAN_INTERVAL_S)
                continue

            opportunity = scanner.scan()
            if opportunity:
                # Fire execution task asynchronously
                await execute_arbitrage(opportunity)
                # Set configurable cooldown before scanning again
                execution_cooldown = now + settings.execution_cooldown_s

        except Exception as e:
            logger.error(f"Error in arbitrage scanning loop: {e}")
            await asyncio.sleep(1)

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
    logger.info(f"Starting Bybit Spot Triangular Arbitrage Bot (mode: {settings.trading_mode})")

    # Initialize components
    feed = BybitPriceFeed()
    scanner = ArbitrageScanner(feed)
    
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
    runner = await start_dashboard(feed)

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
        
        # Close Bybit client session
        await close_bybit_client()
        
        # Wait for tasks to cancel
        await asyncio.gather(feed_task, scan_task, summary_task, return_exceptions=True)
        logger.info("All tasks stopped. Exit complete.")


if __name__ == "__main__":
    asyncio.run(main())

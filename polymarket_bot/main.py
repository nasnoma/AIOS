"""
polymarket_bot/main.py

Main orchestrator for the Bybit Spot SOL/USDT Grid Market Maker Bot.
Runs three concurrent tasks:
  1. price_feed_loop    — WebSocket connection to Bybit Spot SOL/USDT orderbook feed.
  2. grid_manager_loop  — 2Hz loop checking paper fills and managing limit order grids.
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
from polymarket_bot.scanner import GridManager
from polymarket_bot.execution import check_paper_fills, update_resting_grid, close_bybit_client
from polymarket_bot.state import get_status, load_state
from polymarket_bot.dashboard import start_dashboard

_GRID_INTERVAL_S = 0.5        # Run grid manager at 2Hz (every 500ms)
_DAILY_SUMMARY_HOUR = 22      # UTC hour to send daily Telegram summary
_shutdown_event = asyncio.Event()
_last_daily_summary_date = ""


async def grid_manager_loop(manager: GridManager) -> None:
    """Core loop that manages limits and simulates resting fills."""
    logger.info("Grid manager loop started")

    while not _shutdown_event.is_set():
        try:
            # 1. Fetch current SOL mid-price
            bid, ask, _, _ = manager.price_feed.get_best_bid_ask("SOLUSDT")
            
            if bid and ask:
                mid_price = (bid + ask) / 2.0

                # 2. In Paper trading, check resting limit orders against mid-price
                if settings.trading_mode == "paper":
                    await check_paper_fills(mid_price)

                # 3. Check if grid needs to be replaced/rebalanced
                state = load_state()
                trigger_replace = False

                if not state.open_grid_orders:
                    # No active orders (startup or all filled)
                    trigger_replace = True
                elif state.grid_center_price > 0.0:
                    # Check drift of mid-price relative to grid placement center
                    drift = abs(mid_price - state.grid_center_price) / state.grid_center_price
                    if drift > settings.drift_trigger_pct:
                        logger.warning(f"Price drifted from grid center ({state.grid_center_price:.2f} -> {mid_price:.2f}, Drift: {drift:.2%}). Rebalancing...")
                        trigger_replace = True

                # 4. If triggered, calculate and update grid
                if trigger_replace:
                    grid = manager.calculate_grid(state)
                    if grid:
                        await update_resting_grid(grid)

        except Exception as e:
            logger.error(f"Error in grid manager loop: {e}")
            await asyncio.sleep(1)

        await asyncio.sleep(_GRID_INTERVAL_S)


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
    logger.info(f"Starting Bybit Spot Grid Market Maker Bot (mode: {settings.trading_mode})")

    # Initialize components
    feed = BybitPriceFeed()
    manager = GridManager(feed)
    
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
    grid_task = asyncio.create_task(grid_manager_loop(manager))
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
        grid_task.cancel()
        summary_task.cancel()
        
        # Shut down dashboard
        await runner.cleanup()
        
        # Close Bybit client session
        await close_bybit_client()
        
        # Wait for tasks to cancel
        await asyncio.gather(feed_task, grid_task, summary_task, return_exceptions=True)
        logger.info("All tasks stopped. Exit complete.")


if __name__ == "__main__":
    asyncio.run(main())

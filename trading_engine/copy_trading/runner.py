"""
trading_engine/copy_trading/runner.py

Standalone background runner for copy trading intelligence.
Runs two loops:
  - Leaderboard scan: every 6 hours
  - Position monitor: every 5 minutes

Start with: python -m trading_engine.copy_trading.runner
Or from start.sh alongside the main engine.
"""

from __future__ import annotations

import time
from loguru import logger

SCAN_INTERVAL_HOURS  = 6      # full leaderboard re-scan
MONITOR_INTERVAL_SEC = 300    # position check every 5 min


def run() -> None:
    from trading_engine.copy_trading import leaderboard_scanner, position_monitor

    logger.info("🚀 Copy trading runner started")
    last_scan_ts = 0.0

    while True:
        now = time.time()

        # ── Leaderboard scan ───────────────────────────────────────────────────
        if now - last_scan_ts >= SCAN_INTERVAL_HOURS * 3600:
            try:
                results = leaderboard_scanner.scan()
                if results:
                    leaderboard_scanner.send_leaderboard_alert(results)
            except Exception as e:
                logger.error(f"Leaderboard scan error: {e}")
            last_scan_ts = time.time()

        # ── Position monitor ───────────────────────────────────────────────────
        try:
            position_monitor.monitor()
        except Exception as e:
            logger.error(f"Position monitor error: {e}")

        time.sleep(MONITOR_INTERVAL_SEC)


if __name__ == "__main__":
    run()

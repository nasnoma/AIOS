"""
trading_engine/scheduler.py

APScheduler-based runner.
Runs the full pipeline on configured assets at regular intervals.
Also handles position monitoring between signal cycles.
"""
from __future__ import annotations
import signal as os_signal
import sys
from datetime import datetime, timezone
import requests
from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from trading_engine.config import settings
from trading_engine.orchestrator import run_all_assets
from trading_engine.execution import paper_trader, live_trader
from trading_engine.alerts.telegram_bot import send_signal_alert


scheduler = BlockingScheduler(timezone="UTC")


def send_heartbeat():
    """Send heartbeat to the API server."""
    import time
    url = f"http://localhost:{settings.api_port}/api/scheduler/heartbeat"
    for attempt in range(3):
        try:
            requests.post(url, timeout=3)
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                logger.debug(f"Heartbeat failed: {e}")


def run_signal_cycle():
    """Main cycle: analyze all assets and execute if signal found."""
    logger.info(f"\n🔄 Signal cycle started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    if settings.trading_mode == "live":
        trader = live_trader
    else:
        trader = paper_trader

    portfolio = trader._load_state()

    # Get portfolio state for context
    status = trader.get_status()
    portfolio_heat = status["portfolio_heat"] / 100
    open_positions = status["open_positions"]
    win_rate = status.get("win_rate", 50) / 100

    # Run full analysis
    signals = run_all_assets()

    import requests
    for sig in signals:
        # Post signal details to local dashboard server
        try:
            payload = {
                "symbol": sig.symbol,
                "asset_type": sig.asset_type,
                "timeframe": sig.timeframe,
                "timestamp": sig.timestamp,
                "agent_signals": sig.agent_signals,
                "verdict": sig.verdict,
                "risk": sig.risk,
                "final_action": sig.final_action,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "position_size_usd": sig.position_size_usd,
                "reasoning": sig.reasoning,
            }
            requests.post(f"http://localhost:{settings.api_port}/api/signals", json=payload, timeout=2)
        except Exception as e:
            logger.debug(f"Failed to post signal to dashboard: {e}")

        if sig.final_action in ("BUY", "SELL") and settings.trading_mode != "signal_only":
            direction = "long" if sig.final_action == "BUY" else "short"
            
            # Prevent duplicate concurrent positions on the same asset
            if any(p.symbol == sig.symbol for p in portfolio.open_positions):
                logger.info(f"⏭️ Skipping execution for {sig.symbol}: position already open.")
                continue
            
            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Trade execution blocked for {sig.symbol}: Max concurrent positions limit ({max_positions}) reached.")
                continue
            
            size_needed = sig.position_size_usd or 0
            if portfolio.cash < size_needed:
                logger.warning(f"Trade execution blocked for {sig.symbol}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${size_needed:,.2f})")
                continue

            pos = trader.open_trade(
                symbol=sig.symbol,
                direction=direction,
                entry=sig.entry_price,
                size_usd=size_needed,
                stop_loss=sig.stop_loss or 0,
                take_profit=sig.take_profit or 0,
                atr=float((sig.risk or {}).get("atr", 0.0)),
            )
            if pos:
                # Reload portfolio to reflect new position & cash balance in subsequent iterations
                portfolio = trader._load_state()

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
    from trading_engine.market_hours import classify_symbol, AssetClass
    from trading_engine.data.market_data import CryptoDataFetcher, StockDataFetcher
    from trading_engine.data.cfd_data import BybitCFDFetcher
    import ccxt

    current_prices: dict[str, float] = {}

    # Classify all open positions
    crypto_symbols = []
    cfd_symbols    = []
    stock_symbols  = []
    for pos in open_positions:
        ac = classify_symbol(pos.symbol)
        if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
            cfd_symbols.append(pos.symbol)
        elif ac == AssetClass.CRYPTO:
            crypto_symbols.append(pos.symbol)
        else:
            stock_symbols.append(pos.symbol)

    # 1. Crypto — Bybit spot
    if crypto_symbols:
        try:
            exchange_class = getattr(ccxt, settings.crypto_exchange)
            exchange       = exchange_class()
            for asset in crypto_symbols:
                try:
                    ticker = exchange.fetch_ticker(asset)
                    current_prices[asset] = ticker["last"]
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Price monitor fetch error for crypto: {e}")

    # 2. Bybit linear CFDs (stocks + metals)
    if cfd_symbols:
        cfd_fetcher = BybitCFDFetcher()
        for asset in cfd_symbols:
            try:
                price = cfd_fetcher.fetch_latest_price(asset)
                if price > 0:
                    current_prices[asset] = price
            except Exception as e:
                logger.warning(f"Price monitor fetch error for CFD {asset}: {e}")

    # 3. Plain stocks via Massive/Polygon
    unfetched_stocks = [s for s in stock_symbols if s not in current_prices]
    if unfetched_stocks and settings.get_massive_api_key:
        try:
            fetcher = StockDataFetcher()
            for asset in unfetched_stocks:
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


def run_bounty_hunter_cycle():
    """Bounty Hunter cycle: scan crypto, Polygon stocks, and Bybit CFDs for trade opportunities."""
    logger.info(f"\n⚔️ Scheduled Bounty Hunter scan cycle started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    if settings.trading_mode == "live":
        trader = live_trader
    else:
        trader = paper_trader

    # Guard: if state cannot be loaded, abort cleanly rather than letting a NameError
    # propagate and crash the entire cycle (which would bypass all position guards).
    try:
        portfolio = trader._load_state()
    except Exception as e:
        logger.error(f"⚔️ Bounty Hunter aborted — could not load portfolio state: {e}")
        return

    from trading_engine.bounty_hunter import run_bounty_hunt
    watchlist = settings.bounty_hunter_watchlist_assets
    scan_mode = settings.bounty_hunter_scan_mode
    logger.info(f"⚔️ Bounty Hunter scan mode: {scan_mode}")
    results = run_bounty_hunt(
        mode=scan_mode,
        crypto_limit=15,
        stock_limit=5,
        cfd_limit=5,          # Bybit stock CFDs + metals
        scan_cfds=True,
        watchlist=watchlist,
    )

    active_buys  = [r for r in results if r["final_action"] == "BUY"]
    active_sells = [r for r in results if r["final_action"] == "SELL"]

    if settings.trading_mode == "signal_only":
        logger.info(f"Bounty Hunter [signal_only]: {len(active_buys)} BUY + {len(active_sells)} SELL signals — not executing.")
        return

    # Execute BUY signals
    if active_buys:
        logger.info(f"Bounty Hunter: placing {len(active_buys)} BUY trade(s)...")
        for b in active_buys:
            # Prevent duplicate concurrent positions
            if any(p.symbol == b["symbol"] for p in portfolio.open_positions):
                logger.info(f"Bounty Hunter: skipping BUY for {b['symbol']} (position already open).")
                continue

            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Bounty Hunter execution blocked: Max concurrent positions ({max_positions}) reached.")
                break
            size_needed = b["position_size_usd"] or 20.0
            if portfolio.cash < size_needed:
                logger.warning(f"Bounty Hunter execution blocked for {b['symbol']}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${size_needed:,.2f})")
                continue

            pos = trader.open_trade(
                symbol=b["symbol"],
                direction="long",
                entry=b["entry_price"],
                size_usd=size_needed,
                stop_loss=b["stop_loss"]    or (b["entry_price"] * 0.95),
                take_profit=b["take_profit"] or (b["entry_price"] * 1.10),
            )
            if pos:
                portfolio = trader._load_state()
    else:
        logger.info("Bounty Hunter: no approved BUY candidates in this scan.")

    # Execute SELL (short) signals — only valid for Bybit linear CFDs
    if active_sells:
        logger.info(f"Bounty Hunter: placing {len(active_sells)} SELL (short) trade(s)...")
        for s in active_sells:
            # Prevent duplicate concurrent positions
            if any(p.symbol == s["symbol"] for p in portfolio.open_positions):
                logger.info(f"Bounty Hunter: skipping SELL for {s['symbol']} (position already open).")
                continue

            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Bounty Hunter execution blocked: Max concurrent positions ({max_positions}) reached.")
                break
            size_needed = s["position_size_usd"] or 20.0
            if portfolio.cash < size_needed:
                logger.warning(f"Bounty Hunter execution blocked for {s['symbol']}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${size_needed:,.2f})")
                continue

            pos = trader.open_trade(
                symbol=s["symbol"],
                direction="short",
                entry=s["entry_price"],
                size_usd=size_needed,
                stop_loss=s["stop_loss"]    or (s["entry_price"] * 1.05),
                take_profit=s["take_profit"] or (s["entry_price"] * 0.90),
            )
            if pos:
                portfolio = trader._load_state()
    else:
        logger.info("Bounty Hunter: no approved SELL candidates in this scan.")


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

    # Wait for API server to fully start before sending heartbeat
    import time
    logger.info("   Waiting for API server to start...")
    time.sleep(5)

    # Initial heartbeat sent to API server
    send_heartbeat()

    # Schedule recurring runs
    scheduler.add_job(
        send_heartbeat,
        trigger=IntervalTrigger(seconds=30),
        id="heartbeat",
        name="Scheduler Heartbeat",
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        run_signal_cycle,
        trigger=IntervalTrigger(minutes=interval),
        id="signal_cycle",
        name="Trading Signal Cycle",
        next_run_time=datetime.now(timezone.utc),
    )

    # Position monitor every 1 minute, running immediately on startup
    scheduler.add_job(
        monitor_positions,
        trigger=IntervalTrigger(minutes=1),
        id="position_monitor",
        name="Position Monitor",
        next_run_time=datetime.now(timezone.utc),
    )

    # Bounty Hunter scan
    if settings.bounty_hunter_enabled:
        if settings.bounty_hunter_interval_minutes > 0:
            interval_val = settings.bounty_hunter_interval_minutes
            logger.info(f"   Bounty Hunter: scheduled every {interval_val} minutes")
            scheduler.add_job(
                run_bounty_hunter_cycle,
                trigger=IntervalTrigger(minutes=interval_val),
                id="bounty_hunter_cycle",
                name="Bounty Hunter Cycle",
                next_run_time=datetime.now(timezone.utc),
            )
        else:
            interval_hours = settings.bounty_hunter_interval_hours
            logger.info(f"   Bounty Hunter: scheduled every {interval_hours} hours")
            scheduler.add_job(
                run_bounty_hunter_cycle,
                trigger=IntervalTrigger(hours=interval_hours),
                id="bounty_hunter_cycle",
                name="Bounty Hunter Cycle",
                next_run_time=datetime.now(timezone.utc),
            )

    scheduler.start()


if __name__ == "__main__":
    main()

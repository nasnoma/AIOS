"""
trading_engine/scheduler.py

APScheduler-based runner.
Runs the full pipeline on configured assets at regular intervals.
Also handles position monitoring between signal cycles.
"""
from __future__ import annotations
import signal as os_signal
import sys
from datetime import datetime, timezone, timedelta
import requests
from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from trading_engine.config import settings
from trading_engine.orchestrator import run_all_assets
from trading_engine.execution import paper_trader, live_trader
from trading_engine.alerts.telegram_bot import send_signal_alert


scheduler = BlockingScheduler(timezone="UTC")

# Tracks the UTC timestamp of the last stop-out per symbol.
# Prevents re-entry into the same ranging market for 30 minutes.
_stop_cooldowns: dict[str, datetime] = {}
STOP_COOLDOWN_MINUTES = 30


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


def _allocate_cash_proportionally(signals: list, available_cash: float) -> list[tuple[any, float]]:
    """
    Given a list of approved signals (either TradeSignal objects or dicts), allocate the available cash.
    If the total requested cash exceeds available cash, scale down proportionally.
    Skips any trade whose allocated size is below the minimum threshold ($10 USD).
    """
    approved_sigs = []
    for s in signals:
        # Check if s is a dict or TradeSignal object
        if isinstance(s, dict):
            if s.get("final_action") in ("BUY", "SELL"):
                approved_sigs.append(s)
        else:
            if s.final_action in ("BUY", "SELL"):
                approved_sigs.append(s)

    if not approved_sigs:
        return []

    # Get the requested sizes
    requested_sizes = []
    for sig in approved_sigs:
        if isinstance(sig, dict):
            size = sig.get("position_size_usd") or 0.0
        else:
            size = sig.position_size_usd or 0.0
        if size <= 0:
            size = 100.0
        requested_sizes.append(size)

    total_requested = sum(requested_sizes)
    if total_requested <= 0:
        return []

    scale_factor = 1.0
    if total_requested > available_cash:
        scale_factor = available_cash / total_requested
        logger.info(f"⚖️ Proportional allocator: Total requested (${total_requested:,.2f}) exceeds available cash (${available_cash:,.2f}). Scaling factor: {scale_factor:.4f}")

    allocations = []
    min_trade_size = 10.0  # USD hard cap to avoid dust trades
    
    for sig, req_size in zip(approved_sigs, requested_sizes):
        allocated_size = req_size * scale_factor
        if allocated_size < min_trade_size:
            sym = sig.get("symbol") if isinstance(sig, dict) else sig.symbol
            logger.warning(f"Proportional allocator: Skipping {sym} - allocated size (${allocated_size:,.2f}) is below minimum limit (${min_trade_size:.2f})")
            continue
        allocations.append((sig, allocated_size))

    return allocations


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

    # Filter for actionable trade signals
    actionable_signals = [s for s in signals if s.final_action in ("BUY", "SELL") and settings.trading_mode != "signal_only"]
    
    # Run allocator to scale sizes based on available cash
    allocations = _allocate_cash_proportionally(actionable_signals, portfolio.cash)
    allocated_sizes = {sig.symbol: size for sig, size in allocations}

    for sig in signals:
        # Execute trade if it was approved and allocated
        if sig.final_action in ("BUY", "SELL") and settings.trading_mode != "signal_only":
            # Check if we already have an open position in this asset
            is_already_open = any(p.symbol == sig.symbol for p in portfolio.open_positions)
            if is_already_open:
                logger.info(f"⏭️ Skipping execution for {sig.symbol}: position already open.")
                continue

            if sig.symbol not in allocated_sizes:
                logger.info(f"⏭️ Skipping execution for {sig.symbol}: not allocated/scaled below minimum.")
                continue
                
            size_needed = allocated_sizes[sig.symbol]
            direction = "long" if sig.final_action == "BUY" else "short"
            
            # Post-stopout cooldown: skip if last stop-out was within 30 minutes
            cooldown_until = _stop_cooldowns.get(sig.symbol)
            if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
                remaining = (cooldown_until - datetime.now(timezone.utc)).seconds // 60
                logger.info(f"⏳ Skipping {sig.symbol}: on stop-out cooldown for {remaining}m more.")
                continue
            
            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Trade execution blocked for {sig.symbol}: Max concurrent positions limit ({max_positions}) reached.")
                continue
            
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
                agent_signals=sig.agent_signals or [],  # stored on Position for ART grading on close
            )
            if pos:
                # Reload portfolio to reflect new position & cash balance in subsequent iterations
                portfolio = trader._load_state()
                # Only send Telegram alert when a position was actually opened
                try:
                    send_signal_alert(sig)
                except Exception as e:
                    logger.warning(f"Telegram alert failed: {e}")

        elif sig.final_action in ("BUY", "SELL") and settings.trading_mode == "signal_only":
            # In signal_only mode: always alert (no execution path)
            is_already_open = any(p.symbol == sig.symbol for p in portfolio.open_positions)
            if not is_already_open:
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

    # Fetch current prices for all open symbols (all assumed to be crypto)
    import ccxt

    current_prices: dict[str, float] = {}
    crypto_symbols = [pos.symbol for pos in open_positions]

    if crypto_symbols:
        try:
            exchange_class = getattr(ccxt, settings.crypto_exchange)
            exchange       = exchange_class()
            for asset in crypto_symbols:
                try:
                    # Strip linear perpetual suffix if present for ticker fetching
                    fetch_asset = asset.split(":")[0] if ":" in asset else asset
                    ticker = exchange.fetch_ticker(fetch_asset)
                    current_prices[asset] = ticker["last"]
                except Exception as e_asset:
                    logger.warning(f"Price monitor fetch error for asset {asset} (fetch_symbol={fetch_asset}): {e_asset}")
        except Exception as e:
            logger.warning(f"Price monitor fetch error for crypto: {e}")

    if current_prices:
        # Snapshot open symbols before price update to detect stop-outs
        symbols_before = {pos.symbol for pos in open_positions}
        trader.update_prices(current_prices)
        # Check which positions were stopped out (status "stopped") and impose cooldown
        portfolio_after = trader._load_state()
        symbols_after = {pos.symbol for pos in portfolio_after.open_positions}
        closed_symbols = symbols_before - symbols_after
        for sym in closed_symbols:
            # Determine if it was a stop-out by checking closed trades history
            recent_closed = [t for t in portfolio_after.closed_trades if t.get("symbol") == sym]
            if recent_closed and recent_closed[-1].get("exit_reason") == "stopped":
                cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=STOP_COOLDOWN_MINUTES)
                _stop_cooldowns[sym] = cooldown_until
                logger.info(f"🛑 Stop-out cooldown set for {sym}: blocked for {STOP_COOLDOWN_MINUTES}m until {cooldown_until.strftime('%H:%M UTC')}")



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
        watchlist=watchlist,
    )

    active_buys  = [r for r in results if r["final_action"] == "BUY"]
    active_sells = [r for r in results if r["final_action"] == "SELL"]

    if settings.trading_mode == "signal_only":
        logger.info(f"Bounty Hunter [signal_only]: {len(active_buys)} BUY + {len(active_sells)} SELL signals — not executing.")
        return

    # ── Circuit Breaker check ─────────────────────────────────────────────
    # Compute today's realized PnL and halt if the daily loss limit is breached.
    try:
        from trading_engine.orchestrator import _get_daily_pnl
        daily_pnl = _get_daily_pnl(trader)
        daily_loss_limit = getattr(settings, "daily_loss_limit_usd", 300.0)
        if daily_loss_limit > 0 and daily_pnl < -abs(daily_loss_limit):
            logger.warning(
                f"⚔️ Bounty Hunter HALTED — circuit breaker triggered: "
                f"daily PnL ${daily_pnl:,.2f} ≤ -${daily_loss_limit:,.0f}. "
                "No new trades until tomorrow."
            )
            return
    except Exception as _cbe:
        logger.warning(f"⚔️ Bounty Hunter circuit-breaker check failed ({_cbe}); proceeding with caution.")

    # Run allocator across all active buys and sells
    all_candidates = active_buys + active_sells
    allocations = _allocate_cash_proportionally(all_candidates, portfolio.cash)
    allocated_sizes = {c.get("symbol") if isinstance(c, dict) else c.symbol: size for c, size in allocations}

    if active_buys:
        logger.info(f"Bounty Hunter: placing {len(active_buys)} BUY trade(s)...")
        for b in active_buys:
            symbol = b.get("symbol")
            if symbol not in allocated_sizes:
                logger.info(f"Bounty Hunter: skipping BUY for {symbol} (not allocated/scaled below minimum).")
                continue

            # Prevent duplicate concurrent positions
            if any(p.symbol == symbol for p in portfolio.open_positions):
                logger.info(f"Bounty Hunter: skipping BUY for {symbol} (position already open).")
                continue

            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Bounty Hunter execution blocked: Max concurrent positions ({max_positions}) reached.")
                break
                
            size_needed = allocated_sizes[symbol]
            if portfolio.cash < size_needed:
                logger.warning(f"Bounty Hunter execution blocked for {symbol}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${size_needed:,.2f})")
                continue

            pos = trader.open_trade(
                symbol=symbol,
                direction="long",
                entry=b["entry_price"],
                size_usd=size_needed,
                stop_loss=b["stop_loss"]    or (b["entry_price"] * 0.95),
                take_profit=b["take_profit"] or (b["entry_price"] * 1.10),
                atr=float(b.get("atr") or 0.0),
            )
            if pos:
                portfolio = trader._load_state()
    else:
        logger.info("Bounty Hunter: no approved BUY candidates in this scan.")

    # Execute SELL (short) signals — only valid for Bybit linear CFDs
    if active_sells:
        logger.info(f"Bounty Hunter: placing {len(active_sells)} SELL (short) trade(s)...")
        for s in active_sells:
            symbol = s.get("symbol")
            if symbol not in allocated_sizes:
                logger.info(f"Bounty Hunter: skipping SELL for {symbol} (not allocated/scaled below minimum).")
                continue

            # Prevent duplicate concurrent positions
            if any(p.symbol == symbol for p in portfolio.open_positions):
                logger.info(f"Bounty Hunter: skipping SELL for {symbol} (position already open).")
                continue

            max_positions = settings.max_concurrent_positions
            if len(portfolio.open_positions) >= max_positions:
                logger.warning(f"Bounty Hunter execution blocked: Max concurrent positions ({max_positions}) reached.")
                break
                
            size_needed = allocated_sizes[symbol]
            if portfolio.cash < size_needed:
                logger.warning(f"Bounty Hunter execution blocked for {symbol}: Insufficient cash (cash=${portfolio.cash:,.2f}, needed=${size_needed:,.2f})")
                continue

            pos = trader.open_trade(
                symbol=symbol,
                direction="short",
                entry=s["entry_price"],
                size_usd=size_needed,
                stop_loss=s["stop_loss"]    or (s["entry_price"] * 1.05),
                take_profit=s["take_profit"] or (s["entry_price"] * 0.90),
                atr=float(s.get("atr") or 0.0),
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
            logger.info(f"   Bounty Hunter: scheduled every {interval_val} minutes (staggered by 30s)")
            scheduler.add_job(
                run_bounty_hunter_cycle,
                trigger=IntervalTrigger(minutes=interval_val),
                id="bounty_hunter_cycle",
                name="Bounty Hunter Cycle",
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            )
        else:
            interval_hours = settings.bounty_hunter_interval_hours
            logger.info(f"   Bounty Hunter: scheduled every {interval_hours} hours (staggered by 30s)")
            scheduler.add_job(
                run_bounty_hunter_cycle,
                trigger=IntervalTrigger(hours=interval_hours),
                id="bounty_hunter_cycle",
                name="Bounty Hunter Cycle",
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            )

    # Claude Council weekly review
    scheduler.add_job(
        run_claude_council_weekly_review,
        trigger=CronTrigger(day_of_week="sun", hour=0, minute=0),
        id="claude_council_weekly_review",
        name="Claude Council Weekly Performance Review",
    )

    # ── Spot Grid & DCA Jobs ──────────────────────────
    try:
        from trading_engine.config import spot_settings
        if spot_settings.enabled:
            from trading_engine.spot.runner import init_spot_engine, run_spot_grid_tick, run_spot_regime_check, run_spot_dca_check
            init_spot_engine()
            logger.info("   ⚡ Spot Grid Engine enabled & initialised.")
            
            # Spot Grid Tick (every 5 mins)
            scheduler.add_job(
                run_spot_grid_tick,
                trigger=IntervalTrigger(minutes=spot_settings.grid_tick_minutes),
                id="spot_grid_tick",
                name="Spot Grid Tick",
                next_run_time=datetime.now(timezone.utc),
            )
            # Spot Regime Check (every 1 hour)
            scheduler.add_job(
                run_spot_regime_check,
                trigger=IntervalTrigger(hours=spot_settings.regime_check_hours),
                id="spot_regime_check",
                name="Spot Regime Check",
                next_run_time=datetime.now(timezone.utc),
            )
            # Spot DCA Check (every 1 hour, staggered by 2 minutes)
            scheduler.add_job(
                run_spot_dca_check,
                trigger=IntervalTrigger(hours=1),
                id="spot_dca_check",
                name="Spot DCA Oversold Check",
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            )
            # Spot Self-Healing & Automated Backtest Optimizer (every 6 hours)
            from trading_engine.spot.runner import run_spot_self_healing_and_optimize
            scheduler.add_job(
                run_spot_self_healing_and_optimize,
                trigger=IntervalTrigger(hours=6),
                id="spot_self_healing_optimize",
                name="Spot Self-Healing & Backtest Optimizer",
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
    except Exception as e_spot:
        logger.error(f"❌ Failed starting Spot Grid jobs: {e_spot}")


    scheduler.start()


def run_claude_council_weekly_review():
    """Run weekly performance review & parameter tuning via Claude Council."""
    logger.info("🏛️ Running Claude Council Weekly Performance Review...")
    try:
        from trading_engine.claude_council import ClaudeCouncil
        from trading_engine.execution.live_trader import live_trader
        from trading_engine.alerts.telegram_bot import send_message as send_telegram_alert

        state = live_trader._load_state()
        closed_list = getattr(state, "closed_trades", [])
        closed_dicts = []
        for p in closed_list:
            pnl_val = float(getattr(p, "pnl_usd", 0.0) or getattr(p, "pnl", 0.0) or 0.0)
            closed_dicts.append({
                "symbol": getattr(p, "symbol", "UNKNOWN"),
                "pnl": pnl_val,
                "closed_at": getattr(p, "closed_at", None),
            })

        council = ClaudeCouncil()
        review = council.run_weekly_performance_review(closed_dicts, days=7)
        logger.info(f"🏛️ Weekly review completed: {review.get('summary')}")
        
        # Send Telegram alert summary
        summary_text = review.get("summary")
        if summary_text:
            send_telegram_alert(summary_text)

    except Exception as e:
        logger.error(f"❌ Failed running Claude Council weekly review: {e}")


if __name__ == "__main__":
    main()

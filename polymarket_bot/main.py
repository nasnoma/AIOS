"""
polymarket_bot/main.py

Main async event loop orchestrator.
Runs four concurrent tasks:
  1. price_feed_loop     — Binance WS: live BTC/ETH/SOL prices (background)
  2. clob_stream_loop   — Polymarket CLOB WS: live order books (background)
  3. trade_loop         — Every 5s: evaluate signals, execute trades
  4. monitor_loop       — Every 10s: detect window expiry, resolve positions

Run:
    python -m polymarket_bot.main

Or with explicit mode:
    TRADING_MODE=paper python -m polymarket_bot.main
"""
from __future__ import annotations
import asyncio
import signal
import time
from datetime import datetime, timezone

from loguru import logger

from polymarket_bot import alerts
from polymarket_bot.clob_client import clob_cache
from polymarket_bot.config import settings
from polymarket_bot.execution import execute, resolve_expired_positions, compute_unrealized_pnl
from polymarket_bot.market import MarketTokens, get_active_markets, get_current_window
from polymarket_bot.price_feed import PriceFeed
from polymarket_bot.risk import evaluate as risk_evaluate
from polymarket_bot.state import get_status, load_state
from polymarket_bot.strategy import Signal, SignalType, evaluate_signals

# ── Globals ───────────────────────────────────────────────────────────────────
_TRADE_LOOP_INTERVAL_S = 5    # Evaluate signals every 5s
_MONITOR_INTERVAL_S = 10      # Check position resolution every 10s
_DAILY_SUMMARY_HOUR = 23      # UTC hour to send daily Telegram summary
_shutdown_event = asyncio.Event()

# Track which positions we've already opened this window to avoid duplicates
_opened_positions_this_window: set[str] = set()   # key = "asset:window_start"
_last_daily_summary_date = ""


# ── Trade Loop ────────────────────────────────────────────────────────────────

async def trade_loop(feed: PriceFeed) -> None:
    """Hot path: evaluate signals and execute trades every TRADE_LOOP_INTERVAL_S seconds."""
    global _opened_positions_this_window

    logger.info("Trade loop started")
    assets = settings.assets
    last_status_print = 0.0

    while not _shutdown_event.is_set():
        try:
            window = get_current_window()

            # Reset per-window dedup tracker on new window
            window_key_prefix = str(window.window_start)
            current_keys = {f"{a}:{window.window_start}" for a in assets}
            _opened_positions_this_window -= {k for k in _opened_positions_this_window if not k.endswith(window_key_prefix.lstrip("0") or window_key_prefix)}

            # Discover active markets for all assets (cached per window)
            markets: dict[str, MarketTokens | None] = await get_active_markets(assets)

            # Subscribe to any new token IDs in the CLOB stream
            new_token_ids = []
            for mkt in markets.values():
                if mkt:
                    new_token_ids.extend([mkt.token_id_up, mkt.token_id_down])
            if new_token_ids:
                await clob_cache.subscribe(new_token_ids)
                # Prime caches immediately via public REST so trade_loop has books on the very first tick
                tasks = [clob_cache.fetch_book_rest(tid) for tid in new_token_ids]
                await asyncio.gather(*tasks, return_exceptions=True)

            # Load portfolio state once per loop iteration
            state = load_state()

            # Consecutive losses cooldown check: Pause for 2 windows (600s) after 2 consecutive losses
            consecutive_losses = 0
            for t in reversed(state.closed_trades):
                pnl = t.get("pnl_usd")
                if pnl is not None:
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        break

            cooldown_remaining = 0.0
            if consecutive_losses >= 2 and state.closed_trades:
                last_closed_str = state.closed_trades[-1].get("closed_at")
                if last_closed_str:
                    try:
                        last_closed_dt = datetime.fromisoformat(last_closed_str.replace("Z", "+00:00"))
                        now_dt = datetime.now(timezone.utc)
                        elapsed = (now_dt - last_closed_dt).total_seconds()
                        if elapsed < 600.0:
                            cooldown_remaining = 600.0 - elapsed
                    except Exception as e:
                        logger.debug(f"Error parsing loss time: {e}")

            for asset in assets:
                if cooldown_remaining > 0.0:
                    continue  # skip new entries during cooldown

                mkt = markets.get(asset)
                if not mkt:
                    continue

                dedup_key = f"{asset}:{mkt.window_start}"
                if dedup_key in _opened_positions_this_window:
                    continue  # Already traded this window for this asset

                # Get order books (cached in-memory from WS)
                book_yes = await clob_cache.get_book(mkt.token_id_up)
                book_no = await clob_cache.get_book(mkt.token_id_down)

                # Fallback to REST if WS books not yet populated (works in all modes)
                if not book_yes:
                    book_yes = await clob_cache.fetch_book_rest(mkt.token_id_up)
                if not book_no:
                    book_no = await clob_cache.fetch_book_rest(mkt.token_id_down)

                # Momentum is the price change since the window's strike price (matching backtester edge)
                strike = feed.get_strike(asset, mkt.window_start)
                latest_price = feed.get_latest(asset)
                if strike is not None and latest_price is not None:
                    momentum = latest_price - strike
                else:
                    momentum = 0.0

                # ── Evaluate signal ──────────────────────────────────────────
                signal: Signal = evaluate_signals(
                    asset=asset,
                    book_yes=book_yes,
                    book_no=book_no,
                    momentum_usd=momentum,
                    elapsed_s=window.elapsed_s,
                    remaining_s=window.remaining_s,
                    cfg=settings,
                )

                if signal.signal_type == SignalType.NO_SIGNAL:
                    continue

                # ── Risk check ───────────────────────────────────────────────
                risk = risk_evaluate(
                    signal=signal,
                    account_size=state.account_size,
                    cash=state.cash,
                    daily_pnl=state.daily_pnl,
                    open_position_count=len(state.open_positions),
                    cfg=settings,
                )

                if not risk.approved:
                    logger.debug(f"Risk denied: {risk.reason}")
                    if "Circuit breaker" in risk.reason:
                        alerts.circuit_breaker_hit(state.daily_pnl, settings.max_daily_loss_usd)
                    continue

                # ── Liquidity & Slippage check ────────────────────────────────
                liquidity_ok = True
                slippage_msg = ""
                if signal.signal_type == SignalType.SPREAD_ARB:
                    ok_yes, slip_yes = book_yes.check_liquidity(risk.size_usd_yes, settings.max_acceptable_slippage)
                    ok_no, slip_no = book_no.check_liquidity(risk.size_usd_no, settings.max_acceptable_slippage)
                    if not ok_yes or not ok_no:
                        liquidity_ok = False
                    else:
                        slippage_msg = f" | Expected slippage: YES=${slip_yes:.4f} NO=${slip_no:.4f}"
                else:
                    target_book = book_yes if signal.buy_yes else book_no
                    ok, slip = target_book.check_liquidity(risk.total_size_usd, settings.max_acceptable_slippage)
                    if not ok:
                        liquidity_ok = False
                    else:
                        slippage_msg = f" | Expected slippage: ${slip:.4f}"

                if not liquidity_ok:
                    logger.warning(
                        f"⚠️ Slippage check failed: Insufficient resting L2 liquidity for size "
                        f"${risk.total_size_usd:.2f} within slippage limit ${settings.max_acceptable_slippage:.4f}."
                    )
                    continue

                logger.info(f"📡 Signal approved: {signal.description}{slippage_msg}")

                # ── Execute ──────────────────────────────────────────────────
                pos = await execute(signal, risk, mkt)
                if pos:
                    _opened_positions_this_window.add(dedup_key)
                    mode = "LIVE" if settings.is_live else "PAPER"
                    alerts.trade_opened(
                        asset=asset,
                        signal_type=signal.signal_type.value,
                        side="BOTH" if signal.signal_type == SignalType.SPREAD_ARB
                             else ("YES" if signal.buy_yes else "NO"),
                        size_usd=risk.total_size_usd,
                        entry_yes=signal.entry_price_yes,
                        entry_no=signal.entry_price_no,
                        mode=mode,
                    )
                    # Reload state after execution
                    state = load_state()

            # Periodic status print (heartbeat)
            now_ts = time.time()
            if now_ts - last_status_print >= 30.0:
                last_status_print = now_ts
                state = load_state()
                unrealized = 0.0
                for pos in state.open_positions:
                    unrealized += await compute_unrealized_pnl(pos, feed)
                pnl_str = f"${state.total_pnl:+.2f}"
                unreal_str = f" Unrealized={unrealized:+.2f}" if state.open_positions else ""
                cooldown_str = f" [COOLDOWN: {cooldown_remaining:.0f}s]" if cooldown_remaining > 0.0 else ""
                perf_str = f"Cash=${state.cash:.2f} PnL={pnl_str}{unreal_str} (W/L: {state.win_count}/{state.loss_count}){cooldown_str}"
                status_parts = []
                for asset in assets:
                    mkt = markets.get(asset)
                    if mkt:
                        book_yes = await clob_cache.get_book(mkt.token_id_up)
                        book_no = await clob_cache.get_book(mkt.token_id_down)
                        if book_yes and book_yes.best_bid and book_yes.best_ask:
                            yes_str = f"${book_yes.mid_price:.3f} (${book_yes.best_bid:.2f}/{book_yes.best_ask:.2f})"
                        else:
                            yes_str = f"${book_yes.mid_price:.3f}" if book_yes and book_yes.mid_price else "None"
                            
                        if book_no and book_no.best_bid and book_no.best_ask:
                            no_str = f"${book_no.mid_price:.3f} (${book_no.best_bid:.2f}/{book_no.best_ask:.2f})"
                        else:
                            no_str = f"${book_no.mid_price:.3f}" if book_no and book_no.mid_price else "None"
                        spot = f"${feed.get_latest(asset):,.2f}" if feed.get_latest(asset) else "None"
                        mom = f"${feed.get_momentum(asset, settings.momentum_lookback_s):+.2f}" if feed.get_momentum(asset, settings.momentum_lookback_s) is not None else "None"
                        status_parts.append(f"{asset}: Spot={spot} (Mom={mom}) | YES={yes_str} NO={no_str}")
                    else:
                        status_parts.append(f"{asset}: No Active Market")
                logger.info(f"Heartbeat | {perf_str} | Elapsed: {window.elapsed_s:.0f}s | " + " | ".join(status_parts))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Trade loop error: {e}")

        await asyncio.sleep(_TRADE_LOOP_INTERVAL_S)


# ── Monitor Loop ──────────────────────────────────────────────────────────────

async def monitor_loop(feed: PriceFeed) -> None:
    """Check for expired positions and resolve them every MONITOR_INTERVAL_S seconds."""
    global _last_daily_summary_date
    logger.info("Position monitor loop started")

    while not _shutdown_event.is_set():
        try:
            window = get_current_window()
            await resolve_expired_positions(window.window_start, feed)

            # Daily summary at configured hour
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            if now.hour == _DAILY_SUMMARY_HOUR and today != _last_daily_summary_date:
                _last_daily_summary_date = today
                status = get_status()
                alerts.daily_summary(status)
                
                summary_msg = (
                    f"\n"
                    f"============================================================\n"
                    f"📊 DAILY SUMMARY ({today})\n"
                    f"------------------------------------------------------------\n"
                    f"  Daily P&L:  ${status.get('daily_pnl', 0.0):+.2f}\n"
                    f"  Total P&L:  ${status.get('total_pnl', 0.0):+.2f}\n"
                    f"  Win Rate:   {status.get('win_rate_pct', 0.0):.1f}% ({status.get('win_count', 0)}W / {status.get('loss_count', 0)}L)\n"
                    f"  Cash Bal:   ${status.get('cash', 0.0):.2f}\n"
                    f"  Cycles:     {status.get('cycle_count', 0)}\n"
                    f"============================================================"
                )
                logger.info(summary_msg)
                
                try:
                    from pathlib import Path
                    summary_path = Path(__file__).parent / "daily_summaries.log"
                    with open(summary_path, "a", encoding="utf-8") as f:
                        f.write(summary_msg + "\n")
                except Exception as ex:
                    logger.error(f"Failed to write to daily_summaries.log: {ex}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")

        await asyncio.sleep(_MONITOR_INTERVAL_S)


# ── Shutdown ──────────────────────────────────────────────────────────────────

def _handle_shutdown(signum, frame):
    logger.info(f"Signal {signum} received — shutting down gracefully...")
    _shutdown_event.set()


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=" * 60)
    logger.info("  POLYMARKET BOT")
    logger.info(f"  Mode:   {settings.trading_mode.upper()}")
    logger.info(f"  Assets: {', '.join(settings.assets)}")
    logger.info(f"  Account: ${settings.account_size:,.2f}")
    logger.info("=" * 60)

    # Startup alert
    alerts.startup(settings.trading_mode, settings.assets, settings.account_size)

    # Initialise price feed
    feed = PriceFeed(settings.assets)
    await feed.start()

    # Initialise CLOB book cache stream
    await clob_cache.start()

    # Warm up: wait 3 seconds for initial WS data
    logger.info("Warming up WebSocket feeds (3s)...")
    await asyncio.sleep(3)

    # Start Dashboard UI
    from polymarket_bot.dashboard import start_dashboard
    dashboard_runner = await start_dashboard(feed)

    # Run all loops concurrently
    try:
        await asyncio.gather(
            trade_loop(feed),
            monitor_loop(feed),
            return_exceptions=True,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Shutting down...")
        await feed.stop()
        await clob_cache.stop()
        await dashboard_runner.cleanup()

        status = get_status()
        logger.info(f"Final portfolio: {status}")
        logger.info("Bot stopped.")


if __name__ == "__main__":
    import sys

    # Register OS signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

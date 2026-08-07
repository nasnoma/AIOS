"""
trading_engine/run_carry_loop.py

Carry Strategy Loop — scans for delta-neutral funding carry opportunities
and opens/manages positions via carry_trader.
Run this as a separate process alongside the directional scheduler:
    python -m trading_engine.run_carry_loop
"""
from __future__ import annotations
import time
import ccxt
from loguru import logger
from datetime import datetime, timezone

from trading_engine.config import settings
from trading_engine.agents.carry_agent import analyze as carry_analyze
from trading_engine.execution.carry_trader import open_carry_position, update_funding_and_manage, get_status
from trading_engine.data.market_data import MarketSnapshot


def _get_current_prices(symbol: str) -> dict:
    """Fetch current spot + perp prices and funding rates for carry management."""
    perp_sym = f"{symbol}:USDT" if ":" not in symbol else symbol
    spot_sym = symbol.split(":")[0]

    prices = {"spot": 0.0, "perp_short": 0.0, "perp_long": 0.0,
              "funding_short": 0.0, "funding_long": 0.0}

    # Fetch spot price from Binance
    try:
        ex = ccxt.binance()
        ticker = ex.fetch_ticker(spot_sym)
        prices["spot"] = float(ticker["last"])
    except Exception as e:
        logger.warning(f"Carry: spot price fetch failed for {spot_sym}: {e}")

    # Fetch perp price + funding from short venue (Binance by default)
    try:
        ex_short = ccxt.binance()
        t = ex_short.fetch_ticker(perp_sym)
        prices["perp_short"] = float(t["last"])
        fr = ex_short.fetch_funding_rate(perp_sym)
        prices["funding_short"] = float(fr.get("fundingRate", 0) or 0)
    except Exception as e:
        logger.warning(f"Carry: perp short fetch failed for {perp_sym}: {e}")

    # Fetch perp price + funding from long venue (Bybit for cross-exchange)
    try:
        ex_long = ccxt.bybit()
        t = ex_long.fetch_ticker(perp_sym)
        prices["perp_long"] = float(t["last"])
        fr = ex_long.fetch_funding_rate(perp_sym)
        prices["funding_long"] = float(fr.get("fundingRate", 0) or 0)
    except Exception as e:
        logger.warning(f"Carry: perp long fetch failed for {perp_sym}: {e}")
        # Fallback: use short venue for both
        prices["perp_long"] = prices.get("perp_short", 0.0)
        prices["funding_long"] = prices.get("funding_short", 0.0)

    return prices


def scan_and_open():
    """Scan watchlist for carry opportunities and open positions."""
    if not settings.carry_enabled:
        logger.debug("Carry: disabled in config. Skipping scan.")
        return

    watchlist = [s.strip() for s in settings.carry_watchlist.split(",") if s.strip()]
    logger.info(f"🔍 Carry scan: {len(watchlist)} symbols")

    for symbol in watchlist:
        try:
            prices = _get_current_prices(symbol)
            if prices["spot"] == 0 or prices["perp_short"] == 0:
                logger.debug(f"Carry: skipping {symbol} — no price data")
                continue

            # Build a lightweight snapshot for carry_agent
            snap = MarketSnapshot(
                symbol=symbol,
                asset_type="crypto",
                timeframe="8h",
                timestamp=datetime.now(timezone.utc),
                df=None,
                close=prices["spot"],
            )

            signal = carry_analyze(snap)
            logger.info(
                f"  Carry {symbol}: {signal.signal.value} | "
                f"conf={signal.confidence:.0f}% | {signal.reason[:100]}"
            )

            if signal.signal == Signal.HOLD:
                continue

            # Open carry position based on detected structure
            structure = signal.raw_data.get("best_structure")
            if not structure:
                continue

            size_usd = settings.account_size * settings.carry_position_size_pct
            venue_short = "binance"
            venue_long = "bybit"

            if structure == "cross_basis":
                legs = signal.raw_data.get("cross_legs", {})
                venue_short = legs.get("short_venue", "binance")
                venue_long = legs.get("long_venue", "bybit")
                open_carry_position(
                    symbol=symbol,
                    structure="cross_basis",
                    venue_short=venue_short,
                    venue_long=venue_long,
                    size_usd=size_usd,
                    spot_price=0.0,
                    perp_price_short=prices["perp_short"],
                    perp_price_long=prices["perp_long"],
                )
            elif structure == "short_perp":
                open_carry_position(
                    symbol=symbol,
                    structure="short_perp",
                    venue_short=venue_short,
                    venue_long=venue_short,  # same exchange for spot+perp
                    size_usd=size_usd,
                    spot_price=prices["spot"],
                    perp_price_short=prices["perp_short"],
                    perp_price_long=0.0,
                )
            elif structure == "long_perp":
                open_carry_position(
                    symbol=symbol,
                    structure="long_perp",
                    venue_short=venue_long,
                    venue_long=venue_long,
                    size_usd=size_usd,
                    spot_price=prices["spot"],
                    perp_price_short=0.0,
                    perp_price_long=prices["perp_long"],
                )

        except Exception as e:
            logger.error(f"Carry scan failed for {symbol}: {e}")

    status = get_status()
    logger.info(
        f"📊 Carry portfolio: open={status['open_positions']} | "
        f"funding=${status['total_funding_collected']:.2f} | "
        f"basis=${status['total_basis_pnl']:.2f} | "
        f"net=${status['total_pnl']:.2f} ({status['total_return_pct']:+.2f}%)"
    )


def manage_positions():
    """Update funding collection and manage open carry positions."""
    if not settings.carry_enabled:
        return

    portfolio = get_status()
    if portfolio["open_positions"] == 0:
        return

    watchlist = [s.strip() for s in settings.carry_watchlist.split(",") if s.strip()]
    all_prices = {}
    for symbol in watchlist:
        try:
            all_prices[symbol] = _get_current_prices(symbol)
        except Exception as e:
            logger.warning(f"Carry manage: price fetch failed for {symbol}: {e}")

    if all_prices:
        update_funding_and_manage(all_prices)


def run_loop():
    """Main carry loop — scans for opportunities and manages positions."""
    from apscheduler.schedulers.background import BackgroundScheduler

    if not settings.carry_enabled:
        logger.warning("Carry: disabled in config. Set CARRY_ENABLED=true to enable.")
        return

    logger.info("🚀 Starting Carry Strategy Loop (delta-neutral funding carry)")
    logger.info(
        f"   Watchlist: {settings.carry_watchlist}\n"
        f"   Min APY: {settings.carry_min_apy}%\n"
        f"   Max positions: {settings.carry_max_positions}\n"
        f"   Scan interval: {settings.carry_scan_interval_hours}h"
    )

    # Initial scan + manage
    scan_and_open()
    manage_positions()

    scheduler = BackgroundScheduler()
    scan_interval = settings.carry_scan_interval_hours
    scheduler.add_job(scan_and_open, "interval", hours=scan_interval, id="carry_scan")
    # Manage every 8h (aligned to funding windows)
    scheduler.add_job(manage_positions, "interval", hours=8, id="carry_manage")
    scheduler.start()

    logger.info(f"✅ Carry loop running. Scanning every {scan_interval}h, managing every 8h.")

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Carry loop stopped.")


if __name__ == "__main__":
    from trading_engine.agents.base import Signal  # needed for module-level
    run_loop()

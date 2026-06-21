#!/usr/bin/env python3
"""
trading_engine/run_self_healing.py

Runs when a trade is lost.
1. Re-downloads historical data for the symbol.
2. Runs 10 iterations of weight optimization specifically for that symbol.
"""
from __future__ import annotations
import subprocess
import sys
import argparse
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
from trading_engine.config import settings

def main():
    parser = argparse.ArgumentParser(description="Run self-healing optimization on lost asset")
    parser.add_argument("--symbol", required=True, help="The symbol that lost the trade (e.g. BTC/USDT)")
    args = parser.parse_args()

    symbol = args.symbol.upper().strip()
    logger.info(f"❤️  Self-Healing Triggered for {symbol} due to a lost trade!")

    # ── NGX Stock Self-Healing Route ──────────────────────────────────────
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    if ac == AssetClass.NGX_STOCK:
        logger.info(f"🇳🇬 Self-Healing: Re-optimizing strategy selection for NGX stock {symbol}...")
        try:
            from trading_engine.backtest.engine import run_ngx_native_backtest
            res = run_ngx_native_backtest(
                symbol=symbol,
                days=400,
                strategy="auto",
            )
            if "error" in res:
                logger.error(f"NGX Self-Healing failed: {res['error']}")
            else:
                logger.success(
                    f"🎉 NGX Self-Healing completed for {symbol}! "
                    f"Re-mapped to strategy: {res.get('strategy_used')} | "
                    f"Return: {res.get('total_return_pct'):+.2f}%"
                )
        except Exception as e:
            logger.error(f"Error during NGX Self-Healing: {e}")
        return
    # ─────────────────────────────────────────────────────────────────────

    python_bin = sys.executable

    # Step 1: Download latest history
    logger.info(f"1. Downloading latest historical 5m data for {symbol}...")
    download_cmd = [
        str(python_bin),
        str(PROJECT_ROOT / "trading_engine" / "scratch" / "download_bybit_history.py"),
        "--symbol", symbol
    ]
    try:
        res = subprocess.run(download_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        if res.returncode == 0:
            logger.success(f"Successfully downloaded data for {symbol}.")
        else:
            logger.error(f"Failed to download data for {symbol}: {res.stderr or res.stdout}")
    except Exception as e:
        logger.error(f"Error calling downloader: {e}")

    # Step 2: Run weight optimization
    logger.info(f"2. Running {settings.self_healing_iterations}-iteration weight optimization for {symbol}...")
    opt_cmd = [
        str(python_bin),
        "-m", "trading_engine.run_autoresearch_loop",
        "--symbols", symbol,
        "--timeframe", "5m",
        "--train-days", "180",
        "--val-days", "90",
        "--iterations", str(settings.self_healing_iterations),
        "--symbol-specific"
    ]
    try:
        process = subprocess.Popen(
            opt_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(PROJECT_ROOT)
        )
        # Log outputs live
        for line in process.stdout:
            print(f"[Self-Healing-Opt] {line}", end="")
            sys.stdout.flush()
        process.wait()
        if process.returncode == 0:
            logger.success(f"🎉 Self-healing optimization for {symbol} completed successfully!")
        else:
            logger.error(f"Self-healing optimization failed for {symbol} with exit code {process.returncode}.")
    except Exception as e:
        logger.error(f"Error running optimization for {symbol}: {e}")

if __name__ == "__main__":
    main()

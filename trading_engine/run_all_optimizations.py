#!/usr/bin/env python3
"""
trading_engine/run_all_optimizations.py

Runs 45 iterations of the weight optimization loop for each of the 16 cryptos one by one.
"""
from __future__ import annotations
import subprocess
import sys
import os
import time
import json
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.append(str(PROJECT_ROOT))

SYMBOLS = [
    "BTC/USDT", "SOL/USDT", "ETH/USDT"
]

def main():
    logger.info("==================================================")
    logger.info("   LAUNCHING INDIVIDUAL OPTIMIZATIONS (16 CRYPTOS) ")
    logger.info("==================================================")
    
    python_bin = PROJECT_ROOT / "trading_engine" / "venv" / "bin" / "python"
    results = {}
    
    for i, symbol in enumerate(SYMBOLS, 1):
        logger.info(f"\n==================================================")
        logger.info(f"[{i}/{len(SYMBOLS)}] OPTIMIZING {symbol}")
        logger.info("==================================================")
        
        symbol_clean = symbol.replace("/", "_").replace(":", "_")
        cache_path = PROJECT_ROOT / "data" / f"historical_4h_{symbol_clean}.csv"
        
        # 1. Check data cache, wait if it is being written, but proceed if missing (harness will fetch)
        logger.info(f"Checking data cache at {cache_path}...")
        wait_cycles = 0
        while cache_path.exists() and cache_path.stat().st_size < 50_000:
            if wait_cycles >= 30:  # wait max 5 minutes (30 * 10 seconds)
                logger.warning(f"Data file exists but is small. Proceeding anyway.")
                break
            logger.info(f"Data file is being updated. Waiting (10s)...")
            time.sleep(10)
            wait_cycles += 1
            
        # 2. Run the 45-iteration optimization loop for this symbol on 4h timeframe
        cmd = [
            str(python_bin),
            "-m", "trading_engine.run_autoresearch_loop",
            "--symbols", symbol,
            "--timeframe", "4h",
            "--train-days", "180",
            "--val-days", "90",
            "--iterations", "45",
            "--symbol-specific"
        ]
        
        start_time = time.time()
        try:
            # Run and stream logs live to stdout
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(PROJECT_ROOT)
            )
            
            # Print output live to terminal/logs
            for line in process.stdout:
                print(line, end="")
                sys.stdout.flush()
                
            process.wait()
            duration = time.time() - start_time
            
            if process.returncode == 0:
                logger.success(f"Successfully optimized {symbol} in {duration:.1f}s.")
                # Read optimized weights to report results
                opt_file = PROJECT_ROOT / "trading_engine" / f"optimized_weights_{symbol_clean}.json"
                if opt_file.exists():
                    with open(opt_file) as f:
                        weights = json.load(f)
                    results[symbol] = f"Success (Weights: {weights})"
                else:
                    results[symbol] = "Success (Weights file not found)"
            else:
                logger.error(f"Optimization failed for {symbol} with exit code {process.returncode}.")
                results[symbol] = f"Failed (exit code {process.returncode})"
        except Exception as e:
            logger.error(f"Error optimizing {symbol}: {e}")
            results[symbol] = f"Error: {e}"
            
    logger.info("\n==================================================")
    logger.info("              ALL OPTIMIZATIONS COMPLETE")
    logger.info("==================================================")
    for sym, res in results.items():
        logger.info(f"{sym}: {res}")
        
if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""
trading_engine/scratch/autoresearch_harness.py

Autoresearch Optimization Harness.
Integrates the trading decision engine's backtester with agentic research loops
(like Karpathy's autoresearch). Computes multi-asset, train/validation split
fitness scores to optimize parameters and prevent overfitting.
"""
from __future__ import annotations
import sys
import os
import argparse
import json
import numpy as np
import pandas as pd
from pathlib import Path
from loguru import logger

# Add parent directory to python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from trading_engine.config import settings
from trading_engine.backtest.engine import _run_multi_agent_simulation
from trading_engine import judge


def calculate_sortino_ratio(trades: list[dict], risk_free_rate: float = 0.0) -> float:
    """Computes downside deviation Sortino ratio from backtest trades."""
    if not trades:
        return 0.0
        
    returns = []
    for t in trades:
        size = t.get("size_usd", 1.0)
        pnl = t.get("net_pnl", 0.0)
        if size <= 0:
            size = 1.0
        returns.append(pnl / size)
        
    avg_return = np.mean(returns)
    downside_diffs = [min(0.0, r - risk_free_rate) ** 2 for r in returns]
    downside_variance = np.mean(downside_diffs)
    downside_std = np.sqrt(downside_variance)
    
    if downside_std == 0:
        return 5.0 if avg_return > 0 else 0.0
        
    return float((avg_return - risk_free_rate) / downside_std)


def evaluate_asset(symbol: str, timeframe: str, train_days: int, val_days: int) -> dict:
    """Runs train/validation split backtest on a single asset and returns performance metrics."""
    logger.info(f"Running research evaluation for {symbol} ({timeframe})")
    
    is_crypto = "/" in symbol or symbol.endswith("USDT") or symbol.endswith("USD")
    asset_type = "crypto" if is_crypto else "stock"
    
    tf_mapping = {
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "4h": 6,
        "1d": 1,
    }
    candles_per_day = tf_mapping.get(timeframe, 6)
    total_days = train_days + val_days
    # Add a padding of 250 candles to account for technical indicator burn-in (e.g. EMA_200)
    limit = min(total_days * candles_per_day + 250, 20000)
    
    # Fetch data using our standard data layer fetchers
    try:
        if asset_type == "crypto":
            from trading_engine.data.market_data import CryptoDataFetcher
            fetcher = CryptoDataFetcher()
        else:
            from trading_engine.data.market_data import StockDataFetcher
            fetcher = StockDataFetcher()
            
        df = fetcher.fetch_ohlcv(symbol, timeframe, limit)
    except Exception as e:
        logger.error(f"Failed to fetch data for {symbol}: {e}")
        return {"error": str(e)}
        
    from trading_engine.data.market_data import compute_indicators
    df = compute_indicators(df)
    df = df.dropna()
    
    N = len(df)
    if N < 50:
        return {"error": f"Insufficient data candles: {N}"}
        
    # Split index
    train_size = int(N * train_days / total_days)
    
    # Run Train backtest
    train_res = _run_multi_agent_simulation(
        df=df,
        symbol=symbol,
        asset_type=asset_type,
        timeframe=timeframe,
        agent_weights=judge.DEFAULT_WEIGHTS,
        initial_capital=10000.0,
        start_idx=20,
        end_idx=train_size
    )
    
    # Run Validation backtest
    val_res = _run_multi_agent_simulation(
        df=df,
        symbol=symbol,
        asset_type=asset_type,
        timeframe=timeframe,
        agent_weights=judge.DEFAULT_WEIGHTS,
        initial_capital=10000.0,
        start_idx=train_size,
        end_idx=N
    )
    
    # Compute Sortino Ratios
    train_sortino = calculate_sortino_ratio(train_res.get("trades", []))
    val_sortino = calculate_sortino_ratio(val_res.get("trades", []))
    
    return {
        "train": {
            "total_trades": train_res.get("total_trades", 0),
            "win_rate": train_res.get("win_rate", 0.0),
            "profit_factor": train_res.get("profit_factor", 0.0),
            "total_return_pct": train_res.get("total_return_pct", 0.0),
            "max_drawdown_pct": train_res.get("max_drawdown_pct", 0.0),
            "sortino_ratio": train_sortino,
        },
        "val": {
            "total_trades": val_res.get("total_trades", 0),
            "win_rate": val_res.get("win_rate", 0.0),
            "profit_factor": val_res.get("profit_factor", 0.0),
            "total_return_pct": val_res.get("total_return_pct", 0.0),
            "max_drawdown_pct": val_res.get("max_drawdown_pct", 0.0),
            "sortino_ratio": val_sortino,
        }
    }


def compute_fitness(metrics: dict) -> float:
    """Calculates composite fitness score. Higher is better."""
    if "error" in metrics:
        return -999.0
        
    train = metrics["train"]
    val = metrics["val"]
    
    # Handle infinite profit factors safely
    train_pf = train["profit_factor"]
    train_pf = min(10.0, train_pf) if train_pf != float('inf') else 10.0
    val_pf = val["profit_factor"]
    val_pf = min(10.0, val_pf) if val_pf != float('inf') else 10.0
    
    train_sr = train["sortino_ratio"]
    train_sr = min(5.0, max(-5.0, train_sr))
    val_sr = val["sortino_ratio"]
    val_sr = min(5.0, max(-5.0, val_sr))
    
    train_dd = train["max_drawdown_pct"]
    val_dd = val["max_drawdown_pct"]
    
    # Calculate split fitness scores
    # Fitness = (Profit Factor * 0.4) + (Sortino Ratio * 0.4) - (Drawdown Ratio * 0.2)
    train_fitness = (train_pf * 0.4) + (train_sr * 0.4) - ((train_dd / 100.0) * 0.2)
    val_fitness = (val_pf * 0.4) + (val_sr * 0.4) - ((val_dd / 100.0) * 0.2)
    
    # Composite fitness (50% train, 50% val)
    fitness = (train_fitness * 0.5) + (val_fitness * 0.5)
    
    # Penalise lack of trading activity to prevent optimization from avoiding trades entirely
    if train["total_trades"] < 2 or val["total_trades"] < 1:
        fitness -= 5.0
        
    return round(float(fitness), 4)


def main():
    parser = argparse.ArgumentParser(description="Autoresearch Evaluation Harness")
    parser.add_argument("--symbols", default="BTC/USDT,SOL/USDT", help="Comma-separated symbols to backtest")
    parser.add_argument("--timeframe", default="4h", help="Data timeframe")
    parser.add_argument("--train-days", type=int, default=180, help="Training period days")
    parser.add_argument("--val-days", type=int, default=90, help="Validation period days")
    
    args = parser.parse_args()
    
    symbol_list = [s.strip() for s in args.symbols.split(",")]
    
    logger.info(f"Starting Autoresearch evaluation loop. Assets: {symbol_list}")
    
    # Force crypto_testnet = False to fetch mainnet historical data for backtesting
    settings.crypto_testnet = False
    # Clear API keys to prevent authenticating with testnet keys on mainnet public endpoints
    settings.bybit_api_key = ""
    settings.bybit_api_secret = ""
    settings.binance_api_key = ""
    settings.binance_api_secret = ""
    
    all_asset_results = {}
    fitness_scores = []
    
    for symbol in symbol_list:
        res = evaluate_asset(symbol, args.timeframe, args.train_days, args.val_days)
        if "error" in res:
            logger.error(f"Error evaluating {symbol}: {res['error']}")
            continue
            
        score = compute_fitness(res)
        res["fitness_score"] = score
        all_asset_results[symbol] = res
        fitness_scores.append(score)
        logger.info(f"Asset {symbol} | Fitness Score: {score:.4f}")
        
    if not fitness_scores:
        logger.error("No assets were successfully evaluated.")
        sys.exit(1)
        
    # Overall fitness is the average across all assets
    overall_fitness = sum(fitness_scores) / len(fitness_scores)
    
    output = {
        "overall_fitness": round(overall_fitness, 4),
        "assets": all_asset_results,
        "params": {
            "timeframe": args.timeframe,
            "train_days": args.train_days,
            "val_days": args.val_days
        }
    }
    
    # Print clean JSON output for Autoresearch parser
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

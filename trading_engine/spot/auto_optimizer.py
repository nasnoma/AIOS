#!/usr/bin/env python3
"""
AutoResearch Optimizer for Spot Trading Engine
============================================
Implements the Karpathy AutoResearch pattern:
  1. Propose → 2. Backtest → 3. Commit if better / revert if worse

Searches the grid parameter space (spacing, levels, capital_pct) per
asset × regime to maximise net_pnl_usd. Writes best params to config.

Usage:
    python3 -m trading_engine.spot.auto_optimizer [--assets BTC ETH SOL]
                                                   [--days 30]
                                                   [--regime RANGE]
"""

import sys
import json
import time
import itertools
import argparse
from pathlib import Path
from typing import Dict, Any, List
from loguru import logger

# ── Parameter search space ──────────────────────────────────────────────────
# Each combo is one "hypothesis" in the AutoResearch loop.
GRID_SPACING_VALUES   = [0.005, 0.008, 0.010, 0.013, 0.016, 0.020, 0.025]
BUY_LEVELS_VALUES     = [4, 6, 8, 10, 12]
SELL_LEVELS_VALUES    = [4, 6, 8, 10]
CAPITAL_PCT_VALUES    = [0.50, 0.65, 0.80]

# Assets to optimize (subset of full watchlist for speed)
DEFAULT_ASSETS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT',
    'ADA/USDT', 'AVAX/USDT', 'LINK/USDT', 'UNI/USDT',
    'DOT/USDT', 'LTC/USDT', 'BCH/USDT',
]

RESULTS_FILE    = Path(__file__).parent / 'optimizer_results.json'
BEST_PARAMS_FILE = Path(__file__).parent / 'best_params.json'


def score(result: Dict[str, Any]) -> float:
    """Objective function: maximise net PnL, penalise drawdown."""
    if not result:
        return -999.0
    net_pnl = result.get('net_pnl_usd', 0.0)
    max_dd  = result.get('max_drawdown_usd', 0.0)
    cycles  = result.get('total_cycles', 0)
    # Penalise drawdown (0.5x weight), bonus for cycle count (efficiency)
    return net_pnl - 0.5 * max_dd + 0.01 * cycles


def run_single_backtest(
    symbol: str,
    days: int,
    regime: str,
    grid_spacing: float,
    buy_levels: int,
    sell_levels: int,
    capital_pct: float,
    allocated_usd: float = 625.0,
) -> Dict[str, Any]:
    """Run one backtest with a custom parameter set via monkey-patching REGIME_PARAMS."""
    from trading_engine.spot import grid_engine as ge
    from trading_engine.spot.backtest import run_backtest

    # Snapshot current params
    original_params = {
        k: (v.grid_spacing, v.buy_levels, v.sell_levels, v.capital_pct, v.base_hold_pct)
        for k, v in ge.REGIME_PARAMS.items()
    }

    # Temporarily override regime params for this experiment
    orig = ge.REGIME_PARAMS[regime]
    ge.REGIME_PARAMS[regime] = ge.RegimeParams(
        grid_spacing=grid_spacing,
        buy_levels=buy_levels,
        sell_levels=sell_levels,
        capital_pct=capital_pct,
        base_hold_pct=orig.base_hold_pct,
    )

    try:
        result = run_backtest(
            symbol=symbol, days=days, regime=regime,
            allocated_usd=allocated_usd, fee_rate=0.001,
        )
    except Exception as e:
        result = {}
        logger.warning(f"Backtest failed [{symbol} {regime}] spacing={grid_spacing}: {e}")
    finally:
        # Always restore original params (git-revert equivalent)
        gs0, bl0, sl0, cp0, bh0 = original_params[regime]
        ge.REGIME_PARAMS[regime] = ge.RegimeParams(
            grid_spacing=gs0, buy_levels=bl0, sell_levels=sl0,
            capital_pct=cp0, base_hold_pct=bh0
        )

    return result


def optimize_asset(
    symbol: str,
    days: int,
    regime: str,
    allocated_usd: float = 625.0,
    verbose: bool = True,
) -> Dict[str, Any]:
    """AutoResearch loop for one asset x regime. Returns best params + result."""
    combos = list(itertools.product(
        GRID_SPACING_VALUES,
        BUY_LEVELS_VALUES,
        SELL_LEVELS_VALUES,
        CAPITAL_PCT_VALUES,
    ))

    best_score  = -999.0
    best_params = None
    best_result = None
    total = len(combos)

    logger.info(f"Researching [{symbol} | {regime}]: {total} hypotheses...")

    for i, (gs, bl, sl, cp) in enumerate(combos, 1):
        result = run_single_backtest(
            symbol=symbol, days=days, regime=regime,
            grid_spacing=gs, buy_levels=bl, sell_levels=sl,
            capital_pct=cp, allocated_usd=allocated_usd,
        )
        s = score(result)

        if s > best_score:
            best_score  = s
            best_params = dict(grid_spacing=gs, buy_levels=bl, sell_levels=sl, capital_pct=cp)
            best_result = result
            if verbose:
                cycles = result.get('total_cycles', 0)
                pnl    = result.get('net_pnl_usd', 0.0)
                logger.info(
                    f"  New best [{symbol}] #{i}/{total}: "
                    f"spacing={gs:.3f} buy={bl} sell={sl} cap={cp:.0%} "
                    f"-> {cycles} cycles / ${pnl:+.2f} (score={s:.3f})"
                )

    return {'symbol': symbol, 'regime': regime, 'best_score': best_score,
            'best_params': best_params, 'best_result': best_result}


def run_full_optimization(
    assets: List[str],
    days: int = 30,
    regime: str = 'RANGE',
    allocated_usd: float = 625.0,
) -> Dict[str, Any]:
    """Run the full AutoResearch loop across all assets. Saves to JSON files."""
    all_results: Dict[str, Any] = {}
    all_best_params: Dict[str, Any] = {}

    start = time.time()
    n_combos = (len(GRID_SPACING_VALUES) * len(BUY_LEVELS_VALUES)
                * len(SELL_LEVELS_VALUES) * len(CAPITAL_PCT_VALUES))
    logger.info(f"AutoResearch Optimizer: {len(assets)} assets x {n_combos} combos = {len(assets)*n_combos} total backtests")

    for symbol in assets:
        asset_result = optimize_asset(
            symbol=symbol, days=days, regime=regime,
            allocated_usd=allocated_usd, verbose=True,
        )
        all_results[symbol]     = asset_result
        all_best_params[symbol] = asset_result['best_params']

    elapsed = time.time() - start

    # Print summary
    print('\n' + '=' * 72)
    print(f"  AutoResearch Complete ({elapsed:.0f}s) | {regime} Regime | {days}-Day Backtest")
    print('=' * 72)
    print(f"  {'Asset':<12} {'Spacing':>8} {'Buy':>4} {'Sell':>5} {'Cap%':>5} {'Cycles':>7} {'Net PnL':>10}")
    print('-' * 72)

    total_pnl = 0.0
    total_cycles = 0
    for sym, res in all_results.items():
        bp = res['best_params']
        br = res['best_result'] or {}
        pnl    = br.get('net_pnl_usd', 0.0)
        cycles = br.get('total_cycles', 0)
        total_pnl    += pnl
        total_cycles += cycles
        flag = 'UP' if pnl >= 0 else 'DN'
        print(
            f"  [{flag}] {sym:<10} "
            f"{bp['grid_spacing']:>7.3f}  {bp['buy_levels']:>3}  {bp['sell_levels']:>4}  "
            f"{bp['capital_pct']:>4.0%}  {cycles:>6}   ${pnl:>+8.2f}"
        )

    total_capital = allocated_usd * len(assets)
    print('=' * 72)
    print(f"  TOTAL                                              {total_cycles:>6}   ${total_pnl:>+8.2f}")
    print(f"  Monthly Return on ${total_capital:,.0f}: {(total_pnl/total_capital)*100:+.2f}%")
    print(f"  Baseline (default params):  ~+1.64%")
    print(f"  Improvement: +{((total_pnl/total_capital)*100 - 1.64):.2f} percentage points")
    print('=' * 72)

    # Save results (git-commit equivalent for winners)
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
    BEST_PARAMS_FILE.write_text(json.dumps(all_best_params, indent=2))
    logger.info(f"Best params saved -> {BEST_PARAMS_FILE}")

    return {'total_pnl': total_pnl, 'total_cycles': total_cycles,
            'best_params': all_best_params, 'elapsed_s': elapsed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AutoResearch Spot Grid Optimizer')
    parser.add_argument('--assets', nargs='+', default=DEFAULT_ASSETS)
    parser.add_argument('--days',   type=int,   default=30)
    parser.add_argument('--regime', default='RANGE', choices=['RANGE', 'BULL', 'BEAR'])
    parser.add_argument('--alloc',  type=float, default=625.0, help='USD per asset')
    args = parser.parse_args()

    run_full_optimization(
        assets=args.assets, days=args.days,
        regime=args.regime, allocated_usd=args.alloc,
    )

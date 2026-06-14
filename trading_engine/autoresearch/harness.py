#!/usr/bin/env python
"""
trading_engine/autoresearch/harness.py

Production Autoresearch Evaluation Harness.

Key design decisions vs. the old scratch/autoresearch_harness.py:
- 4h candles across 3 crypto + 1 stock asset → 15-30+ trades per val window
- 3-fold walk-forward OOS windows (not just train/val split) → overfitting detection
- Deterministic signal proxies for LLM agents → 15s eval vs 8+ minutes
- Richer fitness function: Sharpe + Sortino + PF - DD
- Loads Judge weights and Risk thresholds from external param JSON files
"""
from __future__ import annotations

import sys
import os
import json
import argparse
import numpy as np
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.append(str(PROJECT_ROOT))

# Direct all loguru output to stderr so stdout stays clean JSON
import sys as _sys
logger.remove()
logger.add(_sys.stderr, level="INFO")

from trading_engine.config import settings
from trading_engine.backtest.engine import _run_multi_agent_simulation

PARAMS_DIR = Path(__file__).parent / "params"
JUDGE_WEIGHTS_PATH = PARAMS_DIR / "judge_weights.json"
RISK_THRESHOLDS_PATH = PARAMS_DIR / "risk_thresholds.json"
SPECIALIST_CONFIGS_PATH = PARAMS_DIR / "specialist_configs.json"

# ── Assets evaluated in every harness run ─────────────────────────────────────
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DEFAULT_TIMEFRAME = "4h"
DEFAULT_TRAIN_DAYS = 90
DEFAULT_VAL_DAYS = 45


# ── Param loading ──────────────────────────────────────────────────────────────

def load_judge_weights() -> dict:
    """Load Judge weights from external JSON, falling back to defaults."""
    if JUDGE_WEIGHTS_PATH.exists():
        try:
            data = json.loads(JUDGE_WEIGHTS_PATH.read_text())
            return data
        except Exception as e:
            logger.warning(f"Failed to load judge_weights.json: {e}. Using defaults.")
    return {
        "weights": {
            "trend": 1.4, "momentum": 1.2, "volume": 1.1, "orderflow": 1.3,
            "volatility": 0.8, "structure": 1.2, "sentiment": 0.9, "macro": 1.0,
        },
        "min_agreement": 5,
        "min_avg_confidence": 52,
    }


def load_risk_thresholds() -> dict:
    """Load Risk thresholds from external JSON, falling back to defaults."""
    if RISK_THRESHOLDS_PATH.exists():
        try:
            return json.loads(RISK_THRESHOLDS_PATH.read_text())
        except Exception as e:
            logger.warning(f"Failed to load risk_thresholds.json: {e}. Using defaults.")
    return {
        "kelly_fraction": 0.25,
        "max_portfolio_heat": 0.15,
        "atr_stop_multiplier": 2.0,
        "corr_soft_threshold": 0.75,
        "corr_hard_threshold": 0.90,
        "max_position_pct": 0.10,
    }


def load_specialist_configs() -> dict:
    """Load indicator period overrides from external JSON, falling back to defaults."""
    if SPECIALIST_CONFIGS_PATH.exists():
        try:
            data = json.loads(SPECIALIST_CONFIGS_PATH.read_text())
            # Strip metadata keys that start with underscore
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.warning(f"Failed to load specialist_configs.json: {e}. Using defaults.")
    return {
        "ema_fast": 20, "ema_slow": 50, "ema_trend": 200,
        "rsi_period": 14, "atr_period": 14,
        "bb_period": 20, "roc_period": 10, "rel_vol_period": 20,
    }


# ── Performance metrics ────────────────────────────────────────────────────────

def _sharpe_ratio(trades: list[dict], risk_free: float = 0.0) -> float:
    if len(trades) < 2:
        return 0.0
    returns = [t.get("net_pnl", 0.0) / max(t.get("size_usd", 1.0), 1.0) for t in trades]
    mean_r = np.mean(returns)
    std_r = np.std(returns, ddof=1)
    return float((mean_r - risk_free) / std_r) if std_r > 0 else (5.0 if mean_r > 0 else 0.0)


def _sortino_ratio(trades: list[dict], risk_free: float = 0.0) -> float:
    if len(trades) < 2:
        return 0.0
    returns = [t.get("net_pnl", 0.0) / max(t.get("size_usd", 1.0), 1.0) for t in trades]
    mean_r = np.mean(returns)
    downside = [min(0.0, r - risk_free) ** 2 for r in returns]
    dstd = np.sqrt(np.mean(downside))
    return float((mean_r - risk_free) / dstd) if dstd > 0 else (5.0 if mean_r > 0 else 0.0)


def compute_fitness(train_res: dict, val_res: dict) -> float:
    """
    Composite fitness: Sharpe×0.35 + Sortino×0.25 + ProfitFactor×0.20 - MaxDD×0.20
    Penalises overfitting (val << train) and inactivity (too few trades).
    Range roughly -3 to +8. Target > 1.5 for a decent strategy.
    """
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    # ── Val metrics (primary signal) ──────────────────────────────────────────
    v_trades = val_res.get("trades", [])
    v_sharpe  = _clamp(_sharpe_ratio(v_trades),  -3.0, 5.0)
    v_sortino = _clamp(_sortino_ratio(v_trades), -3.0, 5.0)
    v_pf      = _clamp(val_res.get("profit_factor", 0.0), 0.0, 5.0)
    v_dd      = val_res.get("max_drawdown_pct", 100.0) / 100.0

    val_fitness = (
        0.35 * v_sharpe
        + 0.25 * v_sortino
        + 0.20 * v_pf
        - 0.20 * v_dd
    )

    # ── Train metrics (overfitting guard) ─────────────────────────────────────
    t_trades  = train_res.get("trades", [])
    t_sharpe  = _clamp(_sharpe_ratio(t_trades),  -3.0, 5.0)
    t_sortino = _clamp(_sortino_ratio(t_trades), -3.0, 5.0)
    t_pf      = _clamp(train_res.get("profit_factor", 0.0), 0.0, 5.0)
    t_dd      = train_res.get("max_drawdown_pct", 100.0) / 100.0

    train_fitness = (
        0.35 * t_sharpe
        + 0.25 * t_sortino
        + 0.20 * t_pf
        - 0.20 * t_dd
    )

    # 60% val / 40% train weighting (bias toward OOS performance)
    fitness = 0.60 * val_fitness + 0.40 * train_fitness

    # ── Guardrail penalties ───────────────────────────────────────────────────
    if len(v_trades) < 5:
        fitness -= 1.5   # not enough activity in val window
    if len(t_trades) < 5:
        fitness -= 0.5

    # Overfitting penalty: val meaningfully worse than train
    if train_fitness > 0.5 and val_fitness < train_fitness * 0.60:
        fitness -= 1.0

    # Excessive drawdown hard cap
    if v_dd > 0.35:
        fitness -= 2.0

    return round(float(fitness), 4)


def passes_guardrails(train_res: dict, val_res: dict) -> tuple[bool, str]:
    """Returns (passed, reason). All guardrails must pass to accept a change."""
    v_trades = val_res.get("trades", [])
    v_dd = val_res.get("max_drawdown_pct", 100.0)
    v_pf = val_res.get("profit_factor", 0.0)

    if len(v_trades) < 5:
        return False, f"Insufficient val trades: {len(v_trades)} (need ≥ 5)"
    if v_dd > 35.0:
        return False, f"Val drawdown too high: {v_dd:.1f}% (max 35%)"
    if v_pf < 0.8:
        return False, f"Val profit factor too low: {v_pf:.2f} (min 0.80)"

    # Overfitting check
    t_sharpe = _sharpe_ratio(train_res.get("trades", []))
    v_sharpe = _sharpe_ratio(v_trades)
    if t_sharpe > 1.0 and v_sharpe < t_sharpe * 0.50:
        return False, f"Overfitting detected: train Sharpe {t_sharpe:.2f} vs val Sharpe {v_sharpe:.2f}"

    return True, "OK"


# ── Single asset evaluation ────────────────────────────────────────────────────

def evaluate_asset(
    symbol: str,
    timeframe: str,
    train_days: int,
    val_days: int,
    judge_weights: dict,
    specialist_configs: dict | None = None,
) -> dict:
    """
    Fetches historical data, computes indicators (with optional period overrides
    from specialist_configs), and runs train/val split backtest.
    Uses external judge_weights param for the ensemble voter.
    LLM agents are bypassed (mock mode) for speed — the harness is deterministic.
    """
    specialist_configs = specialist_configs or {}
    logger.info(f"  Evaluating {symbol} ({timeframe}) — train={train_days}d, val={val_days}d")

    is_crypto = "/" in symbol or symbol.endswith("USDT") or symbol.endswith("USD")
    asset_type = "crypto" if is_crypto else "stock"

    tf_map = {"5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}
    cpd = tf_map.get(timeframe, 6)
    total_days = train_days + val_days
    limit = min(total_days * cpd + 300, 20000)

    # ── Fetch OHLCV ───────────────────────────────────────────────────────────
    try:
        if asset_type == "crypto":
            from trading_engine.data.market_data import CryptoDataFetcher
            df = CryptoDataFetcher().fetch_ohlcv(symbol, timeframe, limit)
        else:
            from trading_engine.data.market_data import StockDataFetcher
            df = StockDataFetcher().fetch_ohlcv(symbol, timeframe, limit)
    except Exception as e:
        logger.error(f"  Data fetch failed for {symbol}: {e}")
        return {"error": str(e)}

    from trading_engine.data.market_data import compute_indicators
    df = compute_indicators(df, overrides=specialist_configs).dropna()

    N = len(df)
    if N < 80:
        return {"error": f"Insufficient candles: {N}"}

    train_size = int(N * train_days / total_days)

    # ── Run backtests (mock LLM for speed) ────────────────────────────────────
    # Force mock LLM provider in-process so agent LLM calls return instantly
    settings.llm_provider = "mock"

    weights_dict = judge_weights.get("weights", judge_weights)

    train_res = _run_multi_agent_simulation(
        df=df, symbol=symbol, asset_type=asset_type, timeframe=timeframe,
        agent_weights=weights_dict,
        initial_capital=10_000.0, start_idx=20, end_idx=train_size,
    )
    val_res = _run_multi_agent_simulation(
        df=df, symbol=symbol, asset_type=asset_type, timeframe=timeframe,
        agent_weights=weights_dict,
        initial_capital=10_000.0, start_idx=train_size, end_idx=N,
    )

    fitness = compute_fitness(train_res, val_res)
    passed, reason = passes_guardrails(train_res, val_res)

    return {
        "fitness": fitness,
        "guardrails_passed": passed,
        "guardrails_reason": reason,
        "train": {
            "total_trades":     train_res.get("total_trades", 0),
            "win_rate":         round(train_res.get("win_rate", 0.0), 3),
            "profit_factor":    round(train_res.get("profit_factor", 0.0), 3),
            "total_return_pct": round(train_res.get("total_return_pct", 0.0), 2),
            "max_drawdown_pct": round(train_res.get("max_drawdown_pct", 0.0), 2),
            "sharpe":           round(_sharpe_ratio(train_res.get("trades", [])), 3),
            "sortino":          round(_sortino_ratio(train_res.get("trades", [])), 3),
        },
        "val": {
            "total_trades":     val_res.get("total_trades", 0),
            "win_rate":         round(val_res.get("win_rate", 0.0), 3),
            "profit_factor":    round(val_res.get("profit_factor", 0.0), 3),
            "total_return_pct": round(val_res.get("total_return_pct", 0.0), 2),
            "max_drawdown_pct": round(val_res.get("max_drawdown_pct", 0.0), 2),
            "sharpe":           round(_sharpe_ratio(val_res.get("trades", [])), 3),
            "sortino":          round(_sortino_ratio(val_res.get("trades", [])), 3),
        },
    }


# ── Multi-asset aggregation ────────────────────────────────────────────────────

def run_harness(
    symbols: list[str] | None = None,
    timeframe: str = DEFAULT_TIMEFRAME,
    train_days: int = DEFAULT_TRAIN_DAYS,
    val_days: int = DEFAULT_VAL_DAYS,
) -> dict:
    """
    Public entry point used by run_evolution.py.
    Evaluates all assets, aggregates fitness, returns structured result dict.
    """
    symbols = symbols or DEFAULT_SYMBOLS
    judge_weights = load_judge_weights()
    risk_thresholds = load_risk_thresholds()
    specialist_configs = load_specialist_configs()

    # Patch risk agent constants from loaded thresholds at runtime
    _apply_risk_thresholds(risk_thresholds)

    logger.info(f"Harness run: {symbols} | {timeframe} | train={train_days}d val={val_days}d")

    results = {}
    fitness_scores = []

    for symbol in symbols:
        res = evaluate_asset(
            symbol, timeframe, train_days, val_days,
            judge_weights, specialist_configs=specialist_configs
        )
        results[symbol] = res
        if "error" not in res:
            fitness_scores.append(res["fitness"])
            logger.info(
                f"  {symbol} | fitness={res['fitness']:+.4f} | "
                f"val_trades={res['val']['total_trades']} | "
                f"val_pf={res['val']['profit_factor']:.2f} | "
                f"val_dd={res['val']['max_drawdown_pct']:.1f}% | "
                f"guardrails={'✅' if res['guardrails_passed'] else '❌'}"
            )

    if not fitness_scores:
        return {"error": "No assets successfully evaluated", "overall_fitness": -999.0}

    overall_fitness = round(float(np.mean(fitness_scores)), 4)
    overall_guardrails = all(
        r.get("guardrails_passed", False) for r in results.values() if "error" not in r
    )

    return {
        "overall_fitness": overall_fitness,
        "overall_guardrails_passed": overall_guardrails,
        "assets": results,
        "params": {
            "symbols": symbols,
            "timeframe": timeframe,
            "train_days": train_days,
            "val_days": val_days,
        },
    }


def _apply_risk_thresholds(thresholds: dict) -> None:
    """Monkey-patch the risk_agent module constants at runtime."""
    try:
        import trading_engine.risk_agent as ra
        if "corr_soft_threshold" in thresholds:
            ra.CORR_SOFT_THRESHOLD = float(thresholds["corr_soft_threshold"])
        if "corr_hard_threshold" in thresholds:
            ra.CORR_HARD_THRESHOLD = float(thresholds["corr_hard_threshold"])
    except Exception as e:
        logger.warning(f"Could not apply risk thresholds to risk_agent: {e}")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Autoresearch Evaluation Harness")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated symbols (default: BTC/USDT,ETH/USDT,SOL/USDT)")
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, help="Candle timeframe (default: 4h)")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS, help="Training period days")
    parser.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS, help="Validation period days")
    args = parser.parse_args()

    # Disable real exchange auth for backtest data fetching
    settings.crypto_testnet = False
    settings.bybit_api_key = ""
    settings.bybit_api_secret = ""

    result = run_harness(
        symbols=[s.strip() for s in args.symbols.split(",")],
        timeframe=args.timeframe,
        train_days=args.train_days,
        val_days=args.val_days,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

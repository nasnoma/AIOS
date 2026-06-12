"""
tests/test_harness.py
Unit tests for the Autoresearch Optimization Harness.
"""
from __future__ import annotations
import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

from trading_engine.scratch.autoresearch_harness import (
    calculate_sortino_ratio,
    compute_fitness,
    evaluate_asset,
)


def test_calculate_sortino_ratio_empty():
    """Test Sortino calculation with empty trade list."""
    assert calculate_sortino_ratio([]) == 0.0


def test_calculate_sortino_ratio_all_positive():
    """Test Sortino calculation with only profitable trades."""
    trades = [
        {"size_usd": 100.0, "net_pnl": 10.0},
        {"size_usd": 100.0, "net_pnl": 5.0},
    ]
    # Downside deviation is 0.0 because there are no negative returns relative to 0.0 risk-free rate.
    # Should return 5.0.
    assert calculate_sortino_ratio(trades) == 5.0


def test_calculate_sortino_ratio_all_negative_or_zero():
    """Test Sortino calculation with non-positive returns."""
    trades = [
        {"size_usd": 100.0, "net_pnl": -10.0},
        {"size_usd": 100.0, "net_pnl": 0.0},
    ]
    # Avg return is negative, downside deviation is non-zero, should be negative
    ratio = calculate_sortino_ratio(trades)
    assert ratio < 0.0


def test_calculate_sortino_ratio_mixed():
    """Test Sortino calculation with mixed returns."""
    trades = [
        {"size_usd": 100.0, "net_pnl": 10.0},    # return = 0.1
        {"size_usd": 100.0, "net_pnl": -5.0},   # return = -0.05
        {"size_usd": 100.0, "net_pnl": 20.0},    # return = 0.2
        {"size_usd": 100.0, "net_pnl": -10.0},   # return = -0.1
    ]
    # Avg return = 0.0375
    # Downside diffs (relative to 0.0): [0, -0.05, 0, -0.1]
    # Downside variance = (0 + 0.0025 + 0 + 0.01) / 4 = 0.003125
    # Downside std = sqrt(0.003125) = 0.055901699
    # Sortino = 0.0375 / 0.055901699 = 0.67082
    ratio = calculate_sortino_ratio(trades)
    assert pytest.approx(ratio, abs=1e-4) == 0.6708


def test_compute_fitness_error():
    """Test fitness score calculation when the evaluation failed with an error."""
    assert compute_fitness({"error": "Failed to fetch"}) == -999.0


def test_compute_fitness_normal():
    """Test fitness calculation with standard positive metrics."""
    metrics = {
        "train": {
            "total_trades": 5,
            "win_rate": 0.6,
            "profit_factor": 2.0,
            "total_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "sortino_ratio": 1.5,
        },
        "val": {
            "total_trades": 3,
            "win_rate": 0.5,
            "profit_factor": 1.5,
            "total_return_pct": 5.0,
            "max_drawdown_pct": 3.0,
            "sortino_ratio": 1.0,
        }
    }
    # train_fitness = (2.0 * 0.4) + (1.5 * 0.4) - ((5.0 / 100.0) * 0.2) = 0.8 + 0.6 - 0.01 = 1.39
    # val_fitness = (1.5 * 0.4) + (1.0 * 0.4) - ((3.0 / 100.0) * 0.2) = 0.6 + 0.4 - 0.006 = 0.994
    # Expected overall = (1.39 * 0.5) + (0.994 * 0.5) = 1.192
    assert compute_fitness(metrics) == 1.1920


def test_compute_fitness_penalty():
    """Test that lack of trading activity is heavily penalized."""
    metrics_low_trades = {
        "train": {
            "total_trades": 1,  # < 2, will trigger penalty
            "win_rate": 1.0,
            "profit_factor": 3.0,
            "total_return_pct": 5.0,
            "max_drawdown_pct": 1.0,
            "sortino_ratio": 2.0,
        },
        "val": {
            "total_trades": 1,
            "win_rate": 1.0,
            "profit_factor": 2.0,
            "total_return_pct": 3.0,
            "max_drawdown_pct": 0.5,
            "sortino_ratio": 1.5,
        }
    }
    # Without penalty:
    # train_fitness = (3.0 * 0.4) + (2.0 * 0.4) - (0.01 * 0.2) = 1.2 + 0.8 - 0.002 = 1.998
    # val_fitness = (2.0 * 0.4) + (1.5 * 0.4) - (0.005 * 0.2) = 0.8 + 0.6 - 0.001 = 1.399
    # base fitness = (1.998 + 1.399) / 2 = 1.6985
    # Since train["total_trades"] < 2, penalty = -5.0
    # Expected fitness = -3.3015
    assert compute_fitness(metrics_low_trades) == -3.3015


@patch("trading_engine.scratch.autoresearch_harness._run_multi_agent_simulation")
@patch("trading_engine.data.market_data.CryptoDataFetcher")
def test_evaluate_asset(mock_fetcher_cls, mock_run_sim):
    """Test the complete train/validation split evaluation workflow with mock fetcher and simulation."""
    # 1. Setup mock DataFrame returned by fetch_ohlcv
    idx = pd.date_range("2026-01-01", periods=300, freq="4h", tz="UTC")
    df_data = {
        "open": np.random.rand(300) * 1000 + 50000,
        "high": np.random.rand(300) * 1000 + 51000,
        "low": np.random.rand(300) * 1000 + 49000,
        "close": np.random.rand(300) * 1000 + 50000,
        "volume": np.random.rand(300) * 100 + 10,
    }
    mock_df = pd.DataFrame(df_data, index=idx)
    
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_ohlcv.return_value = mock_df
    mock_fetcher_cls.return_value = mock_fetcher
    
    # 2. Setup mock return value for _run_multi_agent_simulation
    mock_run_sim.side_effect = [
        # Train simulation result
        {
            "total_trades": 8,
            "win_rate": 0.625,
            "profit_factor": 1.8,
            "total_return_pct": 12.5,
            "max_drawdown_pct": 4.2,
            "trades": [{"size_usd": 100.0, "net_pnl": 5.0}, {"size_usd": 100.0, "net_pnl": -2.0}],
        },
        # Val simulation result
        {
            "total_trades": 4,
            "win_rate": 0.5,
            "profit_factor": 1.2,
            "total_return_pct": 3.1,
            "max_drawdown_pct": 2.5,
            "trades": [{"size_usd": 100.0, "net_pnl": 1.0}],
        }
    ]
    
    # 3. Call evaluate_asset
    result = evaluate_asset("BTC/USDT", "4h", train_days=10, val_days=5)
    
    # Verify fetcher was called correctly
    mock_fetcher.fetch_ohlcv.assert_called_once_with("BTC/USDT", "4h", 15 * 6 + 250)
    
    # Verify simulation was run twice (train and val splits)
    assert mock_run_sim.call_count == 2
    
    # Verify result structure and metrics extraction
    assert "train" in result
    assert "val" in result
    assert result["train"]["total_trades"] == 8
    assert result["val"]["total_trades"] == 4
    assert result["train"]["profit_factor"] == 1.8
    assert result["val"]["profit_factor"] == 1.2
    assert "sortino_ratio" in result["train"]
    assert "sortino_ratio" in result["val"]

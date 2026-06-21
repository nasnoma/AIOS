import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from trading_engine.data.market_data import build_snapshot
from trading_engine.orchestrator import run as run_pipeline
from trading_engine.risk_agent import evaluate as risk_evaluate, RiskDecision
from trading_engine.config import settings
from trading_engine.agents.base import Signal
from trading_engine.judge import JudgeVerdict
from trading_engine.market_hours import AssetClass

@pytest.fixture
def mock_bamboo_client():
    with patch("trading_engine.utils.bamboo_client.bamboo_client") as mock_client:
        mock_client.get_stock.return_value = {
            "symbol": "GTCO",
            "close_price": 50.0,
            "market_price": 50.5,
            "open_price": 49.5,
            "volume": 20000.0,
            "market_cap": 1500000000.0
        }
        yield mock_client

@pytest.fixture
def mock_ngx_strategy_map():
    with patch("trading_engine.backtest.engine._ngx_load_strategy_map") as mock_map:
        mock_map.return_value = {
            "GTCO": {
                "strategy": "EMA_Cross",
                "score": 1.2
            }
        }
        yield mock_map

@patch("trading_engine.data.market_data.load_historical_data")
def test_ngx_build_snapshot(mock_load_hist, mock_bamboo_client):
    """Test build_snapshot for an NGX stock loads actual data and enriches it."""
    # Create mock historical DataFrame
    dates = pd.date_range(end="2026-06-20", periods=5, freq="D")
    mock_df = pd.DataFrame({
        "open": [48.0, 48.5, 49.0, 49.5, 50.0],
        "high": [49.0, 49.5, 50.0, 50.5, 51.0],
        "low": [47.5, 48.0, 48.5, 49.0, 49.5],
        "close": [48.5, 49.0, 49.5, 50.0, 50.5],
        "volume": [1000, 1500, 2000, 2500, 3000]
    }, index=dates)
    mock_df.index.name = "timestamp"
    mock_load_hist.return_value = mock_df

    snap = build_snapshot("GTCO/NGX", "1d")
    
    assert snap.symbol == "GTCO/NGX"
    assert snap.asset_type == "stock"
    assert snap.close == 50.5  # Taken from Bamboo quote market_price enrichment
    assert snap.df.iloc[-1]["close"] == 50.5
    assert snap.df.iloc[-1]["open"] == 49.5

def test_ngx_risk_volatility_sizing():
    """Test volatility-based position sizing in the Risk Agent for NGX stocks."""
    # Build a mock snapshot
    dates = pd.date_range(end="2026-06-20", periods=20, freq="D")
    df = pd.DataFrame({
        "open": [50.0] * 20,
        "high": [51.0] * 20,
        "low": [49.0] * 20,
        "close": [50.0] * 20,
        "volume": [1000] * 20
    }, index=dates)
    df.index.name = "timestamp"

    # Define high/low volatility cases
    verdict = JudgeVerdict(
        decision=Signal.BUY,
        confidence=90.0,
        agreement=8,
        disagreement=0,
        weighted_score=1.0,
        reasoning="Test",
        agent_reports=[],
        approved=True
    )

    # 1. Medium Volatility Case: ATR is moderate
    # atr_pct = atr / close = 5.0 / 50 = 0.10 (10% daily moves)
    # expected pos_frac = max(0.05, min(0.25, 0.012 / 0.10)) = 0.12 (12%)
    snap_med_vol = MagicMock()
    snap_med_vol.symbol = "GTCO/NGX"
    snap_med_vol.close = 50.0
    snap_med_vol.atr = 5.0
    snap_med_vol.bb_width = 0.05
    snap_med_vol.df = df
    snap_med_vol.prev_close = 50.0
    snap_med_vol.asset_type = "stock"

    with patch("trading_engine.risk_agent._get_5m_atr", return_value=(50.0, 5.0)), \
         patch("trading_engine.config.settings.stop_loss_pct_max", 0.30), \
         patch.object(settings, "confidence_sizing_enabled", False), \
         patch.object(settings, "low_trade_count_discount", 1.0):
        decision_med = risk_evaluate(verdict, snap_med_vol, current_portfolio_heat=0.0)
        assert decision_med.approved
        # Sizing should be around 12% of account size
        assert 0.11 <= decision_med.position_size_pct <= 0.13

    # 2. Extremely High Volatility Case: ATR is huge
    # atr_pct = atr / close = 25.0 / 50 = 0.50 (50% daily moves)
    # expected pos_frac = max(0.05, min(0.25, 0.012 / 0.50)) = 0.024 -> capped at 0.05 (5%)
    snap_high_vol = MagicMock()
    snap_high_vol.symbol = "TRANSEXPR/NGX"
    snap_high_vol.close = 50.0
    snap_high_vol.atr = 25.0
    snap_high_vol.bb_width = 0.05
    snap_high_vol.df = df
    snap_high_vol.prev_close = 50.0
    snap_high_vol.asset_type = "stock"

    # Disable volatility checks that reject high volatility or flat bands
    with patch("trading_engine.risk_agent._get_5m_atr", return_value=(50.0, 25.0)), \
         patch("trading_engine.config.settings.min_atr_pct", 0.0), \
         patch("trading_engine.config.settings.stop_loss_pct_max", 2.0), \
         patch.object(settings, "confidence_sizing_enabled", False), \
         patch.object(settings, "low_trade_count_discount", 1.0):
        decision_high = risk_evaluate(verdict, snap_high_vol, current_portfolio_heat=0.0)
        assert decision_high.approved
        assert decision_high.position_size_pct == 0.05

@patch("trading_engine.orchestrator.build_snapshot")
@patch("trading_engine.orchestrator.risk_evaluate")
def test_ngx_pipeline_routing(mock_risk_eval, mock_build_snap, mock_ngx_strategy_map):
    """Test that NGX stocks are routed to native strategies in orchestrator.py."""
    # Ensure at least 60 rows for indicators
    dates = pd.date_range(end="2026-06-20", periods=60, freq="D")
    df = pd.DataFrame({
        "open": [50.0] * 60,
        "high": [51.0] * 60,
        "low": [49.0] * 60,
        "close": [50.0] * 60,
        "volume": [1000] * 60
    }, index=dates)
    df.index.name = "timestamp"

    mock_snap = MagicMock()
    mock_snap.symbol = "GTCO/NGX"
    mock_snap.close = 50.0
    mock_snap.atr = 1.0
    mock_snap.rsi = 50.0
    mock_snap.df = df
    mock_snap.asset_type = "stock"
    mock_build_snap.return_value = mock_snap

    mock_decision = RiskDecision(
        approved=True,
        reason="Approved",
        position_size_pct=0.15,
        position_size_usd=1500.0,
        entry_price=50.0,
        stop_loss=47.0,
        take_profit=59.0,
        stop_loss_pct=0.06,
        take_profit_pct=0.18,
        risk_reward=3.0,
        max_loss_usd=90.0,
        atr=1.0
    )
    mock_risk_eval.return_value = mock_decision

    # Mock the strategy EMA_Cross buy/sell signal function
    # EMA_Cross expects a df and returns (buy, sell) pandas Series
    mock_buy_series = pd.Series([False] * 59 + [True], index=dates)
    mock_sell_series = pd.Series([False] * 60, index=dates)

    mock_strategy_fn = MagicMock(return_value=(mock_buy_series, mock_sell_series))

    with patch.dict("trading_engine.backtest.engine._NGX_STRATEGY_FNS", {"EMA_Cross": mock_strategy_fn}):
        sig = run_pipeline("GTCO/NGX", "1d")
        
        assert sig.symbol == "GTCO/NGX"
        assert sig.final_action == "BUY"
        assert sig.entry_price == 50.0
        assert sig.position_size_usd == 1500.0
        assert sig.stop_loss == 47.0
        
        # Verify it bypassed standard agents
        assert len(sig.agent_signals) == 1
        assert sig.agent_signals[0]["agent"] == "ngx_native"

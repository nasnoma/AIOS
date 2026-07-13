import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from trading_engine.execution.live_trader import Position
from trading_engine import art_weight_updater

def test_art_weight_updates(tmp_path):
    # Setup temporary weights file
    test_weights_file = tmp_path / "judge_weights.json"
    initial_data = {
        "min_agreement": 6,
        "min_avg_confidence": 59.0,
        "weights": {
            "trend": 1.0,
            "momentum": 1.0,
            "volume": 1.0,
            "orderflow": 1.0
        }
    }
    test_weights_file.write_text(json.dumps(initial_data, indent=2))

    # Mock Position
    pos = MagicMock(spec=Position)
    pos.symbol = "BTC/USDT"
    pos.direction = "long"
    pos.pnl_usd = 100.0  # Profitable trade
    pos.agent_signals = [
        {"agent": "trend", "signal": "BUY", "confidence": 80.0},       # agreed + profitable -> boost
        {"agent": "momentum", "signal": "HOLD", "confidence": 50.0},    # disagreed + profitable -> decay
        {"agent": "volume", "signal": "SELL", "confidence": 70.0},      # disagreed + profitable -> decay
        {"agent": "orderflow", "signal": "BUY", "confidence": 90.0}     # agreed + profitable -> boost
    ]

    # Patch WEIGHTS_PATH to use our test file
    with patch("trading_engine.art_weight_updater.WEIGHTS_PATH", test_weights_file):
        art_weight_updater.update_weights_from_outcome(pos)
        
        # Read back weights
        with open(test_weights_file) as f:
            updated_data = json.load(f)
            
        weights = updated_data["weights"]
        
        # Verify boosting: 1.0 * 1.05 = 1.05
        assert weights["trend"] == 1.05
        assert weights["orderflow"] == 1.05
        
        # Verify decay: 1.0 * 0.95 = 0.95
        assert weights["momentum"] == 0.95
        assert weights["volume"] == 0.95

def test_art_weight_clamps(tmp_path):
    # Setup temporary weights file with extreme weights
    test_weights_file = tmp_path / "judge_weights.json"
    initial_data = {
        "min_agreement": 6,
        "min_avg_confidence": 59.0,
        "weights": {
            "trend": 2.95,
            "momentum": 0.52
        }
    }
    test_weights_file.write_text(json.dumps(initial_data, indent=2))

    # Mock Position for profitable trade (trend gets boosted, momentum gets decayed)
    pos = MagicMock(spec=Position)
    pos.symbol = "BTC/USDT"
    pos.direction = "long"
    pos.pnl_usd = 50.0
    pos.agent_signals = [
        {"agent": "trend", "signal": "BUY"},      # 2.95 * 1.05 = 3.0975 -> clamped to 3.0
        {"agent": "momentum", "signal": "HOLD"}   # 0.52 * 0.95 = 0.494 -> clamped to 0.5
    ]

    with patch("trading_engine.art_weight_updater.WEIGHTS_PATH", test_weights_file):
        art_weight_updater.update_weights_from_outcome(pos)
        
        with open(test_weights_file) as f:
            updated_data = json.load(f)
            
        weights = updated_data["weights"]
        assert weights["trend"] == 3.0
        assert weights["momentum"] == 0.5

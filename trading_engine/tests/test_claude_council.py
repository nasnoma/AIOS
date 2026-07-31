"""
trading_engine/tests/test_claude_council.py

Unit tests for ClaudeCouncil module.
"""
import pytest
from datetime import datetime, timezone
import pandas as pd
from unittest.mock import patch, MagicMock

from trading_engine.claude_council import ClaudeCouncil, RedTeamVerdict
from trading_engine.judge import JudgeVerdict
from trading_engine.agents.base import Signal
from trading_engine.data.market_data import MarketSnapshot


def _mock_snapshot() -> MarketSnapshot:
    df = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [105.0] * 30,
        "low": [95.0] * 30,
        "close": [102.0] * 30,
        "volume": [1000.0] * 30,
    })
    return MarketSnapshot(
        symbol="BTC/USDT",
        asset_type="crypto",
        timeframe="15m",
        timestamp=datetime.now(timezone.utc),
        df=df,
        close=102.0,
        rsi=55.0,
        atr=2.0,
        ema20=101.0,
        ema50=100.0,
        ema200=90.0,
        rel_volume=1.2,
        realized_vol=0.35,
    )


def test_red_team_no_signal():
    council = ClaudeCouncil()
    snap = _mock_snapshot()
    judge_verdict = JudgeVerdict(
        decision=Signal.HOLD,
        confidence=50.0,
        agreement=4,
        disagreement=2,
        weighted_score=0.0,
        reasoning="No clear trend",
        agent_reports=[],
        approved=False,
    )
    result = council.red_team_trade(snap, judge_verdict)
    assert result.approved is False
    assert "Judge did not approve" in result.verdict_reason


@patch("trading_engine.claude_council.call_llm")
def test_red_team_approval(mock_call_llm):
    mock_call_llm.return_value = '{"bull_case": "Strong trend", "bear_case": "Slight resistance", "decision": "APPROVE", "confidence_modifier": 1.1, "reason": "Sufficient momentum"}'
    council = ClaudeCouncil()
    snap = _mock_snapshot()
    judge_verdict = JudgeVerdict(
        decision=Signal.BUY,
        confidence=75.0,
        agreement=7,
        disagreement=1,
        weighted_score=5.5,
        reasoning="Strong buy consensus",
        agent_reports=[],
        approved=True,
    )
    result = council.red_team_trade(snap, judge_verdict)
    assert result.approved is True
    assert result.confidence_modifier == 1.1
    assert "Sufficient momentum" in result.verdict_reason


@patch("trading_engine.claude_council.call_llm")
def test_red_team_veto(mock_call_llm):
    mock_call_llm.return_value = '{"bull_case": "Oversold", "bear_case": "Macro headwinds and low volume", "decision": "VETO", "confidence_modifier": 0.8, "reason": "Severe resistance wall"}'
    council = ClaudeCouncil()
    snap = _mock_snapshot()
    judge_verdict = JudgeVerdict(
        decision=Signal.BUY,
        confidence=60.0,
        agreement=6,
        disagreement=2,
        weighted_score=4.2,
        reasoning="Moderate buy consensus",
        agent_reports=[],
        approved=True,
    )
    result = council.red_team_trade(snap, judge_verdict)
    assert result.approved is False
    assert "Severe resistance wall" in result.verdict_reason


def test_weekly_performance_review():
    council = ClaudeCouncil()
    trades = [
        {"symbol": "BTC/USDT", "pnl": 50.0},
        {"symbol": "ETH/USDT", "pnl": -20.0},
        {"symbol": "SOL/USDT", "pnl": 40.0},
        {"symbol": "AVAX/USDT", "pnl": 15.0},
        {"symbol": "LINK/USDT", "pnl": 30.0},
    ]
    review = council.run_weekly_performance_review(trades, days=7)
    assert review["total_trades"] == 5
    assert review["win_rate"] == 80.0
    assert review["total_pnl"] == 115.0
    assert "Claude Council" in review["summary"]

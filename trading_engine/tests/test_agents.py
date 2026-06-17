"""
trading_engine/tests/test_agents.py
Unit tests for all specialist agents using synthetic MarketSnapshot data.
Run with: python -m pytest trading_engine/tests/ -v
"""
import pandas as pd
import numpy as np
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from trading_engine.agents.base import Signal
from trading_engine.data.market_data import MarketSnapshot


def make_snapshot(**kwargs) -> MarketSnapshot:
    """Create a synthetic MarketSnapshot for testing."""
    n = 300
    closes = np.random.uniform(40000, 50000, n)
    closes = np.cumsum(np.random.randn(n) * 200) + 45000

    df = pd.DataFrame({
        "open": closes - np.random.uniform(0, 200, n),
        "high": closes + np.random.uniform(0, 300, n),
        "low": closes - np.random.uniform(0, 300, n),
        "close": closes,
        "volume": np.random.uniform(1000, 10000, n),
    }, index=pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"))

    defaults = dict(
        symbol="BTC/USDT", asset_type="crypto", timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        df=df, close=float(closes[-1]), volume=float(df["volume"].iloc[-1]),
        ema20=float(closes[-1] * 1.01), ema50=float(closes[-1] * 1.005),
        ema200=float(closes[-1] * 0.98),
        rsi=58.0, stoch_rsi_k=60.0, stoch_rsi_d=55.0, roc=2.5,
        obv=1_000_000.0, rel_volume=1.4, vwap=float(closes[-1] * 0.999),
        atr=float(closes[-1] * 0.02), bb_width=0.06, realized_vol=0.6,
        open_interest=500_000_000.0, funding_rate=0.0001,
        long_liq_24h=10_000_000.0, short_liq_24h=5_000_000.0,
        fear_greed_index=35, fear_greed_label="Fear",
    )
    defaults.update(kwargs)
    return MarketSnapshot(**defaults)


# ── Trend Agent ────────────────────────────────────────────

class TestTrendAgent:
    def test_bullish_ema_stack(self):
        from trading_engine.agents import trend_agent
        snap = make_snapshot(ema20=50000, ema50=48000, ema200=45000, close=51000)
        result = trend_agent.analyze(snap)
        assert result.signal == Signal.BUY
        assert result.confidence > 70

    def test_bearish_ema_stack(self):
        from trading_engine.agents import trend_agent
        snap = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000)
        result = trend_agent.analyze(snap)
        assert result.signal == Signal.SELL

    def test_agent_name(self):
        from trading_engine.agents import trend_agent
        snap = make_snapshot()
        result = trend_agent.analyze(snap)
        assert result.agent == "trend"
        assert 0 <= result.confidence <= 100

    def test_higher_timeframe_bearish_filter(self):
        from trading_engine.agents import trend_agent
        # Lower timeframe has bullish stack (score should be +5, BUY signal)
        base_snap = make_snapshot(ema20=50000, ema50=48000, ema200=45000, close=51000)
        
        # But higher timeframe is strongly bearish (close is below ema200)
        htf_snap = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000, timeframe="1d")
        base_snap.htf_snap = htf_snap
        
        result = trend_agent.analyze(base_snap)
        
        # Bullish signal should be vetoed/downgraded to HOLD or SELL due to HTF bearish filter
        assert result.signal in (Signal.HOLD, Signal.SELL)
        assert "HTF Trend Filter active" in result.reason

    def test_higher_timeframe_bullish_filter(self):
        from trading_engine.agents import trend_agent
        # Lower timeframe has bearish stack (SELL signal)
        base_snap = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000)
        
        # But higher timeframe is strongly bullish (close is above ema200)
        htf_snap = make_snapshot(ema20=50000, ema50=48000, ema200=45000, close=51000, timeframe="1d")
        base_snap.htf_snap = htf_snap
        
        result = trend_agent.analyze(base_snap)
        
        # Bearish signal should be vetoed/downgraded to HOLD or BUY due to HTF bullish filter
        assert result.signal in (Signal.HOLD, Signal.BUY)
        assert "HTF Trend Filter active" in result.reason

    def test_multi_timeframe_trend_veto(self):
        from trading_engine.agents import trend_agent
        
        # Lower timeframe has bullish stack (score +5, BUY signal)
        base_snap = make_snapshot(ema20=50000, ema50=48000, ema200=45000, close=51000)
        
        # Mocks for 1H, 4H, and Daily HTF snaps
        # Daily is Bearish, 4H is Bearish, 1H is Bearish -> Veto (3 bearish, 0 bullish)
        snap_1h = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000, timeframe="1h")
        snap_4h = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000, timeframe="4h")
        snap_1d = make_snapshot(ema20=40000, ema50=42000, ema200=46000, close=39000, timeframe="1d")
        
        base_snap.htf_1h_snap = snap_1h
        base_snap.htf_4h_snap = snap_4h
        base_snap.htf_1d_snap = snap_1d
        
        result = trend_agent.analyze(base_snap)
        assert result.signal in (Signal.HOLD, Signal.SELL)
        assert "MTF Veto: Majority of HTFs are BEARISH" in result.reason


# ── Momentum Agent ─────────────────────────────────────────

class TestMomentumAgent:
    def test_strong_bullish_momentum(self):
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=65, stoch_rsi_k=70, stoch_rsi_d=60, roc=4.0)
        result = momentum_agent.analyze(snap)
        assert result.signal == Signal.BUY

    def test_overbought_caution(self):
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=82, stoch_rsi_k=90, stoch_rsi_d=88, roc=1.0)
        result = momentum_agent.analyze(snap)
        # Overbought: should be HOLD or SELL
        assert result.signal in (Signal.HOLD, Signal.SELL)

    def test_bearish_momentum(self):
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=35, stoch_rsi_k=25, stoch_rsi_d=30, roc=-4.0)
        result = momentum_agent.analyze(snap)
        assert result.signal == Signal.SELL


# ── Volatility Agent ───────────────────────────────────────

class TestVolatilityAgent:
    def test_extreme_volatility_rejected(self):
        from trading_engine.agents import volatility_agent
        snap = make_snapshot(atr=5000, close=50000, bb_width=0.20, realized_vol=2.0)
        result = volatility_agent.analyze(snap)
        assert result.signal == Signal.SELL  # SELL = "avoid trade"
        assert result.confidence >= 80

    def test_healthy_volatility(self):
        from trading_engine.agents import volatility_agent
        snap = make_snapshot(atr=1000, close=50000, bb_width=0.06, realized_vol=0.5)
        result = volatility_agent.analyze(snap)
        assert result.signal in (Signal.BUY, Signal.HOLD)


# ── Risk Agent ─────────────────────────────────────────────

class TestRiskAgent:
    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("Unit test fallback"))
    def test_approved_trade(self, mock_build):
        from trading_engine.risk_agent import evaluate, RiskDecision
        from trading_engine.judge import JudgeVerdict
        from trading_engine.agents.base import Signal

        verdict = JudgeVerdict(
            decision=Signal.BUY, confidence=82, agreement=7, disagreement=1,
            weighted_score=0.82, reasoning="Test", agent_reports=[], approved=True,
        )
        snap = make_snapshot(close=50000, atr=1000, bb_width=0.06)
        decision = evaluate(verdict, snap, current_portfolio_heat=0.02, open_positions=1)
        assert decision.approved
        assert decision.stop_loss < 50000
        assert decision.take_profit > 50000
        assert 0 < decision.position_size_pct <= 0.10

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("Unit test fallback"))
    def test_veto_extreme_atr(self, mock_build):
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        snap = make_snapshot(close=50000, atr=5000, bb_width=0.20)
        verdict = JudgeVerdict(
            decision=Signal.BUY, confidence=80, agreement=7, disagreement=1,
            weighted_score=0.80, reasoning="Test", agent_reports=[], approved=True,
        )
        decision = evaluate(verdict, snap)
        assert not decision.approved

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("Unit test fallback"))
    def test_veto_portfolio_heat(self, mock_build):
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        snap = make_snapshot(close=50000, atr=1000, bb_width=0.05)
        verdict = JudgeVerdict(
            decision=Signal.BUY, confidence=85, agreement=7, disagreement=1,
            weighted_score=0.85, reasoning="Test", agent_reports=[], approved=True,
        )
        # Use a high portfolio heat (e.g. 0.20) to exceed the max_portfolio_heat threshold (currently 0.15)
        decision = evaluate(verdict, snap, current_portfolio_heat=0.20)  # above limit
        assert not decision.approved

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("Unit test fallback"))
    def test_low_volatility_veto(self, mock_build):
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        from trading_engine.config import settings

        # Case 1: ATR% is too low (e.g. close=50000, atr=50, which is 0.10%, below 0.15% min)
        snap_low_atr = make_snapshot(close=50000, atr=50, bb_width=0.05)
        verdict = JudgeVerdict(
            decision=Signal.BUY, confidence=85, agreement=7, disagreement=1,
            weighted_score=0.85, reasoning="Test", agent_reports=[], approved=True,
        )
        
        orig_min_atr = settings.min_atr_pct
        try:
            settings.min_atr_pct = 0.15
            decision = evaluate(verdict, snap_low_atr)
            assert not decision.approved
            assert "Volatility filter veto: ATR%" in decision.reason
        finally:
            settings.min_atr_pct = orig_min_atr

        # Case 2: Bollinger Band width is too narrow (e.g. close=50000, atr=500, bb_width=0.01, below 0.015 min)
        snap_low_bb = make_snapshot(close=50000, atr=500, bb_width=0.01)
        orig_min_bb = settings.min_bb_width
        try:
            settings.min_bb_width = 0.015
            decision = evaluate(verdict, snap_low_bb)
            assert not decision.approved
            assert "Volatility filter veto: BBand width" in decision.reason
        finally:
            settings.min_bb_width = orig_min_bb

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("Unit test fallback"))
    def test_crypto_session_awareness(self, mock_build):
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        from trading_engine.config import settings

        # Mock is_crypto_peak_session to return False (off-peak)
        with patch("trading_engine.market_hours.is_crypto_peak_session", return_value=False):
            # Crypto asset
            snap = make_snapshot(symbol="BTC/USDT", asset_type="crypto", close=50000, atr=1000, bb_width=0.05)
            verdict = JudgeVerdict(
                decision=Signal.BUY, confidence=85, agreement=7, disagreement=1,
                weighted_score=0.85, reasoning="Test", agent_reports=[], approved=True,
            )

            # Sub-case A: crypto_peak_sessions_only = True -> Veto
            orig_only = settings.crypto_peak_sessions_only
            try:
                settings.crypto_peak_sessions_only = True
                decision = evaluate(verdict, snap)
                assert not decision.approved
                assert "Crypto session veto" in decision.reason
            finally:
                settings.crypto_peak_sessions_only = orig_only

            # Sub-case B: crypto_peak_sessions_reduce_size = True -> 50% size reduction
            orig_only = settings.crypto_peak_sessions_only
            orig_reduce = settings.crypto_peak_sessions_reduce_size
            try:
                settings.crypto_peak_sessions_only = False
                settings.crypto_peak_sessions_reduce_size = True
                
                decision_normal = evaluate(verdict, snap) # size reduction active
                
                # Mock in-peak to compare size
                with patch("trading_engine.market_hours.is_crypto_peak_session", return_value=True):
                    decision_peak = evaluate(verdict, snap)
                
                # Off-peak size should be exactly half of peak size
                assert decision_normal.position_size_pct == round(decision_peak.position_size_pct * 0.5, 4)
            finally:
                settings.crypto_peak_sessions_only = orig_only
                settings.crypto_peak_sessions_reduce_size = orig_reduce


# ── Judge ──────────────────────────────────────────────────

class TestJudge:
    def test_strong_agreement_approved(self):
        from trading_engine.judge import evaluate
        from trading_engine.agents.base import AgentSignal, Signal

        agents = [
            AgentSignal(agent=f"agent{i}", signal=Signal.BUY, confidence=80, reason="test")
            for i in range(7)
        ] + [
            AgentSignal(agent="agent8", signal=Signal.SELL, confidence=70, reason="test")
        ]
        verdict = evaluate(agents)
        assert verdict.decision == Signal.BUY
        assert verdict.agreement == 7

    def test_below_threshold_not_approved(self):
        from unittest.mock import patch
        from trading_engine.judge import evaluate
        from trading_engine.agents.base import AgentSignal, Signal
        from trading_engine.config import settings

        # Only 4 agree — below min_agent_agreement=6
        agents = [
            AgentSignal(agent=f"a{i}", signal=Signal.BUY, confidence=75, reason="t") for i in range(4)
        ] + [
            AgentSignal(agent=f"b{i}", signal=Signal.SELL, confidence=65, reason="t") for i in range(4)
        ]
        
        original_agreement = settings.min_agent_agreement
        original_confidence = settings.min_avg_confidence
        try:
            settings.min_agent_agreement = 6
            settings.min_avg_confidence = 75.0
            with patch("trading_engine.judge._load_judge_params", return_value={}):
                verdict = evaluate(agents)
                assert not verdict.approved
        finally:
            settings.min_agent_agreement = original_agreement
            settings.min_avg_confidence = original_confidence


# ── Sentiment and Macro Agents (with Mock LLM) ──────────────

class TestSentimentAndMacroAgents:
    def test_sentiment_agent_mock_bullish(self):
        from trading_engine.agents import sentiment_agent
        from trading_engine.utils.llm import call_llm
        
        # Test the smart mock LLM explicitly for Sentiment Agent
        prompt = """You are SentimentAgent analyzing BTC/USDT.
News headlines (last 24h):
- SEC approves new Spot Bitcoin ETF options trading.
- Institutional inflows hit new all-time high this week.
Based ONLY on these headlines, output JSON:
{"signal": "BUY|SELL|HOLD", "confidence": 0-100, "reason": "one sentence"}"""
        
        resp = call_llm(prompt)
        import json
        data = json.loads(resp)
        assert data["signal"] == "BUY"
        assert data["confidence"] > 50

        # Run analyze
        snap = make_snapshot(fear_greed_index=20, fear_greed_label="Extreme Fear")
        result = sentiment_agent.analyze(snap)
        # Extreme fear (contrarian buy) + positive headlines should make it buy
        assert result.agent == "sentiment"
        assert result.signal in (Signal.BUY, Signal.HOLD)

    def test_macro_agent_mock_bearish(self):
        from trading_engine.agents import macro_agent
        
        # Risk-off or rising DXY triggers bearish signals
        snap = make_snapshot(symbol="BTC/USDT", asset_type="crypto")
        # We patch _get_dxy_trend and _get_risk_mode to simulate these
        with patch("trading_engine.agents.macro_agent._get_dxy_trend", return_value="rising"), \
             patch("trading_engine.agents.macro_agent._get_risk_mode", return_value="risk-off"):
            result = macro_agent.analyze(snap)
            assert result.agent == "macro"
            assert result.signal == Signal.SELL


# ── Correlation and Transaction Costs Tests ──────────────

class TestCorrelationFilter:
    def test_correlation_filter_veto(self):
        from trading_engine.risk_agent import evaluate, check_correlation
        from trading_engine.judge import JudgeVerdict
        from trading_engine.agents.base import Signal

        # Create mock snapshots
        snap_btc = make_snapshot()
        snap_btc.symbol = "BTC/USDT"
        
        snap_eth = make_snapshot()
        snap_eth.symbol = "ETH/USDT"

        # Force a high correlation return of 0.95 by patching _compute_price_correlation
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.95):
            verdict = JudgeVerdict(
                decision=Signal.BUY,
                confidence=80.0,
                agreement=7,
                disagreement=1,
                weighted_score=7.0,
                reasoning="mock",
                agent_reports=[],
                approved=True
            )
            decision = evaluate(
                verdict,
                snap_eth,
                current_portfolio_heat=0.0,
                historical_win_rate=0.6,
                open_positions=1,
                open_position_snaps={"BTC/USDT": snap_btc}
            )
            # High correlation should VETO the trade entirely
            assert not decision.approved
            assert "Correlation veto" in decision.reason

    def test_correlation_filter_soft_adjustment(self):
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        from trading_engine.agents.base import Signal

        snap_btc = make_snapshot()
        snap_btc.symbol = "BTC/USDT"
        
        snap_eth = make_snapshot()
        snap_eth.symbol = "ETH/USDT"

        # Force soft correlation of 0.80 (between 0.75 and 0.90)
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.80):
            verdict = JudgeVerdict(
                decision=Signal.BUY,
                confidence=80.0,
                agreement=7,
                disagreement=1,
                weighted_score=7.0,
                reasoning="mock",
                agent_reports=[],
                approved=True
            )
            decision = evaluate(
                verdict,
                snap_eth,
                current_portfolio_heat=0.0,
                historical_win_rate=0.6,
                open_positions=1,
                open_position_snaps={"BTC/USDT": snap_btc}
            )
            # Soft correlation should approve but reduce position size
            assert decision.approved
            # Check reason mentions soft adjustment
            assert "Correlation soft adjustment" in decision.reason or "Kelly sizing" in decision.reason


class TestPaperTraderFees:
    def test_transaction_costs(self):
        from trading_engine.execution import paper_trader
        import shutil

        # Backup state file if exists
        backup_path = paper_trader.STATE_FILE.with_suffix(".json.bak")
        if paper_trader.STATE_FILE.exists():
            shutil.copy(paper_trader.STATE_FILE, backup_path)

        try:
            # Delete active state to start fresh
            if paper_trader.STATE_FILE.exists():
                paper_trader.STATE_FILE.unlink()

            # Open a paper trade of $1000 size
            pos = paper_trader.open_trade(
                symbol="BTC/USDT",
                direction="long",
                entry=50000.0,
                size_usd=1000.0,
                stop_loss=48000.0,
                take_profit=56000.0
            )

            status = paper_trader.get_status()
            # Entry fee: 1000 * 0.0006 = 0.60
            assert status["total_fees"] == 0.60
            assert status["cash"] == 10000.0 - 1000.0 - 0.60  # Initial account size is 10000 by default

            # Update prices to trigger take profit (TP = 56000)
            paper_trader.update_prices({"BTC/USDT": 57000.0})

            status_closed = paper_trader.get_status()
            # Gross profit: (57000 - 50000)/50000 * 1000 = 140.0
            # Exit fee: 1000 * 0.0006 = 0.60
            # Net profit: 140.0 - 0.60 = 139.40
            # Total fees paid: 0.60 (entry) + 0.60 (exit) = 1.20
            assert status_closed["total_fees"] == 1.20
            assert status_closed["total_pnl"] == 139.40

        finally:
            # Restore state backup
            if backup_path.exists():
                shutil.copy(backup_path, paper_trader.STATE_FILE)
                backup_path.unlink()
            elif paper_trader.STATE_FILE.exists():
                paper_trader.STATE_FILE.unlink()


class TestWalkForward:
    def test_make_historical_snapshot(self):
        from trading_engine.backtest.engine import make_historical_snapshot
        
        # Build synthetic DataFrame
        dates = pd.date_range("2026-06-01", periods=100, freq="4h", tz="UTC")
        closes = np.linspace(50000, 55000, 100)
        df = pd.DataFrame({
            "open": closes - 50,
            "high": closes + 100,
            "low": closes - 100,
            "close": closes,
            "volume": np.ones(100) * 1000,
            "EMA_20": closes,
            "EMA_50": closes,
            "EMA_200": closes,
            "RSI_14": np.ones(100) * 50,
            "STOCHRSIk_14_14_3_3": np.ones(100) * 50,
            "STOCHRSId_14_14_3_3": np.ones(100) * 50,
            "ROC_10": np.ones(100) * 0,
            "OBV": np.ones(100) * 1000,
            "REL_VOL": np.ones(100) * 1.0,
            "VWAP_D": closes,
            "ATRr_14": np.ones(100) * 50,
            "REAL_VOL": np.ones(100) * 0.15,
        }, index=dates)
        
        # Check snapshot at index 50
        snap = make_historical_snapshot("BTC/USDT", "crypto", "4h", df, 50)
        assert snap.close == closes[50]
        # Verify no future leakage: the sliced dataframe must contain exactly 51 rows
        assert len(snap.df) == 51
        assert snap.df.index[-1] == dates[50]

    def test_optimize_weights_for_window(self):
        from trading_engine.backtest.engine import optimize_weights_for_window
        from trading_engine.judge import DEFAULT_WEIGHTS

        # Build synthetic df
        dates = pd.date_range("2026-06-01", periods=50, freq="4h", tz="UTC")
        closes = np.ones(50) * 50000
        # Force a clear trend for testing
        closes[25:] = 55000 # price moves up
        
        df = pd.DataFrame({
            "open": closes,
            "high": closes + 50,
            "low": closes - 50,
            "close": closes,
            "volume": np.ones(50) * 1000,
            "EMA_20": closes * 1.01, # trend agents see EMA bullish
            "EMA_50": closes * 1.0,
            "EMA_200": closes * 0.99,
            "RSI_14": np.ones(50) * 65, # momentum bullish
            "STOCHRSIk_14_14_3_3": np.ones(50) * 80,
            "STOCHRSId_14_14_3_3": np.ones(50) * 75,
            "ROC_10": np.ones(50) * 2.0,
            "OBV": np.ones(50) * 1000,
            "REL_VOL": np.ones(50) * 1.0,
            "VWAP_D": closes,
            "ATRr_14": np.ones(50) * 100,
            "REAL_VOL": np.ones(50) * 0.15,
        }, index=dates)
        
        # Base weights
        base = DEFAULT_WEIGHTS.copy()
        optimized = optimize_weights_for_window(df, "BTC/USDT", "crypto", "4h", 0, 30, base, forward_candles=5)
        
        # Verify optimization boundaries and existence of key quant weights
        for key in ["trend", "momentum", "volume", "volatility", "structure", "orderflow"]:
            assert key in optimized
            # Weights should remain within bounds [0.5, 2.0]
            assert 0.5 <= optimized[key] <= 2.0


# ── Structure Agent ─────────────────────────────────────────

class TestStructureAgent:
    def test_htf_resistance_blocking(self):
        from trading_engine.agents import structure_agent
        # Local structure has bullish Break of Structure (score is positive)
        base_df = pd.DataFrame({
            "high": np.ones(50) * 100,
            "low": np.ones(50) * 90,
            "close": np.ones(50) * 95,
        })
        base_snap = make_snapshot(close=100.0, df=base_df)
        
        # Build synthetic higher-timeframe snap
        # Make it have resistance level at 101.0 (just 1% above base close price of 100.0)
        htf_df = pd.DataFrame({
            "high": np.array([101.0] * 50),
            "low": np.array([90.0] * 50),
            "close": np.array([95.0] * 50),
            "is_pivot_high": np.array([True] * 50),
            "is_pivot_low": np.array([True] * 50),
        })
        htf_snap = make_snapshot(close=95.0, df=htf_df, timeframe="4h")
        base_snap.htf_snap = htf_snap
        
        # Mock _find_support_resistance to return local and HTF S/R levels
        with patch("trading_engine.agents.structure_agent._detect_bos", return_value=(True, False)), \
             patch("trading_engine.agents.structure_agent._find_support_resistance", side_effect=[
                 (90.0, 110.0), # local S/R
                 (90.0, 101.0), # HTF S/R
             ]):
            result = structure_agent.analyze(base_snap)
            
            # The BUY signal should be blocked (downgraded to HOLD) because 101.0 is within 1.5% of 100.0
            assert result.signal == Signal.HOLD
            assert "HTF Resistance nearby" in result.reason

    def test_htf_support_boost(self):
        from trading_engine.agents import structure_agent
        base_df = pd.DataFrame({
            "high": np.ones(50) * 100,
            "low": np.ones(50) * 90,
            "close": np.ones(50) * 92,
        })
        base_snap = make_snapshot(close=92.0, df=base_df)
        
        # Build synthetic higher-timeframe snap
        # Make it have support level at 91.0 (just 1.1% below base close price of 92.0)
        htf_df = pd.DataFrame({
            "high": np.array([105.0] * 50),
            "low": np.array([91.0] * 50),
            "close": np.array([95.0] * 50),
            "is_pivot_high": np.array([True] * 50),
            "is_pivot_low": np.array([True] * 50),
        })
        htf_snap = make_snapshot(close=95.0, df=htf_df, timeframe="4h")
        base_snap.htf_snap = htf_snap
        
        # Mock _find_support_resistance to return local and HTF S/R levels
        with patch("trading_engine.agents.structure_agent._detect_bos", return_value=(False, False)), \
             patch("trading_engine.agents.structure_agent._find_support_resistance", side_effect=[
                 (80.0, 110.0), # local S/R
                 (91.0, 110.0), # HTF S/R
             ]):
            result = structure_agent.analyze(base_snap)
            
            # The signal should be boosted to BUY because of the proximity to HTF support
            assert result.signal == Signal.BUY
            assert "HTF Support nearby" in result.reason



# ── Risk Agent ──────────────────────────────────────────────

class TestRiskAgentStage3:
    def test_risk_agent_5m_atr_calculation(self):
        from trading_engine import risk_agent
        from trading_engine.judge import JudgeVerdict
        
        # Base snapshot on 4H timeframe
        base_df = pd.DataFrame({
            "high": np.ones(50) * 105,
            "low": np.ones(50) * 95,
            "close": np.ones(50) * 100,
        })
        base_snap = make_snapshot(close=100.0, df=base_df, timeframe="4h")
        base_snap.atr = 5.0
        
        # Mock 5m snapshot returned by build_snapshot
        mock_5m_df = pd.DataFrame({
            "high": np.ones(50) * 100,
            "low": np.ones(50) * 99,
            "close": np.ones(50) * 99.5,
        })
        mock_5m_snap = make_snapshot(close=99.5, df=mock_5m_df, timeframe="5m")
        mock_5m_snap.atr = 1.0
        
        verdict = JudgeVerdict(
            approved=True, decision=Signal.BUY, confidence=80.0, agreement=6, disagreement=0,
            weighted_score=0.8, reasoning="Test", agent_reports=[]
        )
        
        # Patch build_snapshot to return the 5m snapshot when 5m timeframe is requested
        with patch("trading_engine.data.market_data.build_snapshot", return_value=mock_5m_snap):
            result = risk_agent.evaluate(verdict, base_snap)
            
            assert result.approved is True
            assert result.entry_price == 99.5
            assert result.atr == 1.0
            
            # stop loss = entry - (atr * atr_multiplier)
            expected_sl = 99.5 - (1.0 * risk_agent.settings.atr_multiplier)
            assert abs(result.stop_loss - expected_sl) < 1e-4
            
            # take profit = entry + (atr * atr_multiplier * rr_ratio)
            expected_tp = 99.5 + (1.0 * risk_agent.settings.atr_multiplier * risk_agent.settings.rr_ratio)
            assert abs(result.take_profit - expected_tp) < 1e-4

    def test_risk_agent_5m_fallback(self):
        from trading_engine import risk_agent
        from trading_engine.judge import JudgeVerdict
        
        # Base snapshot on 4H timeframe
        base_df = pd.DataFrame({
            "high": np.ones(50) * 110,
            "low": np.ones(50) * 90,
            "close": np.ones(50) * 100,
        })
        base_snap = make_snapshot(close=100.0, df=base_df, timeframe="4h")
        base_snap.atr = 2.0
        
        verdict = JudgeVerdict(
            approved=True, decision=Signal.BUY, confidence=80.0, agreement=6, disagreement=0,
            weighted_score=0.8, reasoning="Test", agent_reports=[]
        )
        
        # Patch build_snapshot to raise an exception, forcing fallback to 4H
        with patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("API error")):
            result = risk_agent.evaluate(verdict, base_snap)
            
            assert result.approved is True
            assert result.entry_price == 100.0
            assert result.atr == 2.0
            
            # stop loss = entry - (atr * atr_multiplier)
            expected_sl = 100.0 - (2.0 * risk_agent.settings.atr_multiplier)
            assert abs(result.stop_loss - expected_sl) < 1e-4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

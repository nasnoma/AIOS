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

# Patch out fallback regime check globally for unit tests to avoid live internet queries
_fallback_patcher = patch("trading_engine.risk_agent._get_btc_regime_fallback", side_effect=RuntimeError("Unit test fallback bypass"))
_fallback_patcher.start()

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
        # New strict MTF logic emits "MTF Override: All HTFs BEARISH" (unanimous bearish = suppress bullish)
        assert "BEARISH" in result.reason


# ── Momentum Agent ─────────────────────────────────────────

class TestMomentumAgent:
    def test_strong_bullish_momentum(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=65, stoch_rsi_k=70, stoch_rsi_d=60, roc=4.0)
        with patch.object(settings, "scalping_mode", False):
            result = momentum_agent.analyze(snap)
        assert result.signal == Signal.BUY

    def test_overbought_caution(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=82, stoch_rsi_k=90, stoch_rsi_d=88, roc=1.0)
        with patch.object(settings, "scalping_mode", False):
            result = momentum_agent.analyze(snap)
        # Overbought: should be HOLD or SELL
        assert result.signal in (Signal.HOLD, Signal.SELL)

    def test_bearish_momentum(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        snap = make_snapshot(rsi=35, stoch_rsi_k=25, stoch_rsi_d=30, roc=-4.0)
        with patch.object(settings, "scalping_mode", False):
            result = momentum_agent.analyze(snap)
        assert result.signal == Signal.SELL

    def test_scalping_uptrend_pullback(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        # In a strong uptrend (close > ema200) and RSI is oversold/pullback (<40)
        snap = make_snapshot(
            close=100.0,
            ema200=90.0,
            rsi=35.0,
            stoch_rsi_k=15.0,
            stoch_rsi_d=10.0,
            roc=-2.0
        )
        with patch.object(settings, "scalping_mode", True):
            result = momentum_agent.analyze(snap)
        # In scalping mode, an oversold pullback in an uptrend should trigger a BUY signal
        assert result.signal == Signal.BUY
        assert "pullback" in result.reason.lower()

    def test_scalping_downtrend_rally(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        # In a strong downtrend (close < ema200) and RSI is overbought/rally (>60)
        snap = make_snapshot(
            close=80.0,
            ema200=90.0,
            rsi=65.0,
            stoch_rsi_k=85.0,
            stoch_rsi_d=90.0,
            roc=2.0
        )
        with patch.object(settings, "scalping_mode", True):
            result = momentum_agent.analyze(snap)
        # In scalping mode, an overbought rally in a downtrend should trigger a SELL signal
        assert result.signal == Signal.SELL
        assert "rally" in result.reason.lower()

    def test_scalping_sma_macd_bullish(self):
        from unittest.mock import patch
        from trading_engine.config import settings
        from trading_engine.agents import momentum_agent
        
        df = pd.DataFrame({
            "close": [100.0, 101.0, 102.0, 103.0],
            "MACD_12_26_9": [0.1, 0.2, 0.3, 0.4]
        })
        snap = make_snapshot(
            close=103.0,
            sma100=95.0,
            ema20=101.0,
            ema50=99.0,
            macd=0.4,
            macd_signal=0.2,
            rsi=50.0,
            df=df,
            timeframe="5m"
        )
        with patch.object(settings, "scalping_mode", True):
            result = momentum_agent.analyze(snap)
        assert result.signal == Signal.BUY
        assert "Bullish momentum alignment" in result.reason




# ── Volatility Agent ───────────────────────────────────────

class TestVolatilityAgent:
    def test_extreme_volatility_rejected(self):
        from trading_engine.agents import volatility_agent
        snap = make_snapshot(atr=5000, close=50000, bb_width=0.20, realized_vol=2.0)
        result = volatility_agent.analyze(snap)
        assert result.signal == Signal.HOLD  # HOLD = "avoid trade"
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
        # Use a high portfolio heat (e.g. 0.30) to exceed the max_portfolio_heat threshold (currently 0.25)
        decision = evaluate(verdict, snap, current_portfolio_heat=0.30)  # above limit
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

    def test_judge_threshold_by_asset_class(self):
        from trading_engine.judge import evaluate
        from trading_engine.agents.base import AgentSignal, Signal
        
        # 3 agreeing agents, average confidence 49
        agents = [
            AgentSignal(agent="trend", signal=Signal.BUY, confidence=49, reason="test"),
            AgentSignal(agent="momentum", signal=Signal.BUY, confidence=49, reason="test"),
            AgentSignal(agent="volume", signal=Signal.BUY, confidence=49, reason="test"),
        ] + [
            AgentSignal(agent=f"agent{i}", signal=Signal.SELL, confidence=10, reason="test")
            for i in range(5)
        ]

        # 1. Crypto Symbol -> should use strict thresholds (5 and 52) -> should NOT be approved
        verdict_crypto = evaluate(agents, symbol="BTC/USDT")
        assert not verdict_crypto.approved

        # 2. Equity Symbols (US Stock, stock CFD, NGX Stock) -> should use relaxed thresholds (3 and 48) -> should be approved
        verdict_stock = evaluate(agents, symbol="AAPL")
        assert verdict_stock.approved

        verdict_cfd = evaluate(agents, symbol="AAPL/USDT:USDT")
        assert verdict_cfd.approved

        verdict_ngx = evaluate(agents, symbol="GTCO/NGX")
        assert verdict_ngx.approved


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

        # Force a high correlation return of 0.95 by patching _compute_price_correlation.
        # Also patch build_snapshot used by the regime filter to avoid a live API call;
        # RuntimeError causes the regime filter to fall through (allow) so the correlation
        # check is the final veto as intended.
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.95), \
             patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("unit-test regime bypass")):
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

        # Force soft correlation of 0.80 (between 0.75 and 0.90).
        # Patch build_snapshot to avoid a live API call inside the regime filter;
        # the RuntimeError causes the filter to fall through.
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.80), \
             patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("unit-test regime bypass")):
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


class TestCircuitBreakerAndRegimeFilter:
    """Tests for the daily circuit breaker and market regime filter."""

    def _make_buy_verdict(self):
        from trading_engine.judge import JudgeVerdict
        from trading_engine.agents.base import Signal
        return JudgeVerdict(
            decision=Signal.BUY, confidence=85.0, agreement=7, disagreement=1,
            weighted_score=7.0, reasoning="mock", agent_reports=[], approved=True,
        )

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("unit-test"))
    def test_circuit_breaker_halts_on_daily_loss(self, mock_build):
        """When daily realized PnL is below the limit, all new trades must be blocked."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.config import settings

        snap = make_snapshot(symbol="BTC/USDT", asset_type="crypto", close=50000, atr=1000, bb_width=0.06)
        verdict = self._make_buy_verdict()

        orig_limit = settings.daily_loss_limit_usd
        try:
            settings.daily_loss_limit_usd = 300.0
            # daily loss of -350 exceeds the 300 limit
            decision = evaluate(verdict, snap, daily_pnl_usd=-350.0)
            assert not decision.approved
            assert "Circuit breaker" in decision.reason
        finally:
            settings.daily_loss_limit_usd = orig_limit

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("unit-test"))
    def test_circuit_breaker_allows_below_limit(self, mock_build):
        """When daily loss is within the limit, the circuit breaker must NOT fire."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.config import settings

        snap = make_snapshot(symbol="BTC/USDT", asset_type="crypto", close=50000, atr=1000, bb_width=0.06)
        verdict = self._make_buy_verdict()

        orig_limit = settings.daily_loss_limit_usd
        try:
            settings.daily_loss_limit_usd = 300.0
            # daily loss of -100 is within the limit
            decision = evaluate(verdict, snap, daily_pnl_usd=-100.0)
            # Should not veto for circuit breaker reason (may still fail other checks)
            assert "Circuit breaker" not in (decision.reason or "")
        finally:
            settings.daily_loss_limit_usd = orig_limit

    def test_regime_filter_blocks_long_in_bear_market(self):
        """When BTC is below its 50-period MA, LONG crypto trades must be blocked."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.config import settings

        snap = make_snapshot(symbol="ETH/USDT", asset_type="crypto", close=2000, atr=40, bb_width=0.06)
        verdict = self._make_buy_verdict()

        # Synthesise a BTC snapshot where close < ema50 (bear regime)
        btc_bear = make_snapshot(symbol="BTC/USDT", close=60000, ema50=65000)

        orig_enabled = settings.regime_filter_enabled
        try:
            settings.regime_filter_enabled = True
            with patch("trading_engine.data.market_data.build_snapshot", return_value=btc_bear):
                decision = evaluate(verdict, snap)
                assert not decision.approved
                assert "regime filter" in decision.reason.lower()
        finally:
            settings.regime_filter_enabled = orig_enabled

    def test_regime_filter_allows_long_in_bull_market(self):
        """When BTC is above its 50-period MA, LONG crypto trades may proceed."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.config import settings

        snap = make_snapshot(symbol="ETH/USDT", asset_type="crypto", close=2000, atr=40, bb_width=0.06)
        verdict = self._make_buy_verdict()

        # Synthesise a BTC snapshot where close > ema50 (bull regime)
        btc_bull = make_snapshot(symbol="BTC/USDT", close=70000, ema50=65000)

        orig_enabled = settings.regime_filter_enabled
        try:
            settings.regime_filter_enabled = True
            with patch("trading_engine.data.market_data.build_snapshot", return_value=btc_bull):
                decision = evaluate(verdict, snap)
                # regime filter should NOT veto; other checks may still fire but not regime
                assert "regime filter" not in (decision.reason or "").lower()
        finally:
            settings.regime_filter_enabled = orig_enabled

    @patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("unit-test"))
    def test_regime_filter_disabled_bypasses_check(self, mock_build):
        """When regime_filter_enabled=False, BTC trend is ignored entirely."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.config import settings

        snap = make_snapshot(symbol="ETH/USDT", asset_type="crypto", close=2000, atr=40, bb_width=0.06)
        verdict = self._make_buy_verdict()

        orig_enabled = settings.regime_filter_enabled
        try:
            settings.regime_filter_enabled = False
            decision = evaluate(verdict, snap)
            # Regime filter is disabled, so it must not be the veto reason
            assert "regime filter" not in (decision.reason or "").lower()
        finally:
            settings.regime_filter_enabled = orig_enabled


class TestPaperTraderFees:
    def test_transaction_costs(self):
        from trading_engine.execution import paper_trader
        import shutil

        # Backup state file if exists
        backup_path = paper_trader.STATE_FILE.with_suffix(".json.bak")
        if paper_trader.STATE_FILE.exists():
            shutil.copy(paper_trader.STATE_FILE, backup_path)

        from unittest.mock import patch
        from trading_engine.config import settings
        try:
            # Delete active state to start fresh
            if paper_trader.STATE_FILE.exists():
                paper_trader.STATE_FILE.unlink()

            with patch.object(settings, "crypto_use_perpetuals", False):
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
        base = {
            "trend": 1.0,
            "momentum": 1.0,
            "volume": 1.0,
            "volatility": 1.0,
            "structure": 1.0,
            "orderflow": 1.0,
            "sentiment": 1.0,
            "macro": 1.0
        }
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

    def test_ny_range_sweep_bullish(self):
        from trading_engine.agents import structure_agent
        from trading_engine.config import settings
        
        # 4H HTF DataFrame representing the daily candle
        idx_4h = pd.date_range("2026-07-05 04:00:00", periods=5, freq="4h", tz="UTC")
        df_4h = pd.DataFrame({
            "high": [100.0, 102.0, 101.0, 100.0, 99.0],
            "low": [90.0, 91.0, 90.0, 89.0, 88.0],
            "close": [95.0, 96.0, 95.0, 94.0, 93.0],
        }, index=idx_4h)
        htf_snap = make_snapshot(close=93.0, df=df_4h, timeframe="4h")
        
        # 5m timeframe data: low wicks below 90.0 (e.g. 89.5), then closes back above 90.0 (e.g. 91.0)
        idx_5m = pd.date_range("2026-07-05 12:00:00", periods=4, freq="5m", tz="UTC")
        df_5m = pd.DataFrame({
            "open": [91.0, 91.0, 91.0, 91.0],
            "high": [92.0, 92.0, 92.0, 92.0],
            "low": [90.5, 89.5, 90.5, 90.5], # 2nd candle wicks below 90.0 range_low
            "close": [91.0, 91.0, 91.0, 91.0],
            "volume": [100.0, 100.0, 100.0, 100.0]
        }, index=idx_5m)
        
        snap = make_snapshot(close=91.0, df=df_5m, timeframe="5m")
        snap.htf_4h_snap = htf_snap
        
        with patch.object(settings, "scalping_mode", True):
            result = structure_agent.analyze(snap)
            
        assert result.signal == Signal.BUY
        assert "Bullish range sweep" in result.reason




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
        # Ensure ema50 < close so the regime filter (which also calls build_snapshot for
        # BTC) sees a "bull regime" and does not veto the trade.
        mock_5m_snap.ema50 = 90.0
        
        verdict = JudgeVerdict(
            approved=True, decision=Signal.BUY, confidence=80.0, agreement=6, disagreement=0,
            weighted_score=0.8, reasoning="Test", agent_reports=[]
        )
        
        # Patch build_snapshot to return the 5m snapshot when 5m timeframe is requested, and patch _load_risk_params to use settings defaults
        with patch("trading_engine.data.market_data.build_snapshot", return_value=mock_5m_snap), \
             patch("trading_engine.risk_agent._load_risk_params", return_value={}):
            result = risk_agent.evaluate(verdict, base_snap)
            
            assert result.approved is True
            assert result.entry_price == 99.5
            assert result.atr == 1.0
            
            # Since 1.0 * multiplier (1.5) is below 2.2% crypto floor (2.189), stop distance is widened to 2.2%
            expected_stop_dist = 99.5 * 0.022
            expected_sl = 99.5 - expected_stop_dist
            assert abs(result.stop_loss - expected_sl) < 1e-4
            
            # take profit = entry + (stop_distance * rr_ratio)
            expected_tp = 99.5 + (expected_stop_dist * risk_agent.settings.rr_ratio)
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
        
        # Patch build_snapshot to raise an exception, forcing fallback to 4H, and patch _load_risk_params
        with patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("API error")), \
             patch("trading_engine.risk_agent._load_risk_params", return_value={}):
            result = risk_agent.evaluate(verdict, base_snap)
            
            assert result.approved is True
            assert result.entry_price == 100.0
            assert result.atr == 2.0
            
            # stop loss = entry - (atr * atr_multiplier)
            expected_sl = 100.0 - (2.0 * risk_agent.settings.atr_multiplier)
            assert abs(result.stop_loss - expected_sl) < 1e-4


class TestPhase1AndPhase2:
    """Tests for Phase 1 (Fallback regime check, dynamic correlation) and Phase 2 (FIFO reconstruction)."""

    def test_regime_filter_fallback_success(self):
        """Verify that when primary snapshot fails, the regime filter calls fallback and makes a decision."""
        from trading_engine.risk_agent import evaluate
        from trading_engine.judge import JudgeVerdict
        
        snap = make_snapshot(symbol="ETH/USDT", asset_type="crypto", close=2000, atr=40, bb_width=0.06)
        verdict = JudgeVerdict(
            decision=Signal.BUY, confidence=85.0, agreement=7, disagreement=1,
            weighted_score=7.0, reasoning="mock", agent_reports=[], approved=True
        )

        # Stop the global patcher temporarily to test fallback specifically
        _fallback_patcher.stop()
        try:
            # 1. Fallback returns bull market (close=70000, ma=60000)
            with patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("primary failed")), \
                 patch("trading_engine.risk_agent._get_btc_regime_fallback", return_value=(70000.0, 60000.0)) as mock_fall:
                decision = evaluate(verdict, snap)
                assert "regime filter" not in (decision.reason or "").lower()
                mock_fall.assert_called_once()

            # 2. Fallback returns bear market (close=55000, ma=60000)
            with patch("trading_engine.data.market_data.build_snapshot", side_effect=RuntimeError("primary failed")), \
                 patch("trading_engine.risk_agent._get_btc_regime_fallback", return_value=(55000.0, 60000.0)) as mock_fall:
                decision = evaluate(verdict, snap)
                assert not decision.approved
                assert "regime filter" in (decision.reason or "").lower()
                mock_fall.assert_called_once()
        finally:
            _fallback_patcher.start()

    def test_dynamic_correlation_calculation(self):
        """Verify that check_correlation computes dynamic rolling correlation directly for all assets."""
        from trading_engine.risk_agent import check_correlation
        
        # Synthesise two snapshots
        snap_a = make_snapshot(symbol="BTC/USDT")
        snap_b = make_snapshot(symbol="AAPL/USDT")  # Different class, no static group in common
        
        # Mock rolling correlation calculation to return 0.92 (high correlation)
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.92):
            multiplier, reason = check_correlation("BTC/USDT", snap_a, {"AAPL/USDT": snap_b})
            assert multiplier == 0.0
            assert "Correlation veto" in reason

        # Mock rolling correlation calculation to return 0.78 (soft correlation)
        with patch("trading_engine.risk_agent._compute_price_correlation", return_value=0.78):
            multiplier, reason = check_correlation("BTC/USDT", snap_a, {"AAPL/USDT": snap_b})
            assert multiplier == 0.5
            assert "Correlation soft adjustment" in reason

    def test_live_trader_fifo_reconstruction(self):
        """Verify that sync_with_broker parses execution history symmetrically using FIFO logic."""
        from trading_engine.execution.live_trader import sync_with_broker, Position, LivePortfolio, _save_state
        
        portfolio = LivePortfolio(account_size=10000.0, cash=10000.0)
        # Clear positions to force reconstruction
        portfolio.positions = []
        portfolio.closed_trades = []
        _save_state(portfolio)

        # Mock trades: Sell to open short, Buy to close short
        mock_trades = [
            {
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": 60000.0,
                "cost": 600.0,
                "timestamp": 1718600000000,
                "fee": {"cost": 0.6, "currency": "USDT"}
            },
            {
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 59000.0,
                "cost": 590.0,
                "timestamp": 1718610000000,
                "fee": {"cost": 0.59, "currency": "USDT"}
            }
        ]

        with patch("trading_engine.execution.live_trader.settings.trading_mode", "live"), \
             patch("trading_engine.storage.db.get_db_closed_trades", return_value=[]), \
             patch("trading_engine.execution.live_trader.get_bybit_exchange") as mock_ex_getter:
            
            mock_ex = mock_ex_getter.return_value
            mock_ex.fetch_open_orders.return_value = []
            # Mock fetch_my_trades to return [] for spot and mock_trades for linear
            mock_ex.fetch_my_trades.side_effect = [[], mock_trades]
            
            success = sync_with_broker()
            assert success is True
            
            from trading_engine.execution.live_trader import _load_state
            re_portfolio = _load_state()
            
            # Should have reconstructed 0 open positions and 1 closed trade (short)
            assert len(re_portfolio.positions) == 0
            assert len(re_portfolio.closed_trades) == 1
            
            closed_pos = re_portfolio.closed_trades[0]
            assert closed_pos.direction == "short"
            assert closed_pos.entry_price == 60000.0
            assert closed_pos.exit_price == 59000.0
            assert closed_pos.status == "closed"
            assert closed_pos.pnl_usd > 0

    def test_short_position_multiplier_sizing_reduction(self):
        """Verify that when risk evaluates a SELL/short signal, it applies the short_position_multiplier setting."""
        from trading_engine import risk_agent
        from trading_engine.judge import JudgeVerdict
        
        # Base snapshot
        base_df = pd.DataFrame({
            "high": np.ones(50) * 110,
            "low": np.ones(50) * 90,
            "close": np.ones(50) * 100,
        })
        base_snap = make_snapshot(close=100.0, df=base_df, timeframe="4h")
        base_snap.atr = 2.0
        base_snap.bb_width = 0.05
        
        # Mock 5m snapshot
        mock_5m_df = pd.DataFrame({
            "high": np.ones(50) * 100,
            "low": np.ones(50) * 99,
            "close": np.ones(50) * 99.5,
        })
        mock_5m_snap = make_snapshot(close=99.5, df=mock_5m_df, timeframe="5m")
        mock_5m_snap.atr = 1.0
        mock_5m_snap.ema50 = 90.0

        # BUY verdict
        verdict_buy = JudgeVerdict(
            approved=True, decision=Signal.BUY, confidence=80.0, agreement=6, disagreement=0,
            weighted_score=0.8, reasoning="Test", agent_reports=[]
        )
        
        # SELL verdict (Short)
        verdict_sell = JudgeVerdict(
            approved=True, decision=Signal.SELL, confidence=80.0, agreement=6, disagreement=0,
            weighted_score=0.8, reasoning="Test", agent_reports=[]
        )
        
        # We test with different short multipliers (e.g. 0.75 and 0.5)
        with patch("trading_engine.data.market_data.build_snapshot", return_value=mock_5m_snap), \
             patch("trading_engine.risk_agent._load_risk_params", return_value={}), \
             patch("trading_engine.risk_agent.settings.short_position_multiplier", 0.5):
            
            # For buy, short multiplier shouldn't be applied
            res_buy = risk_agent.evaluate(verdict_buy, base_snap)
            assert res_buy.approved is True
            size_buy = res_buy.position_size_pct
            
            # For sell, short multiplier (0.5) should be applied, yielding half the size
            res_sell = risk_agent.evaluate(verdict_sell, base_snap)
            assert res_sell.approved is True
            size_sell = res_sell.position_size_pct
            
            # Verify sizing reduction
            assert abs(size_sell - (size_buy * 0.5)) < 1e-4

    def test_live_trader_reconstruction_with_take_profit_order(self):
        """Verify that sync_with_broker correctly reconstructs both sl_order_id and tp_order_id."""
        from trading_engine.execution.live_trader import sync_with_broker, Position, LivePortfolio, _save_state
        
        portfolio = LivePortfolio(account_size=10000.0, cash=10000.0)
        # Clear positions to force reconstruction
        portfolio.positions = []
        portfolio.closed_trades = []
        _save_state(portfolio)

        # Mock two open conditional orders on Bybit: one Stop Loss (price < entry) and one Take Profit (price > entry)
        # Entry price is 60000.0 (from first trade), so:
        # SL order: triggerPrice = 58000.0
        # TP order: triggerPrice = 66000.0
        mock_open_orders = [
            {
                "id": "sl_order_123",
                "symbol": "BTC/USDT",
                "triggerPrice": 58000.0,
            },
            {
                "id": "tp_order_456",
                "symbol": "BTC/USDT",
                "triggerPrice": 66000.0,
            }
        ]

        mock_trades = [
            {
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 60000.0,
                "cost": 600.0,
                "timestamp": 1718600000000,
                "fee": {"cost": 0.6, "currency": "USDT"}
            }
        ]

        with patch("trading_engine.execution.live_trader.settings.trading_mode", "live"), \
             patch("trading_engine.execution.live_trader.get_bybit_exchange") as mock_ex_getter:
            
            mock_ex = mock_ex_getter.return_value
            # fetch_open_orders returns our mock open conditional orders
            mock_ex.fetch_open_orders.return_value = mock_open_orders
            # fetch_balance returns positive BTC balance to keep spot position open
            mock_ex.fetch_balance.return_value = {'total': {'BTC': 1.0, 'USDT': 10000.0}, 'USDT': {'free': 10000.0}}
            # fetch_my_trades returns mock_trades for spot (first call), and [] for linear (second call)
            mock_ex.fetch_my_trades.side_effect = [mock_trades, []]
            
            success = sync_with_broker()
            assert success is True
            
            from trading_engine.execution.live_trader import _load_state
            re_portfolio = _load_state()
            
            assert len(re_portfolio.positions) == 1
            open_pos = re_portfolio.positions[0]
            
            # Verify reconstructed fields
            assert open_pos.symbol == "BTC/USDT"
            assert open_pos.direction == "long"
            assert open_pos.entry_price == 60000.0
            assert open_pos.sl_order_id == "sl_order_123"
            assert open_pos.tp_order_id == "tp_order_456"
            assert open_pos.stop_loss == 58000.0
            assert open_pos.take_profit == 66000.0

    def test_live_trader_dust_balance_filter(self):
        """Verify that sync_with_broker ignores Spot balances valued at less than $10."""
        from trading_engine.execution.live_trader import sync_with_broker, Position, LivePortfolio, _save_state
        
        portfolio = LivePortfolio(account_size=10000.0, cash=10000.0)
        portfolio.positions = []
        portfolio.closed_trades = []
        _save_state(portfolio)

        mock_tickers = {
            "NEAR/USDT": {"last": 2.2},
            "GRASS/USDT": {"last": 0.42}
        }

        # GRASS: 0.0368 * 0.42 = 0.015 USD (dust)
        # NEAR: 10.0 * 2.2 = 22.0 USD (non-dust)
        mock_balance = {
            'total': {
                'USDT': 10000.0,
                'NEAR': 10.0,
                'GRASS': 0.0368
            },
            'USDT': {'free': 10000.0}
        }

        mock_trades = [
            {
                "symbol": "NEAR/USDT",
                "side": "buy",
                "price": 2.2,
                "cost": 22.0,
                "timestamp": 1718600000000,
                "fee": {"cost": 0.022, "currency": "USDT"}
            },
            {
                "symbol": "GRASS/USDT",
                "side": "buy",
                "price": 0.42,
                "cost": 0.015,
                "timestamp": 1718600000000,
                "fee": {"cost": 0.0001, "currency": "USDT"}
            }
        ]

        with patch("trading_engine.execution.live_trader.settings.trading_mode", "live"), \
             patch("trading_engine.execution.live_trader.get_bybit_exchange") as mock_ex_getter:
            
            mock_ex = mock_ex_getter.return_value
            mock_ex.fetch_open_orders.return_value = []
            mock_ex.fetch_balance.return_value = mock_balance
            mock_ex.fetch_tickers.return_value = mock_tickers
            mock_ex.fetch_my_trades.side_effect = [mock_trades, []]
            
            success = sync_with_broker()
            assert success is True
            
            from trading_engine.execution.live_trader import _load_state
            re_portfolio = _load_state()
            
            # Should only reconstruct 1 position (NEAR/USDT). GRASS/USDT should be ignored as dust.
            assert len(re_portfolio.positions) == 1
            assert re_portfolio.positions[0].symbol == "NEAR/USDT"

    def test_live_trader_database_reconstruction_and_sync(self):
        """Verify that sync_with_broker correctly loads closed trades from DB and merges with Bybit trades."""
        from trading_engine.execution.live_trader import sync_with_broker, Position, LivePortfolio, _save_state
        
        portfolio = LivePortfolio(account_size=10000.0, cash=10000.0)
        portfolio.positions = []
        portfolio.closed_trades = []
        _save_state(portfolio)

        # 1. Mock DB returning 1 closed trade (LIT/USDT)
        mock_db_trades = [{
            "symbol": "LIT/USDT",
            "direction": "long",
            "entry_price": 1.70,
            "exit_price": 1.80,
            "size_usd": 599.0,
            "pnl_usd": 35.0,
            "fee_usd": 0.6,
            "opened_at": "2026-06-15T10:00:00+00:00",
            "closed_at": "2026-06-15T11:00:00+00:00",
            "status": "closed"
        }]

        # 2. Mock Bybit trade history returning 2 closed trades:
        # One is a duplicate of the DB trade (LIT/USDT)
        # The other is a new trade (NEAR/USDT)
        mock_bybit_trades = [
            {
                "symbol": "LIT/USDT",
                "side": "buy",
                "price": 1.70,
                "cost": 599.0,
                "timestamp": 1781517600000, # 2026-06-15T10:00:00 in ms
                "fee": {"cost": 0.3, "currency": "USDT"}
            },
            {
                "symbol": "LIT/USDT",
                "side": "sell",
                "price": 1.80,
                "cost": 599.0,
                "timestamp": 1781521200000, # 2026-06-15T11:00:00 in ms
                "fee": {"cost": 0.3, "currency": "USDT"}
            },
            {
                "symbol": "NEAR/USDT",
                "side": "buy",
                "price": 2.2,
                "cost": 220.0,
                "timestamp": 1781604000000, # 2026-06-16T10:00:00 in ms
                "fee": {"cost": 0.22, "currency": "USDT"}
            },
            {
                "symbol": "NEAR/USDT",
                "side": "sell",
                "price": 2.0,
                "cost": 200.0,
                "timestamp": 1781607600000, # 2026-06-16T11:00:00 in ms
                "fee": {"cost": 0.20, "currency": "USDT"}
            }
        ]

        with patch("trading_engine.execution.live_trader.settings.trading_mode", "live"), \
             patch("trading_engine.storage.db.get_db_closed_trades", return_value=mock_db_trades), \
             patch("trading_engine.storage.db.sync_closed_trades_to_db") as mock_sync, \
             patch("trading_engine.execution.live_trader.get_bybit_exchange") as mock_ex_getter:
            
            mock_ex = mock_ex_getter.return_value
            mock_ex.fetch_open_orders.return_value = []
            mock_ex.fetch_balance.return_value = {'total': {'USDT': 10000.0}, 'USDT': {'free': 10000.0}}
            mock_ex.fetch_my_trades.side_effect = [mock_bybit_trades, []]
            
            success = sync_with_broker()
            assert success is True
            
            from trading_engine.execution.live_trader import _load_state
            re_portfolio = _load_state()
            
            # Reconstructed portfolio should have exactly 2 closed trades (LIT/USDT and NEAR/USDT)
            # The duplicate LIT/USDT should have been merged/deduplicated.
            assert len(re_portfolio.closed_trades) == 2
            
            symbols = [t.symbol for t in re_portfolio.closed_trades]
            assert "LIT/USDT" in symbols
            assert "NEAR/USDT" in symbols
            
            # Verify stats are correctly calculated over the merged list
            assert re_portfolio.win_count == 1
            assert re_portfolio.loss_count == 1
            
            # Save state is triggered inside sync_with_broker, which should call db.sync_closed_trades_to_db
            mock_sync.assert_called_once()
            called_trades = mock_sync.call_args[0][0]
            assert len(called_trades) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

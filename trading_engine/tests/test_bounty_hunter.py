"""
trading_engine/tests/test_bounty_hunter.py
Unit tests for Bounty Hunter scanner module.
"""
import pytest
from unittest.mock import patch, MagicMock
from trading_engine import bounty_hunter
from trading_engine.config import settings


@pytest.fixture
def mock_massive_key():
    with patch.object(settings, "massive_api_key", "mock_massive_key"):
        yield


@pytest.fixture(autouse=True)
def clear_cooldown():
    bounty_hunter._SCAN_COOLDOWN.clear()


class TestBountyHunterDateResolver:
    @patch("trading_engine.bounty_hunter.get_with_retry")
    def test_get_latest_trading_date_success(self, mock_get, mock_massive_key):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"resultsCount": 100, "results": [{"T": "AAPL"}]}
        mock_get.return_value = mock_resp
        
        date = bounty_hunter.get_latest_trading_date()
        assert date is not None
        # It should try today or previous days and succeed on the first one
        assert mock_get.called


class TestBountyHunterCryptoScan:
    @patch("ccxt.bybit")
    def test_scan_bybit_crypto_filtering(self, mock_ccxt_bybit):
        mock_exchange = MagicMock()
        mock_ccxt_bybit.return_value = mock_exchange
        
        # Mock markets mapping
        mock_exchange.markets = {
            "BTC/USDT": {"spot": True},
            "ETH/USDT": {"spot": True},
            "SOL/USDT": {"spot": True},
            "XRP/BTC": {"spot": True},
            "DOGE/USDT": {"spot": True},
            "LTC/USDT:USDT": {"spot": False},
        }
        
        # Mock fetch_tickers output
        mock_exchange.fetch_tickers.return_value = {
            "BTC/USDT": {"quoteVolume": 10_000_000.0, "percentage": 2.5, "symbol": "BTC/USDT"},
            "ETH/USDT": {"quoteVolume": 5_000_000.0, "percentage": -3.5, "symbol": "ETH/USDT"},
            "SOL/USDT": {"quoteVolume": 500_000.0, "percentage": -1.2, "symbol": "SOL/USDT"},  # too low vol
            "XRP/BTC": {"quoteVolume": 2_000_000.0, "percentage": -5.0, "symbol": "XRP/BTC"},    # not USDT
            "DOGE/USDT": {"quoteVolume": 2_500_000.0, "percentage": -8.0, "symbol": "DOGE/USDT"},
            "LTC/USDT:USDT": {"quoteVolume": 10_000_000.0, "percentage": -15.0, "symbol": "LTC/USDT:USDT"},  # swap, should be filtered
        }

        # Test "oversold" mode (biggest decliners first: DOGE/USDT -8%, ETH/USDT -3.5%, BTC/USDT +2.5%)
        selected = bounty_hunter.scan_bybit_crypto(mode="oversold", limit=2)
        assert len(selected) == 2
        assert selected[0] == "DOGE/USDT"
        assert selected[1] == "ETH/USDT"

        # Test "momentum" mode (biggest gainers first: BTC/USDT +2.5%, ETH/USDT -3.5%, DOGE/USDT -8%)
        selected_mom = bounty_hunter.scan_bybit_crypto(mode="momentum", limit=2)
        assert len(selected_mom) == 2
        assert selected_mom[0] == "BTC/USDT"
        assert selected_mom[1] == "ETH/USDT"


class TestBountyHunterStockScan:
    @patch("trading_engine.bounty_hunter.get_latest_trading_date")
    @patch("trading_engine.bounty_hunter.get_with_retry")
    def test_scan_massive_stocks_filtering(self, mock_get, mock_get_date, mock_massive_key):
        mock_get_date.return_value = "2026-06-10"
        
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "results": [
                {"T": "AAPL", "o": 170.0, "c": 175.0, "v": 1_000_000.0},  # +2.94% change, $175m vol
                {"T": "TSLA", "o": 180.0, "c": 171.0, "v": 2_000_000.0},  # -5.00% change, $342m vol
                {"T": "XYZ", "o": 4.0, "c": 4.5, "v": 1_000_000.0},       # under $5 price
                {"T": "PQR", "o": 100.0, "c": 101.0, "v": 1_000.0},        # under 500k volume
                {"T": "NVDA", "o": 120.0, "c": 118.0, "v": 5_000_000.0},  # -1.67% change, $590m vol
            ]
        }
        mock_get.return_value = mock_resp

        # Test "oversold" (biggest decliners: TSLA -5.00%, NVDA -1.67%, AAPL +2.94%)
        selected = bounty_hunter.scan_massive_stocks(mode="oversold", limit=2)
        assert len(selected) == 2
        assert selected[0] == "TSLA"
        assert selected[1] == "NVDA"


class TestBountyHunterExecution:
    @patch("trading_engine.bounty_hunter.scan_bybit_crypto")
    @patch("trading_engine.bounty_hunter.scan_massive_stocks")
    @patch("trading_engine.bounty_hunter.rank_and_enrich_candidates")
    @patch("trading_engine.bounty_hunter.check_preflight_probability")
    @patch("trading_engine.bounty_hunter.run_pipeline")
    def test_run_bounty_hunt(self, mock_pipeline, mock_preflight, mock_enrich, mock_stocks, mock_crypto):
        mock_crypto.return_value = ["BTC/USDT", "ETH/USDT"]
        mock_stocks.return_value = ["AAPL"]
        mock_enrich.side_effect = lambda symbols, mode, limit: (symbols[:limit], {s: MagicMock() for s in symbols[:limit]})
        mock_preflight.return_value = (True, 100.0, "mocked")
        
        mock_sig = MagicMock()
        mock_sig.final_action = "BUY"
        mock_sig.entry_price = 100.0
        mock_sig.stop_loss = 90.0
        mock_sig.take_profit = 120.0
        mock_sig.position_size_usd = 20.0
        mock_sig.verdict = {"confidence": 85.0, "agreement": 7}
        mock_sig.reasoning = "Test reasoning"
        mock_pipeline.return_value = mock_sig

        results = bounty_hunter.run_bounty_hunt(mode="oversold", crypto_limit=2, stock_limit=1, scan_cfds=False)
        
        assert len(results) == 3
        assert results[0]["symbol"] == "BTC/USDT"
        assert results[0]["final_action"] == "BUY"
        assert results[2]["symbol"] == "AAPL"
        
        assert mock_pipeline.call_count == 3


class TestBountyHunterWatchlistAndHotMode:
    @patch("ccxt.bybit")
    def test_scan_bybit_crypto_watchlist(self, mock_ccxt_bybit):
        mock_exchange = MagicMock()
        mock_ccxt_bybit.return_value = mock_exchange
        mock_exchange.markets = {
            "BTC/USDT": {"spot": True},
            "ETH/USDT": {"spot": True},
            "DOGE/USDT": {"spot": True},
        }
        mock_exchange.fetch_tickers.return_value = {
            "BTC/USDT": {"quoteVolume": 5_000_000.0, "percentage": 2.0, "symbol": "BTC/USDT"},
            "ETH/USDT": {"quoteVolume": 3_000_000.0, "percentage": -1.0, "symbol": "ETH/USDT"},
            "DOGE/USDT": {"quoteVolume": 10_000_000.0, "percentage": -5.0, "symbol": "DOGE/USDT"},
        }
        
        # Only BTC/USDT and ETH/USDT in watchlist
        selected = bounty_hunter.scan_bybit_crypto(mode="oversold", limit=3, watchlist=["BTC/USDT", "ETH/USDT"])
        assert "DOGE/USDT" not in selected
        assert "BTC/USDT" in selected
        assert "ETH/USDT" in selected

    @patch("trading_engine.bounty_hunter.get_latest_trading_date")
    @patch("trading_engine.bounty_hunter.get_with_retry")
    def test_scan_massive_stocks_watchlist(self, mock_get, mock_get_date, mock_massive_key):
        mock_get_date.return_value = "2026-06-10"
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "results": [
                {"T": "AAPL", "o": 170.0, "c": 175.0, "v": 1_000_000.0},
                {"T": "TSLA", "o": 180.0, "c": 171.0, "v": 2_000_000.0},
            ]
        }
        mock_get.return_value = mock_resp
        
        # Only TSLA in watchlist
        selected = bounty_hunter.scan_massive_stocks(mode="oversold", limit=2, watchlist=["TSLA"])
        assert len(selected) == 1
        assert selected[0] == "TSLA"

    @patch("trading_engine.data.market_data.build_snapshot")
    @patch("trading_engine.bounty_hunter.fetch_orderbook_imbalance")
    @patch("trading_engine.utils.scrapers.get_social_sentiment_context")
    def test_rank_and_enrich_candidates_hot(self, mock_social, mock_imbalance, mock_snapshot):
        mock_snap = MagicMock()
        mock_snap.rsi = 50.0
        mock_snap.rel_volume = 3.0
        mock_snapshot.return_value = mock_snap
        mock_imbalance.return_value = 0.8
        
        # 2 tweets, 1 line stocktwits, 1 news headline = 4 mentions
        mock_social.return_value = {
            "tweets": ["Tweet1", "Tweet2"],
            "stocktwits_raw": "Post1\n",
            "news_headlines": ["Headline1"]
        }
        
        ranked, snaps = bounty_hunter.rank_and_enrich_candidates(["BTC/USDT"], "hot", limit=1)
        assert len(ranked) == 1


class TestBountyHunterPreflightCheck:
    def test_check_preflight_probability_hard_vetoes(self):
        mock_snap = MagicMock()
        mock_snap.close = 100.0
        mock_snap.realized_vol = 0.5
        mock_snap.df = MagicMock()
        mock_snap.df.empty = True
        mock_snap.fear_greed_index = None

        # 1. Zero ATR veto
        mock_snap.atr = 0.0
        mock_snap.bb_width = 0.05
        is_viable, prob, reason = bounty_hunter.check_preflight_probability(mock_snap, "oversold")
        assert not is_viable
        assert prob == 0.0
        assert "Veto: ATR or price is zero" in reason

        # 2. Too volatile BB width veto
        mock_snap.atr = 2.0
        mock_snap.bb_width = 0.15
        is_viable, prob, reason = bounty_hunter.check_preflight_probability(mock_snap, "oversold")
        assert not is_viable
        assert prob == 0.0
        assert "Veto: Market too volatile" in reason

        # 3. Stop loss too wide veto (ATR=6.0 -> SL pct = 1.5 * 6 / 100 = 9% > 8%)
        mock_snap.atr = 6.0
        mock_snap.bb_width = 0.05
        is_viable, prob, reason = bounty_hunter.check_preflight_probability(mock_snap, "oversold")
        assert not is_viable
        assert prob == 0.0
        assert "Veto: Stop loss too wide" in reason

    def test_check_preflight_probability_scoring(self):
        mock_snap = MagicMock()
        mock_snap.close = 100.0
        mock_snap.atr = 1.5      # atr_pct = 1.5% (healthy)
        mock_snap.bb_width = 0.05 # good bb width
        mock_snap.realized_vol = 0.5
        mock_snap.df = MagicMock()
        mock_snap.df.empty = True
        mock_snap.fear_greed_index = None

        # Oversold Mode: RSI=25, StochRSI K&D = 10
        mock_snap.rsi = 25.0
        mock_snap.stoch_rsi_k = 10.0
        mock_snap.stoch_rsi_d = 10.0
        
        is_viable, prob, reason = bounty_hunter.check_preflight_probability(mock_snap, "oversold")
        # Base: 50
        # Vol check: Healthy atr (+10) -> 60
        # Good bb (+10) -> 70
        # RSI <= 30 (+20) -> 90
        # StochRSI < 20 (+15) -> 105 (clamped to 100)
        assert is_viable
        assert prob == 100.0
        assert "Oversold RSI" in reason

        # Momentum Mode: price above EMA200, EMAs aligned bullish, rel_vol=2.0, RSI=65
        # We need a non-empty df to calculate trend
        mock_snap.df.empty = False
        import pandas as pd
        mock_snap.df = pd.DataFrame({"close": [96.0, 97.0, 98.0, 99.0, 100.0]}) # price increasing -> bullish trend
        mock_snap.ema20 = 95.0
        mock_snap.ema50 = 90.0
        mock_snap.ema200 = 80.0
        mock_snap.rel_volume = 2.0
        mock_snap.vwap = 97.0
        mock_snap.rsi = 65.0
        
        is_viable, prob, reason = bounty_hunter.check_preflight_probability(mock_snap, "momentum")
        # Base: 50
        # Vol check: Healthy atr (+10) -> 60
        # Good bb (+10) -> 70
        # Bullish EMA alignment (+20) -> 90
        # Close > EMA200 (+10) -> 100
        # rel_vol >= 1.5 (+15) -> 115
        # Close > vwap (+10) -> 125
        # RSI 50-75 (+15) -> 140 -> clamped to 100
        assert is_viable
        assert prob == 100.0

    @patch("trading_engine.bounty_hunter.scan_bybit_crypto")
    @patch("trading_engine.bounty_hunter.scan_massive_stocks")
    @patch("trading_engine.bounty_hunter.rank_and_enrich_candidates")
    @patch("trading_engine.data.market_data.build_snapshot")
    @patch("trading_engine.bounty_hunter.run_pipeline")
    def test_run_bounty_hunt_preflight_filtering(self, mock_pipeline, mock_snapshot, mock_enrich, mock_stocks, mock_crypto):
        # We have BTC/USDT (fails pre-flight) and ETH/USDT (passes pre-flight)
        mock_crypto.return_value = ["BTC/USDT", "ETH/USDT"]
        mock_stocks.return_value = []
        mock_enrich.side_effect = lambda symbols, mode, limit: (symbols[:limit], {"BTC/USDT": mock_snap_btc, "ETH/USDT": mock_snap_eth})
        
        # BTC/USDT snapshot: bad ATR or BB width -> fails
        mock_snap_btc = MagicMock()
        mock_snap_btc.close = 100.0
        mock_snap_btc.atr = 1.0
        mock_snap_btc.bb_width = 0.15 # Vetoed (too high BB width)
        
        # ETH/USDT snapshot: passes preflight
        mock_snap_eth = MagicMock()
        mock_snap_eth.close = 100.0
        mock_snap_eth.atr = 1.5
        mock_snap_eth.bb_width = 0.05
        mock_snap_eth.rsi = 25.0
        mock_snap_eth.stoch_rsi_k = 10.0
        mock_snap_eth.stoch_rsi_d = 10.0
        mock_snap_eth.df = MagicMock()
        mock_snap_eth.df.empty = True
        mock_snap_eth.fear_greed_index = None
        mock_snap_eth.realized_vol = 0.5
        
        mock_snapshot.side_effect = lambda symbol, timeframe: mock_snap_btc if symbol == "BTC/USDT" else mock_snap_eth
        
        # Pipeline mock for ETH
        mock_sig = MagicMock()
        mock_sig.final_action = "BUY"
        mock_sig.entry_price = 100.0
        mock_sig.stop_loss = 90.0
        mock_sig.take_profit = 120.0
        mock_sig.position_size_usd = 20.0
        mock_sig.verdict = {"confidence": 85.0, "agreement": 7}
        mock_sig.reasoning = "ETH passed"
        mock_pipeline.return_value = mock_sig

        results = bounty_hunter.run_bounty_hunt(mode="oversold", crypto_limit=2, stock_limit=0, scan_cfds=False)
        
        assert len(results) == 2
        
        # BTC should be rejected early (NO_TRADE)
        assert results[0]["symbol"] == "BTC/USDT"
        assert results[0]["final_action"] == "NO_TRADE"
        assert "pre-flight checklist" in results[0]["reasoning"]
        
        # ETH should proceed to pipeline (BUY)
        assert results[1]["symbol"] == "ETH/USDT"
        assert results[1]["final_action"] == "BUY"
        
        # Pipeline should only be called once (for ETH)
        assert mock_pipeline.call_count == 1



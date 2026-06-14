"""
trading_engine/tests/test_anomalies.py
Unit tests for mathematical anomaly detection and ranking.
"""
import pytest
from unittest.mock import patch, MagicMock
from trading_engine import bounty_hunter


class TestAnomalyCalculations:
    @patch("ccxt.bybit")
    def test_fetch_orderbook_imbalance_crypto(self, mock_ccxt_bybit):
        mock_exchange = MagicMock()
        mock_ccxt_bybit.return_value = mock_exchange
        
        # 10 bids, 5 asks
        mock_exchange.fetch_order_book.return_value = {
            "bids": [[100.0, 1.0], [99.0, 9.0]],
            "asks": [[101.0, 2.0], [102.0, 3.0]]
        }
        
        # Crypto symbol
        imbalance = bounty_hunter.fetch_orderbook_imbalance("BTC/USDT")
        # bid_vol = 1.0 + 9.0 = 10.0
        # ask_vol = 2.0 + 3.0 = 5.0
        # imbalance = 10.0 / 15.0 = 0.6666...
        assert imbalance == pytest.approx(0.666666, rel=1e-4)

    @patch("alpaca.data.historical.StockHistoricalDataClient")
    def test_fetch_orderbook_imbalance_stock(self, mock_alpaca_client):
        mock_client = MagicMock()
        mock_alpaca_client.return_value = mock_client
        
        mock_quote = MagicMock()
        mock_quote.bid_size = 300.0
        mock_quote.ask_size = 100.0
        
        mock_client.get_stock_latest_quote.return_value = {
            "AAPL": mock_quote
        }
        
        # Stock symbol
        imbalance = bounty_hunter.fetch_orderbook_imbalance("AAPL")
        # bid_size = 300
        # ask_size = 100
        # imbalance = 300 / 400 = 0.75
        assert imbalance == 0.75


class TestRankAndEnrichCandidates:
    @patch("trading_engine.data.market_data.build_snapshot")
    @patch("trading_engine.bounty_hunter.fetch_orderbook_imbalance")
    @patch("trading_engine.utils.scrapers.get_social_sentiment_context")
    def test_rank_and_enrich_candidates_oversold(self, mock_social, mock_imbalance, mock_snapshot):
        # Setup mocks
        mock_snap1 = MagicMock()
        mock_snap1.rsi = 25.0
        mock_snap1.rel_volume = 1.2
        
        mock_snap2 = MagicMock()
        mock_snap2.rsi = 45.0
        mock_snap2.rel_volume = 3.5  # High relative volume breakout
        
        mock_snapshot.side_effect = lambda symbol, timeframe: mock_snap1 if symbol == "BTC/USDT" else mock_snap2
        mock_imbalance.return_value = 0.6
        mock_social.return_value = {"tweets": [], "stocktwits_raw": "", "news_headlines": []}
        
        # Call rank_and_enrich
        ranked, snaps = bounty_hunter.rank_and_enrich_candidates(["BTC/USDT", "ETH/USDT"], "oversold", limit=2)
        
        assert len(ranked) == 2
        # BTC should have higher anomaly score in oversold mode due to much lower RSI (25 vs 45)
        # BTC score = (50 - 25)*1.5 + 1.2*2.0 + 0.6*5.0 = 37.5 + 2.4 + 3.0 = 42.9
        # ETH score = (50 - 45)*1.5 + 3.5*2.0 + 0.6*5.0 = 7.5 + 7.0 + 3.0 = 17.5
        assert ranked[0] == "BTC/USDT"
        assert ranked[1] == "ETH/USDT"

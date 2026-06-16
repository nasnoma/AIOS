"""
trading_engine/tests/test_live_trader.py
Unit tests for live_trader module and scheduler integration.
"""
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from trading_engine.config import settings
from trading_engine.execution import live_trader
from trading_engine.execution import paper_trader
from trading_engine.agents.base import Signal


@pytest.fixture(autouse=True)
def mock_state_file(tmp_path):
    """Isolate live_state.json storage to a temporary directory."""
    temp_file = tmp_path / "live_state_test.json"
    with patch("trading_engine.execution.live_trader.STATE_FILE", temp_file):
        yield temp_file


@pytest.fixture
def mock_bybit_keys():
    """Ensure mock keys are present to allow client initialization tests."""
    with patch.object(settings, "bybit_api_key", "mock_key"), \
         patch.object(settings, "bybit_api_secret", "mock_secret"):
        yield


@pytest.fixture
def mock_alpaca_keys():
    """Ensure mock keys are present to allow client initialization tests."""
    with patch.object(settings, "alpaca_api_key", "mock_key"), \
         patch.object(settings, "alpaca_secret_key", "mock_secret"):
        yield


class TestLiveTraderInitialization:
    def test_bybit_initialization_no_keys(self):
        with patch.object(settings, "bybit_api_key", ""), \
             patch.object(settings, "bybit_api_secret", ""):
            with pytest.raises(ValueError, match="Bybit API key and secret must be configured"):
                live_trader.get_bybit_exchange()

    def test_bybit_initialization_with_keys(self, mock_bybit_keys):
        exchange = live_trader.get_bybit_exchange()
        assert exchange is not None
        assert exchange.apiKey == "mock_key"
        assert exchange.secret == "mock_secret"

    def test_alpaca_initialization_no_keys(self):
        with patch.object(settings, "alpaca_api_key", ""), \
             patch.object(settings, "alpaca_secret_key", ""):
            with pytest.raises(ValueError, match="Alpaca API key and secret key must be configured"):
                live_trader.get_alpaca_client()

    def test_alpaca_initialization_with_keys(self, mock_alpaca_keys):
        client = live_trader.get_alpaca_client()
        assert client is not None


class TestOrderPlacement:
    @patch("trading_engine.execution.live_trader.get_bybit_exchange")
    def test_bybit_market_order(self, mock_get_exchange, mock_bybit_keys):
        mock_exchange = MagicMock()
        mock_get_exchange.return_value = mock_exchange
        
        mock_exchange.amount_to_precision.return_value = "0.015"
        mock_exchange.create_order.return_value = {
            "id": "12345",
            "average": 65100.0,
            "price": 65100.0,
        }

        fill_price = live_trader.place_bybit_market_order("BTC/USDT", "buy", 1000.0, 65000.0)
        
        assert fill_price == 65100.0
        mock_exchange.load_markets.assert_called_once()
        mock_exchange.amount_to_precision.assert_called_once_with("BTC/USDT", 1000.0 / 65000.0)
        mock_exchange.create_order.assert_called_once_with("BTC/USDT", "market", "buy", 0.015)

    @patch("trading_engine.execution.live_trader.get_alpaca_client")
    def test_alpaca_market_order(self, mock_get_client, mock_alpaca_keys):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        
        mock_order = MagicMock()
        mock_order.filled_avg_price = 155.5
        mock_client.submit_order.return_value = mock_order

        fill_price = live_trader.place_alpaca_market_order("AAPL", "buy", 10.0, 150.0)
        
        assert fill_price == 155.5
        mock_client.submit_order.assert_called_once()


class TestTradeLifecycle:
    @patch("trading_engine.execution.live_trader.place_bybit_market_order")
    def test_open_trade_crypto(self, mock_place_order, mock_bybit_keys):
        mock_place_order.return_value = 65000.0
        
        pos = live_trader.open_trade("BTC/USDT", "long", 64900.0, 1000.0, 60000.0, 75000.0)
        
        assert pos is not None
        assert pos.symbol == "BTC/USDT"
        assert pos.entry_price == 65000.0
        assert pos.size_usd == 1000.0
        assert pos.direction == "long"
        
        status = live_trader.get_status()
        assert status["open_positions"] == 1
        assert len(live_trader._load_state().positions) == 1

    @patch("trading_engine.execution.live_trader.place_bybit_linear_order")
    def test_open_trade_stock(self, mock_place_order, mock_alpaca_keys):
        mock_place_order.return_value = 170.0
        
        pos = live_trader.open_trade("AAPL", "long", 169.0, 1700.0, 150.0, 200.0)
        
        assert pos is not None
        assert pos.symbol == "AAPL/USDT:USDT"
        assert pos.entry_price == 170.0
        assert pos.size_usd == 1700.0
        
        status = live_trader.get_status()
        assert status["open_positions"] == 1
        
        # Verify call to place_bybit_linear_order
        mock_place_order.assert_called_once_with("AAPL/USDT:USDT", "buy", 1700.0, 169.0)

    def test_open_trade_short_rejected(self):
        pos = live_trader.open_trade("BTC/USDT", "short", 65000.0, 1000.0, 66000.0, 60000.0)
        assert pos is None
        status = live_trader.get_status()
        assert status["open_positions"] == 0

    @patch("trading_engine.execution.live_trader.place_bybit_market_order")
    def test_update_prices_hits_stop_loss(self, mock_place_order, mock_bybit_keys):
        # Open position
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        
        # Current price drops to 59500 (below 60000 Stop Loss)
        mock_place_order.return_value = 59500.0  # Fill price on sell close
        live_trader.update_prices({"BTC/USDT": 59500.0})
        
        status = live_trader.get_status()
        assert status["open_positions"] == 0
        assert status["win_count"] == 0
        assert status["loss_count"] == 1
        assert status["total_pnl"] < 0
        assert len(status["trades"]) == 1
        assert status["trades"][0]["status"] == "stopped"

    @patch("trading_engine.execution.live_trader.place_bybit_market_order")
    def test_update_prices_hits_take_profit(self, mock_place_order, mock_bybit_keys):
        # Open position
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        
        # Current price rises to 76000 (above 75000 Take Profit)
        mock_place_order.return_value = 76000.0  # Fill price on sell close
        live_trader.update_prices({"BTC/USDT": 76000.0})
        
        status = live_trader.get_status()
        assert status["open_positions"] == 0
        assert status["win_count"] == 1
        assert status["loss_count"] == 0
        assert status["total_pnl"] > 0
        assert len(status["trades"]) == 1
        assert status["trades"][0]["status"] == "closed"


class TestSchedulerRouting:
    @patch("trading_engine.orchestrator.run_all_assets")
    @patch("trading_engine.execution.live_trader.get_status")
    @patch("trading_engine.execution.paper_trader.get_status")
    def test_scheduler_routing_live(self, mock_paper_status, mock_live_status, mock_run_assets):
        from trading_engine import scheduler
        mock_live_status.return_value = {
            "portfolio_heat": 0.0,
            "open_positions": 0,
            "win_rate": 50.0,
        }
        mock_run_assets.return_value = []
        
        with patch.object(settings, "trading_mode", "live"):
            scheduler.run_signal_cycle()
            mock_live_status.assert_called_once()
            mock_paper_status.assert_not_called()

    @patch("trading_engine.orchestrator.run_all_assets")
    @patch("trading_engine.execution.live_trader.get_status")
    @patch("trading_engine.execution.paper_trader.get_status")
    def test_scheduler_routing_paper(self, mock_paper_status, mock_live_status, mock_run_assets):
        from trading_engine import scheduler
        mock_paper_status.return_value = {
            "portfolio_heat": 0.0,
            "open_positions": 0,
            "win_rate": 50.0,
        }
        mock_run_assets.return_value = []
        
        with patch.object(settings, "trading_mode", "paper"):
            scheduler.run_signal_cycle()
            mock_paper_status.assert_called_once()
            mock_live_status.assert_not_called()

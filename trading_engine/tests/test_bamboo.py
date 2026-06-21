import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import base64
import json
from fastapi.testclient import TestClient

from trading_engine.config import settings
from trading_engine.market_hours import classify_symbol, AssetClass, market_status
from trading_engine.utils.bamboo_client import bamboo_client
from trading_engine.execution import live_trader
from trading_engine.api.server import app

@pytest.fixture(autouse=True)
def mock_settings_bamboo():
    with patch.object(settings, "bamboo_username", "test_user"), \
         patch.object(settings, "bamboo_password", "test_pass"), \
         patch.object(settings, "bamboo_api_key", "test_api_key"), \
         patch.object(settings, "bamboo_user_id", "test_user_id"), \
         patch.object(settings, "bamboo_webhook_auth_hash", "test_auth_hash"):
        yield

@pytest.fixture
def mock_state_file(tmp_path):
    temp_file = tmp_path / "live_state_test.json"
    with patch("trading_engine.execution.live_trader.STATE_FILE", temp_file):
        yield temp_file

class TestBambooClient:
    @patch("requests.post")
    def test_login_and_token_caching(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "mock_jwt_token",
            "expires_in": 3600
        }
        mock_post.return_value = mock_resp

        # Clear token first
        bamboo_client.client_token = None
        bamboo_client.token_expiry = 0.0

        token = bamboo_client.get_client_token()
        assert token == "mock_jwt_token"
        assert bamboo_client.client_token == "mock_jwt_token"
        mock_post.assert_called_once()

        # Call again, should use cached token
        token2 = bamboo_client.get_client_token()
        assert token2 == "mock_jwt_token"
        assert mock_post.call_count == 1

    @patch("requests.get")
    @patch.object(bamboo_client, "get_client_token", return_value="mock_jwt")
    def test_get_portfolio_breakdown(self, mock_token, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cash": 12500.50, "equity": 45000.0}
        mock_get.return_value = mock_resp

        res = bamboo_client.get_portfolio_breakdown()
        assert res["cash"] == 12500.50
        mock_get.assert_called_once()

    @patch("requests.get")
    @patch.object(bamboo_client, "get_client_token", return_value="mock_jwt")
    def test_get_my_stocks(self, mock_token, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"symbol": "ZENITHBANK", "quantity": 100}]
        mock_get.return_value = mock_resp

        res = bamboo_client.get_my_stocks()
        assert len(res) == 1
        assert res[0]["symbol"] == "ZENITHBANK"

    @patch("requests.post")
    @patch.object(bamboo_client, "get_client_token", return_value="mock_jwt")
    def test_calculate_order(self, mock_token, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "fee": 50.0,
            "total_price": 5000.0,
            "price_per_share": 50.0,
            "quantity": 100,
            "order_price": 5050.0
        }
        mock_post.return_value = mock_resp

        res = bamboo_client.calculate_order("ZENITHBANK/NGX", "BUY", 100, 50.0)
        assert res["fee"] == 50.0
        assert res["quantity"] == 100

    @patch("requests.post")
    @patch.object(bamboo_client, "get_client_token", return_value="mock_jwt")
    def test_calculate_order_us(self, mock_token, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "fee": 1.5,
            "total_price": 1000.0,
            "price_per_share": 100.0,
            "quantity": 10.0,
            "order_price": 1001.5
        }
        mock_post.return_value = mock_resp

        res = bamboo_client.calculate_order("AAPL/BAMBOO", "BUY", 10.0, 100.0)
        assert res["fee"] == 1.5
        assert res["quantity"] == 10.0
        call_args = mock_post.call_args[1]
        assert "/api/lsx/us/" in mock_post.call_args[0][0]
        assert call_args["json"]["quantity"] == 10.0
        assert call_args["json"]["currency"] == "USD"

class TestMarketHoursNGX:
    def test_classification(self):
        assert classify_symbol("ZENITHBANK/NGX") == AssetClass.NGX_STOCK
        assert classify_symbol("GTCO:NGX") == AssetClass.NGX_STOCK
        assert classify_symbol("AAPL") == AssetClass.STOCK
        assert classify_symbol("AAPL/BAMBOO") == AssetClass.BAMBOO_US_STOCK
        assert classify_symbol("TSLA:BAMBOO_US") == AssetClass.BAMBOO_US_STOCK

    def test_ngx_hours(self):
        # Mon 10:00 UTC (11:00 WAT) -> Open
        dt_open = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = dt_open
            assert market_status("ZENITHBANK/NGX").is_open is True

        # Sunday 10:00 UTC -> Closed
        dt_weekend = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = dt_weekend
            assert market_status("ZENITHBANK/NGX").is_open is False

class TestLiveTraderNGX:
    @patch.object(bamboo_client, "calculate_order")
    @patch.object(bamboo_client, "place_order")
    @patch("trading_engine.market_hours.market_status")
    def test_open_trade_ngx(self, mock_status, mock_place, mock_calc, mock_state_file):
        # Mock market open
        ms = MagicMock()
        ms.is_open = True
        mock_status.return_value = ms

        mock_calc.return_value = {
            "fee": 10.0,
            "total_price": 1000.0,
            "price_per_share": 10.0,
            "quantity": 100,
            "order_price": 1010.0
        }
        mock_place.return_value = {"status": "accepted"}

        # Initialize portfolio cash
        portfolio = live_trader._load_state()
        portfolio.cash = 5000.0
        live_trader._save_state(portfolio)

        pos = live_trader.open_trade(
            symbol="ZENITHBANK/NGX",
            direction="long",
            entry=10.0,
            size_usd=1000.0,
            stop_loss=8.5,
            take_profit=13.0
        )

        assert pos is not None
        assert pos.symbol == "ZENITHBANK/NGX"
        assert pos.entry_price == 10.0
        assert pos.size_usd == 1000.0
        assert pos.fee_usd == 10.0
        assert pos.sl_order_id is None  # Bamboo doesn't support broker SL
        assert pos.tp_order_id is None  # Bamboo doesn't support broker TP

        # Check cash deduction: size_usd (1000) + entry_fee (10)
        updated_portfolio = live_trader._load_state()
        assert updated_portfolio.cash == 5000.0 - 1000.0 - 10.0

    @patch.object(bamboo_client, "calculate_order")
    @patch.object(bamboo_client, "place_order")
    @patch("trading_engine.market_hours.market_status")
    def test_open_trade_bamboo_us(self, mock_status, mock_place, mock_calc, mock_state_file):
        # Mock market open
        ms = MagicMock()
        ms.is_open = True
        mock_status.return_value = ms

        mock_calc.return_value = {
            "fee": 1.5,
            "total_price": 500.0,
            "price_per_share": 100.0,
            "quantity": 5.0,
            "order_price": 501.5
        }
        mock_place.return_value = {"status": "accepted"}

        portfolio = live_trader._load_state()
        portfolio.cash = 2000.0
        live_trader._save_state(portfolio)

        pos = live_trader.open_trade(
            symbol="AAPL/BAMBOO",
            direction="long",
            entry=100.0,
            size_usd=500.0,
            stop_loss=90.0,
            take_profit=120.0
        )

        assert pos is not None
        assert pos.symbol == "AAPL/BAMBOO"
        assert pos.entry_price == 100.0
        assert pos.size_usd == 500.0
        assert pos.fee_usd == 1.5

        # Check cash deduction
        updated_portfolio = live_trader._load_state()
        assert updated_portfolio.cash == 2000.0 - 500.0 - 1.5

    @patch("trading_engine.execution.live_trader.get_bybit_exchange")
    @patch.object(bamboo_client, "get_portfolio_breakdown")
    @patch.object(bamboo_client, "get_my_stocks")
    @patch("trading_engine.execution.live_trader.get_alpaca_client")
    def test_sync_with_broker_ngx(self, mock_alpaca, mock_my_stocks, mock_breakdown, mock_bybit, mock_state_file):
        # Mock Bybit
        mock_bybit_client = MagicMock()
        mock_bybit.return_value = mock_bybit_client
        mock_bybit_client.fetch_balance.return_value = {"USDT": {"free": 0.0}}

        # Mock Alpaca
        mock_alp_client = MagicMock()
        mock_alpaca.return_value = mock_alp_client
        mock_alp_client.get_all_positions.return_value = []
        mock_alp_acct = MagicMock()
        mock_alp_acct.cash = 1000.0
        mock_alp_acct.portfolio_value = 1000.0
        mock_alp_client.get_account.return_value = mock_alp_acct

        # Mock Bamboo
        from trading_engine.market_hours import AssetClass
        mock_breakdown.side_effect = lambda asset_class=None: \
            {"cash": 2500.0, "equity": 3500.0} if asset_class == AssetClass.NGX_STOCK else {"cash": 0.0, "equity": 0.0}
        mock_my_stocks.side_effect = lambda asset_class=None: \
            [{"symbol": "ZENITHBANK"}] if asset_class == AssetClass.NGX_STOCK else []

        portfolio = live_trader._load_state()
        portfolio.cash = 0.0
        portfolio.positions = [
            live_trader.Position(
                symbol="ZENITHBANK/NGX",
                direction="long",
                entry_price=10.0,
                size_usd=1000.0,
                stop_loss=8.5,
                take_profit=13.0,
                opened_at=datetime.now(timezone.utc).isoformat(),
                fee_usd=10.0,
                status="open"
            )
        ]
        live_trader._save_state(portfolio)

        success = live_trader.sync_with_broker()
        assert success is True

        updated = live_trader._load_state()
        # Combined cash: Alpaca cash (1000) + Bamboo cash (2500) = 3500
        assert updated.cash == 3500.0
        # Combined equity: Alpaca equity (1000) + Bamboo equity (3500) = 4500
        assert updated.account_size == 4500.0

class TestWebhooks:
    client = TestClient(app)

    def test_deposit_status_verification(self):
        # Without headers -> 401
        resp = self.client.get("/deposit/status/dep-123")
        assert resp.status_code == 401

        # With incorrect header -> 401
        resp = self.client.get("/deposit/status/dep-123", headers={"X-Bamboo-Webhook-Token": "bad"})
        assert resp.status_code == 401

        # With correct header -> 200
        resp = self.client.get("/deposit/status/dep-123", headers={"X-Bamboo-Webhook-Token": "test_auth_hash"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"

    @patch("trading_engine.api.server.send_message")
    def test_deposit_webhook(self, mock_send, mock_state_file):
        portfolio = live_trader._load_state()
        portfolio.cash = 1000.0
        portfolio.account_size = 1000.0
        live_trader._save_state(portfolio)

        payload = {
            "event_type": "deposit_status_update",
            "status": "Settlemented",
            "reference": "dep-456",
            "amount": 500.0
        }

        resp = self.client.post(
            "/api/webhooks/bamboo",
            json=payload,
            headers={"X-Bamboo-Webhook-Token": "test_auth_hash"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"

        updated = live_trader._load_state()
        assert updated.cash == 1500.0
        assert updated.account_size == 1500.0
        mock_send.assert_called_once()

    @patch("trading_engine.api.server.send_message")
    def test_trade_cancel_webhook(self, mock_send, mock_state_file):
        portfolio = live_trader._load_state()
        portfolio.cash = 3990.0
        pos = live_trader.Position(
            symbol="ZENITHBANK/NGX",
            direction="long",
            entry_price=10.0,
            size_usd=1000.0,
            stop_loss=8.5,
            take_profit=13.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            fee_usd=10.0,
            status="open"
        )
        portfolio.positions.append(pos)
        live_trader._save_state(portfolio)

        payload = {
            "event_type": "trade_status_update",
            "status": "rejected",
            "side": "BUY",
            "symbol": "ZENITHBANK",
            "order_id": "ord-789"
        }

        resp = self.client.post(
            "/api/webhooks/bamboo",
            json=payload,
            headers={"X-Bamboo-Webhook-Token": "test_auth_hash"}
        )
        assert resp.status_code == 200
        
        updated = live_trader._load_state()
        # Reserved cash (1000) + fee (10) should be refunded: 3990 + 1010 = 5000
        assert updated.cash == 5000.0
        assert len(updated.positions) == 0

    @patch("trading_engine.api.server.send_message")
    def test_trade_sell_filled_webhook(self, mock_send, mock_state_file):
        portfolio = live_trader._load_state()
        portfolio.cash = 4000.0
        pos = live_trader.Position(
            symbol="ZENITHBANK/NGX",
            direction="long",
            entry_price=10.0,
            size_usd=1000.0,
            stop_loss=8.5,
            take_profit=13.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            fee_usd=10.0,
            status="open"
        )
        portfolio.positions.append(pos)
        live_trader._save_state(portfolio)

        payload = {
            "event_type": "trade_status_update",
            "status": "filled",
            "side": "SELL",
            "symbol": "ZENITHBANK",
            "order_id": "ord-999",
            "price_per_share": 12.0,
            "fee": 12.0
        }

        resp = self.client.post(
            "/api/webhooks/bamboo",
            json=payload,
            headers={"X-Bamboo-Webhook-Token": "test_auth_hash"}
        )
        assert resp.status_code == 200
        
        updated = live_trader._load_state()
        assert len(updated.positions) == 0
        assert len(updated.closed_trades) == 1
        
        # PnL: entry=10, exit=12 -> 20% gain. gross_pnl = 200.0. net_pnl = 200.0 - exit_fee (12.0) = 188.0.
        # Cash: 4000 + size_usd (1000) + net_pnl (188) = 5188.0
        assert updated.cash == 5188.0

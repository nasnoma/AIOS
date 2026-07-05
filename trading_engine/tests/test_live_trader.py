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

    @patch("trading_engine.market_hours.market_status")
    @patch("trading_engine.execution.live_trader.place_bybit_linear_order")
    def test_open_trade_stock(self, mock_place_order, mock_market_status, mock_alpaca_keys):
        mock_place_order.return_value = 170.0
        mock_status = MagicMock()
        mock_status.is_open = True
        mock_market_status.return_value = mock_status
        
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
        with patch.object(settings, "bybit_api_key", ""), \
             patch.object(settings, "bybit_api_secret", ""):
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

    @patch("trading_engine.execution.live_trader._trigger_self_healing")
    @patch("trading_engine.execution.live_trader.place_bybit_market_order")
    def test_self_healing_trigger(self, mock_place_order, mock_trigger_healing, mock_bybit_keys):
        # Reset state file
        live_trader._save_state(live_trader.LivePortfolio(account_size=10000.0, cash=10000.0))
        
        # Configure thresholds
        settings.self_healing_consecutive_losses = 2
        settings.self_healing_cooldown_hours = 24.0

        # Loss 1
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        mock_place_order.return_value = 59500.0
        live_trader.update_prices({"BTC/USDT": 59500.0})
        
        # Assert healing NOT triggered (losses = 1)
        mock_trigger_healing.assert_not_called()
        
        # Loss 2
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        mock_place_order.return_value = 59500.0
        live_trader.update_prices({"BTC/USDT": 59500.0})
        
        # Assert healing triggered (losses = 2)
        mock_trigger_healing.assert_called_once_with("BTC/USDT")
        mock_trigger_healing.reset_mock()
        
        # Loss 3 (cooldown should prevent execution)
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        mock_place_order.return_value = 59500.0
        live_trader.update_prices({"BTC/USDT": 59500.0})
        
        # Assert healing NOT triggered again due to cooldown
        mock_trigger_healing.assert_not_called()
        
        # Win trade (resets consecutive losses)
        mock_place_order.return_value = 65000.0
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        mock_place_order.return_value = 76000.0
        live_trader.update_prices({"BTC/USDT": 76000.0})
        
        status = live_trader.get_status()
        assert status["self_healing_state"]["BTC/USDT"]["consecutive_losses"] == 0

    @patch("trading_engine.execution.live_trader._trigger_self_healing")
    @patch("trading_engine.execution.live_trader.get_bybit_exchange")
    @patch("trading_engine.execution.live_trader.place_bybit_market_order")
    def test_sync_with_broker_triggers_self_healing(self, mock_place_order, mock_get_exchange, mock_trigger_healing, mock_bybit_keys):
        # Reset state file with a LivePortfolio that has 1 open trade
        portfolio = live_trader.LivePortfolio(account_size=10000.0, cash=10000.0)
        # Open BTC position
        pos = live_trader.Position(
            symbol="BTC/USDT",
            direction="long",
            entry_price=65000.0,
            size_usd=1000.0,
            stop_loss=60000.0,
            take_profit=75000.0,
            opened_at="2026-06-15T10:00:00+00:00",
            status="open"
        )
        portfolio.positions.append(pos)
        # Set self-healing losses to 2 consecutive (requires 3, so next loss triggers)
        portfolio.self_healing_state["BTC/USDT"] = {"consecutive_losses": 2, "last_optimized_at": None}
        live_trader._save_state(portfolio)
        
        # Configure settings
        settings.self_healing_consecutive_losses = 3
        settings.trading_mode = "live"
        
        # Mock Bybit exchange
        mock_ex = MagicMock()
        mock_get_exchange.return_value = mock_ex
        
        # Mock active conditional orders (none left)
        mock_ex.fetch_open_orders.return_value = []
        
        # Mock active positions (empty, meaning broker closed it)
        mock_ex.fetch_positions.return_value = []
        mock_ex.fetch_balance.return_value = {"USDT": {"free": 10000.0}}
        
        # Mock fetch_my_trades to return the closing trade (executed at a loss)
        mock_ex.fetch_my_trades.return_value = [
            {
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": 59000.0, # Loss!
                "cost": 1000.0,
                "timestamp": 1781517600000, # 2026-06-15T11:00:00Z
                "fee": {"cost": 0.6, "currency": "USDT"}
            }
        ]
        
        # Run sync_with_broker
        success = live_trader.sync_with_broker()
        assert success is True
        
        # Verify it closed locally and triggered self-healing
        status = live_trader.get_status()
        assert status["self_healing_state"]["BTC/USDT"]["consecutive_losses"] == 3
        mock_trigger_healing.assert_called_once_with("BTC/USDT")


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


class TestNextSteps:
    @patch("trading_engine.execution.live_trader.get_bybit_exchange")
    def test_bybit_trigger_direction_parameters(self, mock_get_exchange, mock_bybit_keys):
        # Setup mock exchange
        mock_ex = MagicMock()
        mock_get_exchange.return_value = mock_ex
        mock_ex.price_to_precision.side_effect = lambda sym, p: str(p)
        mock_ex.amount_to_precision.side_effect = lambda sym, q: str(q)
        mock_ex.create_order.return_value = {
            "id": "mock_order_123",
            "average": 65000.0,
            "price": 65000.0,
        }

        # 1. Test Stop Loss Placement on Long crypto
        live_trader.open_trade("BTC/USDT", "long", 65000.0, 1000.0, 60000.0, 75000.0)
        
        # Verify stop loss order call parameters
        # create_order is called for: Spot order (1st), Stop Loss (2nd), Take Profit (3rd)
        sl_call = mock_ex.create_order.call_args_list[1]
        assert sl_call[1]["params"]["triggerDirection"] == "descending"
        
        tp_call = mock_ex.create_order.call_args_list[2]
        assert tp_call[1]["params"]["triggerDirection"] == "ascending"

        # 2. Test Stop Loss Ratchet Long trigger direction
        mock_ex.reset_mock()
        mock_ex.create_order.return_value = {
            "id": "mock_order_123",
            "average": 65000.0,
            "price": 65000.0,
        }
        pos = live_trader.Position(
            symbol="BTC/USDT",
            direction="long",
            entry_price=65000.0,
            size_usd=1000.0,
            stop_loss=61000.0,
            take_profit=75000.0,
            opened_at="2026-06-15T10:00:00+00:00",
            status="open"
        )
        live_trader._update_broker_stop_loss(pos)
        mock_ex.create_order.assert_called_once()
        assert mock_ex.create_order.call_args[1]["params"]["triggerDirection"] == "descending"

    def test_database_weights_and_timestamp_persistence(self):
        from trading_engine.storage import db
        from datetime import datetime, timezone
        
        # Test DB helper saving and loading weights
        weights_dict = {"ema_short": 12, "ema_long": 26}
        test_sym = "TEST/USDT"
        now_dt = datetime.now(timezone.utc)
        
        db.save_symbol_state(test_sym, weights=weights_dict, last_optimized_at=now_dt)
        
        # Fetch back
        state = db.get_symbol_state(test_sym)
        assert state is not None
        assert state["weights"] == weights_dict
        # naive comparison
        assert abs((state["last_optimized_at"] - now_dt).total_seconds()) < 1.0

        # Verify orchestrator loads weights from database
        from trading_engine.orchestrator import run_all_assets
        # Mock build_snapshot and run_agents_parallel
        with patch("trading_engine.orchestrator.build_snapshot") as mock_snap, \
             patch("trading_engine.orchestrator._run_agents_parallel") as mock_agents, \
             patch("trading_engine.orchestrator.judge_evaluate") as mock_judge, \
             patch("trading_engine.orchestrator.risk_evaluate") as mock_risk:
             
            snap = MagicMock()
            snap.close = 65000.0
            snap.rsi = 50.0
            snap.atr = 100.0
            snap.asset_type = "crypto"
            mock_snap.return_value = snap
            mock_agents.return_value = []

            from trading_engine.judge import JudgeVerdict
            from trading_engine.risk_agent import RiskDecision
            from trading_engine.agents.base import Signal

            mock_judge.return_value = JudgeVerdict(
                decision=Signal.HOLD,
                confidence=50.0,
                agreement=3,
                disagreement=1,
                weighted_score=0.0,
                reasoning="test reasoning",
                agent_reports=[],
                approved=False
            )
            mock_risk.return_value = RiskDecision(
                approved=False,
                reason="test reason",
                position_size_pct=0.0,
                position_size_usd=0.0,
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                risk_reward=0.0,
                max_loss_usd=0.0,
                atr=0.0,
                entry_price=65000.0,
                stop_loss=0.0,
                take_profit=0.0
            )
            
            from trading_engine import orchestrator
            orchestrator.run(test_sym, "5m", 0.0)
            
            # Verify judge was called with DB-stored weights
            mock_judge.assert_called_once()
            called_weights = mock_judge.call_args[1].get("agent_weights")
            assert called_weights == weights_dict

    @patch("trading_engine.execution.live_trader.os.kill")
    @patch("trading_engine.execution.live_trader.subprocess.Popen")
    def test_self_healing_concurrency_locking(self, mock_popen, mock_kill):
        # 1. Lock file has active PID (mock_kill doesn't throw)
        lock_file = Path("/tmp/self_healing.lock")
        lock_file.write_text("99999")
        mock_kill.return_value = None # Process is active
        
        live_trader._trigger_self_healing("BTC/USDT")
        
        # Verify it skipped launching (Popen not called)
        mock_popen.assert_not_called()
        
        # 2. Lock file has stale PID (mock_kill throws OSError)
        mock_kill.side_effect = OSError()
        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_popen.return_value = mock_proc
        
        live_trader._trigger_self_healing("BTC/USDT")
        
        # Verify it launched and wrote new PID to lock
        mock_popen.assert_called_once()
        assert lock_file.read_text().strip() == "11111"
        
        # Cleanup
        if lock_file.exists():
            lock_file.unlink()

    @patch("trading_engine.execution.live_trader.os.kill")
    @patch("trading_engine.execution.live_trader.subprocess.Popen")
    @patch("trading_engine.execution.live_trader._save_state")
    def test_self_healing_queue_sequential_execution(self, mock_save_state, mock_popen, mock_kill):
        from trading_engine.execution.live_trader import LivePortfolio, process_self_healing_queue
        
        # Setup portfolio with pending symbols
        portfolio = LivePortfolio()
        portfolio.pending_self_healing = ["BTC/USDT", "ETH/USDT"]
        
        # 1. Lock file has active PID (mock_kill doesn't throw)
        lock_file = Path("/tmp/self_healing.lock")
        lock_file.write_text("99999")
        mock_kill.return_value = None # Active process
        
        process_self_healing_queue(portfolio)
        
        # Verify it skipped launching (Popen not called) and queue remains unchanged
        mock_popen.assert_not_called()
        assert portfolio.pending_self_healing == ["BTC/USDT", "ETH/USDT"]
        
        # 2. Lock file is stale (mock_kill throws OSError)
        mock_kill.side_effect = OSError()
        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_popen.return_value = mock_proc
        
        process_self_healing_queue(portfolio)
        
        # Verify it launched for first symbol and popped it
        mock_popen.assert_called_once()
        assert portfolio.pending_self_healing == ["ETH/USDT"]
        assert lock_file.read_text().strip() == "11111"
        
        # Cleanup
        if lock_file.exists():
            lock_file.unlink()


class TestTrailingStopRatchet:
    @patch("trading_engine.execution.live_trader._update_broker_stop_loss")
    def test_apply_trailing_stop_long(self, mock_update_sl):
        mock_update_sl.return_value = "new_sl_id"
        from trading_engine.execution.live_trader import Position, _apply_trailing_stop

        # Long position: Entry=100.0, Stop=90.0 (stop_dist=10.0), InitialStop=90.0, ATR=5.0
        pos = Position(
            symbol="BTC/USDT",
            direction="long",
            entry_price=100.0,
            size_usd=1000.0,
            stop_loss=90.0,
            take_profit=130.0,
            opened_at="2026-07-05T00:00:00+00:00",
            atr=5.0,
            initial_stop_loss=90.0,
            trailing_high=100.0
        )

        # 1. Price moves to 104.0 (0.8×ATR profit). No ratchet (needs 1.0×ATR, which is 10.0 since stop_dist=10.0 > atr=5.0)
        _apply_trailing_stop(pos, 104.0)
        assert pos.stop_loss == 90.0
        assert not mock_update_sl.called

        # 2. Price moves to 111.0 (1.1×ATR profit). Moves to breakeven (100.0)
        _apply_trailing_stop(pos, 111.0)
        assert pos.stop_loss == 100.0
        assert mock_update_sl.called
        mock_update_sl.reset_mock()

        # 3. Test that stop_dist doesn't shrink when stop_loss changes.
        # Price moves to 116.0 (1.6×ATR profit). Moves to entry + 0.5×ATR (100.0 + 5.0 = 105.0)
        _apply_trailing_stop(pos, 116.0)
        assert pos.stop_loss == 105.0
        assert mock_update_sl.called
        mock_update_sl.reset_mock()

    @patch("trading_engine.execution.live_trader._update_broker_stop_loss")
    def test_apply_trailing_stop_short(self, mock_update_sl):
        mock_update_sl.return_value = "new_sl_id"
        from trading_engine.execution.live_trader import Position, _apply_trailing_stop

        # Short position: Entry=100.0, Stop=110.0 (stop_dist=10.0), InitialStop=110.0, ATR=5.0
        pos = Position(
            symbol="BTC/USDT",
            direction="short",
            entry_price=100.0,
            size_usd=1000.0,
            stop_loss=110.0,
            take_profit=70.0,
            opened_at="2026-07-05T00:00:00+00:00",
            atr=5.0,
            initial_stop_loss=110.0,
            trailing_low=100.0
        )

        # 1. Price moves to 96.0 (0.8×ATR profit). No ratchet
        _apply_trailing_stop(pos, 96.0)
        assert pos.stop_loss == 110.0
        assert not mock_update_sl.called

        # 2. Price moves to 89.0 (1.1×ATR profit). Moves to breakeven (100.0)
        _apply_trailing_stop(pos, 89.0)
        assert pos.stop_loss == 100.0
        assert mock_update_sl.called
        mock_update_sl.reset_mock()

        # 3. Price moves to 84.0 (1.6×ATR profit). Moves to entry - 0.5×ATR (100.0 - 5.0 = 95.0)
        _apply_trailing_stop(pos, 84.0)
        assert pos.stop_loss == 95.0
        assert mock_update_sl.called
        mock_update_sl.reset_mock()


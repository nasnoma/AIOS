import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from trading_engine.risk_agent import _get_5m_atr
from trading_engine.execution.live_trader import sync_with_broker, LivePortfolio, Position
from trading_engine.market_hours import AssetClass

def test_get_5m_atr_by_asset_class():
    """Asserts that _get_5m_atr skips 5m fetch for non-crypto assets and uses native timeframe."""
    # 1. Crypto asset: should call build_snapshot
    crypto_snap = MagicMock()
    crypto_snap.symbol = "BTC/USDT"
    crypto_snap.timeframe = "4h"
    crypto_snap.close = 50000.0
    crypto_snap.atr = 1000.0
    
    with patch("trading_engine.data.market_data.build_snapshot") as mock_build:
        mock_build.return_value = MagicMock(close=49950.0, atr=950.0)
        entry, atr = _get_5m_atr(crypto_snap)
        assert entry == 49950.0
        assert atr == 950.0
        mock_build.assert_called_once_with("BTC/USDT", timeframe="5m", is_htf=True)

    # 2. Non-crypto asset (Equities): should immediately return native timeframe atr without calling build_snapshot
    stock_snap = MagicMock()
    stock_snap.symbol = "AAPL"
    stock_snap.timeframe = "4h"
    stock_snap.close = 180.0
    stock_snap.atr = 5.0
    
    with patch("trading_engine.data.market_data.build_snapshot") as mock_build:
        entry, atr = _get_5m_atr(stock_snap)
        assert entry == 180.0
        assert atr == 5.0
        mock_build.assert_not_called()


@patch("trading_engine.execution.live_trader._load_state")
@patch("trading_engine.execution.live_trader._save_state")
@patch("trading_engine.storage.db.get_db_closed_trades")
@patch("trading_engine.execution.live_trader.get_bybit_exchange")
@patch("trading_engine.execution.live_trader.get_alpaca_client")
def test_sync_reconstruction_skips_ghost_positions(
    mock_alpaca, mock_bybit, mock_db_trades, mock_save_state, mock_load_state
):
    """Asserts that sync_with_broker ignores buy/sell trades with closedSize > 0 as ghost positions."""
    # Setup empty portfolio state
    portfolio = LivePortfolio()
    portfolio.positions = []
    portfolio.closed_trades = []
    portfolio.cash = 10000.0
    portfolio.account_size = 10000.0
    mock_load_state.return_value = portfolio
    
    # DB closed trades is empty
    mock_db_trades.return_value = []
    
    # Mock exchange
    ex = MagicMock()
    mock_bybit.return_value = ex
    
    # Mock Open orders to return empty
    ex.fetch_open_orders.return_value = []
    
    # Mock spot/linear executions
    # Trade 1: A BUY trade with closedSize > 0.09. This represents a short cover.
    # Because closedSize > 0, it should NOT reconstruct an open long position.
    trade_cover = {
        "symbol": "NVDA/USDT:USDT",
        "side": "buy",
        "price": 209.6,
        "cost": 394.0,
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        "fee": {"cost": 0.11, "currency": "USDT"},
        "info": {
            "closedSize": "0.09",
            "side": "Buy"
        }
    }
    # Trade 2: A BUY trade with closedSize == 0. This is a normal long entry.
    # It should reconstruct an open long position.
    trade_normal = {
        "symbol": "AAPL/USDT:USDT",
        "side": "buy",
        "price": 180.0,
        "cost": 360.0,
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000) + 1000,
        "fee": {"cost": 0.10, "currency": "USDT"},
        "info": {
            "closedSize": "0",
            "side": "Buy"
        }
    }
    
    # fetch_my_trades gets called for spot, then for linear
    ex.fetch_my_trades.side_effect = [
        [],                 # spot trades
        [trade_cover, trade_normal]  # linear trades
    ]
    
    # Mock Alpaca client positions
    alpaca_client = MagicMock()
    mock_alpaca.return_value = alpaca_client
    alpaca_client.get_all_positions.return_value = []
    
    # Mock Bybit linear positions to match AAPL/USDT:USDT (so the reconstructed position is kept open)
    # NVDA/USDT:USDT is NOT in bybit linear positions (so even if reconstructed, it would be closed, but we want it skipped entirely)
    ex.fetch_positions.return_value = [
        {"symbol": "AAPL/USDT:USDT", "size": 2.0, "contracts": 2.0}
    ]
    
    # Run sync
    with patch("trading_engine.execution.live_trader.settings.trading_mode", "live"), \
         patch("trading_engine.execution.live_trader.settings.bamboo_username", None), \
         patch("trading_engine.execution.live_trader.settings.bamboo_password", None), \
         patch("trading_engine.execution.live_trader.get_alpaca_client", return_value=alpaca_client), \
         patch("trading_engine.execution.live_trader.get_bybit_exchange", return_value=ex):
        
        sync_with_broker()
        
    # Verify that ONLY AAPL/USDT:USDT was reconstructed as an open position.
    # NVDA/USDT:USDT should be skipped because closedSize > 0.
    reconstructed_symbols = [pos.symbol for pos in portfolio.positions]
    assert "AAPL/USDT:USDT" in reconstructed_symbols
    assert "NVDA/USDT:USDT" not in reconstructed_symbols

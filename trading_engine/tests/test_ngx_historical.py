import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from trading_engine.data.market_data import NGXHistoricalFetcher, load_historical_data
from trading_engine.config import settings

@pytest.fixture
def mock_settings():
    with patch("trading_engine.data.market_data.settings") as mock_s:
        mock_s.eodhd_api_key = ""
        mock_s.ngx_pulse_api_key = ""
        mock_s.bamboo_api_key = "test_bamboo_key"
        mock_s.bamboo_username = "test_user"
        mock_s.bamboo_password = "test_password"
        mock_s.bamboo_user_id = "test_user_id"
        mock_s.bamboo_base_url = "https://powered-by-bamboo-sandbox.investbamboo.com"
        mock_s.bamboo_subject_type = "tenant"
        yield mock_s

@patch("trading_engine.data.market_data.get_with_retry")
def test_ngx_fetcher_eodhd(mock_get, mock_settings):
    """Test fetching from EODHD API when key is configured."""
    mock_settings.eodhd_api_key = "eodhd_test_key"
    
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = [
        {"date": "2026-06-01", "open": 20.5, "high": 21.0, "low": 20.0, "close": 20.8, "volume": 5000},
        {"date": "2026-06-02", "open": 20.8, "high": 21.5, "low": 20.6, "close": 21.2, "volume": 6000}
    ]
    mock_get.return_value = mock_response

    fetcher = NGXHistoricalFetcher()
    # Mock local dump check to return False so it falls through to EODHD
    with patch("pathlib.Path.exists", return_value=False):
        df = fetcher.fetch_ohlcv("ZENITHBANK/NGX", "1d", 10)
        
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == 20.8
    assert df["close"].iloc[1] == 21.2
    
    # Check that URL and params were correct
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "eodhd.com/api/eod/ZENITHBANK.XNSA" in args[0]
    assert kwargs["params"]["api_token"] == "eodhd_test_key"

@patch("trading_engine.data.market_data.get_with_retry")
def test_ngx_fetcher_ngx_pulse(mock_get, mock_settings):
    """Test fetching from NGX Pulse API when key is configured."""
    mock_settings.ngx_pulse_api_key = "ngx_pulse_test_key"
    
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "prices": [
            {"trade_date": "2026-06-01", "open_price": 15.0, "high": 15.5, "low": 14.8, "close_price": 15.2, "volume": 3000},
            {"trade_date": "2026-06-02", "open_price": 15.2, "high": 15.8, "low": 15.1, "close_price": 15.5, "volume": 4000}
        ]
    }
    mock_get.return_value = mock_response

    fetcher = NGXHistoricalFetcher()
    with patch("pathlib.Path.exists", return_value=False):
        df = fetcher.fetch_ohlcv("GTCO/NGX", "1d", 10)
        
    assert len(df) == 2
    assert df["close"].iloc[0] == 15.2
    assert df["close"].iloc[1] == 15.5
    
    # Check headers and parameters
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "ngxpulse.ng/api/ngxdata/prices/GTCO" in args[0]
    assert kwargs["headers"]["X-API-Key"] == "ngx_pulse_test_key"

def test_ngx_fetcher_local_dump(mock_settings):
    """Test loading from a local CSV database dump."""
    fetcher = NGXHistoricalFetcher()
    
    # Mock Path.exists to return True for a specific path, and pd.read_csv to return a mock df
    mock_csv_df = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [1000]},
        index=pd.to_datetime(["2026-06-01"])
    )
    
    def mock_exists_side_effect(path_obj):
        # Return True only if it's the expected ticker.csv
        return "ZENITHBANK" in str(path_obj)
        
    with patch("pathlib.Path.exists", mock_exists_side_effect), \
         patch("pandas.read_csv", return_value=mock_csv_df):
        df = fetcher.fetch_ohlcv("ZENITHBANK/NGX", "1d", 10)
        
    assert len(df) == 1
    assert df["close"].iloc[0] == 10.5

@patch("trading_engine.utils.bamboo_client.bamboo_client.get_stock")
def test_ngx_fetcher_synthetic_fallback(mock_get_stock, mock_settings):
    """Test fallback to synthetic candles if no key/file is available."""
    mock_get_stock.return_value = {
        "close_price": 50.0,
        "market_price": 50.5,
        "open_price": 49.5,
        "volume": 2000,
        "market_cap": 1000000
    }
    
    fetcher = NGXHistoricalFetcher()
    with patch("pathlib.Path.exists", return_value=False):
        df = fetcher.fetch_ohlcv("ZENITHBANK/NGX", "1d", 10)
        
    assert len(df) == 10
    # Last close should be the market_price we provided
    assert pytest.approx(df["close"].iloc[-1]) == 50.5
    assert pytest.approx(df["open"].iloc[-1]) == 49.5
    assert pytest.approx(df["volume"].iloc[-1]) == 2000

@patch("trading_engine.data.market_data.NGXHistoricalFetcher")
@patch("trading_engine.data.market_data.CryptoDataFetcher")
def test_load_historical_data_routing(mock_crypto_cls, mock_ngx_cls, mock_settings):
    """Test that load_historical_data routes to correct fetchers."""
    mock_ngx_fetcher = mock_ngx_cls.return_value
    mock_ngx_fetcher.fetch_ohlcv.return_value = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [1000]},
        index=pd.to_datetime(["2026-06-01"])
    )
    
    mock_crypto_fetcher = mock_crypto_cls.return_value
    mock_crypto_fetcher.fetch_ohlcv.return_value = pd.DataFrame(
        {"open": [50000.0], "high": [51000.0], "low": [49000.0], "close": [50500.0], "volume": [20]},
        index=pd.to_datetime(["2026-06-01"])
    )
    
    with patch("pathlib.Path.exists", return_value=False), \
         patch("pandas.DataFrame.to_csv") as mock_to_csv:
        # 1. NGX routing
        df1 = load_historical_data("ZENITHBANK/NGX", "1d", 10)
        mock_ngx_fetcher.fetch_ohlcv.assert_called_once_with("ZENITHBANK/NGX", "1d", 10)
        assert df1["close"].iloc[0] == 10.5
        
        # 2. Crypto routing
        df2 = load_historical_data("BTC/USDT", "4h", 20)
        mock_crypto_fetcher.fetch_ohlcv.assert_called_once_with("BTC/USDT", "4h", 20)
        assert df2["close"].iloc[0] == 50500.0


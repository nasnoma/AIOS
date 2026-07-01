import sys
sys.path.append("/Users/nasir.noma/claude_projects/AIOS")

import os
import ccxt
from loguru import logger
from unittest.mock import patch

from trading_engine.data.market_data import CryptoDataFetcher

# Force Bybit OHLCV to fail (mock 403 geo-blocked response)
def mock_fetch_ohlcv(*args, **kwargs):
    raise ccxt.ExchangeError("Simulated 403 Forbidden / Geo-blocking")

fetcher = CryptoDataFetcher()

with patch.object(fetcher.exchange, 'fetch_ohlcv', side_effect=mock_fetch_ohlcv):
    try:
        logger.info("Triggering simulated geo-blocked fetch for BTC/USDT...")
        df = fetcher.fetch_ohlcv("BTC/USDT", timeframe="5m", limit=300)
        logger.success(f"Success! Fetched {len(df)} rows from fallback.")
        logger.info(f"Columns: {df.columns.tolist()}")
        logger.info(f"Last row close: {df['close'].iloc[-1]}")
    except Exception as e:
        logger.exception("Fallback failed:")

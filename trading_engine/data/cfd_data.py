"""
trading_engine/data/cfd_data.py

Bybit linear perpetual OHLCV fetcher for CFD symbols (US stock CFDs and precious metals).

Uses ccxt with defaultType='linear' so that symbols like AAPL/USDT:USDT and
XAU/USDT:USDT are resolved against Bybit's linear derivatives market.
"""
from __future__ import annotations

import time
from typing import Optional
from loguru import logger

import ccxt
import pandas as pd

from trading_engine.config import settings


# ── Exchange singleton ────────────────────────────────────────────────────────

_bybit_linear: Optional[ccxt.bybit] = None


def _get_exchange() -> ccxt.bybit:
    """Return (or create) a ccxt.bybit instance configured for linear markets."""
    global _bybit_linear
    if _bybit_linear is None:
        params: dict = {
            "options": {"defaultType": "linear"},
        }
        if settings.bybit_api_key and settings.bybit_api_secret:
            params["apiKey"] = settings.bybit_api_key
            params["secret"] = settings.bybit_api_secret
        _bybit_linear = ccxt.bybit(params)
        # NOTE: Do NOT enable demo/testnet for market data reads.
        # api-demo.bybit.com is geo-blocked on cloud providers (CloudFront 403).
        # Market data always comes from the public api.bybit.com.
    return _bybit_linear


# ── OHLCV fetch ───────────────────────────────────────────────────────────────

_TF_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "12h": "12h", "1d": "1d", "1w": "1w",
}


def fetch_cfd_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 300,
    retries: int = 3,
) -> pd.DataFrame:
    """
    Fetch OHLCV data for a Bybit linear CFD symbol.

    Returns a DataFrame with columns: [timestamp, open, high, low, close, volume]
    indexed by datetime (UTC).

    Raises RuntimeError if data cannot be fetched after `retries` attempts.
    """
    exchange = _get_exchange()
    tf = _TF_MAP.get(timeframe, timeframe)

    for attempt in range(1, retries + 1):
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if not raw:
                raise ValueError(f"Empty OHLCV response for {symbol}")

            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)
            logger.debug(f"CFD OHLCV fetched: {symbol} | {len(df)} bars | latest close={df['close'].iloc[-1]:.4f}")
            return df

        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            wait = 2 ** attempt
            logger.warning(f"CFD fetch attempt {attempt}/{retries} failed for {symbol}: {e} — retrying in {wait}s")
            time.sleep(wait)
        except ccxt.BadSymbol:
            raise RuntimeError(f"Symbol {symbol!r} not found on Bybit linear market")
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"CFD OHLCV fetch failed for {symbol} after {retries} attempts: {e}") from e
            time.sleep(2 ** attempt)

    raise RuntimeError(f"CFD OHLCV fetch exhausted retries for {symbol}")


def fetch_cfd_ticker(symbol: str) -> dict:
    """
    Fetch the latest ticker (bid/ask/last/volume) for a CFD symbol.
    Returns raw ccxt ticker dict.
    """
    exchange = _get_exchange()
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker
    except Exception as e:
        raise RuntimeError(f"CFD ticker fetch failed for {symbol}: {e}") from e


def fetch_cfd_price(symbol: str) -> float:
    """Return the latest price for a CFD symbol."""
    ticker = fetch_cfd_ticker(symbol)
    price = ticker.get("last") or ticker.get("close") or 0.0
    if price <= 0:
        raise RuntimeError(f"Invalid price {price} for {symbol}")
    return float(price)


class BybitCFDFetcher:
    """
    Thin wrapper around the module-level functions for use in the data pipeline.
    Mirrors the interface of CryptoDataFetcher / StockDataFetcher.
    """

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        return fetch_cfd_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_latest_price(self, symbol: str) -> float:
        return fetch_cfd_price(symbol)

    def fetch_ticker(self, symbol: str) -> dict:
        return fetch_cfd_ticker(symbol)

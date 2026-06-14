"""
trading_engine/data/cfd_data.py

Bybit linear perpetual OHLCV fetcher for CFD symbols (US stock CFDs and precious metals).

Uses Bybit's public REST API directly (v5/market/kline) instead of ccxt, to avoid
the ccxt market-loading step which hits v5/asset/coin/query-info — a CloudFront-blocked
endpoint on many cloud providers (Railway US servers).
"""
from __future__ import annotations

import time
from typing import Optional
from loguru import logger

import requests
import ccxt
import pandas as pd

from trading_engine.config import settings


# ── Bybit symbol mapping ──────────────────────────────────────────────────────
# ccxt uses  "XAU/USDT:USDT"  →  Bybit REST uses  "XAUUSDT"

def _to_bybit_symbol(symbol: str) -> str:
    """Convert ccxt-style  XAU/USDT:USDT  →  XAUUSDT  for the REST API."""
    # Strip settle currency (:USDT) then remove slash
    base = symbol.split(":")[0]          # "XAU/USDT"
    return base.replace("/", "")         # "XAUUSDT"


# ── Timeframe map ─────────────────────────────────────────────────────────────

_TF_MAP = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360,
    "12h": 720, "1d": "D", "1w": "W",
}

_BYBIT_REST = "https://api.bybit.com"


# ── Direct REST OHLCV fetch ───────────────────────────────────────────────────

def fetch_cfd_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 300,
    retries: int = 3,
) -> pd.DataFrame:
    """
    Fetch OHLCV data for a Bybit linear CFD symbol via direct REST (no ccxt market loading).

    Returns a DataFrame with columns: [timestamp, open, high, low, close, volume]
    indexed by datetime (UTC).

    Raises RuntimeError if data cannot be fetched after `retries` attempts.
    """
    bybit_symbol = _to_bybit_symbol(symbol)
    interval = _TF_MAP.get(timeframe, timeframe)
    url = f"{_BYBIT_REST}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": bybit_symbol,
        "interval": str(interval),
        "limit": min(limit, 1000),
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("retCode") != 0:
                raise ValueError(f"Bybit API error: {data.get('retMsg')} (code={data.get('retCode')})")

            raw = data["result"]["list"]
            if not raw:
                raise ValueError(f"Empty OHLCV response for {symbol}")

            # Bybit returns: [startTime, open, high, low, close, volume, turnover]
            # Newest first — reverse to chronological order
            df = pd.DataFrame(
                reversed(raw),
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float)

            logger.debug(
                f"CFD OHLCV fetched: {symbol} | {len(df)} bars | latest close={df['close'].iloc[-1]:.4f}"
            )
            return df

        except requests.HTTPError as e:
            wait = 2 ** attempt
            logger.warning(f"CFD fetch attempt {attempt}/{retries} failed for {symbol}: {e} — retrying in {wait}s")
            time.sleep(wait)
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"CFD OHLCV fetch exhausted retries for {symbol}: {e}") from e
            wait = 2 ** attempt
            logger.warning(f"CFD fetch attempt {attempt}/{retries} failed for {symbol}: {e} — retrying in {wait}s")
            time.sleep(wait)

    raise RuntimeError(f"CFD OHLCV fetch exhausted retries for {symbol}")


# ── Ticker / latest price via REST ────────────────────────────────────────────

def fetch_cfd_ticker(symbol: str) -> dict:
    """
    Fetch the latest ticker for a CFD symbol via Bybit REST.
    Returns a dict with keys: last, bid, ask, volume.
    """
    bybit_symbol = _to_bybit_symbol(symbol)
    url = f"{_BYBIT_REST}/v5/market/tickers"
    params = {"category": "linear", "symbol": bybit_symbol}
    try:
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise ValueError(f"Bybit ticker error: {data.get('retMsg')}")
        item = data["result"]["list"][0]
        return {
            "last":   float(item.get("lastPrice", 0)),
            "bid":    float(item.get("bid1Price", 0)),
            "ask":    float(item.get("ask1Price", 0)),
            "volume": float(item.get("volume24h", 0)),
        }
    except Exception as e:
        raise RuntimeError(f"CFD ticker fetch failed for {symbol}: {e}") from e


def fetch_cfd_price(symbol: str) -> float:
    """Return the latest price for a CFD symbol."""
    ticker = fetch_cfd_ticker(symbol)
    price = ticker.get("last", 0.0)
    if price <= 0:
        raise RuntimeError(f"Invalid price {price} for {symbol}")
    return float(price)


# ── Exchange singleton (kept for compatibility, not used for OHLCV) ───────────

_bybit_linear: Optional[ccxt.bybit] = None


def _get_exchange() -> ccxt.bybit:
    """ccxt instance — only used for order placement in live_trader, not for data reads."""
    global _bybit_linear
    if _bybit_linear is None:
        params: dict = {
            "options": {"defaultType": "linear"},
        }
        if settings.bybit_api_key and settings.bybit_api_secret:
            params["apiKey"] = settings.bybit_api_key
            params["secret"] = settings.bybit_api_secret
        _bybit_linear = ccxt.bybit(params)
        if settings.bybit_demo_trading:
            _bybit_linear.enable_demo_trading(True)
    return _bybit_linear


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

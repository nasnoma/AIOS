"""
trading_engine/data/cfd_data.py

Bybit linear perpetual OHLCV/ticker fetcher for CFD symbols (stock CFDs and metals).

Primary:  Bybit public REST  v5/market/kline  (works outside US)
Fallback: Yahoo Finance (yfinance) for metals/commodities when Bybit is
          geo-blocked (e.g. Railway US West servers — api.bybit.com returns 403).

Symbol mapping  (ccxt → Bybit REST → Yahoo Finance):
    XAU/USDT:USDT  →  XAUUSDT  →  GC=F   (Gold futures)
    XAG/USDT:USDT  →  XAGUSDT  →  SI=F   (Silver futures)
    CL/USDT:USDT   →  CLUSDT   →  CL=F   (WTI Crude Oil futures)
    AAPL/USDT:USDT →  AAPLUSDT →  AAPL   (Stock)
    TSLA/USDT:USDT →  TSLAUSDT →  TSLA
"""
from __future__ import annotations

import time
from typing import Optional
from loguru import logger

import requests
import ccxt
import pandas as pd

from trading_engine.config import settings


# ── Symbol maps ───────────────────────────────────────────────────────────────

# ccxt "BASE/QUOTE:SETTLE" → Bybit REST symbol
def _to_bybit_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")   # "XAU/USDT:USDT" → "XAUUSDT"

# ccxt symbol → Yahoo Finance ticker
_YAHOO_MAP: dict[str, str] = {
    "XAU/USDT:USDT": "GC=F",    # Gold futures
    "XAG/USDT:USDT": "SI=F",    # Silver futures
    "CL/USDT:USDT":  "CL=F",    # WTI Crude Oil futures
    "AAPL/USDT:USDT": "AAPL",
    "TSLA/USDT:USDT": "TSLA",
    "NVDA/USDT:USDT": "NVDA",
    "MSFT/USDT:USDT": "MSFT",
    "AMZN/USDT:USDT": "AMZN",
    "GOOGL/USDT:USDT": "GOOGL",
}

# timeframe → Bybit REST interval
_TF_MAP = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360,
    "12h": 720, "1d": "D", "1w": "W",
}

# timeframe → yfinance (interval, period) — commodities have limited intraday history
_YF_TF = {
    "1m":  ("2m",  "5d"),     # use 2m as closest to 1m for futures
    "5m":  ("5m",  "5d"),     # only 5d of 5m data available on futures
    "15m": ("15m", "30d"),
    "30m": ("30m", "30d"),
    "1h":  ("1h",  "90d"),
    "2h":  ("1h",  "90d"),    # yfinance has no 2h; use 1h
    "4h":  ("1h",  "90d"),    # yfinance has no 4h; use 1h
    "6h":  ("1h",  "90d"),
    "12h": ("1d",  "2y"),
    "1d":  ("1d",  "5y"),
    "1w":  ("1wk", "10y"),
}

_BYBIT_REST = "https://api.bybit.com"


# ── Yahoo Finance fallback ────────────────────────────────────────────────────

def _fetch_yfinance_ohlcv(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """Fetch OHLCV via yfinance. Used when Bybit is geo-blocked."""
    import yfinance as yf

    yf_ticker = _YAHOO_MAP.get(symbol)
    if not yf_ticker:
        raise RuntimeError(f"No Yahoo Finance mapping for {symbol}")

    yf_interval, yf_period = _YF_TF.get(timeframe, ("1h", "90d"))
    logger.info(f"CFD fallback → Yahoo Finance: {symbol} ({yf_ticker}) interval={yf_interval} period={yf_period}")

    ticker_obj = yf.Ticker(yf_ticker)
    df = ticker_obj.history(period=yf_period, interval=yf_interval, auto_adjust=True)

    if df is None or df.empty:
        # Last resort: daily data
        logger.warning(f"Yahoo Finance returned empty for {yf_ticker} at {yf_interval} — retrying with 1d")
        df = ticker_obj.history(period="2y", interval="1d", auto_adjust=True)

    if df is None or df.empty:
        raise RuntimeError(f"Yahoo Finance returned no data for {yf_ticker}")

    # Normalise columns
    df.index = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].tail(limit)
    return df.astype(float)



# ── Primary Bybit REST fetch ──────────────────────────────────────────────────

def fetch_cfd_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 300,
    retries: int = 2,
) -> pd.DataFrame:
    """
    Fetch OHLCV for a Bybit linear CFD symbol.
    Primary: Bybit v5/market/kline (direct REST, no ccxt market loading).
    Fallback: Yahoo Finance when Bybit is geo-blocked.
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
            if resp.status_code == 403:
                # Geo-blocked — go straight to yfinance fallback
                logger.info(f"Bybit geo-blocked for {symbol} — switching to Yahoo Finance fallback")
                return _fetch_yfinance_ohlcv(symbol, timeframe, limit)

            resp.raise_for_status()
            data = resp.json()

            if data.get("retCode") != 0:
                raise ValueError(f"Bybit API error: {data.get('retMsg')}")

            raw = data["result"]["list"]
            if not raw:
                raise ValueError(f"Empty OHLCV from Bybit for {symbol}")

            # Bybit returns newest-first → reverse to chronological
            df = pd.DataFrame(
                reversed(raw),
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            logger.debug(f"CFD OHLCV (Bybit): {symbol} | {len(df)} bars | close={df['close'].iloc[-1]:.4f}")
            return df

        except RuntimeError:
            raise
        except Exception as e:
            if attempt == retries:
                # Last attempt — try yfinance fallback before giving up
                logger.warning(f"Bybit failed for {symbol} after {retries} attempts: {e} — trying Yahoo Finance")
                try:
                    return _fetch_yfinance_ohlcv(symbol, timeframe, limit)
                except Exception as yf_e:
                    raise RuntimeError(f"CFD OHLCV exhausted all sources for {symbol}: Bybit={e}, Yahoo={yf_e}")
            wait = 2 ** attempt
            logger.warning(f"CFD fetch attempt {attempt}/{retries} failed for {symbol}: {e} — retrying in {wait}s")
            time.sleep(wait)

    raise RuntimeError(f"CFD OHLCV fetch exhausted retries for {symbol}")


# ── Ticker / latest price ─────────────────────────────────────────────────────

def fetch_cfd_ticker(symbol: str) -> dict:
    """Latest ticker via Bybit REST, falling back to yfinance."""
    bybit_symbol = _to_bybit_symbol(symbol)
    url = f"{_BYBIT_REST}/v5/market/tickers"
    params = {"category": "linear", "symbol": bybit_symbol}
    try:
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code == 403:
            raise RuntimeError("geo-blocked")
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
    except Exception:
        # Fallback: pull latest close from yfinance
        try:
            df = _fetch_yfinance_ohlcv(symbol, "1d", 2)
            last = float(df["close"].iloc[-1])
            return {"last": last, "bid": last, "ask": last, "volume": 0.0}
        except Exception as e:
            raise RuntimeError(f"CFD ticker fetch failed for {symbol}: {e}") from e


def fetch_cfd_price(symbol: str) -> float:
    ticker = fetch_cfd_ticker(symbol)
    price = ticker.get("last", 0.0)
    if price <= 0:
        raise RuntimeError(f"Invalid price {price} for {symbol}")
    return float(price)


# ── Exchange singleton (for order execution in live_trader only) ──────────────

_bybit_linear: Optional[ccxt.bybit] = None


def _get_exchange() -> ccxt.bybit:
    """ccxt instance — only used for order placement in live_trader, not for data reads."""
    global _bybit_linear
    if _bybit_linear is None:
        params: dict = {"options": {"defaultType": "linear"}}
        if settings.bybit_api_key and settings.bybit_api_secret:
            params["apiKey"] = settings.bybit_api_key
            params["secret"] = settings.bybit_api_secret
        _bybit_linear = ccxt.bybit(params)
        if settings.bybit_demo_trading:
            _bybit_linear.enable_demo_trading(True)
    return _bybit_linear


class BybitCFDFetcher:
    """Thin wrapper for use in the data pipeline."""

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        return fetch_cfd_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_latest_price(self, symbol: str) -> float:
        return fetch_cfd_price(symbol)

    def fetch_ticker(self, symbol: str) -> dict:
        return fetch_cfd_ticker(symbol)

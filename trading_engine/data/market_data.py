"""
trading_engine/data/market_data.py

Unified market data layer:
- Fetches OHLCV for crypto (via ccxt/Binance) and stocks (via Polygon/Alpaca)
- Computes ALL technical indicators in one place (pandas-ta)
- Returns a MarketSnapshot dataclass consumed by all agents
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import ccxt
import pandas as pd
import pandas_ta as ta
import requests
from loguru import logger

from trading_engine.config import settings
from trading_engine.utils.http import get_with_retry


# ─────────────────────────────────────────────
#  Data Model
# ─────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """All market data consumed by specialist agents."""
    symbol: str
    asset_type: str           # 'crypto' | 'stock'
    timeframe: str
    timestamp: datetime

    # OHLCV
    df: pd.DataFrame          # Full OHLCV + indicators DataFrame

    # Convenience: latest values
    close: float = 0.0
    volume: float = 0.0

    # Trend indicators
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0

    # Momentum
    rsi: float = 0.0
    stoch_rsi_k: float = 0.0
    stoch_rsi_d: float = 0.0
    roc: float = 0.0

    # Volume
    obv: float = 0.0
    rel_volume: float = 0.0   # current volume / 20-period avg volume
    vwap: float = 0.0

    # Volatility
    atr: float = 0.0
    bb_width: float = 0.0     # Bollinger Band width
    realized_vol: float = 0.0 # 14-period realized volatility

    # Order flow (crypto-specific, None for stocks)
    open_interest: Optional[float] = None
    funding_rate: Optional[float] = None
    long_liq_24h: Optional[float] = None
    short_liq_24h: Optional[float] = None

    # Sentiment
    fear_greed_index: Optional[int] = None
    fear_greed_label: Optional[str] = None

    # Macro (set by MacroAgent separately)
    dxy_trend: Optional[str] = None       # 'rising' | 'falling' | 'neutral'
    risk_mode: Optional[str] = None       # 'risk-on' | 'risk-off'

    # Stock-specific fundamental metrics (from Massive/Polygon)
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    net_income: Optional[float] = None
    revenue: Optional[float] = None

    # Scraped / Enriched fields
    tweets: list[str] = field(default_factory=list)
    stocktwits_raw: str = ""
    news_headlines: list[str] = field(default_factory=list)
    orderbook_imbalance: float = 0.5
    htf_snap: Optional[MarketSnapshot] = None


# ─────────────────────────────────────────────
#  Crypto Data Fetcher (ccxt)
# ─────────────────────────────────────────────

class CryptoDataFetcher:
    def __init__(self):
        exchange_name = settings.crypto_exchange.lower()
        exchange_class = getattr(ccxt, exchange_name)
        params = {}
        
        # Load correct API credentials based on selected exchange
        if exchange_name == "binance" and settings.binance_api_key:
            params["apiKey"] = settings.binance_api_key
            params["secret"] = settings.binance_api_secret
        elif exchange_name == "bybit":
            params["options"] = {"defaultType": "spot"}
            # Public market data read does NOT need API key & secret. Passing a demo/testnet
            # API key to the production endpoint results in invalid API key errors (retCode 10003).
            
        self.exchange: ccxt.Exchange = exchange_class(params)
        
        # NOTE: Do NOT enable demo trading for market data fetches —
        # api-demo.bybit.com is geo-blocked on many cloud providers (CloudFront 403).
        # Market data always comes from the public api.bybit.com.
        # Demo/testnet mode is only applied in the live_trader execution layer.
        if settings.crypto_testnet and exchange_name == "binance":
            self.exchange.options = {"defaultType": "future"}
            self.exchange.urls["api"]["public"] = "https://testnet.binance.vision/api"

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        """Fetch OHLCV candles and return raw DataFrame with auto-pagination for large limits."""
        # Map IOTA/USDT to linear perp since Bybit doesn't have IOTA/USDT spot
        if symbol == "IOTA/USDT" and settings.crypto_exchange.lower() == "bybit":
            symbol = "IOTA/USDT:USDT"
        logger.info(f"Fetching OHLCV: {symbol} {timeframe} (limit={limit})")
        import time
        try:
            if limit <= 1000:
                raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            else:
                raw = []
                remaining = limit
                # We calculate 'since' starting from the past.
                tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
                since = self.exchange.milliseconds() - (limit * tf_ms)
                
                while remaining > 0:
                    fetch_limit = min(remaining, 1000)
                    batch = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=fetch_limit)
                    if not batch:
                        break
                    raw.extend(batch)
                    remaining -= len(batch)
                    # Next batch starts after the last candle in this batch
                    since = batch[-1][0] + tf_ms
                    # Small sleep to prevent rate limiting
                    time.sleep(0.05)
            
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df.drop_duplicates(subset=["timestamp"], inplace=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            logger.warning(f"Crypto fetch failed via exchange for {symbol}: {e}")
            
            # Fallback 1: Direct Yahoo Finance (free, fast, covers all major crypto under TICKER-USD format)
            try:
                base_ticker = symbol.split(":")[0].split("/")[0]
                yf_symbol = f"{base_ticker}-USD"
                
                import yfinance as yf
                _YF_INTERVAL = {
                    "1m": ("2m", "5d"), "5m": ("5m", "5d"), "15m": ("15m", "30d"),
                    "30m": ("30m", "30d"), "1h": ("1h", "90d"), "2h": ("1h", "90d"),
                    "4h": ("1h", "90d"), "1d": ("1d", "5y"), "1w": ("1wk", "10y"),
                }
                yf_interval, yf_period = _YF_INTERVAL.get(timeframe, ("1h", "90d"))
                logger.info(f"Crypto fallback → Yahoo Finance: {symbol} ({yf_symbol}) interval={yf_interval}")
                ticker_obj = yf.Ticker(yf_symbol)
                df = ticker_obj.history(period=yf_period, interval=yf_interval, auto_adjust=True)
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index, utc=True)
                    df.columns = [c.lower() for c in df.columns]
                    df = df[["open", "high", "low", "close", "volume"]].tail(limit)
                    logger.info(f"Successfully fetched {len(df)} crypto bars from Yahoo Finance for {yf_symbol}")
                    return df.astype(float)
            except Exception as yf_err:
                logger.warning(f"Yahoo Finance crypto fallback failed for {symbol}: {yf_err}")

            # Fallback 2: Massive / Polygon API
            api_key = settings.get_massive_api_key
            if api_key:
                logger.info(f"Attempting fallback fetch via Massive API for {symbol}")
                try:
                    massive_symbol = symbol.replace("/USDT", "USD").replace("/", "")
                    if not massive_symbol.startswith("X:"):
                        massive_symbol = "X:" + massive_symbol
                    fetcher = StockDataFetcher()
                    try:
                        return fetcher.fetch_ohlcv(massive_symbol, timeframe, limit)
                    except Exception as massive_err:
                        direct_symbol = "X:" + symbol.replace("/", "")
                        logger.warning(f"Massive fetch failed for {massive_symbol}: {massive_err}. Trying {direct_symbol}...")
                        return fetcher.fetch_ohlcv(direct_symbol, timeframe, limit)
                except Exception as fallback_e:
                    logger.error(f"Fallback fetch via Massive API failed for {symbol}: {fallback_e}")
            raise e

    def fetch_order_flow(self, symbol: str) -> dict:
        """Fetch funding rate and open interest via Binance futures."""
        result = {
            "open_interest": None,
            "funding_rate": None,
            "long_liq_24h": None,
            "short_liq_24h": None,
        }
        try:
            # Funding rate
            ticker = self.exchange.fetch_funding_rate(symbol)
            result["funding_rate"] = ticker.get("fundingRate")

            # Open interest
            oi = self.exchange.fetch_open_interest(symbol)
            result["open_interest"] = oi.get("openInterest")
        except Exception as e:
            logger.warning(f"Order flow fetch failed for {symbol}: {e}")
        return result


# ─────────────────────────────────────────────
#  Stock Data Fetcher (Polygon.io)
# ─────────────────────────────────────────────

class StockDataFetcher:
    BASE = "https://api.massive.com"

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        """Fetch aggregated bars from Alpaca (with fallback to Massive/Polygon)."""
        logger.info(f"Fetching stock OHLCV: {symbol} {timeframe}")
        
        # 1. Attempt to fetch via Alpaca to match broker executions
        if settings.alpaca_api_key and settings.alpaca_secret_key:
            try:
                alpaca_tf = self._alpaca_timeframe(timeframe)
                url = "https://data.alpaca.markets/v2/stocks/bars"
                headers = {
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_secret_key
                }
                params = {
                    "symbols": symbol,
                    "timeframe": alpaca_tf,
                    "limit": limit,
                    "adjustment": "all"
                }
                resp = get_with_retry(url, headers=headers, params=params, timeout=10)
                if resp.ok:
                    data = resp.json()
                    bars = data.get("bars", {}).get(symbol, [])
                    min_bars = min(100, int(limit * 0.5))
                    if len(bars) >= min_bars:
                        df = pd.DataFrame(bars)
                        df.rename(columns={"t": "timestamp", "o": "open", "h": "high",
                                            "l": "low", "c": "close", "v": "volume"}, inplace=True)
                        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                        df.set_index("timestamp", inplace=True)
                        logger.info(f"Successfully fetched {len(df)} stock bars from Alpaca for {symbol}")
                        return df[["open", "high", "low", "close", "volume"]]
                    else:
                        logger.warning(f"Alpaca returned insufficient bars ({len(bars)} < {min_bars}) for {symbol}. Trying fallback.")
            except Exception as e:
                logger.warning(f"Failed to fetch stock bars from Alpaca for {symbol}: {e}. Falling back to Massive.")

        # 2. Fallback to Massive.com
        try:
            api_key = settings.get_massive_api_key
            multiplier, span = self._parse_timeframe(timeframe)
            url = f"{self.BASE}/v2/aggs/ticker/{symbol}/range/{multiplier}/{span}/2023-01-01/2099-01-01"
            params = {
                "adjusted": "true",
                "sort": "desc",
                "limit": limit,
                "apiKey": api_key,
            }
            resp = get_with_retry(url, params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            min_bars = min(100, int(limit * 0.5))
            if len(results) >= min_bars:
                df = pd.DataFrame(results)
                df.rename(columns={"t": "timestamp", "o": "open", "h": "high",
                                    "l": "low", "c": "close", "v": "volume"}, inplace=True)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)
                df.sort_index(inplace=True)
                return df[["open", "high", "low", "close", "volume"]]
            else:
                logger.warning(f"Massive returned insufficient bars ({len(results)} < {min_bars}) for {symbol}. Trying fallback.")
        except Exception as e:
            logger.warning(f"Massive API failed for {symbol}: {e} — falling back to Yahoo Finance")

        # 3. Yahoo Finance fallback (free, no rate limits, covers all US stocks)
        try:
            import yfinance as yf
            yf_symbol = symbol
            if symbol.startswith("X:"):
                # "X:BTCUSD" -> "BTC-USD"
                clean_sym = symbol[2:]
                if clean_sym.endswith("USD"):
                    yf_symbol = clean_sym[:-3] + "-USD"
                else:
                    yf_symbol = clean_sym + "-USD"

            _YF_INTERVAL = {
                "1m": ("2m", "5d"), "5m": ("5m", "5d"), "15m": ("15m", "30d"),
                "30m": ("30m", "30d"), "1h": ("1h", "90d"), "2h": ("1h", "90d"),
                "4h": ("1h", "90d"), "1d": ("1d", "5y"), "1w": ("1wk", "10y"),
            }
            yf_interval, yf_period = _YF_INTERVAL.get(timeframe, ("1h", "90d"))
            logger.info(f"Yahoo Finance fallback for stock {symbol} ({yf_symbol}) (interval={yf_interval})")
            ticker_obj = yf.Ticker(yf_symbol)
            df = ticker_obj.history(period=yf_period, interval=yf_interval, auto_adjust=True)
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index, utc=True)
                df.columns = [c.lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].tail(limit)
                logger.info(f"Yahoo Finance: {len(df)} bars for {yf_symbol}")
                return df.astype(float)
        except Exception as e:
            logger.warning(f"Yahoo Finance also failed for {symbol}: {e}")

        raise RuntimeError(f"All data sources exhausted for stock {symbol}")


    def fetch_latest_price(self, symbol: str) -> float:
        """Fetch latest close price for a stock via Alpaca (with fallback to Massive/Polygon)."""
        # 1. Attempt to fetch via Alpaca to match broker executions
        if settings.alpaca_api_key and settings.alpaca_secret_key:
            try:
                url = "https://data.alpaca.markets/v2/stocks/trades/latest"
                headers = {
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_secret_key
                }
                resp = get_with_retry(url, headers=headers, params={"symbols": symbol}, timeout=5)
                if resp.ok:
                    trade_data = resp.json().get("trades", {}).get(symbol, {})
                    if trade_data:
                        price = float(trade_data.get("p", 0.0))
                        if price > 0:
                            return price
            except Exception as e:
                logger.warning(f"Failed to fetch latest price from Alpaca for {symbol}: {e}. Falling back to Massive.")

        # 2. Fallback to Massive Previous Close
        api_key = settings.get_massive_api_key
        if not api_key:
            return 0.0
        url = f"{self.BASE}/v2/aggs/ticker/{symbol}/prev"
        try:
            resp = get_with_retry(url, params={"apiKey": api_key}, timeout=5)
            if resp.ok:
                results = resp.json().get("results", [])
                if results:
                    return float(results[0].get("c", 0.0))
        except Exception as e:
            logger.warning(f"Error fetching latest price for {symbol}: {e}")
        return 0.0

    def fetch_metrics(self, symbol: str) -> dict:
        """Fetch stock metrics from Massive.com: market cap, shares outstanding, net income, revenue."""
        metrics = {
            "market_cap": None,
            "shares_outstanding": None,
            "net_income": None,
            "revenue": None
        }
        api_key = settings.get_massive_api_key
        if not api_key:
            return metrics

        # 1. Fetch Ticker Details (v3)
        ticker_url = f"{self.BASE}/v3/reference/tickers/{symbol}"
        try:
            resp = get_with_retry(ticker_url, params={"apiKey": api_key}, timeout=5)
            if resp.ok:
                res = resp.json().get("results", {})
                metrics["market_cap"] = res.get("market_cap")
                metrics["shares_outstanding"] = res.get("weighted_shares_outstanding") or res.get("share_class_shares_outstanding")
        except Exception as e:
            logger.warning(f"Error fetching ticker details for {symbol}: {e}")

        # 2. Fetch Financials (vX)
        fin_url = f"{self.BASE}/vX/reference/financials"
        try:
            resp = get_with_retry(fin_url, params={"ticker": symbol, "limit": 1, "apiKey": api_key}, timeout=5)
            if resp.ok:
                results = resp.json().get("results", [])
                if results:
                    fin = results[0].get("financials", {})
                    # Net income
                    net_inc_obj = fin.get("income_statement", {}).get("net_income_loss", {})
                    if net_inc_obj:
                        metrics["net_income"] = net_inc_obj.get("value")
                    # Revenue
                    rev_obj = fin.get("income_statement", {}).get("revenues", {})
                    if rev_obj:
                        metrics["revenue"] = rev_obj.get("value")
        except Exception as e:
            logger.warning(f"Error fetching financials for {symbol}: {e}")

        return metrics

    @staticmethod
    def _parse_timeframe(tf: str) -> tuple[int, str]:
        MAP = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
               "1h": (1, "hour"), "4h": (4, "hour"), "1d": (1, "day")}
        return MAP.get(tf, (4, "hour"))

    @staticmethod
    def _alpaca_timeframe(tf: str) -> str:
        MAP = {
            "1m": "1Min",
            "5m": "5Min",
            "15m": "15Min",
            "1h": "1Hour",
            "4h": "4Hour",
            "1d": "1Day"
        }
        return MAP.get(tf, "4Hour")


# ─────────────────────────────────────────────
#  Indicator Calculator
# ─────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, overrides: dict | None = None) -> pd.DataFrame:
    """Apply all technical indicators via pandas-ta.

    Args:
        df: OHLCV DataFrame
        overrides: Optional period overrides from specialist_configs.json, e.g.
            {"rsi_period": 14, "ema_fast": 20, "ema_slow": 50, "ema_trend": 200,
             "atr_period": 14, "bb_period": 20, "roc_period": 10}
    """
    p = overrides or {}

    ema_fast  = int(p.get("ema_fast",  20))
    ema_slow  = int(p.get("ema_slow",  50))
    ema_trend = int(p.get("ema_trend", 200))
    rsi_p     = int(p.get("rsi_period",  14))
    atr_p     = int(p.get("atr_period",  14))
    bb_p      = int(p.get("bb_period",   20))
    roc_p     = int(p.get("roc_period",  10))
    rel_vol_p = int(p.get("rel_vol_period", 20))

    # Trend
    if len(df) >= ema_fast:
        df.ta.ema(length=ema_fast,  append=True)
    if len(df) >= ema_slow:
        df.ta.ema(length=ema_slow,  append=True)
    if len(df) >= ema_trend:
        df.ta.ema(length=ema_trend, append=True)

    # Normalise column names to canonical names so downstream code is stable
    # pandas-ta names them EMA_<length>; alias non-default periods to standard names
    for actual, canonical in [
        (f"EMA_{ema_fast}",  "EMA_20"),
        (f"EMA_{ema_slow}",  "EMA_50"),
        (f"EMA_{ema_trend}", "EMA_200"),
    ]:
        if actual != canonical and actual in df.columns:
            df["EMA_20"]  = df[f"EMA_{ema_fast}"]  if canonical == "EMA_20"  else df.get("EMA_20",  0)
            df["EMA_50"]  = df[f"EMA_{ema_slow}"]  if canonical == "EMA_50"  else df.get("EMA_50",  0)
            df["EMA_200"] = df[f"EMA_{ema_trend}"] if canonical == "EMA_200" else df.get("EMA_200", 0)

    # Always ensure canonical columns exist (pandas-ta writes EMA_<n>)
    df["EMA_20"]  = df.get(f"EMA_{ema_fast}",  df.get("EMA_20",  df["close"]))
    df["EMA_50"]  = df.get(f"EMA_{ema_slow}",  df.get("EMA_50",  df["close"]))
    df["EMA_200"] = df.get(f"EMA_{ema_trend}", df.get("EMA_200", df["close"]))

    # Momentum
    if len(df) >= rsi_p:
        df.ta.rsi(length=rsi_p, append=True)
    df["RSI_14"] = df.get(f"RSI_{rsi_p}", df.get("RSI_14", pd.Series(50.0, index=df.index)))

    if len(df) >= rsi_p:
        try:
            # Calculate StochRSI manually to avoid cross-platform import/accessor bugs in pandas-ta
            min_rsi = df["RSI_14"].rolling(rsi_p).min()
            max_rsi = df["RSI_14"].rolling(rsi_p).max()
            rsi_range = max_rsi - min_rsi
            stochrsi_series = 100 * (df["RSI_14"] - min_rsi) / rsi_range.replace(0, 1e-9)
            df["STOCHRSIk_14_14_3_3"] = stochrsi_series.rolling(3).mean()
            df["STOCHRSId_14_14_3_3"] = df["STOCHRSIk_14_14_3_3"].rolling(3).mean()
        except Exception as e:
            logger.warning(f"Failed to calculate StochRSI: {e}")
    df["STOCHRSIk_14_14_3_3"] = df.get("STOCHRSIk_14_14_3_3", pd.Series(50.0, index=df.index))
    df["STOCHRSId_14_14_3_3"] = df.get("STOCHRSId_14_14_3_3", pd.Series(50.0, index=df.index))

    if len(df) >= roc_p:
        df.ta.roc(length=roc_p, append=True)
    df[f"ROC_10"] = df.get(f"ROC_{roc_p}", df.get("ROC_10", pd.Series(0.0, index=df.index)))

    # Volume
    df.ta.obv(append=True)
    try:
        df.ta.vwap(append=True)
    except Exception as e:
        logger.warning(f"Failed to calculate VWAP: {e}")
    df["VWAP_D"] = df.get("VWAP_D", df["close"])

    # Volatility — use dollar ATR (percent=False) so snap.atr is a real price delta
    if len(df) >= atr_p:
        df.ta.atr(length=atr_p, percent=False, append=True)
    df["ATR_14"] = df.get(f"ATRr_{atr_p}", df.get(f"ATR_{atr_p}", df.get("ATR_14", pd.Series(0.0, index=df.index))))


    if len(df) >= bb_p:
        df.ta.bbands(length=bb_p, std=2, append=True)
    # Canonical BB column names (used by snapshot builder)
    df[f"BBU_20_2.0"] = df.get(f"BBU_{bb_p}_2.0", df.get("BBU_20_2.0", df["close"] * 1.02))
    df[f"BBL_20_2.0"] = df.get(f"BBL_{bb_p}_2.0", df.get("BBL_20_2.0", df["close"] * 0.98))

    # Relative volume
    df["REL_VOL"] = df["volume"] / df["volume"].rolling(rel_vol_p).mean()
    df["REL_VOL"] = df["REL_VOL"].fillna(1.0)

    # Realized volatility
    df["REAL_VOL"] = df["close"].pct_change().rolling(atr_p).std() * (252 ** 0.5)
    df["REAL_VOL"] = df["REAL_VOL"].fillna(0.0)

    # Precompute pivots for Market Structure Agent
    df["is_pivot_high"] = (df["high"] > df["high"].shift(1)) & (df["high"] > df["high"].shift(2)) & \
                          (df["high"] > df["high"].shift(-1)) & (df["high"] > df["high"].shift(-2))
    df["is_pivot_low"] = (df["low"] < df["low"].shift(1)) & (df["low"] < df["low"].shift(2)) & \
                         (df["low"] < df["low"].shift(-1)) & (df["low"] < df["low"].shift(-2))

    return df



# ─────────────────────────────────────────────
#  Fear & Greed Index
# ─────────────────────────────────────────────

def fetch_fear_greed() -> tuple[Optional[int], Optional[str]]:
    try:
        resp = get_with_retry("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = resp.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        logger.warning(f"Fear & Greed fetch failed: {e}")
        return None, None


# ─────────────────────────────────────────────
#  Main Factory
# ─────────────────────────────────────────────

import time

_SNAPSHOT_CACHE = {}  # (symbol, timeframe) -> (timestamp_float, MarketSnapshot)
CACHE_TTL_SECONDS = 60


def get_higher_timeframe(tf: str) -> str:
    """Map lower timeframe to its higher-timeframe trend filter."""
    tf_clean = tf.lower().strip()
    if tf_clean in ("5m", "15m"):
        return "4h"
    elif tf_clean in ("1h", "4h"):
        return "1d"
    return "1d"


def build_snapshot(symbol: str, timeframe: str = None, is_htf: bool = False) -> MarketSnapshot:
    """
    Main entry point. Fetch data, compute indicators,
    return a ready-to-use MarketSnapshot.

    Asset routing:
      - Bybit linear perpetual CFDs (AAPL/USDT:USDT, XAU/USDT:USDT …) → BybitCFDFetcher
      - Crypto spot/perp (BTC/USDT, ETH/USDT …)                        → CryptoDataFetcher
      - Stock tickers (AAPL, TSLA, …)                                   → StockDataFetcher
    """
    from trading_engine.market_hours import classify_symbol, AssetClass
    from trading_engine.data.cfd_data import BybitCFDFetcher

    tf = timeframe or settings.timeframe
    cache_key = (symbol, tf)

    # Check cache to avoid redundant API calls within the same cycle
    now = time.time()
    if cache_key in _SNAPSHOT_CACHE:
        cached_time, cached_snap = _SNAPSHOT_CACHE[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            logger.info(f"Using cached market snapshot for {symbol} ({tf})")
            return cached_snap

    # Classify the symbol to select the right fetcher
    asset_class = classify_symbol(symbol)

    # Bybit linear CFDs (stock CFDs + precious metals)
    if asset_class in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL):
        asset_type = "cfd"
        fetcher = BybitCFDFetcher()
        df = fetcher.fetch_ohlcv(symbol, tf)
        metrics = {}
        order_flow = {}
        fg_value, fg_label = None, None

    elif asset_class == AssetClass.CRYPTO:
        asset_type = "crypto"
        fetcher = CryptoDataFetcher()
        df = fetcher.fetch_ohlcv(symbol, tf)
        metrics = {}
        fg_value, fg_label = fetch_fear_greed()
        order_flow = fetcher.fetch_order_flow(symbol)

    else:
        # Plain stock ticker (AAPL, TSLA without the :USDT suffix)
        asset_type = "stock"
        fetcher = StockDataFetcher()
        df = fetcher.fetch_ohlcv(symbol, tf)
        metrics = fetcher.fetch_metrics(symbol)
        order_flow = {}
        fg_value, fg_label = None, None

    df = compute_indicators(df)
    latest = df.iloc[-1]

    # Build Bollinger Width
    bb_upper = latest.get("BBU_20_2.0", latest["close"] * 1.02)
    bb_lower = latest.get("BBL_20_2.0", latest["close"] * 0.98)
    bb_width = (bb_upper - bb_lower) / latest["close"] if latest["close"] > 0 else 0

    snap = MarketSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        timeframe=tf,
        timestamp=datetime.now(timezone.utc),
        df=df,
        close=float(latest["close"]),
        volume=float(latest["volume"]),
        ema20=float(latest.get("EMA_20", 0) or 0),
        ema50=float(latest.get("EMA_50", 0) or 0),
        ema200=float(latest.get("EMA_200", 0) or 0),
        rsi=float(latest.get("RSI_14", 50) or 50),
        stoch_rsi_k=float(latest.get("STOCHRSIk_14_14_3_3", 50) or 50),
        stoch_rsi_d=float(latest.get("STOCHRSId_14_14_3_3", 50) or 50),
        roc=float(latest.get("ROC_10", 0) or 0),
        obv=float(latest.get("OBV", 0) or 0),
        rel_volume=float(latest.get("REL_VOL", 1) or 1),
        vwap=float(latest.get("VWAP_D", latest["close"]) or latest["close"]),
        atr=float(latest.get("ATR_14", 0) or 0),
        bb_width=float(bb_width),
        realized_vol=float(latest.get("REAL_VOL", 0) or 0),
        open_interest=order_flow.get("open_interest"),
        funding_rate=order_flow.get("funding_rate"),
        long_liq_24h=order_flow.get("long_liq_24h"),
        short_liq_24h=order_flow.get("short_liq_24h"),
        fear_greed_index=fg_value,
        fear_greed_label=fg_label,
        market_cap=metrics.get("market_cap"),
        shares_outstanding=metrics.get("shares_outstanding"),
        net_income=metrics.get("net_income"),
        revenue=metrics.get("revenue"),
    )

    if not is_htf:
        htf = get_higher_timeframe(tf)
        if htf != tf:
            try:
                snap.htf_snap = build_snapshot(symbol, htf, is_htf=True)
            except Exception as e_htf:
                logger.warning(f"Failed to build higher timeframe ({htf}) snapshot for {symbol}: {e_htf}")

    logger.success(f"Snapshot built: {symbol} ({asset_type}) | Close={snap.close:.4f} | RSI={snap.rsi:.1f}")
    _SNAPSHOT_CACHE[cache_key] = (time.time(), snap)
    return snap

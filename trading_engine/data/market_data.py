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
            if settings.bybit_api_key:
                params["apiKey"] = settings.bybit_api_key
                params["secret"] = settings.bybit_api_secret
            
        self.exchange: ccxt.Exchange = exchange_class(params)
        
        # Enable sandbox/testnet mode
        if settings.crypto_testnet:
            if exchange_name == "binance":
                self.exchange.options = {"defaultType": "future"}
                self.exchange.urls["api"]["public"] = "https://testnet.binance.vision/api"
            elif exchange_name == "bybit":
                if settings.bybit_demo_trading:
                    self.exchange.enable_demo_trading(True)
                else:
                    self.exchange.set_sandbox_mode(True)
            else:
                self.exchange.set_sandbox_mode(True)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        """Fetch OHLCV candles and return raw DataFrame with auto-pagination for large limits."""
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
        """Fetch aggregated bars from Massive.com."""
        logger.info(f"Fetching stock OHLCV: {symbol} {timeframe}")
        api_key = settings.get_massive_api_key
        multiplier, span = self._parse_timeframe(timeframe)
        url = f"{self.BASE}/v2/aggs/ticker/{symbol}/range/{multiplier}/{span}/2023-01-01/2099-01-01"
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": limit,
            "apiKey": api_key,
        }
        resp = get_with_retry(url, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        df = pd.DataFrame(results)
        df.rename(columns={"t": "timestamp", "o": "open", "h": "high",
                            "l": "low", "c": "close", "v": "volume"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def fetch_latest_price(self, symbol: str) -> float:
        """Fetch latest close price for a stock via Previous Close endpoint."""
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


# ─────────────────────────────────────────────
#  Indicator Calculator
# ─────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all technical indicators via pandas-ta."""
    # Trend
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)

    # Momentum
    df.ta.rsi(length=14, append=True)
    df.ta.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)
    df.ta.roc(length=10, append=True)

    # Volume
    df.ta.obv(append=True)
    df.ta.vwap(append=True)

    # Volatility
    df.ta.atr(length=14, append=True)
    df.ta.bbands(length=20, std=2, append=True)

    # Relative volume (current volume vs 20-period avg)
    df["REL_VOL"] = df["volume"] / df["volume"].rolling(20).mean()

    # Realized volatility (14-period std of log returns * sqrt(252))
    log_ret = (df["close"] / df["close"].shift(1)).apply(lambda x: x if x > 0 else 1).apply(
        lambda x: x.__class__(x) if x > 0 else 1
    )
    df["REAL_VOL"] = df["close"].pct_change().rolling(14).std() * (252 ** 0.5)

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


def build_snapshot(symbol: str, timeframe: str = None) -> MarketSnapshot:
    """
    Main entry point. Fetch data, compute indicators,
    return a ready-to-use MarketSnapshot.
    """
    tf = timeframe or settings.timeframe
    cache_key = (symbol, tf)
    
    # Check cache to avoid redundant API calls within the same cycle
    now = time.time()
    if cache_key in _SNAPSHOT_CACHE:
        cached_time, cached_snap = _SNAPSHOT_CACHE[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            logger.info(f"Using cached market snapshot for {symbol} ({tf})")
            return cached_snap

    is_crypto = "/" in symbol or symbol.endswith("USDT") or symbol.endswith("USD")
    # Clean symbol formatting for consistency
    asset_type = "crypto" if is_crypto else "stock"

    # Fetch OHLCV
    metrics = {}
    if asset_type == "crypto":
        fetcher = CryptoDataFetcher()
        df = fetcher.fetch_ohlcv(symbol, tf)
    else:
        fetcher = StockDataFetcher()
        df = fetcher.fetch_ohlcv(symbol, tf)
        metrics = fetcher.fetch_metrics(symbol)

    df = compute_indicators(df)
    latest = df.iloc[-1]

    # Fear & Greed (crypto only)
    fg_value, fg_label = (None, None)
    if asset_type == "crypto":
        fg_value, fg_label = fetch_fear_greed()

    # Order flow (crypto only)
    order_flow = {}
    if asset_type == "crypto":
        order_flow = fetcher.fetch_order_flow(symbol)

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
        atr=float(latest.get("ATRr_14", 0) or 0),
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

    logger.success(f"Snapshot built: {symbol} | Close={snap.close:.2f} | RSI={snap.rsi:.1f}")
    _SNAPSHOT_CACHE[cache_key] = (time.time(), snap)
    return snap

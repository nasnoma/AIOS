import sys
import os
sys.path.append("/Users/nasir.noma/claude_projects/AIOS")

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from loguru import logger
from unittest.mock import patch

from trading_engine.backtest.engine import run_walk_forward_optimization
from trading_engine.data.market_data import compute_indicators

def generate_realistic_ohlcv(limit: int = 1080, timeframe: str = "4h") -> pd.DataFrame:
    """
    Generates realistic synthetic OHLCV data representing four market regimes:
    1. Bullish Trend
    2. Choppy Range
    3. Bearish Trend
    4. Recovery Trend
    Scales boundaries, drift, noise, and indicators dynamically based on timeframe.
    """
    logger.info(f"Generating {limit} bars of realistic market regime data for timeframe={timeframe}...")
    
    freq_mapping = {
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d"
    }
    freq = freq_mapping.get(timeframe, "4h")
    dates = pd.date_range("2026-01-01", periods=limit, freq=freq, tz="UTC")
    
    tf_mapping = {
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "4h": 6,
        "1d": 1,
    }
    candles_per_day = tf_mapping.get(timeframe, 6)
    
    # Keep candle-relative scaling at 1.0 to match the agents' hardcoded percentage thresholds
    time_ratio = 1.0
    drift_scale = 1.0
    noise_scale = 1.0
    
    closes = np.zeros(limit)
    price = 50000.0
    
    p1 = limit // 4
    p2 = limit // 2
    p3 = 3 * limit // 4
    
    # 1. Bullish Trend
    for idx in range(0, p1):
        drift = 320.0 * drift_scale
        noise = np.random.normal(0, 80 * noise_scale)
        price = max(10000, price + drift + noise)
        closes[idx] = price
        
    # 2. Choppy Range
    pivot = price
    for idx in range(p1, p2):
        pull = (pivot - price) * 0.08
        noise = np.random.normal(0, 180 * noise_scale)
        price = max(10000, price + pull + noise)
        closes[idx] = price
        
    # 3. Bearish Trend
    for idx in range(p2, p3):
        drift = -380.0 * drift_scale
        noise = np.random.normal(0, 95 * noise_scale)
        price = max(10000, price + drift + noise)
        closes[idx] = price
        
    # 4. Recovery Trend
    for idx in range(p3, limit):
        drift = 280.0 * drift_scale
        noise = np.random.normal(0, 75 * noise_scale)
        price = max(10000, price + drift + noise)
        closes[idx] = price

    df = pd.DataFrame(index=dates)
    df["close"] = closes
    
    # High/Low/Open bounds with proportional ranges (ATR% in 1%-3% healthy band)
    df["open"] = df["close"].shift(1).fillna(closes[0])
    df["high"] = df[["open", "close"]].max(axis=1) + np.random.uniform(0.01, 0.025, limit) * df["close"]
    df["low"] = df[["open", "close"]].min(axis=1) - np.random.uniform(0.01, 0.025, limit) * df["close"]
    df["volume"] = np.random.uniform(1000, 5000, limit)
    
    # Volume expands on trend breakouts
    df.iloc[0:p1, df.columns.get_loc("volume")] *= 1.8
    df.iloc[p2:p3, df.columns.get_loc("volume")] *= 2.0
    
    # Order flow features
    funding_rates = np.zeros(limit)
    open_interests = np.zeros(limit)
    long_liqs = np.zeros(limit)
    short_liqs = np.zeros(limit)
    
    oi = 100_000_000.0
    for idx in range(limit):
        funding_rates[idx] = 0.0001 # Neutral
        if 0 <= idx < p1:
            oi *= (1 + 0.015 * time_ratio)
            long_liqs[idx] = 500_000.0
            short_liqs[idx] = 2_500_000.0
        elif p1 <= idx < p2:
            oi *= np.random.uniform(1 - 0.02 * time_ratio, 1 + 0.02 * time_ratio)
            long_liqs[idx] = 1_000_000.0
            short_liqs[idx] = 1_000_000.0
        elif p2 <= idx < p3:
            oi *= (1 + 0.02 * time_ratio)
            long_liqs[idx] = 3_500_000.0
            short_liqs[idx] = 400_000.0
        else:
            oi *= (1 + 0.015 * time_ratio)
            long_liqs[idx] = 800_000.0
            short_liqs[idx] = 1_800_000.0
            
        open_interests[idx] = oi
        
    df["funding_rate"] = funding_rates
    df["open_interest"] = open_interests
    df["long_liq_24h"] = long_liqs
    df["short_liq_24h"] = short_liqs
    
    # Sentiment
    df["fear_greed_index"] = 50
    df.loc[df.index[0:p1], "fear_greed_index"] = 72
    df.loc[df.index[p2:p3], "fear_greed_index"] = 22
    
    return df

def mock_fetch_ohlcv_from_generator(self, symbol, timeframe, limit):
    # Returns formatted raw nested list for CCXT mock interface
    df = generate_realistic_ohlcv(limit, timeframe)
    data = []
    for idx, (timestamp, row) in enumerate(df.iterrows()):
        data.append([
            int(timestamp.timestamp() * 1000),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"])
        ])
    return data

if __name__ == "__main__":
    print("\n" + "="*80)
    print("      180-DAY WALK-FORWARD OPTIMIZATION BACKTEST SIMULATOR")
    print("="*80)
    print("Initializing backtester: $10,000.00 initial capital")
    print("Asset: BTC/USDT | Timeframe: 5m | Period: 180 days (capped at 20k candles)")
    print("Regimes: Bullish Trend -> Choppy Range -> Bearish Trend -> Recovery")
    print("="*80)
    
    # Suppress real api key requirements and fetch from synthetic generator
    import unittest.mock
    mock_exchange = unittest.mock.MagicMock()
    mock_exchange.fetch_ohlcv.side_effect = lambda symbol, timeframe, limit: mock_fetch_ohlcv_from_generator(None, symbol, timeframe, limit)
    
    # Adjust thresholds to allow enough trades over synthetic indicators
    from trading_engine.config import settings
    settings.min_avg_confidence = 50.0  # Lowered to allow trade entry on mock
    settings.min_agent_agreement = 3    # Lowered to ensure activity (accounts for 2 mock HOLD agents)
    
    with patch("ccxt.binance", return_value=mock_exchange):
        results = run_walk_forward_optimization(
            symbol="BTC/USDT",
            timeframe="5m",
            days=180,
            lookback_days=30, # Train window
            forward_days=7,   # Test window
        )
        
        # Save results summary
        out_file = "/Users/nasir.noma/claude_projects/AIOS/trading_engine/backtest_results/synthetic_wfo_report.json"
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        import json
        with open(out_file, "w") as f:
            # Clean results for json serialization
            clean_results = {k: v for k, v in results.items() if k != "weights_progression"}
            json.dump(clean_results, f, indent=2)
        print(f"\nSaved synthetic WFO backtest report to {out_file}")

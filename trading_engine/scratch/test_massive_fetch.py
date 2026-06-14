import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent))

import pandas as pd
from trading_engine.config import settings
from trading_engine.data.market_data import StockDataFetcher

class TestFetcher(StockDataFetcher):
    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        api_key = settings.get_massive_api_key
        multiplier, span = self._parse_timeframe(timeframe)
        # Using sort=desc to get the latest candles
        url = f"{self.BASE}/v2/aggs/ticker/{symbol}/range/{multiplier}/{span}/2023-01-01/2099-01-01"
        params = {
            "adjusted": "true",
            "sort": "desc",
            "limit": limit,
            "apiKey": api_key,
        }
        import requests
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        df = pd.DataFrame(results)
        df.rename(columns={"t": "timestamp", "o": "open", "h": "high",
                            "l": "low", "c": "close", "v": "volume"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True) # Sort ascending chronologically
        return df[["open", "high", "low", "close", "volume"]]

fetcher = TestFetcher()
try:
    print("Fetching X:BTCUSD with sort=desc...")
    df = fetcher.fetch_ohlcv("X:BTCUSD", "5m", 300)
    print("Success! DataFrame shape:", df.shape)
    print("Head (oldest):")
    print(df.head(2))
    print("Tail (newest):")
    print(df.tail(2))
except Exception as e:
    print("Error:", e)

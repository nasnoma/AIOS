import datetime
from enum import Enum
from dataclasses import dataclass
import pandas as pd
import pandas_ta as ta
import ccxt
from loguru import logger

class Regime(Enum):
    BULL = "BULL"
    RANGE = "RANGE"
    BEAR = "BEAR"

@dataclass
class RegimeState:
    regime: str
    adx: float
    plus_di: float
    minus_di: float
    sma_50: float
    sma_200: float
    price: float
    confirmed_bars: int
    last_updated: str

class RegimeDetector:
    def __init__(self, symbol='BTC/USDT', timeframe='1h', confirm_bars=3):
        self.symbol = symbol
        self.timeframe = timeframe
        self.confirm_bars = confirm_bars
        self._history = []
        self._cached_state = None
    
    def detect(self, exchange: ccxt.Exchange) -> RegimeState:
        now = datetime.datetime.now(datetime.timezone.utc)
        
        if self._cached_state:
            last_dt = datetime.datetime.fromisoformat(self._cached_state.last_updated)
            if (now - last_dt).total_seconds() < 1800: # 30 minutes
                return self._cached_state
                
        try:
            ohlcv = exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=210)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            df.ta.sma(length=50, append=True)
            df.ta.sma(length=200, append=True)
            df.ta.adx(length=14, append=True)
            
            latest = df.iloc[-1]
            
            price = latest['close']
            sma_50 = latest['SMA_50']
            sma_200 = latest['SMA_200']
            adx = latest['ADX_14']
            plus_di = latest['DMP_14']
            minus_di = latest['DMN_14']
            
            current_raw_regime = Regime.RANGE
            
            if price > sma_50 > sma_200 and adx > 25 and plus_di > minus_di:
                current_raw_regime = Regime.BULL
            elif price < sma_50 and adx > 25 and minus_di > plus_di:
                current_raw_regime = Regime.BEAR
                
            self._history.append(current_raw_regime)
            if len(self._history) > self.confirm_bars:
                self._history.pop(0)
                
            confirmed_regime = current_raw_regime
            if len(self._history) == self.confirm_bars and all(r == current_raw_regime for r in self._history):
                confirmed_regime = current_raw_regime
            else:
                if self._cached_state:
                    confirmed_regime = Regime(self._cached_state.regime)
                    
            state = RegimeState(
                regime=confirmed_regime.value,
                adx=float(adx),
                plus_di=float(plus_di),
                minus_di=float(minus_di),
                sma_50=float(sma_50),
                sma_200=float(sma_200),
                price=float(price),
                confirmed_bars=len([r for r in self._history if r == confirmed_regime]),
                last_updated=now.isoformat()
            )
            
            self._cached_state = state
            return state
            
        except Exception as e:
            logger.error(f"Error detecting regime for {self.symbol}: {e}")
            if self._cached_state:
                return self._cached_state
            raise

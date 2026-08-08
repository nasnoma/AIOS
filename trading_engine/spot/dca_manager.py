import datetime
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import pandas_ta as ta
import ccxt
from loguru import logger

@dataclass
class DCASignal:
    symbol: str
    trigger_type: str
    rsi: float
    bb_pct: float
    adx: float
    regime: str
    timestamp: str

class DCAManager:
    def __init__(self, bb_period=20, bb_std=2.5, rsi_period=14, adx_period=14):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        
    def check(self, symbol: str, exchange: ccxt.Exchange, regime: str) -> Optional[DCASignal]:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            bb = df.ta.bbands(length=self.bb_period, std=self.bb_std)
            df = pd.concat([df, bb], axis=1)
            df.ta.rsi(length=self.rsi_period, append=True)
            df.ta.adx(length=self.adx_period, append=True)
            
            latest = df.iloc[-1]
            price = latest['close']
            lower_band = latest[f'BBL_{self.bb_period}_{self.bb_std}']
            rsi = latest[f'RSI_{self.rsi_period}']
            adx = latest[f'ADX_{self.adx_period}']
            
            signal = None
            
            if regime == "RANGE":
                if price < lower_band and rsi < 30 and adx < 25:
                    signal = "bb_rsi"
            elif regime == "BULL":
                if price < lower_band and rsi < 35:
                    signal = "bb_rsi"
            elif regime == "BEAR":
                if price < lower_band and rsi < 20 and adx < 30:
                    signal = "extreme_oversold"
                    
            if signal:
                bb_pct = (price - lower_band) / lower_band * 100
                return DCASignal(
                    symbol=symbol,
                    trigger_type=signal,
                    rsi=float(rsi),
                    bb_pct=float(bb_pct),
                    adx=float(adx),
                    regime=regime,
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking DCA conditions for {symbol}: {e}")
            return None
            
    def extra_buy_multiplier(self, regime: str) -> float:
        if regime == "BULL":
            return 1.0
        elif regime == "RANGE":
            return 1.5
        elif regime == "BEAR":
            return 2.0
        return 1.0

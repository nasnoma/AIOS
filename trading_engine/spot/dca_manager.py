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
    vwap_diff_pct: float
    supertrend_direction: int  # 1 for bullish, -1 for bearish
    adx: float
    regime: str
    timestamp: str

class DCAManager:
    """
    Enhanced Day-Trading DCA & Dip Engine.
    Uses VWAP (Volume Weighted Average Price), Supertrend (10, 3.0),
    Bollinger Bands (20, 2.5), and RSI(14) to catch high-probability day trading dips.
    """
    def __init__(self, bb_period=20, bb_std=2.5, rsi_period=14, adx_period=14):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.adx_period = adx_period

    def check(self, symbol: str, exchange: ccxt.Exchange, regime: str) -> Optional[DCASignal]:
        try:
            # Fetch 15m candles for fast day-trading responsiveness
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Compute technical indicators
            bb = df.ta.bbands(length=self.bb_period, std=self.bb_std)
            df = pd.concat([df, bb], axis=1)
            df.ta.rsi(length=self.rsi_period, append=True)
            df.ta.adx(length=self.adx_period, append=True)
            
            # Compute VWAP
            try:
                df.ta.vwap(append=True)
            except Exception:
                # Fallback VWAP if pandas_ta vwap needs datetime index
                df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

            # Compute Supertrend
            try:
                st = df.ta.supertrend(length=10, multiplier=3.0)
                df = pd.concat([df, st], axis=1)
            except Exception:
                pass

            latest = df.iloc[-1]
            price = latest['close']
            lower_band = latest.get(f'BBL_{self.bb_period}_{self.bb_std}', price * 0.98)
            rsi = latest.get(f'RSI_{self.rsi_period}', 50)
            adx = latest.get(f'ADX_{self.adx_period}', 15)
            vwap = latest.get('VWAP_D') if 'VWAP_D' in latest else latest.get('vwap', price)
            
            # Supertrend direction: 1 = bullish (green), -1 = bearish (red)
            st_dir = 1
            st_col = [c for c in df.columns if c.startswith('SUPERTd_')]
            if st_col:
                st_dir = int(latest[st_col[0]])

            vwap_diff_pct = ((price - vwap) / vwap) * 100 if vwap else 0.0
            
            signal = None
            
            # Day trading signal logic:
            # 1. Price is at a discount to VWAP (price < vwap)
            # 2. RSI is oversold or recovering
            # 3. Regime checks
            if regime == "RANGE":
                if price <= lower_band and rsi < 32 and price < vwap:
                    signal = "vwap_bb_dip"
            elif regime == "BULL":
                # In Bull regime: buy dip when price dips below VWAP and RSI < 40 with Supertrend green
                if (price <= lower_band or rsi < 36) and price < vwap and st_dir == 1:
                    signal = "bull_vwap_pullback"
            elif regime == "BEAR":
                # In Bear regime: require deep extreme oversold (RSI < 22) + below lower band
                if price <= lower_band and rsi < 22 and vwap_diff_pct < -2.0:
                    signal = "bear_extreme_oversold"
                    
            if signal:
                return DCASignal(
                    symbol=symbol,
                    trigger_type=signal,
                    rsi=float(rsi),
                    vwap_diff_pct=float(vwap_diff_pct),
                    supertrend_direction=st_dir,
                    adx=float(adx),
                    regime=regime,
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error in day-trading DCA check for {symbol}: {e}")
            return None
            
    def extra_buy_multiplier(self, regime: str) -> float:
        if regime == "BULL":
            return 1.0
        elif regime == "RANGE":
            return 1.5
        elif regime == "BEAR":
            return 2.0
        return 1.0

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

            # Compute Supertrend manually (pandas-ta .supertrend() not available in all versions)
            try:
                atr_col = f'ATRr_{self.adx_period}'
                if atr_col not in df.columns:
                    df.ta.atr(length=10, append=True)
                    atr_col = 'ATRr_10'
                atr_series = df.get(atr_col, df.get('ATR_14', pd.Series(0.0, index=df.index)))
                hl2 = (df['high'] + df['low']) / 2.0
                upper_band = hl2 + 3.0 * atr_series
                lower_band = hl2 - 3.0 * atr_series
                st_final = pd.Series(index=df.index, dtype=float)
                st_dir = pd.Series(index=df.index, dtype=int)
                prev_dir = 0
                prev_close = df['close'].iloc[0]
                for idx_i in range(len(df)):
                    close_i = df['close'].iloc[idx_i]
                    if prev_dir == 1:
                        st_val = lower_band.iloc[idx_i] if close_i > lower_band.iloc[idx_i] else upper_band.iloc[idx_i]
                    elif prev_dir == -1:
                        st_val = upper_band.iloc[idx_i] if close_i < upper_band.iloc[idx_i] else lower_band.iloc[idx_i]
                    else:
                        st_val = upper_band.iloc[idx_i] if close_i < hl2.iloc[idx_i] else lower_band.iloc[idx_i]
                    d = 1 if close_i > st_val else -1
                    st_final.iloc[idx_i] = st_val
                    st_dir.iloc[idx_i] = d
                    prev_dir = d
                    prev_close = close_i
                df['ST_VAL'] = st_final.fillna(0.0)
                df['ST_DIR'] = st_dir.fillna(0)
            except Exception as st_err:
                logger.debug(f"Supertrend computation failed in DCA: {st_err}")
                df['ST_DIR'] = 1

            latest = df.iloc[-1]
            price = latest['close']
            lower_band = latest.get(f'BBL_{self.bb_period}_{self.bb_std}', price * 0.98)
            rsi = latest.get(f'RSI_{self.rsi_period}', 50)
            adx = latest.get(f'ADX_{self.adx_period}', 15)
            vwap = latest.get('VWAP_D') if 'VWAP_D' in latest else latest.get('vwap', price)
            
            # Supertrend direction: 1 = bullish (green), -1 = bearish (red)
            st_dir = int(latest.get('ST_DIR', 1))

            vwap_diff_pct = ((price - vwap) / vwap) * 100 if vwap else 0.0
            
            # Premium vs Discount Zone Equilibrium (50% Fibonacci Range level):
            swing_high = df['high'].max()
            swing_low = df['low'].min()
            equilibrium_price = (swing_high + swing_low) / 2.0 if swing_high > swing_low else price
            is_discount_zone = price <= equilibrium_price  # Price is in Discount Zone (cheap)

            # Quick Flip Opening Range Reversal (UTC Day Session Low Reversal):
            utc_open_low = df['low'].tail(24).min() if len(df) >= 24 else swing_low
            is_opening_range_reversal = (latest['low'] <= utc_open_low) and (latest['close'] > utc_open_low) and (rsi < 38)

            # Liquidity Sweep & POI Mitigation detection (Smart Money Concept):
            # Detects when low price swept below lower band or prior low but candle closed higher (bullish rejection wick)
            prev_candle = df.iloc[-2] if len(df) >= 2 else latest
            is_liquidity_sweep = (latest['low'] < lower_band or latest['low'] < prev_candle['low']) and (latest['close'] > latest['low'] + (latest['high'] - latest['low']) * 0.4)

            # Day trading signal logic:
            if regime == "RANGE":
                if (price <= lower_band or is_liquidity_sweep or is_opening_range_reversal) and rsi < 36 and price < vwap and is_discount_zone:
                    signal = "quick_flip_opening_range_dip" if is_opening_range_reversal else ("liquidity_sweep_poi_dip" if is_liquidity_sweep else "vwap_bb_dip")
            elif regime == "BULL":
                # In Bull regime: buy dip when price sweeps liquidity or opening range in Discount Zone below VWAP with green Supertrend
                if (price <= lower_band or is_liquidity_sweep or is_opening_range_reversal or rsi < 40) and price < vwap and st_dir == 1 and is_discount_zone:
                    signal = "quick_flip_opening_range_dip" if is_opening_range_reversal else ("bull_liquidity_sweep" if is_liquidity_sweep else "bull_vwap_pullback")
            elif regime == "BEAR":
                # In Bear regime: require deep liquidity sweep / opening range reclaim in Discount Zone + extreme oversold (RSI < 22)
                if (price <= lower_band and rsi < 22 and vwap_diff_pct < -2.0 and is_discount_zone) or ((is_liquidity_sweep or is_opening_range_reversal) and rsi < 20):
                    signal = "bear_sweep_extreme_oversold"



                    
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

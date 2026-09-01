"""
trading_engine/spot/btc_master_filter.py

Zero-latency, in-memory Bitcoin Master Trend & Safety Filter (BTC Guard).
Tracks BTC 1h/4h price action, trend momentum, and flash-dump shocks.
Gates and adapts altcoin buy orders without introducing API latency.
"""
import time
import datetime
from dataclasses import dataclass, field
from typing import Tuple, Dict, Any, Optional
import pandas as pd
import pandas_ta as ta
import ccxt
from loguru import logger


@dataclass
class BTCMasterState:
    btc_price: float = 0.0
    btc_1h_change_pct: float = 0.0
    btc_4h_change_pct: float = 0.0
    btc_24h_change_pct: float = 0.0
    regime: str = "RANGE"  # BULL, RANGE, BEAR
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    is_flash_dump: bool = False
    flash_dump_until: float = 0.0
    is_safe_for_alt_buys: bool = True
    safety_reason: str = "BTC conditions healthy"
    last_updated: float = 0.0
    last_updated_iso: str = ""


class BTCMasterFilter:
    """
    Singleton in-memory Bitcoin filter.
    Updated once per 30-second tick loop; read in <0.001ms by any grid engine.
    """
    _instance: Optional['BTCMasterFilter'] = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(BTCMasterFilter, cls).__new__(cls)
            cls._instance._init_filter()
        return cls._instance

    def _init_filter(self):
        self.state = BTCMasterState()
        self.symbol = "BTC/USDT"
        self._cache_ttl_sec = 25.0  # Max age before re-polling
        self._flash_dump_cooldown_sec = 1800.0  # 30-minute freeze after a flash dump

    def update(self, exchange: ccxt.Exchange, force: bool = False) -> BTCMasterState:
        """
        Polls BTC 1h OHLCV from Bybit, computes momentum indicators,
        and writes updated state to CPU RAM.
        """
        now_ts = time.time()
        if not force and (now_ts - self.state.last_updated) < self._cache_ttl_sec and self.state.btc_price > 0:
            return self.state

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        try:
            ohlcv = exchange.fetch_ohlcv(self.symbol, timeframe='1h', limit=210)
            if not ohlcv or len(ohlcv) < 50:
                return self.state

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            df.ta.sma(length=50, append=True)
            df.ta.sma(length=200, append=True)
            df.ta.adx(length=14, append=True)

            latest = df.iloc[-1]
            prev_1h = df.iloc[-2] if len(df) >= 2 else latest
            prev_4h = df.iloc[-5] if len(df) >= 5 else df.iloc[0]
            prev_24h = df.iloc[-25] if len(df) >= 25 else df.iloc[0]

            price = float(latest['close'])
            sma_50 = float(latest.get('SMA_50', price) or price)
            sma_200 = float(latest.get('SMA_200', price) or price)
            adx = float(latest.get('ADX_14', 0.0) or 0.0)
            plus_di = float(latest.get('DMP_14', 0.0) or 0.0)
            minus_di = float(latest.get('DMN_14', 0.0) or 0.0)

            # Calculate returns
            p_1h_ago = float(prev_1h['close'])
            p_4h_ago = float(prev_4h['close'])
            p_24h_ago = float(prev_24h['close'])

            ret_1h = ((price - p_1h_ago) / p_1h_ago) * 100.0 if p_1h_ago > 0 else 0.0
            ret_4h = ((price - p_4h_ago) / p_4h_ago) * 100.0 if p_4h_ago > 0 else 0.0
            ret_24h = ((price - p_24h_ago) / p_24h_ago) * 100.0 if p_24h_ago > 0 else 0.0

            # Determine BTC macro regime
            if price > sma_50 > sma_200 and adx > 25 and plus_di > minus_di:
                regime = "BULL"
            elif price < sma_50 and adx > 25 and minus_di > plus_di:
                regime = "BEAR"
            else:
                regime = "RANGE"

            # 🛑 Flash Dump Detection:
            # Drop > 1.5% in 1 hour OR > 3.0% in 4 hours
            is_flash_dump = False
            flash_until = self.state.flash_dump_until
            if ret_1h < -1.50 or ret_4h < -3.00:
                is_flash_dump = True
                flash_until = max(flash_until, now_ts + self._flash_dump_cooldown_sec)
                logger.warning(
                    f"⚠️ [BTC FLASH DUMP DETECTED] BTC dropped {ret_1h:+.2f}% (1h), {ret_4h:+.2f}% (4h). "
                    f"Freezing altcoin buy orders for {int(self._flash_dump_cooldown_sec/60)} minutes."
                )

            # Check if previous flash-dump freeze is still active
            if flash_until > now_ts:
                is_flash_dump = True

            # Evaluate overall safety for altcoin buys
            is_safe = True
            reason = "BTC conditions healthy"
            if is_flash_dump:
                rem_mins = int((flash_until - now_ts) / 60)
                is_safe = False
                reason = f"BTC Flash Dump active ({ret_1h:+.2f}% 1h drop). Alt buys paused for {rem_mins}m."
            elif regime == "BEAR" and adx > 30 and ret_24h < -4.0:
                is_safe = False
                reason = f"BTC Severe Bear Breakdown (ADX: {adx:.1f}, 24h: {ret_24h:+.1f}%). Alt buys paused."

            self.state = BTCMasterState(
                btc_price=price,
                btc_1h_change_pct=round(ret_1h, 2),
                btc_4h_change_pct=round(ret_4h, 2),
                btc_24h_change_pct=round(ret_24h, 2),
                regime=regime,
                adx=round(adx, 2),
                plus_di=round(plus_di, 2),
                minus_di=round(minus_di, 2),
                sma_50=round(sma_50, 2),
                sma_200=round(sma_200, 2),
                is_flash_dump=is_flash_dump,
                flash_dump_until=flash_until,
                is_safe_for_alt_buys=is_safe,
                safety_reason=reason,
                last_updated=now_ts,
                last_updated_iso=now_utc.isoformat()
            )

        except Exception as e:
            logger.debug(f"BTC master filter update: {e}")

        return self.state

    def is_safe_for_alt_buys(self, symbol: str) -> Tuple[bool, str]:
        """
        Instant sub-microsecond in-memory lookup (<0.001 ms).
        BTC/USDT itself is never blocked by the altcoin filter.
        """
        if symbol == "BTC/USDT":
            return True, "BTC trades permitted on native grid"
        
        # If cache expired by > 120s, default safe to prevent deadlock
        if (time.time() - self.state.last_updated) > 120.0 and self.state.last_updated > 0:
            return True, "BTC cache stale — allowing orders"

        return self.state.is_safe_for_alt_buys, self.state.safety_reason

    def get_spacing_multiplier(self) -> float:
        """
        Returns spacing expansion factor during BTC weakness.
        """
        if self.state.regime == "BEAR":
            return 1.40  # Widen grid spacing by 40% defensively
        return 1.00

    def summary(self) -> Dict[str, Any]:
        """Returns serializable dictionary for API status endpoint."""
        return {
            "btc_price": self.state.btc_price,
            "btc_1h_change_pct": self.state.btc_1h_change_pct,
            "btc_4h_change_pct": self.state.btc_4h_change_pct,
            "btc_24h_change_pct": self.state.btc_24h_change_pct,
            "regime": self.state.regime,
            "adx": self.state.adx,
            "is_flash_dump": self.state.is_flash_dump,
            "is_safe_for_alt_buys": self.state.is_safe_for_alt_buys,
            "safety_reason": self.state.safety_reason,
            "last_updated": self.state.last_updated_iso,
        }


# Global singleton instance
btc_master_filter = BTCMasterFilter()

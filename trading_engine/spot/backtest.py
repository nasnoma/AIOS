import ccxt
import pandas as pd
import pandas_ta as ta
from loguru import logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from .grid_engine import GridEngine

class DummyPortfolio:
    def __init__(self):
        self.positions = {}
    def get_position(self, symbol):
        return self.positions.get(symbol, 0.0)
    def record_buy(self, symbol, qty, price, fee):
        base = symbol.split('/')[0]
        self.positions[base] = self.positions.get(base, 0.0) + qty
    def record_sell(self, symbol, qty, price, fee):
        base = symbol.split('/')[0]
        self.positions[base] = max(0.0, self.positions.get(base, 0.0) - qty)

def run_backtest(
    symbol: str = 'BTC/USDT',
    days: int = 30,
    regime: str = 'RANGE',
    allocated_usd: float = 10000.0,
    fee_rate: float = 0.001,
) -> Dict[str, Any]:
    
    exchange = ccxt.bybit({'enableRateLimit': True})
    
    # Fetch historical data
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    ohlcv = []
    
    try:
        # Fetching in chunks if necessary, but 30 days of 1h is 720 candles (Bybit limit usually 1000)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', since=since, limit=1000)
    except Exception as e:
        logger.error(f"Error fetching data for {symbol}: {e}")
        return {}

    if not ohlcv:
        logger.warning(f"No data fetched for {symbol}")
        return {}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

    # Compute true 14-period ATR from OHLCV for dynamic grid spacing
    df.ta.atr(length=14, append=True)
    atr_col = 'ATRr_14' if 'ATRr_14' in df.columns else ('ATR_14' if 'ATR_14' in df.columns else None)
    
    engine = GridEngine(
        symbol=symbol,
        allocated_usd=allocated_usd,
        paper_mode=True,
        fee_rate=fee_rate
    )
    engine.set_regime(regime)
    
    portfolio = DummyPortfolio()
    
    start_price = df.iloc[0]['open']
    end_price = df.iloc[-1]['close']
    
    # Initial Grid — pass real ATR for accurate dynamic spacing
    init_atr = float(df[atr_col].iloc[14]) if atr_col and pd.notna(df[atr_col].iloc[14]) else 0.0
    engine.build_grid(start_price, portfolio, atr=init_atr)
    engine.place_grid_orders(portfolio, exchange=None)
    
    last_rebuild_time = df.iloc[0]['timestamp']
    
    total_buys = 0
    total_sells = 0
    peak_capital = allocated_usd
    max_drawdown_usd = 0.0
    current_capital = allocated_usd
    
    for idx, row in df.iterrows():
        # Rebuild grid daily — use real ATR at this point in history
        if (row['timestamp'] - last_rebuild_time).total_seconds() >= 86400:
            row_atr = float(df.loc[idx, atr_col]) if atr_col and pd.notna(df.loc[idx, atr_col]) else 0.0
            engine.cancel_all()
            engine.build_grid(row['close'], portfolio, atr=row_atr)
            engine.place_grid_orders(portfolio, exchange=None)
            last_rebuild_time = row['timestamp']
            
        # Simulate fills (simplified low/high crossing)
        fills = engine.tick(row['low'], portfolio)  # Check buys
        total_buys += sum(1 for f in fills if f['side'] == 'buy')
        
        fills = engine.tick(row['high'], portfolio) # Check sells
        total_sells += sum(1 for f in fills if f['side'] == 'sell')
        
        # Track drawdown based on realized PnL
        pnl = sum(c['net_pnl'] for c in engine.completed_cycles)
        current_capital = allocated_usd + pnl
        if current_capital > peak_capital:
            peak_capital = current_capital
        drawdown = peak_capital - current_capital
        if drawdown > max_drawdown_usd:
            max_drawdown_usd = drawdown

    # Calculate metrics
    gross_pnl_usd = sum(c['gross_pnl'] for c in engine.completed_cycles)
    fees_paid_usd = sum(c['fee'] for c in engine.completed_cycles)
    net_pnl_usd = sum(c['net_pnl'] for c in engine.completed_cycles)
    
    return {
        'symbol': symbol,
        'days': days,
        'regime': regime,
        'total_cycles': len(engine.completed_cycles),
        'gross_pnl_usd': float(gross_pnl_usd),
        'fees_paid_usd': float(fees_paid_usd),
        'net_pnl_usd': float(net_pnl_usd),
        'net_pnl_pct': float((net_pnl_usd / allocated_usd) * 100),
        'avg_daily_pnl_usd': float(net_pnl_usd / days) if days > 0 else 0.0,
        'max_drawdown_usd': float(max_drawdown_usd),
        'start_price': float(start_price),
        'end_price': float(end_price),
        'price_change_pct': float(((end_price - start_price) / start_price) * 100),
    }


if __name__ == '__main__':
    for sym in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        for regime in ['BULL', 'RANGE', 'BEAR']:
            result = run_backtest(sym, days=30, regime=regime, allocated_usd=10000)
            if result:
                print(f"{sym} {regime}: {result['total_cycles']} cycles, net PnL: ${result['net_pnl_usd']:+.2f} ({result['net_pnl_pct']:+.2f}%)")

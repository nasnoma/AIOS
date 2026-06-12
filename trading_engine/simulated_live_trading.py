"""
trading_engine/simulated_live_trading.py
Runs a real-time, 7-minute simulated live trading environment.
Generates fluctuating market price ticks, updates indicator dataframes,
runs agent pipeline decisions, triggers paper trades, and monitors positions.
"""
from __future__ import annotations
import time
import json
import random
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import patch

from trading_engine import orchestrator
from trading_engine.data.market_data import MarketSnapshot
from trading_engine.execution import paper_trader
from trading_engine.agents.base import Signal
from trading_engine.config import settings

# Shared state for current live prices
live_prices = {
    "BTC/USDT": 65000.0,
    "ETH/USDT": 3500.0,
}

# Initial snapshots lookup
snapshots = {}

def make_simulation_snapshot(symbol: str, close_price: float, trend_direction: str = "neutral") -> MarketSnapshot:
    """Generate a dynamic MarketSnapshot with custom trend patterns."""
    n = 300
    
    # Generate price series based on trend direction
    if trend_direction == "bullish":
        closes = np.linspace(close_price * 0.95, close_price, n) + np.random.randn(n) * 15
        rsi = 68.0
        roc = 3.5
        ema_multiplier_20 = 0.99
        ema_multiplier_50 = 0.98
        ema_multiplier_200 = 0.95
        fear_greed = 22  # Extreme Fear (contrarian BUY)
        fg_label = "Extreme Fear"
    elif trend_direction == "bearish":
        closes = np.linspace(close_price * 1.05, close_price, n) + np.random.randn(n) * 15
        rsi = 32.0
        roc = -3.8
        ema_multiplier_20 = 1.01
        ema_multiplier_50 = 1.02
        ema_multiplier_200 = 1.05
        fear_greed = 82  # Extreme Greed (contrarian SELL)
        fg_label = "Extreme Greed"
    else:  # neutral
        closes = np.ones(n) * close_price + np.random.randn(n) * 20
        rsi = 50.0
        roc = 0.0
        ema_multiplier_20 = 1.0
        ema_multiplier_50 = 1.0
        ema_multiplier_200 = 1.0
        fear_greed = 50  # Neutral
        fg_label = "Neutral"

    freq_map = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "h", "4h": "4h", "1d": "D"}
    freq = freq_map.get(settings.timeframe.lower(), "4h")

    df = pd.DataFrame({
        "open": closes - np.random.uniform(0, 50, n),
        "high": closes + np.random.uniform(0, 100, n),
        "low": closes - np.random.uniform(0, 100, n),
        "close": closes,
        "volume": np.random.uniform(2000, 12000, n),
    }, index=pd.date_range("2026-06-01", periods=n, freq=freq, tz="UTC"))
    
    # Calculate indicators
    df["EMA_20"] = closes * ema_multiplier_20
    df["EMA_50"] = closes * ema_multiplier_50
    df["EMA_200"] = closes * ema_multiplier_200
    df["RSI_14"] = rsi
    df["STOCHRSIk_14_14_3_3"] = rsi + 5
    df["STOCHRSId_14_14_3_3"] = rsi
    df["ROC_10"] = roc
    df["OBV"] = 3_000_000.0
    df["REL_VOL"] = 1.5
    df["VWAP_D"] = close_price * 0.998 if trend_direction == "bullish" else close_price * 1.002
    df["ATRr_14"] = close_price * 0.015  # 1.5% ATR
    df["REAL_VOL"] = 0.22
    df["BBU_20_2.0"] = close_price * 1.02
    df["BBL_20_2.0"] = close_price * 0.98
    
    return MarketSnapshot(
        symbol=symbol,
        asset_type="crypto",
        timeframe=settings.timeframe,
        timestamp=datetime.now(timezone.utc),
        df=df,
        close=close_price,
        volume=df["volume"].iloc[-1],
        ema20=df["EMA_20"].iloc[-1],
        ema50=df["EMA_50"].iloc[-1],
        ema200=df["EMA_200"].iloc[-1],
        rsi=rsi,
        stoch_rsi_k=rsi + 5,
        stoch_rsi_d=rsi,
        roc=roc,
        obv=3_000_000.0,
        rel_volume=1.5,
        vwap=df["VWAP_D"].iloc[-1],
        atr=df["ATRr_14"].iloc[-1],
        bb_width=0.04,
        realized_vol=0.22,
        open_interest=500_000_000.0,
        funding_rate=0.0001,
        long_liq_24h=8_000_000.0,
        short_liq_24h=2_000_000.0,
        fear_greed_index=fear_greed,
        fear_greed_label=fg_label,
    )

def mock_build_snapshot(symbol: str, timeframe: str = None) -> MarketSnapshot:
    """Build or retrieve the mocked snapshot with current price."""
    # Retrieve current trend scenario defined in our main loop
    trend = current_scenarios.get(symbol, "neutral")
    price = live_prices[symbol]
    return make_simulation_snapshot(symbol, price, trend)

# Set the active trend scenarios for each symbol
current_scenarios = {
    "BTC/USDT": "neutral",
    "ETH/USDT": "neutral",
}

def simulate_market_ticks():
    """Simulate random micro-fluctuations in prices for open positions."""
    status = paper_trader.get_status()
    open_pos = paper_trader._load_state().open_positions
    
    # Only fluctuate if we have open positions or randomly over time
    for symbol in live_prices.keys():
        current_price = live_prices[symbol]
        
        # Check if there is an active position to guide the price path
        active_pos = next((p for p in open_pos if p.symbol == symbol), None)
        if active_pos:
            # 65% chance the trade moves favorably to simulate success, 35% chance against
            direction_factor = 1 if active_pos.direction == "long" else -1
            change_pct = random.uniform(-0.001, 0.002) * direction_factor
        else:
            # Random walk
            change_pct = random.uniform(-0.001, 0.001)
            
        live_prices[symbol] = round(current_price * (1.0 + change_pct), 2)
        
    # Trigger paper trader update with new prices
    paper_trader.update_prices(live_prices)

def run_trading_cycle():
    """Run orchestrator decision pipeline across all assets."""
    print(f"\n🔄 [CYCLE START] Running Multi-Agent Scanning at {datetime.now().strftime('%H:%M:%S')}...")
    
    status = paper_trader.get_status()
    portfolio_heat = status["portfolio_heat"] / 100
    open_positions = status["open_positions"]
    win_rate = status.get("win_rate", 50) / 100
    
    for symbol in live_prices.keys():
        print(f"\nScanning {symbol} (Current Price: ${live_prices[symbol]:,.2f})...")
        
        # Check if we already have an open position in this asset
        open_pos = paper_trader._load_state().open_positions
        if any(p.symbol == symbol for p in open_pos):
            print(f"  ↳ Skip: Position already open for {symbol}.")
            continue
            
        try:
            signal = orchestrator.run(
                symbol=symbol,
                portfolio_heat=portfolio_heat,
                open_positions=open_positions,
                win_rate=win_rate
            )
            
            if signal.final_action in ("BUY", "SELL"):
                direction = "long" if signal.final_action == "BUY" else "short"
                paper_trader.open_trade(
                    symbol=signal.symbol,
                    direction=direction,
                    entry=signal.entry_price,
                    size_usd=signal.position_size_usd or 0,
                    stop_loss=signal.stop_loss or 0,
                    take_profit=signal.take_profit or 0,
                )
        except Exception as e:
            print(f"  Error running pipeline for {symbol}: {e}")

def main():
    import sys
    duration_mins = 7
    until_closed = False
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "until-closed":
            until_closed = True
            duration_mins = 60  # safety cap of 60 mins
        else:
            try:
                duration_mins = int(sys.argv[1])
            except ValueError:
                pass

    print("\n" + "="*80)
    if until_closed:
        print("           REAL-TIME TRADING SIMULATION STARTED (UNTIL TARGETS HIT)")
    else:
        print(f"           {duration_mins}-MINUTE REAL-TIME TRADING SIMULATION STARTED")
    print("="*80)
    print(f"Initializing paper account: ${settings.account_size:,.2f}")
    print(f"Timeframe: {settings.timeframe} | Scanning assets: BTC/USDT, ETH/USDT")
    print("Live price updates will run every 5 seconds.")
    print("Scanning pipeline runs every 1 minute.")
    print("="*80)
    
    # Reset state to clean starting point
    if paper_trader.STATE_FILE.exists():
        try:
            paper_trader.STATE_FILE.unlink()
        except Exception:
            pass
            
    # Set starting prices
    live_prices["BTC/USDT"] = 65000.0
    live_prices["ETH/USDT"] = 3500.0
    
    start_time = time.time()
    duration = duration_mins * 60  # duration in seconds
    
    last_cycle_time = 0.0
    cycle_interval = 60.0  # Run signal cycle every 60 seconds
    has_opened = False
    
    # Define scheduled market trend scenarios for the cycles to showcase different signals
    # Cycle 1: Bullish BTC setup -> BUY
    # Cycle 2: Bearish ETH setup -> SELL (short)
    # Cycle 3: Neutral setups -> HOLD
    # Cycle 4: Extreme trend moves to trigger SL/TP hits
    scenarios_timeline = [
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 0-1
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 1-2
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 2-3
        {"BTC/USDT": "bullish", "ETH/USDT": "bearish"},  # Minute 3-4
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 4-5
        {"BTC/USDT": "bearish", "ETH/USDT": "bullish"},  # Minute 5-6
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 6-7
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 7-8
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 8-9
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 9-10
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 10-11
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 11-12
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 12-13
        {"BTC/USDT": "bullish", "ETH/USDT": "bearish"},  # Minute 13-14
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 14-15
        {"BTC/USDT": "bearish", "ETH/USDT": "bullish"},  # Minute 15-16
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 16-17
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 17-18
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 18-19
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 19-20
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 20-21
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 21-22
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 22-23
        {"BTC/USDT": "bullish", "ETH/USDT": "bearish"},  # Minute 23-24
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 24-25
        {"BTC/USDT": "bullish", "ETH/USDT": "neutral"},  # Minute 25-26
        {"BTC/USDT": "neutral", "ETH/USDT": "bearish"},  # Minute 26-27
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 27-28
        {"BTC/USDT": "bullish", "ETH/USDT": "bearish"},  # Minute 28-29
        {"BTC/USDT": "neutral", "ETH/USDT": "neutral"},  # Minute 29-30
    ]
    
    # Patch the market builder globally
    with patch("trading_engine.orchestrator.build_snapshot", side_effect=mock_build_snapshot):
        
        while True:
            elapsed = time.time() - start_time
            if elapsed >= duration:
                break
                
            minute_idx = min(int(elapsed / 60), len(scenarios_timeline) - 1)
            
            # Update active scenario
            global current_scenarios
            current_scenarios = scenarios_timeline[minute_idx]
            
            # Run scanning cycle
            if time.time() - last_cycle_time >= cycle_interval:
                # In until-closed mode, only run scanning for the first 2 minutes to open initial positions
                if not until_closed or elapsed < 120:
                    run_trading_cycle()
                last_cycle_time = time.time()
                
            # Simulate market ticks (price fluctuations + check stop loss/take profit)
            simulate_market_ticks()
            
            # Print status update
            open_pos = paper_trader._load_state().open_positions
            status = paper_trader.get_status()
            
            if len(open_pos) > 0:
                has_opened = True
                
            if until_closed and has_opened and len(open_pos) == 0:
                print("\n\n🎉 All open positions have hit their targets/stops! Exiting simulation.")
                break
            
            if until_closed:
                print(f"\r⏳ Running (Until Closed): {int(elapsed)//60}m {int(elapsed)%60:02d}s | "
                      f"BTC: ${live_prices['BTC/USDT']:,.2f} | ETH: ${live_prices['ETH/USDT']:,.2f} | "
                      f"Open Pos: {len(open_pos)} | Cash: ${status['cash']:,.2f} | PnL: ${status['total_pnl']:+,.2f}", end="")
            else:
                print(f"\r⏳ Running: {int(elapsed)//60}m {int(elapsed)%60:02d}s / {duration_mins}m 00s | "
                      f"BTC: ${live_prices['BTC/USDT']:,.2f} | ETH: ${live_prices['ETH/USDT']:,.2f} | "
                      f"Open Pos: {len(open_pos)} | Cash: ${status['cash']:,.2f} | PnL: ${status['total_pnl']:+,.2f}", end="")
            
            # Sleep 5 seconds between ticks
            time.sleep(5)
            
    # Simulation Complete - Output Final Summary
    print("\n\n" + "="*80)
    print("                       SIMULATION RUN COMPLETE")
    print("="*80)
    status = paper_trader.get_status()
    print(f"Final Account Balance:  ${status['account_size'] + status['total_pnl']:,.2f}")
    print(f"Total Return:           ${status['total_pnl']:+,.2f} ({status['total_pnl_pct']}% growth)")
    print(f"Win Rate:               {status['win_rate']}%")
    print(f"Total Wins:             {status['win_count']}")
    print(f"Total Losses:           {status['loss_count']}")
    
    print("\nClosed Trade History:")
    for t in status["trades"]:
        print(f"  - {t['opened_at'][:19]} | {t['symbol']} {t['direction'].upper()} | Entry: ${t['entry_price']:,.2f} | Exit: ${t['exit_price']:,.2f} | PnL: ${t['pnl_usd']:+,.2f} ({t['status'].upper()})")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()

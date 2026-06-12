"""
trading_engine/run_simulation.py
Simulates the entire multi-agent trading decision pipeline and execution layer.
Constructs synthetic market structures and shows step-by-step processing.
"""
from __future__ import annotations
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import patch

from trading_engine import orchestrator
from trading_engine.data.market_data import MarketSnapshot
from trading_engine.execution import paper_trader

def make_bullish_snapshot(symbol: str, close_price: float) -> MarketSnapshot:
    """Generate a high-confluence bullish snapshot for simulation."""
    n = 300
    # Create an upward trending price series
    closes = np.linspace(close_price * 0.90, close_price, n) + np.random.randn(n) * 10
    
    df = pd.DataFrame({
        "open": closes - np.random.uniform(0, 100, n),
        "high": closes + np.random.uniform(0, 200, n),
        "low": closes - np.random.uniform(0, 200, n),
        "close": closes,
        "volume": np.random.uniform(2000, 15000, n),
    }, index=pd.date_range("2026-06-01", periods=n, freq="4h", tz="UTC"))
    
    # Calculate indicators
    df["EMA_20"] = closes * 0.99
    df["EMA_50"] = closes * 0.98
    df["EMA_200"] = closes * 0.95
    df["RSI_14"] = 68.0
    df["STOCHRSIk_14_14_3_3"] = 75.0
    df["STOCHRSId_14_14_3_3"] = 70.0
    df["ROC_10"] = 4.2
    df["OBV"] = 5_000_000.0
    df["REL_VOL"] = 1.9
    df["VWAP_D"] = close_price * 0.995
    df["ATRr_14"] = close_price * 0.015  # ATR is 1.5% of price
    df["REAL_VOL"] = 0.25
    df["BBU_20_2.0"] = close_price * 1.02
    df["BBL_20_2.0"] = close_price * 0.98
    
    return MarketSnapshot(
        symbol=symbol,
        asset_type="crypto",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        df=df,
        close=close_price,
        volume=df["volume"].iloc[-1],
        ema20=df["EMA_20"].iloc[-1],
        ema50=df["EMA_50"].iloc[-1],
        ema200=df["EMA_200"].iloc[-1],
        rsi=68.0,
        stoch_rsi_k=75.0,
        stoch_rsi_d=70.0,
        roc=4.2,
        obv=5_000_000.0,
        rel_volume=1.9,
        vwap=df["VWAP_D"].iloc[-1],
        atr=df["ATRr_14"].iloc[-1],
        bb_width=0.04,
        realized_vol=0.25,
        open_interest=620_000_000.0,
        funding_rate=0.00018,
        long_liq_24h=15_000_000.0,
        short_liq_24h=3_500_000.0,
        fear_greed_index=22,           # Extreme Fear -> Contrarian Bullish BUY
        fear_greed_label="Extreme Fear",
    )

def main():
    symbol = "BTC/USDT"
    entry_price = 65000.0
    
    print("\n" + "="*80)
    print("      PROFESSIONAL TRADING DECISION ENGINE — SYSTEM SIMULATION")
    print("="*80)
    
    # 1. Clear previous paper portfolio state for clean simulation
    if paper_trader.STATE_FILE.exists():
        try:
            paper_trader.STATE_FILE.unlink()
        except Exception:
            pass
            
    # Generate the snapshot
    snap = make_bullish_snapshot(symbol, entry_price)
    
    print("\n[STEP 1] Mocking Market Snapshot:")
    print(f"  Asset: {snap.symbol} ({snap.asset_type})")
    print(f"  Current Price: ${snap.close:,.2f}")
    print(f"  RSI: {snap.rsi:.1f} | ATR: ${snap.atr:.2f}")
    print(f"  Fear & Greed Index: {snap.fear_greed_index} ({snap.fear_greed_label})")
    
    # 2. Run Orchestrator Pipeline
    print("\n[STEP 2] Running Orchestrator Pipeline...")
    with patch("trading_engine.orchestrator.build_snapshot", return_value=snap):
        signal = orchestrator.run(symbol)
        
    print("\n[STEP 3] Output Audit Trail Details:")
    print(json.dumps(signal.verdict, indent=4))
    print("\nReasoning Explanation generated:")
    print(f"  \"{signal.reasoning}\"")
    
    # 3. Simulate Paper Execution
    if signal.final_action != "NO_TRADE":
        print("\n[STEP 4] Executing Paper Trade Entry:")
        pos = paper_trader.open_trade(
            symbol=signal.symbol,
            direction="long" if signal.final_action == "BUY" else "short",
            entry=entry_price,
            size_usd=signal.position_size_usd,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit
        )
        
        # Display current paper portfolio status after entry
        status = paper_trader.get_status()
        print("\nPortfolio Status (Open Position):")
        print(f"  Cash Remaining: ${status['cash']:,.2f}")
        print(f"  Portfolio Heat (Open Risk): {status['portfolio_heat']}%")
        print(f"  Open Positions: {status['open_positions']}")
        
        # 4. Simulate Price Update to hit Take Profit
        tp_price = signal.take_profit
        print(f"\n[STEP 5] Simulating price change hitting Take Profit target at ${tp_price:,.2f}...")
        paper_trader.update_prices({symbol: tp_price})
        
        # Display final paper portfolio status
        status = paper_trader.get_status()
        print("\nPortfolio Status (Post-Exit):")
        print(f"  Total P&L: ${status['total_pnl']:+,.2f} ({status['total_pnl_pct']}% growth)")
        print(f"  Win Rate: {status['win_rate']}% ({status['win_count']} wins, {status['loss_count']} losses)")
        print(f"  Cash: ${status['cash']:,.2f}")
    else:
        print("\n[STEP 4] No trade executed as risk parameters rejected the trade.")
        
    print("\n" + "="*80)
    print("                           SIMULATION COMPLETED")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()

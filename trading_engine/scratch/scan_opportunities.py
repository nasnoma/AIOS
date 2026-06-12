import sys
import os
from loguru import logger

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from trading_engine import orchestrator
from trading_engine.config import settings
from trading_engine.data.market_data import build_snapshot

def run_scan():
    print("=" * 80)
    print("         MULTI-AGENT TRADING OPPORTUNITY SCAN (LIVE MARKET FEED)")
    print("=" * 80)
    
    crypto_assets = settings.crypto_assets
    stock_assets = settings.stock_assets
    
    all_assets = crypto_assets + stock_assets
    
    opportunities = []
    
    for symbol in all_assets:
        print(f"\n🔍 Analyzing {symbol}...")
        try:
            # Rebuild snapshot to get current live statistics
            snap = build_snapshot(symbol)
            
            # Run the multi-agent decision pipeline
            signal = orchestrator.run(
                symbol=symbol,
                portfolio_heat=0.0,
                open_positions=0,
                win_rate=0.50
            )
            
            # Extract details
            opportunities.append({
                "symbol": symbol,
                "asset_type": snap.asset_type,
                "price": snap.close,
                "rsi": snap.rsi,
                "ema20": snap.ema20,
                "ema50": snap.ema50,
                "atr": snap.atr,
                "final_action": signal.final_action,
                "verdict_decision": signal.verdict["decision"],
                "confidence": signal.verdict["confidence"],
                "agreement": signal.verdict["agreement"],
                "reasoning": signal.reasoning
            })
            
        except Exception as e:
            print(f"Error scanning {symbol}: {e}")
            
    print("\n" + "=" * 80)
    print("                      MARKET SCAN OPPORTUNITIES REPORT")
    print("=" * 80)
    
    buys = [o for o in opportunities if o["final_action"] == "BUY"]
    holds = [o for o in opportunities if o["final_action"] not in ("BUY", "SELL")]
    
    if buys:
        print("\n🔥 ACTIVE BUY OPPORTUNITIES (Approved by Judge & Risk Agent):")
        for b in buys:
            print(f"\n✅ {b['symbol']} ({b['asset_type'].upper()}) — Price: ${b['price']:,.2f} | RSI: {b['rsi']:.1f}")
            print(f"   Consensus: {b['verdict_decision']} (Confidence: {b['confidence']:.0f}% | Agreement: {b['agreement']}/8)")
            print(f"   Reasoning: {b['reasoning']}")
    else:
        print("\nℹ️ No active BUY signals triggered (Judge/Risk ensembles did not approve any entries).")
        
    print("\n📊 ASSET SETUP DETAILS (Ranked by Bullish Potential - RSI Ascending):")
    # Rank assets by RSI (lower RSI = more oversold / potential buy low)
    ranked = sorted(opportunities, key=lambda x: x["rsi"])
    for r in ranked:
        trend_status = "Bullish Stack" if r["price"] > r["ema20"] > r["ema50"] else "Neutral/Bearish"
        action_emoji = "🔵 HOLD"
        if r["final_action"] == "BUY":
            action_emoji = "🟢 BUY"
        elif r["final_action"] == "SELL":
            action_emoji = "🔴 SELL"
            
        print(f"\n• {r['symbol']} ({r['asset_type'].upper()})")
        print(f"  Current Price: ${r['price']:,.4f} | RSI: {r['rsi']:.1f} ({'Oversold / Buy Low Zone' if r['rsi'] < 40 else 'Neutral Zone'})")
        print(f"  Trend Regime:  {trend_status} (EMA20=${r['ema20']:.2f}, EMA50=${r['ema50']:.2f})")
        print(f"  Signal Status: {action_emoji} | Ensemble Conf: {r['confidence']:.0f}% | Agree: {r['agreement']}/8")
        print(f"  Reasoning:     {r['reasoning']}")
        
    print("\n" + "=" * 80)

if __name__ == "__main__":
    # Force settings to live mode for accurate scanning
    settings.trading_mode = "live"
    run_scan()

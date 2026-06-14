"""
trading_engine/run_bounty_hunter.py

Command-line runner for the Bounty Hunter scan.
Usage:
  python trading_engine/run_bounty_hunter.py --mode=oversold --crypto-limit=5 --stock-limit=5
"""
from __future__ import annotations
import argparse
import sys
import os

# Add parent dir to path if run directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.bounty_hunter import run_bounty_hunt
from trading_engine.config import settings

def main():
    parser = argparse.ArgumentParser(description="⚔️ Bounty Hunter Scan Runner ⚔️")
    parser.add_argument(
        "--mode",
        choices=["oversold", "momentum", "volume", "hot"],
        default="oversold",
        help="Scan criteria strategy (default: oversold)"
    )
    parser.add_argument(
        "--crypto-limit",
        type=int,
        default=5,
        help="Number of crypto candidates to scan (default: 5)"
    )
    parser.add_argument(
        "--stock-limit",
        type=int,
        default=5,
        help="Number of stock candidates to scan (default: 5)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Place live trades for candidates found (forces execution)"
    )
    parser.add_argument(
        "--favourites",
        action="store_true",
        help="Restrict scanning to the watchlist/favourites configured in settings"
    )
    
    args = parser.parse_args()
    
    # Force live settings trading mode to allow true live queries
    settings.trading_mode = "live"
    
    # Resolve watchlist if favourites is selected
    watchlist = None
    if args.favourites:
        watchlist = settings.watchlist_assets
        
    print("\n" + "=" * 80)
    print(f"⚔️  BOUNTY HUNTER ACTIVE SCANNING INITIATED (Mode: {args.mode.upper()})  ⚔️")
    if watchlist:
        print(f"Restricted Watchlist: {watchlist}")
    print("=" * 80)
    print(f"Targeting: top {args.crypto_limit} crypto decliners on Bybit, and top {args.stock_limit} stock decliners on Massive.")
    print("Running multi-agent deep evaluation on candidates...")
    print("=" * 80)
    
    results = run_bounty_hunt(
        mode=args.mode,
        crypto_limit=args.crypto_limit,
        stock_limit=args.stock_limit,
        watchlist=watchlist
    )
    
    print("\n" + "=" * 80)
    print("                    ⚔️  BOUNTY HUNT ANALYSIS REPORT  ⚔️")
    print("=" * 80)
    
    buys = [r for r in results if r["final_action"] == "BUY"]
    sells = [r for r in results if r["final_action"] == "SELL"]
    holds = [r for r in results if r["final_action"] not in ("BUY", "SELL")]
    
    if buys:
        print("\n🟢 ACTIVE BUY OPPORTUNITIES DETECTED:")
        for b in buys:
            print(f"\n✅ BUY {b['symbol']} — Price: ${b['entry_price']:,.4f}")
            print(f"   Target: TP=${b['take_profit']:.4f} | SL=${b['stop_loss']:.4f}")
            print(f"   Consensus: Conf={b['confidence']:.0f}% | Agreement={b['agreement']}/8")
            print(f"   Reasoning: {b['reasoning']}")
    else:
        print("\nℹ️ No active BUY signals triggered (Risk/Judge ensembles rejected all entries).")
        
    print("\n📊 CANDIDATE SUMMARIES (Neutral / Rejected):")
    for h in holds:
        if "pre-flight checklist" in h["reasoning"].lower():
            status_label = f"PRE-FLIGHT REJECTED ({h['confidence']:.0f}% Pass Prob)"
            print(f"\n• {h['symbol']} — Status: {status_label}")
        else:
            print(f"\n• {h['symbol']} — Status: HOLD (Conf={h['confidence']:.0f}% | Agree={h['agreement']}/8)")
        print(f"  Reasoning: {h['reasoning']}")
        
    print("\n" + "=" * 80 + "\n")
    
    if args.execute:
        print("=" * 80)
        print("                 ⚔️  EXECUTING LIVE BOUNTY OPPORTUNITIES  ⚔️")
        print("=" * 80)

        # Load live trader
        from trading_engine.execution import live_trader

        # Fix #6: Execute BOTH BUY and SELL signals
        active_buys  = [r for r in results if r["final_action"] == "BUY"]
        active_sells = [r for r in results if r["final_action"] == "SELL"]

        if active_buys or active_sells:
            total_signals = len(active_buys) + len(active_sells)
            print(f"Placing {total_signals} approved signal(s): {len(active_buys)} BUY, {len(active_sells)} SELL...")

            for b in active_buys:
                try:
                    live_trader.open_trade(
                        symbol=b["symbol"],
                        direction="long",
                        entry=b["entry_price"],
                        size_usd=b["position_size_usd"] or 20.0,
                        stop_loss=b["stop_loss"] or (b["entry_price"] * 0.95),
                        take_profit=b["take_profit"] or (b["entry_price"] * 1.10),
                    )
                    print(f"  ✅ BUY placed: {b['symbol']} @ ${b['entry_price']:.4f}")
                except Exception as e:
                    print(f"  ❌ BUY failed for {b['symbol']}: {e}")

            for s in active_sells:
                try:
                    live_trader.open_trade(
                        symbol=s["symbol"],
                        direction="short",
                        entry=s["entry_price"],
                        size_usd=s["position_size_usd"] or 20.0,
                        stop_loss=s["stop_loss"] or (s["entry_price"] * 1.05),
                        take_profit=s["take_profit"] or (s["entry_price"] * 0.90),
                    )
                    print(f"  ✅ SELL placed: {s['symbol']} @ ${s['entry_price']:.4f}")
                except Exception as e:
                    print(f"  ❌ SELL failed for {s['symbol']}: {e}")

        else:
            print("No approved BUY or SELL setups detected. No trades placed.")

        print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()

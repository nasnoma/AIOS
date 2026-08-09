#!/usr/bin/env python3
"""
Fast AutoResearch Optimizer — data-efficient version.
Pre-fetches OHLCV once per asset, runs all param combos in-memory.
~100x faster than the original optimizer (no repeated API calls).
"""
import sys, json, time, itertools, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Suppress ALL loguru output — grid engine logs thousands of order lines per combo
# which floods disk and makes the optimizer 10-20x slower than necessary.
import loguru
loguru.logger.disable("trading_engine")
loguru.logger.remove()   # remove default stderr handler too
import logging
logging.disable(logging.CRITICAL)  # silence any stdlib logging as well

import ccxt
import pandas as pd
import pandas_ta as ta

# ── must import after sys.path fix ─────────────────────────────────────────
from trading_engine.spot import grid_engine as ge

BEST_PARAMS_FILE   = Path(__file__).parent.parent / "spot" / "best_params.json"
RESULTS_FILE       = Path(__file__).parent.parent / "spot" / "optimizer_results.json"

# ── 3x Yield Tripling Search Space (Ultra-Dense Micro-Grid Scalping: 0.15% - 0.30%) ──
SPACINGS     = [0.0015, 0.0018, 0.0020, 0.0022, 0.0025, 0.0030]
BUY_LEVELS   = [6, 8, 10, 12, 14]
SELL_LEVELS  = [6, 8, 10, 12]
CAPITAL_PCTS = [0.90, 0.95, 0.995]

# Top 15 Highest Volatility Screened Halal Tokens (99.5% Active Deployed Capital = $9,950)
ASSETS = [
    ("UNI/USDT",    1200.0), # #1 Volatility (1.23% ATR, 1588 est cycles)
    ("TIA/USDT",    1000.0), # #2 Volatility (1.06% ATR, 1356 est cycles)
    ("INJ/USDT",     950.0), # #3 Volatility (1.04% ATR, 1344 est cycles)
    ("ARB/USDT",     900.0), # #4 Volatility (1.03% ATR, 1320 est cycles)
    ("PENDLE/USDT",  900.0), # #5 Volatility (1.02% ATR, 1303 est cycles)
    ("ADA/USDT",     850.0), # #6 Volatility (1.01% ATR, 1300 est cycles)
    ("JUP/USDT",     800.0), # #7 Volatility (1.00% ATR, 1278 est cycles)
    ("AAVE/USDT",    750.0), # #8 Volatility (0.98% ATR, 1253 est cycles)
    ("NEAR/USDT",    700.0), # #9 Volatility (0.96% ATR, 1240 est cycles)
    ("STX/USDT",     550.0), # #10 Volatility (0.90% ATR, 1157 est cycles)
    ("OP/USDT",      450.0), # #11 Volatility (0.89% ATR, 1147 est cycles)
    ("APT/USDT",     350.0), # #12 Volatility (0.85% ATR, 1094 est cycles)
    ("FET/USDT",     250.0), # #13 Volatility (0.84% ATR, 1086 est cycles)
    ("AVAX/USDT",    150.0), # #14 Volatility (0.80% ATR, 1024 est cycles)
    ("DOT/USDT",     150.0), # #15 Volatility (0.79% ATR, 1019 est cycles)
]

DAYS     = 30
FEE_RATE = 0.001


class DummyPortfolio:
    def __init__(self):
        self.positions = {}
    def get_position(self, s): return self.positions.get(s, 0.0)
    def record_buy(self, s, q, p, f): b=s.split('/')[0]; self.positions[b]=self.positions.get(b,0)+q
    def record_sell(self, s, q, p, f): b=s.split('/')[0]; self.positions[b]=max(0,self.positions.get(b,0)-q)


def score(r):
    if not r: return -999.0
    return r.get("net_pnl_usd", 0) - 0.2 * r.get("max_drawdown_usd", 0) + 0.04 * r.get("total_cycles", 0)


def run_combo(df_with_atr, atr_col, symbol, alloc, spacing, buy_lvl, sell_lvl, cap_pct):
    """Run one backtest combo against pre-loaded dataframe with High-Frequency Auto-Compounding Sizing."""
    engine = ge.GridEngine(symbol=symbol, allocated_usd=alloc, paper_mode=True, fee_rate=FEE_RATE)
    engine.current_regime = "RANGE"
    engine.params = ge.RegimeParams(
        grid_spacing=spacing, buy_levels=buy_lvl, sell_levels=sell_lvl,
        capital_pct=cap_pct, base_hold_pct=0.20
    )

    portfolio   = DummyPortfolio()
    df          = df_with_atr.copy()
    start_price = df.iloc[0]["open"]

    init_atr = float(df[atr_col].iloc[14]) if (atr_col and pd.notna(df[atr_col].iloc[14])) else 0.0
    engine.build_grid(start_price, portfolio, atr=init_atr)
    engine.place_grid_orders(portfolio, exchange=None)

    last_rebuild = df.iloc[0]["timestamp"]
    peak_cap     = alloc
    max_dd       = 0.0

    for idx, row in df.iterrows():
        # High-Frequency Compounding Rebalance (Every 12h): reinvest accumulated profits into grid order sizes
        if (row["timestamp"] - last_rebuild).total_seconds() >= 43200:
            row_atr = float(df.loc[idx, atr_col]) if (atr_col and pd.notna(df.loc[idx, atr_col])) else 0.0
            cum_pnl = sum(c["net_pnl"] for c in engine.completed_cycles)
            # Dynamic compounding capital expansion
            engine.allocated_usd = max(alloc * 0.5, alloc + cum_pnl)
            engine.cancel_all()
            engine.build_grid(row["close"], portfolio, atr=row_atr)
            engine.place_grid_orders(portfolio, exchange=None)
            last_rebuild = row["timestamp"]

        engine.tick(row["low"],  portfolio)
        engine.tick(row["high"], portfolio)

        pnl = sum(c["net_pnl"] for c in engine.completed_cycles)
        cur = alloc + pnl
        if cur > peak_cap: peak_cap = cur
        dd = peak_cap - cur
        if dd > max_dd: max_dd = dd

    net_pnl = sum(c["net_pnl"] for c in engine.completed_cycles)
    cycles  = len(engine.completed_cycles)

    return {
        "symbol": symbol, "days": DAYS, "regime": "RANGE",
        "total_cycles": cycles,
        "net_pnl_usd":  float(net_pnl),
        "net_pnl_pct":  float(net_pnl / alloc * 100),
        "max_drawdown_usd": float(max_dd),
    }


def fetch_data(exchange, symbol, retries=5):
    """Fetch 30 days of 1h OHLCV + compute ATR(14) once. Retries on network errors."""
    since = exchange.milliseconds() - DAYS * 86400 * 1000
    for attempt in range(1, retries + 1):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
            break
        except Exception as e:
            if attempt == retries:
                print(f"FAILED after {retries} attempts: {e}")
                return None, None
            wait = 2 ** attempt
            print(f"  Network error (attempt {attempt}/{retries}), retrying in {wait}s... ", end="", flush=True)
            time.sleep(wait)
    if not ohlcv:
        return None, None
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.ta.atr(length=14, append=True)
    atr_col = "ATRr_14" if "ATRr_14" in df.columns else ("ATR_14" if "ATR_14" in df.columns else None)
    return df, atr_col


def main():
    exchange = ccxt.bybit({"enableRateLimit": True})
    combos   = list(itertools.product(SPACINGS, BUY_LEVELS, SELL_LEVELS, CAPITAL_PCTS))
    n_combos = len(combos)
    total_runs = n_combos * len(ASSETS)

    print(f"\n{'='*68}")
    print(f"  AutoResearch Optimizer (Fast — data pre-fetched)")
    print(f"  {len(ASSETS)} assets × {n_combos} combos = {total_runs} simulations")
    print(f"  Search: {len(SPACINGS)} spacings × {len(BUY_LEVELS)} buy_lvl × {len(SELL_LEVELS)} sell_lvl × {len(CAPITAL_PCTS)} cap%")
    print(f"{'='*68}\n")

    # Load any previously completed assets so a crash/resume doesn't re-run them
    all_best_params = json.loads(BEST_PARAMS_FILE.read_text()) if BEST_PARAMS_FILE.exists() else {}
    all_results     = json.loads(RESULTS_FILE.read_text())     if RESULTS_FILE.exists()     else {}
    if all_results:
        print(f"  Resuming — {len(all_results)} asset(s) already done: {list(all_results.keys())}")
    t0 = time.time()

    for asset_idx, (symbol, alloc) in enumerate(ASSETS, 1):
        if symbol in all_results:
            print(f"[{asset_idx}/{len(ASSETS)}] {symbol} already done — skipping.")
            continue
        print(f"[{asset_idx}/{len(ASSETS)}] Fetching {symbol} data... ", end="", flush=True)
        df, atr_col = fetch_data(exchange, symbol)
        if df is None:
            print("SKIP (no data)")
            continue
        print(f"OK ({len(df)} candles). Running {n_combos} combos...", flush=True)

        best_score  = -999.0
        best_params = None
        best_result = None

        for i, (sp, bl, sl, cp) in enumerate(combos, 1):
            r = run_combo(df, atr_col, symbol, alloc, sp, bl, sl, cp)
            s = score(r)
            if s > best_score:
                best_score  = s
                best_params = {"grid_spacing": sp, "buy_levels": bl,
                               "sell_levels": sl, "capital_pct": cp, "regime": "RANGE"}
                best_result = r
                pnl    = r["net_pnl_usd"]
                cycles = r["total_cycles"]
                elapsed = time.time() - t0
                eta = (elapsed / (asset_idx - 1 + i / n_combos)) * (len(ASSETS) - asset_idx + 1 - i / n_combos) if (asset_idx > 1 or i > 1) else 0
                print(f"  ✓ #{i:>3}/{n_combos}  spacing={sp:.3f} buy={bl:>2} sell={sl:>2} "
                      f"cap={cp:.0%} → {cycles:>3}c ${pnl:>+7.2f}  ETA {eta/60:.1f}min")

        all_best_params[symbol] = best_params
        all_results[symbol]     = {"best_params": best_params, "best_result": best_result, "best_score": best_score}
        # Save after every asset — crash-safe
        BEST_PARAMS_FILE.write_text(json.dumps(all_best_params, indent=2))
        RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
        print(f"  💾 Saved partial results ({len(all_results)}/{len(ASSETS)} assets done)")

    # ── Summary ─────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\n{'='*68}")
    print(f"  COMPLETE in {elapsed:.0f}s  |  30-Day Optimized Results")
    print(f"{'='*68}")
    print(f"  {'Asset':<12} {'Spacing':>8} {'Buy':>4} {'Sell':>5} {'Cap%':>5} {'Cycles':>7} {'Net PnL':>10}")
    print(f"  {'-'*64}")

    total_pnl    = 0.0
    total_cycles = 0
    for sym, res in all_results.items():
        bp = res["best_params"]
        br = res["best_result"] or {}
        pnl    = br.get("net_pnl_usd", 0.0)
        cycles = br.get("total_cycles", 0)
        total_pnl    += pnl
        total_cycles += cycles
        flag = "🟢" if pnl >= 0 else "🔴"
        print(f"  {flag} {sym:<10} {bp['grid_spacing']:>7.3f}  {bp['buy_levels']:>3}  "
              f"{bp['sell_levels']:>4}  {bp['capital_pct']:>4.0%}  {cycles:>6}   ${pnl:>+8.2f}")

    total_capital = sum(a for _, a in ASSETS)
    monthly_pct   = (total_pnl / total_capital) * 100
    baseline_pct  = 2.92  # our last best
    print(f"  {'='*64}")
    print(f"  TOTAL                                       {total_cycles:>6}   ${total_pnl:>+8.2f}")
    print(f"  Monthly Return:    {monthly_pct:+.2f}%")
    print(f"  Previous Baseline: +{baseline_pct:.2f}%")
    print(f"  Improvement:       +{monthly_pct - baseline_pct:.2f}pp")
    print(f"  Extra $/month:     ${total_pnl - (baseline_pct/100 * total_capital):+.2f}")
    print(f"{'='*68}\n")

    # Save
    BEST_PARAMS_FILE.write_text(json.dumps(all_best_params, indent=2))
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"✅ Optimized params saved → {BEST_PARAMS_FILE}")
    print(f"   GridEngine will auto-load these on next Railway restart.\n")


if __name__ == "__main__":
    main()

"""
scratch/ngx_native_backtest.py

NGX-Native Strategy Backtester
-------------------------------
Purpose-built for Nigerian Stock Exchange daily equities.
Uses strategies that actually match NGX market behaviour:

Strategy A — EMA Crossover + RSI Filter (trend-following)
  BUY:  EMA10 crosses above EMA30, RSI 35-65, volume > 0.8× 20d avg
  SELL: EMA10 crosses below EMA30 OR RSI > 75

Strategy B — Momentum Breakout
  BUY:  Close > highest close in last 20 days (new 20d high), volume spike (>1.5×)
  SELL: Close < EMA30 OR trailing stop 8%

Strategy C — RSI Mean-Reversion
  BUY:  RSI < 35 (oversold), price > EMA50
  SELL: RSI > 65 OR trailing stop 6%

Runs all 3 and reports best per stock, plus cross-stock ranking.
"""
from __future__ import annotations
import sys, json, time
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.data.market_data import load_historical_data, compute_indicators

NGX_TICKERS = [
    "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
    "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
    "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
    "NASCON", "FLOURMILL", "NB", "MEYER",
]

BACKTEST_DAYS   = 400
INITIAL_CAPITAL = 10_000
STOP_LOSS_PCT   = 0.08    # 8% hard stop
RISK_PER_TRADE  = 0.02    # 2% of capital risked per trade

OUT_DIR = PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────
# SIMPLE EVENT-DRIVEN BACKTESTER
# ─────────────────────────────────────────────────────────────────
def backtest_signals(df: pd.DataFrame, buy_signal: pd.Series, sell_signal: pd.Series,
                     initial_capital: float = 10_000,
                     stop_loss_pct: float = 0.08,
                     take_profit_pct: float = 0.20,
                     risk_pct: float = 0.02) -> dict:
    capital = initial_capital
    trades = []
    in_trade = False
    entry_price = 0.0
    trailing_stop = 0.0

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    buys   = buy_signal.values
    sells  = sell_signal.values

    for i in range(1, len(df)):
        price = closes[i]
        if in_trade:
            # Update trailing stop: move up with price
            trailing_stop = max(trailing_stop, price * (1 - stop_loss_pct))
            hit_stop = lows[i] <= trailing_stop
            hit_tp   = highs[i] >= entry_price * (1 + take_profit_pct)
            hit_sell = sells[i]
            if hit_stop or hit_tp or hit_sell:
                exit_price = trailing_stop if hit_stop else (entry_price*(1+take_profit_pct) if hit_tp else price)
                pnl_pct    = (exit_price - entry_price) / entry_price
                size_usd   = capital * risk_pct / stop_loss_pct
                pnl        = size_usd * pnl_pct
                capital   += pnl
                trades.append({
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "pnl_pct": pnl_pct,
                    "result": "win" if pnl > 0 else "loss",
                    "exit_reason": "stop" if hit_stop else ("tp" if hit_tp else "signal"),
                })
                in_trade = False
        else:
            if buys[i]:
                in_trade     = True
                entry_price  = price
                trailing_stop = price * (1 - stop_loss_pct)

    if not trades:
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                "total_return_pct": 0, "max_drawdown_pct": 0,
                "final_capital": initial_capital}

    df_t = pd.DataFrame(trades)
    wins = df_t[df_t["result"] == "win"]
    losses = df_t[df_t["result"] == "loss"]
    total_pnl = df_t["pnl"].sum()
    win_rate  = len(wins) / len(df_t) * 100
    gross_w = wins["pnl"].sum() if len(wins) else 0
    gross_l = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf = gross_w / gross_l if gross_l > 0 else float("inf")

    # Max drawdown
    curve = [initial_capital]
    for t in trades:
        curve.append(curve[-1] + t["pnl"])
    peak, max_dd = initial_capital, 0.0
    for c in curve:
        if c > peak: peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    return {
        "total_trades": len(df_t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(min(pf, 99.9), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((capital - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "final_capital": round(capital, 2),
        "exit_breakdown": df_t["exit_reason"].value_counts().to_dict(),
    }


# ─────────────────────────────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────────────────────────────
def strategy_ema_cross(df: pd.DataFrame) -> tuple:
    """EMA10/30 crossover with RSI filter."""
    import pandas_ta as ta
    close = df["close"]
    ema10 = ta.ema(close, length=10)
    ema30 = ta.ema(close, length=30)
    rsi   = ta.rsi(close, length=14)
    vol   = df["volume"]
    avg_vol = vol.rolling(20).mean()

    cross_up   = (ema10 > ema30) & (ema10.shift(1) <= ema30.shift(1))
    rsi_ok     = (rsi >= 35) & (rsi <= 68)
    vol_ok     = vol >= avg_vol * 0.8
    buy        = cross_up & rsi_ok & vol_ok

    cross_down = (ema10 < ema30) & (ema10.shift(1) >= ema30.shift(1))
    rsi_exit   = rsi > 75
    sell       = cross_down | rsi_exit

    return buy.fillna(False), sell.fillna(False)


def strategy_momentum_breakout(df: pd.DataFrame) -> tuple:
    """20-day high breakout + volume spike."""
    close = df["close"]
    vol   = df["volume"]
    avg_vol = vol.rolling(20).mean()

    high_20 = close.rolling(20).max().shift(1)
    vol_spike = vol > avg_vol * 1.5
    breakout  = close > high_20

    buy  = breakout & vol_spike

    import pandas_ta as ta
    ema30 = ta.ema(close, length=30)
    below_ema = close < ema30
    sell = below_ema

    return buy.fillna(False), sell.fillna(False)


def strategy_rsi_reversion(df: pd.DataFrame) -> tuple:
    """RSI oversold bounce with trend confirmation."""
    import pandas_ta as ta
    close = df["close"]
    rsi   = ta.rsi(close, length=14)
    ema50 = ta.ema(close, length=50)

    oversold     = rsi < 35
    above_ema50  = close > ema50
    rsi_rising   = rsi > rsi.shift(1)
    buy          = oversold & above_ema50 & rsi_rising

    rsi_ob = rsi > 65
    sell   = rsi_ob

    return buy.fillna(False), sell.fillna(False)


STRATEGIES = {
    "EMA_Cross":     strategy_ema_cross,
    "MomBreakout":   strategy_momentum_breakout,
    "RSI_Reversion": strategy_rsi_reversion,
}


# ─────────────────────────────────────────────────────────────────
# PER-TICKER RUNNER
# ─────────────────────────────────────────────────────────────────
def run_ticker(ticker: str) -> dict:
    symbol = f"{ticker}/NGX"
    try:
        df_raw = load_historical_data(symbol, timeframe="1d", limit=BACKTEST_DAYS)
        df = compute_indicators(df_raw)
        # Don't dropna fully — strategies compute their own indicators
        df = df.dropna(subset=["close", "high", "low", "volume"])
    except Exception as e:
        return {"ticker": ticker, "error": str(e), "best_strategy": None, "results": {}}

    strategy_results = {}
    for name, strat_fn in STRATEGIES.items():
        try:
            buy_sig, sell_sig = strat_fn(df)
            res = backtest_signals(df, buy_sig, sell_sig,
                                   initial_capital=INITIAL_CAPITAL,
                                   stop_loss_pct=STOP_LOSS_PCT,
                                   risk_pct=RISK_PER_TRADE)
            strategy_results[name] = res
        except Exception as e:
            strategy_results[name] = {"error": str(e), "total_trades": 0}

    # Pick best strategy by composite score
    def score(r):
        if r.get("total_trades", 0) < 2:
            return -999.0
        pf  = min(r.get("profit_factor", 0), 5.0)
        wr  = r.get("win_rate", 0) / 100
        ret = r.get("total_return_pct", 0)
        dd  = r.get("max_drawdown_pct", 100)
        return 0.35*pf + 0.25*wr + 0.25*(ret/100) - 0.15*(dd/100) + 0.01*np.log1p(r.get("total_trades",1))

    best = max(strategy_results.items(), key=lambda x: score(x[1]))
    return {
        "ticker": ticker,
        "symbol": symbol,
        "results": strategy_results,
        "best_strategy": best[0],
        "best_result": best[1],
        "best_score": score(best[1]),
    }


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    print("═"*70)
    print("  NGX NATIVE STRATEGY BACKTEST  (EMA Cross / Breakout / RSI Reversion)")
    print(f"  {len(NGX_TICKERS)} stocks  ·  {BACKTEST_DAYS}d daily  ·  Real NGX Pulse data")
    print("═"*70)

    all_results = []
    for ticker in NGX_TICKERS:
        print(f"\n  ▸ {ticker:12s} ...", end=" ", flush=True)
        t0 = time.time()
        rec = run_ticker(ticker)
        elapsed = time.time() - t0
        if "error" in rec and not rec.get("best_result"):
            print(f"ERROR: {rec['error']}")
        else:
            br = rec.get("best_result", {})
            print(f"{elapsed:.1f}s | Best: {rec['best_strategy']:13s} | "
                  f"trades={br.get('total_trades',0):3d} | WR={br.get('win_rate',0):5.1f}% | "
                  f"ret={br.get('total_return_pct',0):+7.2f}% | PF={br.get('profit_factor',0):.2f}")
        all_results.append(rec)

    # Sort by best score
    all_results.sort(key=lambda x: x.get("best_score", -999), reverse=True)

    # Print strategy breakdown table
    print("\n" + "═"*70)
    print("  FULL STRATEGY BREAKDOWN")
    print("═"*70)
    header = f"{'Ticker':<12} {'Strategy':<14} {'Trades':>6} {'WR%':>6} {'PF':>5} {'Ret%':>8} {'MaxDD%':>7} {'Score':>7}"
    sep = "─"*len(header)
    print(f"\n{sep}\n{header}\n{sep}")
    for rec in all_results:
        t = rec["ticker"]
        for sname, sres in rec.get("results", {}).items():
            marker = " ◄" if sname == rec.get("best_strategy") else "  "
            if sres.get("total_trades", 0) == 0:
                print(f"{t:<12} {sname:<14} {'—':>6} {'—':>6} {'—':>5} {'—':>8} {'—':>7}  (no trades){marker}")
            else:
                print(f"{t:<12} {sname:<14}"
                      f" {sres.get('total_trades',0):>6}"
                      f" {sres.get('win_rate',0):>6.1f}"
                      f" {sres.get('profit_factor',0):>5.2f}"
                      f" {sres.get('total_return_pct',0):>+8.2f}"
                      f" {sres.get('max_drawdown_pct',0):>7.1f}"
                      f" {0.35*min(sres.get('profit_factor',0),5)+0.25*sres.get('win_rate',0)/100+0.25*sres.get('total_return_pct',0)/100-0.15*sres.get('max_drawdown_pct',0)/100:>7.4f}"
                      f"{marker}")
    print(sep)

    # Top 5 summary
    print("\n  ┌─ TOP 5 PERFORMERS ─────────────────────────────────────────────┐")
    for rec in all_results[:5]:
        br  = rec.get("best_result", {})
        scr = rec.get("best_score", -999)
        if scr < -10:
            print(f"  │  {rec['ticker']:<12}  no trades across all strategies")
            continue
        print(f"  │  {rec['ticker']:<12}  [{rec['best_strategy']}]  "
              f"ret={br.get('total_return_pct',0):+.2f}%  "
              f"WR={br.get('win_rate',0):.1f}%  "
              f"PF={br.get('profit_factor',0):.2f}  "
              f"MaxDD={br.get('max_drawdown_pct',0):.1f}%  "
              f"score={scr:.4f}")
    print("  └────────────────────────────────────────────────────────────────┘")

    # Save report
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"ngx_native_backtest_{ts}.json"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {"days": BACKTEST_DAYS, "initial_capital": INITIAL_CAPITAL,
                   "stop_loss_pct": STOP_LOSS_PCT, "risk_pct": RISK_PER_TRADE},
        "results": all_results,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    elapsed = time.time() - start
    print(f"\n  📄 Saved → {out_path.name}")
    print(f"\n{'═'*70}")
    print(f"  DONE  ·  {elapsed:.1f}s")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()

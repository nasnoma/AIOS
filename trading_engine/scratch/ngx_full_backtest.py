"""
scratch/ngx_full_backtest.py

Full NGX Backtest Suite:
  1. Batch backtest all 19 Nigerian stocks (real daily data from NGX Pulse)
  2. Rank by composite score (profit factor, return, win rate, drawdown)
  3. Run Walk-Forward Optimisation on top-3 performers
  4. Save rich summary report to backtest_results/ngx/
  5. Restores judge params after backtest
"""
from __future__ import annotations
import sys
import json
import time
import shutil
from pathlib import Path
from datetime import datetime, timezone

import numpy as np

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger
from trading_engine.backtest.engine import run_backtest, run_walk_forward_optimization

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
NGX_TICKERS = [
    "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
    "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
    "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
    "NASCON", "FLOURMILL", "NB", "MEYER",
]

BACKTEST_DAYS   = 300       # ~1.2 yrs (real data goes back to 2024)
TIMEFRAME       = "1d"
INITIAL_CAPITAL = 10_000

WFO_TOP_N           = 3
WFO_LOOKBACK_DAYS   = 60
WFO_FORWARD_DAYS    = 14

OUT_DIR = PROJECT_ROOT / "trading_engine" / "backtest_results" / "ngx"
OUT_DIR.mkdir(parents=True, exist_ok=True)

JUDGE_PARAMS_PATH = PROJECT_ROOT / "trading_engine" / "autoresearch" / "params" / "judge_weights.json"

# Relaxed params for daily equity backtesting
# (fewer agents need to agree; confidence bar is lower on slower-moving instruments)
BACKTEST_JUDGE_PARAMS = {
    "_comment": "NGX equity backtest mode — relaxed thresholds for daily candles",
    "weights": {
        "trend":      1.5,
        "momentum":   1.2,
        "volume":     1.0,
        "orderflow":  0.8,   # Low weight: no funding/OI data for NGX
        "volatility": 0.9,
        "structure":  1.3,
        "sentiment":  0.7,
        "macro":      1.0,
    },
    "min_agreement": 3,        # was 5 — relax for daily equity (8 agents → 3 must agree)
    "min_avg_confidence": 48,  # was 52 — lower confidence bar for slow-moving NGX stocks
}

# ─────────────────────────────────────────────
def composite_score(r: dict) -> float:
    if r.get("total_trades", 0) == 0:
        return -999.0
    pf  = min(r.get("profit_factor", 0), 5.0)
    wr  = r.get("win_rate", 0) / 100
    ret = r.get("total_return_pct", 0)
    dd  = r.get("max_drawdown_pct", 100)
    n   = max(r.get("total_trades", 1), 1)
    score = (
        0.35 * pf
        + 0.25 * wr
        + 0.25 * (ret / 100)
        - 0.15 * (dd / 100)
    )
    score += 0.01 * float(np.log1p(n))
    return round(score, 4)


def pretty_banner(text: str, width: int = 62):
    border = "═" * width
    print(f"\n{border}\n  {text}\n{border}")


def print_summary_table(rows: list):
    header = f"{'#':<3} {'Ticker':<12} {'Trades':>6} {'WinRate':>8} {'PF':>6} {'Return%':>9} {'MaxDD%':>7} {'Score':>7}"
    sep = "─" * len(header)
    print(f"\n{sep}\n{header}\n{sep}")
    for i, r in enumerate(rows, 1):
        t   = r["ticker"]
        res = r["result"]
        if "error" in res:
            print(f"{i:<3} {t:<12} {'ERROR':>6}  {res['error'][:40]}")
            continue
        print(
            f"{i:<3} {t:<12}"
            f" {res.get('total_trades',0):>6}"
            f" {res.get('win_rate',0):>7.1f}%"
            f" {res.get('profit_factor',0):>6.2f}"
            f" {res.get('total_return_pct',0):>+9.2f}%"
            f" {res.get('max_drawdown_pct',0):>6.1f}%"
            f" {r['score']:>7.4f}"
        )
    print(sep)


# ─────────────────────────────────────────────
# JUDGE PARAM PATCHING
# ─────────────────────────────────────────────
def patch_judge_params() -> dict:
    """Temporarily swap in lenient backtest thresholds. Returns original params."""
    original = {}
    if JUDGE_PARAMS_PATH.exists():
        original = json.loads(JUDGE_PARAMS_PATH.read_text())
        backup = JUDGE_PARAMS_PATH.with_suffix(".json.bak")
        shutil.copy2(JUDGE_PARAMS_PATH, backup)
        print(f"  ✓ Backed up judge params → {backup.name}")
    JUDGE_PARAMS_PATH.write_text(json.dumps(BACKTEST_JUDGE_PARAMS, indent=2))
    print(f"  ✓ Patched judge: min_agreement={BACKTEST_JUDGE_PARAMS['min_agreement']}, "
          f"min_confidence={BACKTEST_JUDGE_PARAMS['min_avg_confidence']}")
    return original


def restore_judge_params(original: dict):
    """Restore original judge params."""
    if original:
        JUDGE_PARAMS_PATH.write_text(json.dumps(original, indent=2))
        backup = JUDGE_PARAMS_PATH.with_suffix(".json.bak")
        if backup.exists():
            backup.unlink()
        print(f"  ✓ Restored original judge params")


# ─────────────────────────────────────────────
# PHASE 1
# ─────────────────────────────────────────────
def run_batch_backtest() -> list:
    pretty_banner(f"PHASE 1 · NGX BATCH BACKTEST  ({len(NGX_TICKERS)} stocks, {BACKTEST_DAYS}d daily)")
    results = []
    for ticker in NGX_TICKERS:
        symbol = f"{ticker}/NGX"
        print(f"\n  ▸ {symbol} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            res = run_backtest(
                symbol=symbol,
                timeframe=TIMEFRAME,
                days=BACKTEST_DAYS,
                initial_capital=INITIAL_CAPITAL,
                output_dir=str(OUT_DIR),
                use_multi_agent=True,
            )
        except Exception as e:
            res = {"error": str(e)}
            print(f"FAILED ({e})")
        else:
            elapsed = time.time() - t0
            trades = res.get("total_trades", 0)
            wr = res.get("win_rate", 0)
            ret = res.get("total_return_pct", 0)
            print(f"done {elapsed:.1f}s | {trades} trades | WR {wr:.1f}% | return {ret:+.2f}%")

        score = composite_score(res)
        results.append({"ticker": ticker, "symbol": symbol, "result": res, "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─────────────────────────────────────────────
# PHASE 2
# ─────────────────────────────────────────────
def run_batch_wfo(top_tickers: list) -> list:
    pretty_banner(f"PHASE 2 · WALK-FORWARD OPTIMISATION  (top {len(top_tickers)})")
    wfo_results = []
    for entry in top_tickers:
        ticker = entry["ticker"]
        symbol = f"{ticker}/NGX"
        print(f"\n  ▸ WFO {symbol} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            wfo = run_walk_forward_optimization(
                symbol=symbol,
                timeframe=TIMEFRAME,
                days=BACKTEST_DAYS,
                lookback_days=WFO_LOOKBACK_DAYS,
                forward_days=WFO_FORWARD_DAYS,
                initial_capital=INITIAL_CAPITAL,
            )
        except Exception as e:
            wfo = {"error": str(e)}
            print(f"FAILED ({e})")
        else:
            elapsed = time.time() - t0
            ret = wfo.get("total_return_pct", 0)
            base_ret = wfo.get("baseline_return_pct", 0)
            print(f"done {elapsed:.1f}s | WFO {ret:+.2f}% vs baseline {base_ret:+.2f}% | edge {ret-base_ret:+.2f}%")
        wfo_results.append({"ticker": ticker, "symbol": symbol, "wfo": wfo})
    return wfo_results


# ─────────────────────────────────────────────
# PHASE 3
# ─────────────────────────────────────────────
def build_and_save_report(batch: list, wfo_list: list) -> tuple:
    wfo_map = {w["ticker"]: w["wfo"] for w in wfo_list}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "tickers": NGX_TICKERS, "timeframe": TIMEFRAME,
            "days": BACKTEST_DAYS, "initial_capital": INITIAL_CAPITAL,
            "wfo_top_n": WFO_TOP_N,
            "judge_min_agreement": BACKTEST_JUDGE_PARAMS["min_agreement"],
            "judge_min_confidence": BACKTEST_JUDGE_PARAMS["min_avg_confidence"],
        },
        "rankings": [
            {
                "rank": i + 1, "ticker": r["ticker"], "symbol": r["symbol"],
                "score": r["score"],
                "backtest": {k: v for k, v in r["result"].items() if k != "trades"},
                "wfo": wfo_map.get(r["ticker"]),
            }
            for i, r in enumerate(batch)
        ],
    }

    valid = [r for r in batch if "error" not in r["result"] and r["result"].get("total_trades", 0) > 0]
    if valid:
        avg_wr  = float(np.mean([r["result"]["win_rate"] for r in valid]))
        avg_pf  = float(np.mean([min(r["result"]["profit_factor"], 99) for r in valid]))
        avg_ret = float(np.mean([r["result"]["total_return_pct"] for r in valid]))
        avg_dd  = float(np.mean([r["result"]["max_drawdown_pct"] for r in valid]))
        report["aggregate"] = {
            "stocks_tested": len(NGX_TICKERS),
            "stocks_with_trades": len(valid),
            "avg_win_rate_pct": round(avg_wr, 1),
            "avg_profit_factor": round(avg_pf, 2),
            "avg_return_pct": round(avg_ret, 2),
            "avg_max_drawdown_pct": round(avg_dd, 1),
            "best_ticker": batch[0]["ticker"] if batch else "N/A",
            "worst_ticker": batch[-1]["ticker"] if batch else "N/A",
        }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"ngx_backtest_report_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return out_path, report


def print_final_summary(report: dict, out_path: Path):
    pretty_banner("FINAL SUMMARY")
    agg = report.get("aggregate", {})
    print(f"  Stocks tested      : {agg.get('stocks_tested','?')}")
    print(f"  With trades        : {agg.get('stocks_with_trades','?')}")
    print(f"  Avg Win Rate       : {agg.get('avg_win_rate_pct','?')}%")
    print(f"  Avg Profit Factor  : {agg.get('avg_profit_factor','?')}")
    print(f"  Avg Return         : {agg.get('avg_return_pct','?')}%")
    print(f"  Avg Max Drawdown   : {agg.get('avg_max_drawdown_pct','?')}%")
    print(f"  🏆 Best            : {agg.get('best_ticker','?')}")
    print(f"  📉 Worst           : {agg.get('worst_ticker','?')}")

    top3 = report["rankings"][:3]
    if top3:
        print("\n  ┌─ TOP 3 ─────────────────────────────────────────────┐")
        for r in top3:
            bt = r.get("backtest", {})
            wfo = r.get("wfo") or {}
            wfo_ret  = wfo.get("total_return_pct")
            base_ret = wfo.get("baseline_return_pct")
            wfo_str = (f"WFO {wfo_ret:+.2f}% vs base {base_ret:+.2f}%"
                       if wfo_ret is not None else "WFO n/a")
            print(f"  │  #{r['rank']} {r['ticker']:<12}  ret {bt.get('total_return_pct',0):+.2f}%  "
                  f"WR {bt.get('win_rate',0):.1f}%  PF {bt.get('profit_factor',0):.2f}  | {wfo_str}")
        print("  └──────────────────────────────────────────────────────┘")
    print(f"\n  📄 Full report: {out_path}")


# ─────────────────────────────────────────────
def main():
    start = time.time()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    pretty_banner("NGX FULL BACKTEST SUITE  (real data + relaxed thresholds)")
    original_params = patch_judge_params()

    try:
        batch = run_batch_backtest()
        pretty_banner("RANKINGS — ALL 19 NGX STOCKS")
        print_summary_table(batch)

        valid_top = [r for r in batch if "error" not in r["result"] and r["result"].get("total_trades", 0) > 0][:WFO_TOP_N]
        wfo_results = run_batch_wfo(valid_top) if valid_top else []

        out_path, report = build_and_save_report(batch, wfo_results)
        print_final_summary(report, out_path)
    finally:
        restore_judge_params(original_params)

    elapsed = time.time() - start
    pretty_banner(f"ALL DONE  ·  {elapsed:.1f}s total")
    print()


if __name__ == "__main__":
    main()

"""
trading_engine/scratch/ngx_wfo_20stocks.py
Walk-Forward Optimization for the full 20-stock NGX universe.
Train window: 300 days | Test window: 100 days | 3 folds
"""
import sys, json, math
import numpy as np
import pandas as pd
import pandas_ta as ta
from pathlib import Path

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.data.market_data import load_historical_data

NGX_20 = [
    "TRANSEXPR","WEMABANK","VFDGROUP","CHAMS","NGXGROUP",
    "GUINEAINS","GTCO","CONHALLPLC","INTENEGINS","ZICHIS",
    "NEIMETH","MBENEFIT","DEAPCAP","UPDCREIT","NPFMCRFBK",
    "VERITASKAP","AIICO","UNIVINSURE","WAPIC","JAPAULGOLD",
]

TRAIN_DAYS=300; TEST_DAYS=100; STOP_LOSS_PCT=0.08; RISK_PCT=0.02; INITIAL_CAPITAL=10_000

def backtest_signals(df, buy_sig, sell_sig):
    capital=INITIAL_CAPITAL; trades=[]; in_trade=False; entry_price=trailing_stop=0.0
    closes=df["close"].values; highs=df["high"].values; lows=df["low"].values
    buys=buy_sig.values; sells=sell_sig.values
    for i in range(1, len(df)):
        price=closes[i]
        if in_trade:
            trailing_stop=max(trailing_stop, price*(1-STOP_LOSS_PCT))
            hit_stop=lows[i]<=trailing_stop; hit_tp=highs[i]>=entry_price*1.20; hit_sell=sells[i]
            if hit_stop or hit_tp or hit_sell:
                exit_p=trailing_stop if hit_stop else (entry_price*1.20 if hit_tp else price)
                pnl_pct=(exit_p-entry_price)/entry_price; size=capital*RISK_PCT/STOP_LOSS_PCT
                pnl=size*pnl_pct; capital+=pnl; trades.append({"pnl":pnl,"win":pnl>0}); in_trade=False
        else:
            if buys[i]: in_trade=True; entry_price=price; trailing_stop=price*(1-STOP_LOSS_PCT)
    if not trades: return {"total_trades":0,"win_rate":0.0,"profit_factor":0.0,"return_pct":0.0}
    df_t=pd.DataFrame(trades); wins=df_t[df_t["win"]]; losses=df_t[~df_t["win"]]
    pf=wins["pnl"].sum()/abs(losses["pnl"].sum()) if len(losses) and losses["pnl"].sum()!=0 else 99.9
    return {"total_trades":len(df_t),"win_rate":round(len(wins)/len(df_t)*100,1),
            "profit_factor":round(min(pf,99.9),2),"return_pct":round((capital-INITIAL_CAPITAL)/INITIAL_CAPITAL*100,2)}

def strategy_ema_cross(df):
    close,vol=df["close"],df["volume"]; avg_vol=vol.rolling(20).mean()
    ema10=ta.ema(close,length=10); ema30=ta.ema(close,length=30); rsi=ta.rsi(close,length=14)
    buy=((ema10>ema30)&(ema10.shift(1)<=ema30.shift(1))&(rsi>=35)&(rsi<=68)&(vol>=avg_vol*0.8)).fillna(False)
    sell=(((ema10<ema30)&(ema10.shift(1)>=ema30.shift(1)))|(rsi>75)).fillna(False)
    return buy,sell

def strategy_mom_breakout(df):
    close,vol=df["close"],df["volume"]; avg_vol=vol.rolling(20).mean()
    buy=((close>close.rolling(20).max().shift(1))&(vol>avg_vol*1.5)).fillna(False)
    sell=(close<ta.ema(close,length=30)).fillna(False)
    return buy,sell

def composite_score(r):
    n=r["total_trades"]
    if n<2: return -999.0
    return 0.35*min(r["profit_factor"],5)+0.25*r["win_rate"]/100+0.25*r["return_pct"]/100+0.01*math.log1p(n)

def run_wfo(df):
    total=len(df)
    if total < TRAIN_DAYS+TEST_DAYS: return None, f"Only {total} rows (need {TRAIN_DAYS+TEST_DAYS})"
    folds=[]; start=0; fold_idx=0
    while start+TRAIN_DAYS+TEST_DAYS<=total and fold_idx<3:
        tr=df.iloc[start:start+TRAIN_DAYS]; te=df.iloc[start+TRAIN_DAYS:start+TRAIN_DAYS+TEST_DAYS]
        r_ema=backtest_signals(tr,*strategy_ema_cross(tr)); r_mom=backtest_signals(tr,*strategy_mom_breakout(tr))
        if composite_score(r_ema)>=composite_score(r_mom): best_strat,strat_fn="EMA_Cross",strategy_ema_cross
        else: best_strat,strat_fn="MomBreakout",strategy_mom_breakout
        r_oos=backtest_signals(te,*strat_fn(te))
        folds.append({"fold":fold_idx,"strategy":best_strat,"oos_result":r_oos,"oos_score":composite_score(r_oos)})
        start+=TEST_DAYS; fold_idx+=1
    return folds,"OK"

print("="*72)
print(f"{'NGX 20-STOCK WALK-FORWARD VALIDATION':^72}")
print(f"{'Train:300d | Test:100d | 3 folds':^72}")
print("="*72)

summary=[]
for ticker in NGX_20:
    try:
        df=load_historical_data(f"{ticker}/NGX",timeframe="1d",limit=600)
        df=df.dropna(subset=["close","high","low","volume"])
        folds,msg=run_wfo(df)
        if folds is None: print(f"  {ticker:15s}: SKIP — {msg}"); summary.append({"ticker":ticker,"status":"skip","reason":msg}); continue
        oos_trades=sum(f["oos_result"]["total_trades"] for f in folds)
        oos_wrs=[f["oos_result"]["win_rate"] for f in folds if f["oos_result"]["total_trades"]>0]
        oos_pfs=[f["oos_result"]["profit_factor"] for f in folds if f["oos_result"]["total_trades"]>0]
        oos_rets=[f["oos_result"]["return_pct"] for f in folds]
        avg_wr=sum(oos_wrs)/len(oos_wrs) if oos_wrs else 0.0
        avg_pf=sum(oos_pfs)/len(oos_pfs) if oos_pfs else 0.0
        avg_ret=sum(oos_rets)/len(oos_rets)
        passes=avg_wr>=45.0 and avg_pf>=1.2 and oos_trades>=2
        status="PASS" if passes else "DEMOTE"
        flag="✅" if passes else "❌"
        print(f"  {ticker:15s}: {flag} {status} | folds={len(folds)} OOS_trades={oos_trades:3d} avg_WR={avg_wr:5.1f}% avg_PF={avg_pf:5.2f} avg_ret={avg_ret:+6.2f}%")
        summary.append({"ticker":ticker,"status":"pass" if passes else "demote","folds":len(folds),
                        "oos_trades":oos_trades,"avg_wr":round(avg_wr,1),"avg_pf":round(avg_pf,2),
                        "avg_ret":round(avg_ret,2),"fold_details":folds})
    except Exception as e:
        print(f"  {ticker:15s}: ERROR — {e}"); summary.append({"ticker":ticker,"status":"error","reason":str(e)})

print(); print("="*72); print("FINAL VERDICT:")
passes_list=[s for s in summary if s["status"]=="pass"]
demotes=[s for s in summary if s["status"]=="demote"]
errors=[s for s in summary if s["status"] in ("error","skip")]
print(f"  PASS   ({len(passes_list)}): {[s['ticker'] for s in passes_list]}")
print(f"  DEMOTE ({len(demotes)}): {[s['ticker'] for s in demotes]}")
print(f"  SKIP/ERROR ({len(errors)}): {[s['ticker'] for s in errors]}")

out_path=PROJECT_ROOT/"trading_engine"/"backtest_results"/"ngx"/"wfo_20stocks.json"
with open(out_path,"w") as f: json.dump(summary,f,indent=2,default=str)
print(f"\nSaved to {out_path}")

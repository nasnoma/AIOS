"""
trading_engine/backtest/engine.py

Vectorized backtesting using vectorbt.
Runs the full signal logic on historical data and generates performance report.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger
import json

from trading_engine.data.market_data import MarketSnapshot
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.agents import (
    trend_agent, momentum_agent, volume_agent,
    orderflow_agent, volatility_agent, structure_agent
)
from trading_engine import judge
from trading_engine import risk_agent
from trading_engine.config import settings

try:
    import vectorbt as vbt
    HAS_VBT = True
except ImportError:
    HAS_VBT = False
    logger.warning("vectorbt not installed — using simple backtester fallback")


def get_timeframe_timedelta(timeframe: str) -> pd.Timedelta:
    tf_clean = timeframe.lower().strip()
    if tf_clean.endswith("m"):
        return pd.Timedelta(minutes=int(tf_clean[:-1]))
    elif tf_clean.endswith("h"):
        return pd.Timedelta(hours=int(tf_clean[:-1]))
    elif tf_clean.endswith("d"):
        return pd.Timedelta(days=int(tf_clean[:-1]))
    else:
        return pd.Timedelta(days=1)


def prepare_htf_dfs(symbol: str, timeframe: str) -> dict[str, pd.DataFrame]:
    """Pre-loads and computes indicators for all required HTF timeframes."""
    from trading_engine.data.market_data import load_historical_data, compute_indicators, get_higher_timeframe
    tf_clean = timeframe.lower().strip()
    
    # Determine which HTFs are needed
    htf_tfs = []
    if tf_clean in ("5m", "15m"):
        htf_tfs = ["1h", "4h", "1d"]
    elif tf_clean == "4h":
        htf_tfs = ["1h", "1d"]
    elif tf_clean == "1h":
        htf_tfs = ["4h", "1d"]
    else:
        htf_tfs = [get_higher_timeframe(timeframe)]
        
    htf_dfs = {}
    for htf in htf_tfs:
        try:
            limit_map = {
                "1h": 12000,
                "4h": 4000,
                "1d": 1000
            }
            limit = limit_map.get(htf, 1000)
            logger.info(f"Pre-loading HTF {htf} data for {symbol} (limit={limit})")
            df_htf = load_historical_data(symbol, timeframe=htf, limit=limit)
            df_htf = compute_indicators(df_htf)
            df_htf = df_htf.dropna()
            htf_dfs[htf] = df_htf
        except Exception as e:
            logger.warning(f"Could not pre-load HTF {htf} for backtest: {e}")
            
    return htf_dfs


def make_historical_snapshot(
    symbol: str,
    asset_type: str,
    timeframe: str,
    df: pd.DataFrame,
    i: int,
    htf_dfs: dict[str, pd.DataFrame] | None = None
) -> MarketSnapshot:
    """
    Constructs a MarketSnapshot at index `i` of the DataFrame.
    Slices the inner df up to index `i` to prevent future data leakage during analysis.
    """
    df_sliced = df.iloc[:i+1]
    latest = df.iloc[i]
    
    # Calculate Bollinger Width
    bb_upper = latest.get("BBU_20_2.0", latest["close"] * 1.02)
    bb_lower = latest.get("BBL_20_2.0", latest["close"] * 0.98)
    bb_width = (bb_upper - bb_lower) / latest["close"] if latest["close"] > 0 else 0
    
    snap = MarketSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        timeframe=timeframe,
        timestamp=df.index[i],
        df=df_sliced,
        close=float(latest["close"]),
        volume=float(latest["volume"]),
        ema20=float(latest.get("EMA_20", 0) or 0),
        ema50=float(latest.get("EMA_50", 0) or 0),
        ema200=float(latest.get("EMA_200", 0) or 0),
        rsi=float(latest.get("RSI_14", 50) or 50),
        stoch_rsi_k=float(latest.get("STOCHRSIk_14_14_3_3", 50) or 50),
        stoch_rsi_d=float(latest.get("STOCHRSId_14_14_3_3", 50) or 50),
        roc=float(latest.get("ROC_10", 0) or 0),
        obv=float(latest.get("OBV", 0) or 0),
        rel_volume=float(latest.get("REL_VOL", 1) or 1),
        vwap=float(latest.get("VWAP_D", latest["close"]) or latest["close"]),
        atr=float(latest.get("ATR_14", 0) or 0),
        bb_width=float(bb_width),
        realized_vol=float(latest.get("REAL_VOL", 0) or 0),
        open_interest=float(latest.get("open_interest", 0) or 0) if "open_interest" in latest else None,
        funding_rate=float(latest.get("funding_rate", 0) or 0) if "funding_rate" in latest else None,
        long_liq_24h=float(latest.get("long_liq_24h", 0) or 0) if "long_liq_24h" in latest else None,
        short_liq_24h=float(latest.get("short_liq_24h", 0) or 0) if "short_liq_24h" in latest else None,
        fear_greed_index=int(latest.get("fear_greed_index", 50)) if "fear_greed_index" in latest else None,
        fear_greed_label=latest.get("fear_greed_label", "Neutral") if "fear_greed_label" in latest else None,
    )
    
    if htf_dfs:
        base_duration = get_timeframe_timedelta(timeframe)
        current_time = df.index[i] + base_duration
        tf_clean = timeframe.lower().strip()
        
        # 1H HTF
        if "1h" in htf_dfs:
            df_htf = htf_dfs["1h"]
            htf_dur = get_timeframe_timedelta("1h")
            df_sliced = df_htf[df_htf.index + htf_dur <= current_time]
            if not df_sliced.empty:
                snap.htf_1h_snap = make_historical_snapshot(
                    symbol, asset_type, "1h", df_sliced, len(df_sliced) - 1, htf_dfs=None
                )
        # 4H HTF
        if "4h" in htf_dfs:
            df_htf = htf_dfs["4h"]
            htf_dur = get_timeframe_timedelta("4h")
            df_sliced = df_htf[df_htf.index + htf_dur <= current_time]
            if not df_sliced.empty:
                snap.htf_4h_snap = make_historical_snapshot(
                    symbol, asset_type, "4h", df_sliced, len(df_sliced) - 1, htf_dfs=None
                )
        # 1D HTF
        if "1d" in htf_dfs:
            df_htf = htf_dfs["1d"]
            htf_dur = get_timeframe_timedelta("1d")
            df_sliced = df_htf[df_htf.index + htf_dur <= current_time]
            if not df_sliced.empty:
                snap.htf_1d_snap = make_historical_snapshot(
                    symbol, asset_type, "1d", df_sliced, len(df_sliced) - 1, htf_dfs=None
                )
                
        # Also set htf_snap fallback
        if tf_clean in ("5m", "15m"):
            snap.htf_snap = snap.htf_4h_snap
        elif tf_clean in ("1h", "4h"):
            snap.htf_snap = snap.htf_1d_snap
        else:
            for htf_tf, df_htf in htf_dfs.items():
                htf_dur = get_timeframe_timedelta(htf_tf)
                df_sliced = df_htf[df_htf.index + htf_dur <= current_time]
                if not df_sliced.empty:
                    snap.htf_snap = make_historical_snapshot(
                        symbol, asset_type, htf_tf, df_sliced, len(df_sliced) - 1, htf_dfs=None
                    )
                    break
    return snap


def _simple_backtest(df: pd.DataFrame, signals: pd.Series, initial_capital: float = 10000,
                     stop_loss_pct: float = 0.03, take_profit_pct: float = 0.09) -> dict:
    """
    Simple event-driven backtester when vectorbt is unavailable.
    signals: Series with values 1 (BUY), -1 (SELL), 0 (HOLD)
    """
    capital = initial_capital
    trades = []
    in_trade = False
    entry_price = 0
    entry_idx = 0

    closes = df["close"].values
    sig_vals = signals.values

    for i in range(1, len(closes)):
        price = closes[i]

        if not in_trade and sig_vals[i] == 1:
            # Enter long
            in_trade = True
            entry_price = price
            entry_idx = i
        elif in_trade:
            change = (price - entry_price) / entry_price
            if change <= -stop_loss_pct or change >= take_profit_pct:
                # Exit
                pnl_pct = change
                trade_cap = capital * 0.02 / stop_loss_pct  # Risk-based sizing
                pnl = trade_cap * pnl_pct
                capital += pnl
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry": entry_price,
                    "exit": price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "result": "win" if pnl > 0 else "loss",
                })
                in_trade = False

    if not trades:
        return {"error": "No trades generated"}

    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["result"] == "win"]
    losses = df_trades[df_trades["result"] == "loss"]
    total_pnl = df_trades["pnl"].sum()
    win_rate = len(wins) / len(df_trades) * 100
    profit_factor = abs(wins["pnl"].sum()) / abs(losses["pnl"].sum()) if len(losses) > 0 else float("inf")
    returns = (capital - initial_capital) / initial_capital * 100

    # Max drawdown
    capital_curve = [initial_capital]
    for t in trades:
        capital_curve.append(capital_curve[-1] + t["pnl"])
    peak = initial_capital
    max_dd = 0
    for c in capital_curve:
        if c > peak:
            peak = c
        dd = (peak - c) / peak
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": len(df_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(returns, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
    }


def _run_multi_agent_simulation(
    df: pd.DataFrame,
    symbol: str,
    asset_type: str,
    timeframe: str,
    agent_weights: dict[str, float],
    initial_capital: float = 10000,
    start_idx: int = 20,
    end_idx: int = None,
    htf_dfs: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """
    Runs row-by-row simulation of the actual quant agents,
    evaluating via Judge and RiskAgent. Incorporates transaction fees
    and dynamic ATR-based stops.
    """
    capital = initial_capital
    trades = []
    open_position = None
    
    # 0.06% entry + 0.06% exit (Binance VIP0 taker)
    entry_fee_rate = 0.0006
    exit_fee_rate = 0.0006
    # Slippage: adverse fill assumption. Crypto liquid majors ~2-3bps,
    # alts wider. Conservative 5bps each side applied as cost.
    entry_slippage_rate = 0.0005
    exit_slippage_rate = 0.0005

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    
    if end_idx is None:
        end_idx = len(df)
        
    # Start at start_idx to allow rolling indicators setup
    for i in range(start_idx, end_idx):
        price = closes[i]
        
        # Check active position exit first
        if open_position:
            direction = open_position["direction"]
            entry_price = open_position["entry"]
            stop_loss = open_position["stop_loss"]
            take_profit = open_position["take_profit"]
            size_usd = open_position["size_usd"]

            triggered = False
            exit_price = price
            status = "closed"

            high_price = highs[i]
            low_price = lows[i]

            if direction == "long":
                if low_price <= stop_loss:
                    triggered = True; exit_price = stop_loss; status = "stopped"
                elif high_price >= take_profit:
                    triggered = True; exit_price = take_profit; status = "take_profit"
            else: # short
                if high_price >= stop_loss:
                    triggered = True; exit_price = stop_loss; status = "stopped"
                elif low_price <= take_profit:
                    triggered = True; exit_price = take_profit; status = "take_profit"
                else: # short
                    if high_price >= stop_loss:
                        triggered = True; exit_price = stop_loss; status = "stopped"
                    elif low_price <= take_profit:
                        triggered = True; exit_price = take_profit; status = "take_profit"
                    
            if triggered:
                if direction == "long":
                    gross_pnl = (exit_price - entry_price) / entry_price * size_usd
                else:
                    gross_pnl = (entry_price - exit_price) / entry_price * size_usd
                    
                exit_fee = size_usd * exit_fee_rate
                exit_slippage = size_usd * exit_slippage_rate
                net_pnl = gross_pnl - exit_fee - exit_slippage
                capital += net_pnl
                
                trades.append({
                    "entry_idx": open_position["entry_idx"],
                    "exit_idx": i,
                    "symbol": symbol,
                    "direction": direction,
                    "entry": entry_price,
                    "exit": exit_price,
                    "size_usd": size_usd,
                    "gross_pnl": gross_pnl,
                    "fee": open_position["entry_fee"] + exit_fee,
                    "slippage": open_position["entry_slippage"] + exit_slippage,
                    "net_pnl": net_pnl,
                    "result": "win" if net_pnl > 0 else "loss",
                    "status": status,
                    "opened_at": df.index[open_position["entry_idx"]].isoformat(),
                    "closed_at": df.index[i].isoformat(),
                })
                open_position = None
                
        # Look for entry signals
        if not open_position:
            snap = make_historical_snapshot(symbol, asset_type, timeframe, df, i, htf_dfs)
            
            # Run 6 quant agents (clean baseline — mean_reversion disabled
            # after 365d WFO showed it reduces edge on 4h/1h crypto majors)
            quant_signals = [
                trend_agent.analyze(snap),
                momentum_agent.analyze(snap),
                volume_agent.analyze(snap),
                volatility_agent.analyze(snap),
                structure_agent.analyze(snap),
                orderflow_agent.analyze(snap),
            ]
            
            # Derive mock sentiment/macro from quant consensus
            # (no LLM call — mirrors majority direction with moderate confidence)
            _buy_ct  = sum(1 for s in quant_signals if s.signal == Signal.BUY)
            _sell_ct = sum(1 for s in quant_signals if s.signal == Signal.SELL)
            if _buy_ct > _sell_ct:
                _mock_dir, _mock_conf = Signal.BUY, 58.0
            elif _sell_ct > _buy_ct:
                _mock_dir, _mock_conf = Signal.SELL, 58.0
            else:
                _mock_dir, _mock_conf = Signal.HOLD, 50.0
            
            signals = quant_signals + [
                AgentSignal(agent="sentiment", signal=_mock_dir, confidence=_mock_conf, reason="backtest-proxy"),
                AgentSignal(agent="macro",     signal=_mock_dir, confidence=_mock_conf, reason="backtest-proxy"),
            ]
            
            # Evaluate verdict
            verdict = judge.evaluate(signals, agent_weights, symbol=symbol)
            
            if verdict.approved:
                # Apply current backtester capital as simulated account size for RiskAgent
                original_acc_size = settings.account_size
                settings.account_size = capital
                try:
                    decision = risk_agent.evaluate(verdict, snap, current_portfolio_heat=0.0, open_positions=0)
                finally:
                    settings.account_size = original_acc_size
                    
                if decision.approved:
                    entry_fee = decision.position_size_usd * entry_fee_rate
                    entry_slippage = decision.position_size_usd * entry_slippage_rate
                    capital -= entry_fee + entry_slippage
                    open_position = {
                        "entry_idx": i,
                        "direction": "long" if verdict.decision == Signal.BUY else "short",
                        "entry": price,
                        "size_usd": decision.position_size_usd,
                        "stop_loss": decision.stop_loss,
                        "take_profit": decision.take_profit,
                        "entry_fee": entry_fee,
                        "entry_slippage": entry_slippage,
                    }
                    
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "initial_capital": initial_capital,
            "final_capital": capital,
            "trades": []
        }
        
    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["result"] == "win"]
    losses = df_trades[df_trades["result"] == "loss"]
    total_pnl = df_trades["net_pnl"].sum()
    win_rate = len(wins) / len(df_trades) * 100
    
    gross_profits = wins["net_pnl"].sum()
    gross_losses = abs(losses["net_pnl"].sum())
    profit_factor = gross_profits / gross_losses if gross_losses > 0 else float("inf")
    
    capital_curve = [initial_capital]
    for t in trades:
        capital_curve.append(capital_curve[-1] + t["net_pnl"])
    peak = initial_capital
    max_dd = 0
    for c in capital_curve:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            
    return {
        "total_trades": len(df_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((capital - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
        "trades": trades
    }


# ══════════════════════════════════════════════════════════════════
# NGX NATIVE STRATEGY ENGINE
# Purpose-built for Nigerian Stock Exchange daily equity backtesting.
# Bypasses the crypto-tuned multi-agent system entirely.
# ══════════════════════════════════════════════════════════════════

_NGX_STRATEGY_MAP_PATH = Path(__file__).parent.parent / "data" / "ngx" / "strategy_map.json"


def _ngx_load_strategy_map() -> dict:
    """Load persisted per-ticker best-strategy assignments."""
    if _NGX_STRATEGY_MAP_PATH.exists():
        try:
            return json.loads(_NGX_STRATEGY_MAP_PATH.read_text())
        except Exception:
            pass
    return {}


def _ngx_save_strategy_map(strategy_map: dict):
    """Persist per-ticker best-strategy assignments."""
    _NGX_STRATEGY_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _NGX_STRATEGY_MAP_PATH.write_text(json.dumps(strategy_map, indent=2))
    except Exception as e:
        logger.warning(f"Could not save NGX strategy map: {e}")


def _ngx_signals_ema_cross(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """EMA10/30 crossover with RSI and volume filter."""
    try:
        import pandas_ta as ta
    except ImportError:
        raise RuntimeError("pandas_ta required for NGX native strategies — pip install pandas_ta")
    close = df["close"]
    vol = df["volume"]
    avg_vol = vol.rolling(20).mean()
    ema10 = ta.ema(close, length=10)
    ema30 = ta.ema(close, length=30)
    rsi = ta.rsi(close, length=14)
    cross_up = (ema10 > ema30) & (ema10.shift(1) <= ema30.shift(1))
    rsi_ok = (rsi >= 35) & (rsi <= 68)
    vol_ok = vol >= avg_vol * 0.8
    buy = (cross_up & rsi_ok & vol_ok).fillna(False)
    cross_down = (ema10 < ema30) & (ema10.shift(1) >= ema30.shift(1))
    sell = (cross_down | (rsi > 75)).fillna(False)
    return buy, sell


def _ngx_signals_mom_breakout(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """20-day high breakout with volume surge."""
    try:
        import pandas_ta as ta
    except ImportError:
        raise RuntimeError("pandas_ta required")
    close = df["close"]
    vol = df["volume"]
    avg_vol = vol.rolling(20).mean()
    high_20 = close.rolling(20).max().shift(1)
    buy = ((close > high_20) & (vol > avg_vol * 1.5)).fillna(False)
    ema30 = ta.ema(close, length=30)
    sell = (close < ema30).fillna(False)
    return buy, sell


def _ngx_signals_rsi_reversion(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """RSI oversold bounce with trend confirmation."""
    try:
        import pandas_ta as ta
    except ImportError:
        raise RuntimeError("pandas_ta required")
    close = df["close"]
    rsi = ta.rsi(close, length=14)
    ema50 = ta.ema(close, length=50)
    buy = ((rsi < 35) & (close > ema50) & (rsi > rsi.shift(1))).fillna(False)
    sell = (rsi > 65).fillna(False)
    return buy, sell


_NGX_STRATEGY_FNS: dict = {
    "EMA_Cross":     _ngx_signals_ema_cross,
    "MomBreakout":   _ngx_signals_mom_breakout,
    "RSI_Reversion": _ngx_signals_rsi_reversion,
}


def _ngx_run_signals(
    df: pd.DataFrame,
    buy_sig: pd.Series,
    sell_sig: pd.Series,
    initial_capital: float = 10_000,
    stop_loss_pct: float = 0.08,
    take_profit_pct: float = 0.20,
    risk_pct: float = 0.02,
) -> dict:
    """
    Event-driven backtester for NGX native strategies.
    Uses a trailing stop that ratchets up with the price.
    """
    capital = initial_capital
    trades = []
    in_trade = False
    entry_price = 0.0
    trailing_stop = 0.0

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    buys   = buy_sig.values
    sells  = sell_sig.values

    for i in range(1, len(df)):
        price = closes[i]
        if in_trade:
            trailing_stop = max(trailing_stop, price * (1 - stop_loss_pct))
            hit_stop = lows[i] <= trailing_stop
            hit_tp   = highs[i] >= entry_price * (1 + take_profit_pct)
            hit_sell = sells[i]
            if hit_stop or hit_tp or hit_sell:
                exit_price = (trailing_stop if hit_stop
                              else entry_price * (1 + take_profit_pct) if hit_tp
                              else price)
                pnl_pct  = (exit_price - entry_price) / entry_price
                size_usd = capital * risk_pct / stop_loss_pct
                pnl      = size_usd * pnl_pct
                capital += pnl
                trades.append({
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "pnl_pct": pnl_pct,
                    "result": "win" if pnl > 0 else "loss",
                    "exit_reason": "stop" if hit_stop else ("tp" if hit_tp else "signal"),
                    "opened_at": df.index[i - 1].isoformat() if hasattr(df.index[i - 1], "isoformat") else str(df.index[i - 1]),
                    "closed_at": df.index[i].isoformat() if hasattr(df.index[i], "isoformat") else str(df.index[i]),
                })
                in_trade = False
        else:
            if buys[i]:
                in_trade      = True
                entry_price   = price
                trailing_stop = price * (1 - stop_loss_pct)

    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "profit_factor": 0.0,
            "total_pnl": 0.0, "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "initial_capital": initial_capital, "final_capital": capital, "trades": []
        }

    df_t  = pd.DataFrame(trades)
    wins   = df_t[df_t["result"] == "win"]
    losses = df_t[df_t["result"] == "loss"]
    total_pnl = df_t["pnl"].sum()
    win_rate  = len(wins) / len(df_t) * 100
    gross_w   = wins["pnl"].sum() if len(wins) else 0.0
    gross_l   = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf        = gross_w / gross_l if gross_l > 0 else float("inf")

    # Max drawdown on capital curve
    curve = [initial_capital]
    for t in trades:
        curve.append(curve[-1] + t["pnl"])
    peak, max_dd = initial_capital, 0.0
    for c in curve:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": len(df_t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(min(pf, 99.9), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((capital - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
        "trades": trades,
    }


def _ngx_composite_score(r: dict) -> float:
    """Higher is better. Blends profit factor, win rate, return, drawdown."""
    if r.get("total_trades", 0) < 2:
        return -999.0
    pf  = min(r.get("profit_factor", 0), 5.0)
    wr  = r.get("win_rate", 0) / 100
    ret = r.get("total_return_pct", 0)
    dd  = r.get("max_drawdown_pct", 100)
    n   = max(r.get("total_trades", 1), 1)
    return (
        0.35 * pf
        + 0.25 * wr
        + 0.25 * (ret / 100)
        - 0.15 * (dd / 100)
        + 0.01 * float(np.log1p(n))
    )


def run_ngx_native_backtest(
    symbol: str,
    days: int = 400,
    initial_capital: float = 10_000,
    strategy: str = "auto",
    stop_loss_pct: float = 0.08,
    take_profit_pct: float = 0.20,
    output_dir: str = None,
) -> dict:
    """
    NGX-native backtest entry point.

    strategy: "auto"         — try all 3, pick best (updates strategy_map.json)
              "EMA_Cross"    — force EMA crossover
              "MomBreakout"  — force momentum breakout
              "RSI_Reversion"— force RSI mean-reversion
              "map"          — load from saved strategy_map.json (default if available)
    """
    from trading_engine.data.market_data import load_historical_data

    ticker = symbol.split("/")[0].upper()
    logger.info(f"🇳🇬 NGX Native Backtest: {symbol} | {days}d | strategy={strategy}")

    # Load data
    try:
        df = load_historical_data(symbol, timeframe="1d", limit=days)
        df = df.dropna(subset=["close", "high", "low", "volume"])
    except Exception as e:
        logger.error(f"Data load failed: {e}")
        return {"error": str(e)}

    if len(df) < 30:
        return {"error": f"Insufficient data: only {len(df)} rows"}

    # Resolve strategy
    resolved_strategy = strategy
    if strategy in ("auto", "map"):
        smap = _ngx_load_strategy_map()
        if ticker in smap and strategy == "map":
            resolved_strategy = smap[ticker].get("strategy", "auto")
        else:
            resolved_strategy = "auto"

    # Run strategies
    if resolved_strategy == "auto":
        # Try all, pick best, update strategy map
        best_result, best_name, best_score = None, None, -999.0
        all_results = {}
        for name, fn in _NGX_STRATEGY_FNS.items():
            try:
                buy_sig, sell_sig = fn(df)
                res = _ngx_run_signals(df, buy_sig, sell_sig,
                                       initial_capital, stop_loss_pct, take_profit_pct)
                all_results[name] = res
                sc = _ngx_composite_score(res)
                if sc > best_score:
                    best_score, best_result, best_name = sc, res, name
            except Exception as e:
                logger.warning(f"Strategy {name} failed for {ticker}: {e}")
                all_results[name] = {"error": str(e), "total_trades": 0}

        if best_result is None:
            best_name, best_result = "EMA_Cross", all_results.get("EMA_Cross", {"total_trades": 0})

        # Persist best strategy
        smap = _ngx_load_strategy_map()
        smap[ticker] = {
            "strategy": best_name,
            "score": round(best_score, 4),
            "return_pct": best_result.get("total_return_pct", 0),
            "win_rate": best_result.get("win_rate", 0),
            "profit_factor": best_result.get("profit_factor", 0),
            "max_drawdown_pct": best_result.get("max_drawdown_pct", 0),
            "total_trades": best_result.get("total_trades", 0),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _ngx_save_strategy_map(smap)

        result = {**best_result, "strategy_used": best_name, "all_strategies": all_results}
    else:
        fn = _NGX_STRATEGY_FNS.get(resolved_strategy)
        if fn is None:
            return {"error": f"Unknown strategy: {resolved_strategy}"}
        try:
            buy_sig, sell_sig = fn(df)
            result = _ngx_run_signals(df, buy_sig, sell_sig,
                                      initial_capital, stop_loss_pct, take_profit_pct)
            result["strategy_used"] = resolved_strategy
        except Exception as e:
            return {"error": str(e)}

    logger.success(
        f"NGX {symbol}: {result.get('strategy_used')} | "
        f"trades={result.get('total_trades',0)} | "
        f"WR={result.get('win_rate',0):.1f}% | "
        f"ret={result.get('total_return_pct',0):+.2f}% | "
        f"PF={result.get('profit_factor',0):.2f}"
    )

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        out_file = out / f"backtest_{ticker}_NGX_1d.json"
        with open(out_file, "w") as f:
            json.dump({k: v for k, v in result.items() if k != "trades"}, f, indent=2)

    return result


def run_ngx_wfo(
    symbol: str,
    days: int = 400,
    lookback_days: int = 60,
    forward_days: int = 14,
    initial_capital: float = 10_000,
    stop_loss_pct: float = 0.08,
    take_profit_pct: float = 0.20,
) -> dict:
    """
    Walk-Forward Optimisation for NGX native strategies.

    Each window:
      1. Train (lookback_days): try all 3 strategies, pick best by composite score
      2. Test  (forward_days):  apply picked strategy out-of-sample

    Returns OOS performance vs static buy-and-hold and static best strategy.
    """
    from trading_engine.data.market_data import load_historical_data

    ticker = symbol.split("/")[0].upper()
    logger.info(f"🎯 NGX WFO: {symbol} | lookback={lookback_days}d | forward={forward_days}d")

    try:
        df = load_historical_data(symbol, timeframe="1d", limit=days)
        df = df.dropna(subset=["close", "high", "low", "volume"])
    except Exception as e:
        return {"error": str(e)}

    if len(df) < lookback_days + forward_days * 2:
        return {"error": f"Insufficient data ({len(df)} rows) for WFO"}

    wfo_capital = initial_capital
    all_oos_trades = []
    weights_history = []

    idx = 0
    while idx + lookback_days + forward_days <= len(df):
        train_df = df.iloc[idx: idx + lookback_days]
        test_df  = df.iloc[idx + lookback_days: idx + lookback_days + forward_days]

        # ── Train: pick best strategy on train window ──
        best_name, best_score = "EMA_Cross", -999.0
        for name, fn in _NGX_STRATEGY_FNS.items():
            try:
                buy_sig, sell_sig = fn(train_df)
                res = _ngx_run_signals(train_df, buy_sig, sell_sig,
                                       initial_capital, stop_loss_pct, take_profit_pct)
                sc = _ngx_composite_score(res)
                if sc > best_score:
                    best_score, best_name = sc, name
            except Exception:
                pass

        weights_history.append({
            "window_start": df.index[idx].isoformat() if hasattr(df.index[idx], "isoformat") else str(df.index[idx]),
            "train_end":    df.index[idx + lookback_days - 1].isoformat() if hasattr(df.index[idx + lookback_days - 1], "isoformat") else str(df.index[idx + lookback_days - 1]),
            "best_strategy": best_name,
            "train_score":   round(best_score, 4),
        })

        # ── Test: apply best strategy on forward window ──
        try:
            fn = _NGX_STRATEGY_FNS[best_name]
            # Compute signals on full df up to test_end to avoid look-ahead in indicators,
            # then slice to the test window
            full_up_to_test = df.iloc[:idx + lookback_days + forward_days]
            buy_full, sell_full = fn(full_up_to_test)
            test_buy  = buy_full.iloc[idx + lookback_days:]
            test_sell = sell_full.iloc[idx + lookback_days:]
            test_res  = _ngx_run_signals(test_df, test_buy, test_sell,
                                          wfo_capital, stop_loss_pct, take_profit_pct)
        except Exception as e:
            logger.warning(f"WFO test window failed at idx={idx}: {e}")
            test_res = {"final_capital": wfo_capital, "trades": []}

        for t in test_res.get("trades", []):
            all_oos_trades.append({**t, "window": idx, "strategy": best_name})
        wfo_capital = test_res.get("final_capital", wfo_capital)
        idx += forward_days  # slide by forward window

    # ── Compile OOS stats ──
    if not all_oos_trades:
        return {
            "symbol": symbol, "total_trades": 0, "win_rate": 0.0,
            "profit_factor": 0.0, "total_return_pct": 0.0,
            "final_capital": wfo_capital, "weights_progression": weights_history,
            "error": "No out-of-sample trades generated",
        }

    df_t  = pd.DataFrame(all_oos_trades)
    wins   = df_t[df_t["result"] == "win"]
    losses = df_t[df_t["result"] == "loss"]
    total_pnl = df_t["pnl"].sum()
    win_rate  = len(wins) / len(df_t) * 100
    gross_w   = wins["pnl"].sum() if len(wins) else 0.0
    gross_l   = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf        = gross_w / gross_l if gross_l > 0 else float("inf")

    # Baseline: static best strategy over the full OOS period
    oos_start = lookback_days
    oos_df    = df.iloc[oos_start:]
    baseline_results = {"total_return_pct": 0.0, "total_trades": 0}
    try:
        # Use the strategy that won the most training windows
        strategy_counts = {}
        for w in weights_history:
            s = w["best_strategy"]
            strategy_counts[s] = strategy_counts.get(s, 0) + 1
        dominant = max(strategy_counts, key=strategy_counts.get)
        fn = _NGX_STRATEGY_FNS[dominant]
        buy_sig, sell_sig = fn(oos_df)
        baseline_results = _ngx_run_signals(oos_df, buy_sig, sell_sig,
                                             initial_capital, stop_loss_pct, take_profit_pct)
        baseline_results["strategy"] = dominant
    except Exception as e:
        logger.warning(f"Baseline calculation failed: {e}")

    wfo_return = (wfo_capital - initial_capital) / initial_capital * 100

    result = {
        "symbol": symbol,
        "total_trades": len(df_t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(min(pf, 99.9), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(wfo_return, 2),
        "final_capital": round(wfo_capital, 2),
        "baseline_return_pct": baseline_results.get("total_return_pct", 0),
        "baseline_trades": baseline_results.get("total_trades", 0),
        "baseline_strategy": baseline_results.get("strategy", "unknown"),
        "wfo_edge_pct": round(wfo_return - baseline_results.get("total_return_pct", 0), 2),
        "windows_tested": len(weights_history),
        "weights_progression": weights_history,
    }

    logger.success(
        f"NGX WFO {symbol}: OOS ret={result['total_return_pct']:+.2f}% | "
        f"baseline={result['baseline_return_pct']:+.2f}% | "
        f"edge={result['wfo_edge_pct']:+.2f}% | trades={result['total_trades']}"
    )
    return result


def run_ngx_portfolio_wfo(
    tickers: list[str] | None = None,
    days: int = 400,
    lookback_days: int = 90,
    forward_days: int = 45,
    initial_capital: float = 10_000,
    stop_loss_pct: float = 0.08,
    risk_pct: float = 0.02,
    output_dir: str | None = None,
) -> dict:
    """
    Portfolio-level Walk-Forward Optimisation for NGX native strategies.

    Solves the sparse-signal problem: individual NGX stocks rarely generate
    a trade in a 45-day window, but testing all tickers simultaneously
    produces enough out-of-sample trades for meaningful statistics.

    Per window:
      Train: For each stock, pick best strategy by composite score.
      Test:  Apply each stock's best strategy on its forward window.
             Accumulate trades across the whole portfolio.

    Default tickers: the 19 original NGX stocks (or pass a custom list).
    """
    from trading_engine.data.market_data import load_historical_data
    from collections import Counter

    if tickers is None:
        tickers = [
            "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
            "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
            "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
            "NASCON", "FLOURMILL", "NB", "MEYER",
        ]

    logger.info(
        f"🇳🇬 NGX Portfolio WFO: {len(tickers)} stocks | "
        f"lookback={lookback_days}d | forward={forward_days}d"
    )

    # Load all data
    stock_data: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = load_historical_data(f"{ticker}/NGX", timeframe="1d", limit=days)
            df = df.dropna(subset=["close", "high", "low", "volume"])
            if len(df) >= lookback_days + forward_days:
                stock_data[ticker] = df
        except Exception as e:
            logger.warning(f"Skipping {ticker}: {e}")

    if not stock_data:
        return {"error": "No stocks loaded"}

    # Use the stock with the most data as reference timeline
    ref_ticker = max(stock_data, key=lambda t: len(stock_data[t]))
    ref_df = stock_data[ref_ticker]

    portfolio_capital = initial_capital
    allocation_per_stock = initial_capital / len(stock_data)
    all_oos_trades: list[dict] = []
    window_log: list[dict] = []

    idx = 0
    while idx + lookback_days + forward_days <= len(ref_df):
        train_start = ref_df.index[idx].date()
        train_end   = ref_df.index[idx + lookback_days - 1].date()
        test_end    = ref_df.index[min(idx + lookback_days + forward_days - 1, len(ref_df) - 1)].date()

        window_strategies: dict[str, str] = {}
        window_trades = 0

        for ticker, df in stock_data.items():
            train_mask = (df.index.date >= train_start) & (df.index.date <= train_end)
            test_mask  = (df.index.date > train_end) & (df.index.date <= test_end)
            train_df   = df[train_mask]
            test_df    = df[test_mask]

            if len(train_df) < 20 or len(test_df) < 5:
                continue

            # Train: pick best strategy
            best_name, best_score = "EMA_Cross", -999.0
            for name, fn in _NGX_STRATEGY_FNS.items():
                try:
                    b, s = fn(train_df)
                    res  = _ngx_run_signals(train_df, b, s, allocation_per_stock, stop_loss_pct, risk_pct=risk_pct)
                    sc   = _ngx_composite_score(res)
                    if sc > best_score:
                        best_score, best_name = sc, name
                except Exception:
                    pass
            window_strategies[ticker] = best_name

            # Test: apply on forward window with look-ahead-safe context
            ctx_df = df[df.index.date <= test_end].tail(lookback_days + forward_days)
            try:
                fn = _NGX_STRATEGY_FNS[best_name]
                buy_full, sell_full = fn(ctx_df)
                test_buy  = buy_full[buy_full.index.isin(test_df.index)]
                test_sell = sell_full[sell_full.index.isin(test_df.index)]
                res = _ngx_run_signals(
                    test_df, test_buy, test_sell,
                    allocation_per_stock, stop_loss_pct,
                    risk_pct=risk_pct
                )
                for t in res.get("trades", []):
                    all_oos_trades.append({**t, "ticker": ticker, "window": idx, "strategy": best_name})
                    portfolio_capital += t["pnl"]
                    window_trades += 1
            except Exception as e:
                logger.debug(f"WFO test failed for {ticker} window {idx}: {e}")

        strat_counts = Counter(window_strategies.values())
        window_log.append({
            "window": idx,
            "train_start": str(train_start), "train_end": str(train_end), "test_end": str(test_end),
            "oos_trades": window_trades,
            "dominant_strategy": strat_counts.most_common(1)[0][0] if strat_counts else "?",
            "strategy_breakdown": dict(strat_counts),
        })
        logger.info(
            f"  Window {idx:3d} ({train_start}→{test_end})  "
            f"trades={window_trades}  dominant={strat_counts.most_common(1)[0][0] if strat_counts else '?'}"
        )
        idx += forward_days

    # Compile results
    if not all_oos_trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "total_return_pct": 0.0, "final_capital": portfolio_capital,
            "windows": window_log, "error": "No OOS trades generated",
        }

    df_t   = pd.DataFrame(all_oos_trades)
    wins   = df_t[df_t["result"] == "win"]
    losses = df_t[df_t["result"] == "loss"]
    total_pnl = df_t["pnl"].sum()
    win_rate  = len(wins) / len(df_t) * 100
    gross_w   = wins["pnl"].sum() if len(wins) else 0.0
    gross_l   = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf        = gross_w / gross_l if gross_l > 0 else float("inf")
    wfo_ret   = (portfolio_capital - initial_capital) / initial_capital * 100

    result = {
        "tickers_tested": len(stock_data),
        "total_trades": len(df_t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(min(pf, 99.9), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(wfo_ret, 2),
        "final_capital": round(portfolio_capital, 2),
        "windows_tested": len(window_log),
        "windows": window_log,
        "per_ticker": {
            tkr: {
                "trades": len(sub := df_t[df_t["ticker"] == tkr]),
                "wins": int((sub["result"] == "win").sum()),
                "pnl": round(sub["pnl"].sum(), 2),
            }
            for tkr in df_t["ticker"].unique()
        },
    }

    logger.success(
        f"NGX Portfolio WFO: OOS ret={wfo_ret:+.2f}% | "
        f"trades={result['total_trades']} | WR={win_rate:.1f}% | PF={pf:.2f}"
    )

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "ngx_portfolio_wfo.json", "w") as f:
            json.dump(result, f, indent=2, default=str)

    return result


def run_ngx_dynamic_portfolio_wfo(
    tickers: list[str] | None = None,
    days: int = 400,
    lookback_days: int = 90,
    forward_days: int = 45,
    initial_capital: float = 10_000,
    stop_loss_pct: float = 0.08,
    take_profit_pct: float = 0.20,
    position_fraction: float = 0.15,
    volatility_sizing: bool = True,
    output_dir: str | None = None,
) -> dict:
    """
    Chronological Portfolio-level Walk-Forward Optimisation for NGX native strategies.
    Uses a shared cash pool (Dynamic Cash Sharing) and concentrates capital.
    Supports Volatility-based Regime-Aware Position Sizing.
    """
    from trading_engine.data.market_data import load_historical_data
    from collections import Counter
    import pandas as pd
    import pandas_ta as ta

    if tickers is None:
        # Concentrate: Top 10 scoring stocks + original 19
        top_10 = [
            "TRANSEXPR", "WEMABANK", "VFDGROUP", "CHAMS", "FIRSTHOLDCO",
            "NGXGROUP", "GUINEAINS", "GTCO", "CONHALLPLC", "INTENEGINS"
        ]
        original_19 = [
            "ARADEL", "AIRTELAFRI", "BUACEMENT", "BUAFOODS", "CAP",
            "DANGCEM", "JAIZBANK", "WAPCO", "MTNN", "OANDO",
            "SEPLAT", "PRESCO", "OKOMUOIL", "UNILEVER", "CADBURY",
            "NASCON", "FLOURMILL", "NB", "MEYER",
        ]
        tickers = list(sorted(list(set(top_10 + original_19))))

    logger.info(
        f"🇳🇬 NGX Dynamic Portfolio WFO: {len(tickers)} stocks | "
        f"lookback={lookback_days}d | forward={forward_days}d | "
        f"pos_pct={position_fraction*100:.1f}% | vol_size={volatility_sizing}"
    )

    # Load all data
    stock_data: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = load_historical_data(f"{ticker}/NGX", timeframe="1d", limit=days)
            df = df.dropna(subset=["close", "high", "low", "volume"])
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            
            # Pre-compute Average True Range % for Volatility Sizing
            atr = ta.atr(df["high"], df["low"], df["close"], length=14)
            df["atr_pct"] = (atr / df["close"]).fillna(0.03) # 3% daily volatility default
            
            if len(df) >= lookback_days + forward_days:
                stock_data[ticker] = df
        except Exception as e:
            logger.warning(f"Skipping {ticker}: {e}")

    if not stock_data:
        return {"error": "No stocks loaded"}

    # Use the stock with the most data as reference timeline
    ref_ticker = max(stock_data, key=lambda t: len(stock_data[t]))
    ref_df = stock_data[ref_ticker]

    portfolio_capital = initial_capital
    all_oos_trades: list[dict] = []
    window_log: list[dict] = []

    idx = 0
    while idx + lookback_days + forward_days <= len(ref_df):
        train_start = ref_df.index[idx].date()
        train_end   = ref_df.index[idx + lookback_days - 1].date()
        test_end    = ref_df.index[min(idx + lookback_days + forward_days - 1, len(ref_df) - 1)].date()

        window_strategies: dict[str, str] = {}
        window_trades = 0

        # Train Phase
        for ticker, df in stock_data.items():
            train_mask = (df.index.date >= train_start) & (df.index.date <= train_end)
            train_df   = df[train_mask]

            if len(train_df) < 20:
                continue

            # Train: pick best strategy
            best_name, best_score = "EMA_Cross", -999.0
            # Temp capital sizing for scoring
            temp_capital = portfolio_capital / len(stock_data)
            for name, fn in _NGX_STRATEGY_FNS.items():
                try:
                    b, s = fn(train_df)
                    res  = _ngx_run_signals(train_df, b, s, temp_capital, stop_loss_pct)
                    sc   = _ngx_composite_score(res)
                    if sc > best_score:
                        best_score, best_name = sc, name
                except Exception:
                    pass
            window_strategies[ticker] = best_name

        # Test Phase (Daily Chronological Loop)
        test_dates = sorted(list(set().union(*(
            df[(df.index.date > train_end) & (df.index.date <= test_end)].index
            for df in stock_data.values()
        ))))

        # Pre-generate signals
        stock_signals = {}
        for ticker, df in stock_data.items():
            best_strat = window_strategies.get(ticker)
            if not best_strat:
                continue
            ctx_df = df[df.index.date <= test_end].tail(lookback_days + forward_days)
            test_mask = (df.index.date > train_end) & (df.index.date <= test_end)
            fn = _NGX_STRATEGY_FNS[best_strat]
            try:
                buy_full, sell_full = fn(ctx_df)
                stock_signals[ticker] = {
                    "df": df[test_mask],
                    "buy": buy_full[buy_full.index.isin(df[test_mask].index)],
                    "sell": sell_full[sell_full.index.isin(df[test_mask].index)]
                }
            except Exception:
                pass

        open_positions = {}
        free_cash = portfolio_capital

        for current_date in test_dates:
            # 1. Update existing positions
            closed_tickers = []
            for ticker, pos in open_positions.items():
                sig_data = stock_signals.get(ticker)
                if sig_data is None:
                    continue
                df_test = sig_data["df"]
                if current_date not in df_test.index:
                    continue
                row = df_test.loc[current_date]
                close = row["close"]
                high = row["high"]
                low = row["low"]
                
                sell_triggered = sig_data["sell"].get(current_date, False)
                pos["trailing_stop"] = max(pos["trailing_stop"], close * (1 - stop_loss_pct))
                
                hit_stop = low <= pos["trailing_stop"]
                hit_tp = high >= pos["entry_price"] * (1 + take_profit_pct)
                
                if hit_stop or hit_tp or sell_triggered:
                    exit_price = (pos["trailing_stop"] if hit_stop 
                                  else pos["entry_price"] * (1 + take_profit_pct) if hit_tp 
                                  else close)
                    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
                    pnl = pos["size_usd"] * pnl_pct
                    
                    portfolio_capital += pnl
                    free_cash += pos["size_usd"] + pnl
                    
                    all_oos_trades.append({
                        "ticker": ticker,
                        "entry": pos["entry_price"],
                        "exit": exit_price,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "result": "win" if pnl > 0 else "loss",
                        "exit_reason": "stop" if hit_stop else ("tp" if hit_tp else "signal"),
                        "opened_at": str(pos["entry_date"].date()),
                        "closed_at": str(current_date.date()),
                        "strategy": pos["strategy"],
                        "window": idx
                    })
                    closed_tickers.append(ticker)
                    window_trades += 1
            
            for tkr in closed_tickers:
                del open_positions[tkr]
                
            # 2. Check new entries
            for ticker, best_strat in window_strategies.items():
                if ticker in open_positions:
                    continue
                sig_data = stock_signals.get(ticker)
                if sig_data is None:
                    continue
                df_test = sig_data["df"]
                if current_date not in df_test.index:
                    continue
                
                buy_triggered = sig_data["buy"].get(current_date, False)
                if buy_triggered:
                    if volatility_sizing:
                        atr_val = df_test.loc[current_date, "atr_pct"]
                        # Target 1.2% risk of total capital per trade, capped between 5% and 25%
                        pos_frac = max(0.05, min(0.25, 0.012 / atr_val)) if atr_val > 0 else position_fraction
                    else:
                        pos_frac = position_fraction
                    
                    pos_size = portfolio_capital * pos_frac
                    if free_cash >= pos_size and pos_size > 0:
                        close_price = df_test.loc[current_date, "close"]
                        open_positions[ticker] = {
                            "entry_price": close_price,
                            "size_usd": pos_size,
                            "trailing_stop": close_price * (1 - stop_loss_pct),
                            "entry_date": current_date,
                            "strategy": best_strat
                        }
                        free_cash -= pos_size

        # Close any remaining open positions at the end of the window to count capital
        for ticker, pos in open_positions.items():
            sig_data = stock_signals.get(ticker)
            if sig_data is not None:
                df_test = sig_data["df"]
                close_price = df_test["close"].iloc[-1]
                pnl_pct = (close_price - pos["entry_price"]) / pos["entry_price"]
                pnl = pos["size_usd"] * pnl_pct
                portfolio_capital += pnl
                all_oos_trades.append({
                    "ticker": ticker,
                    "entry": pos["entry_price"],
                    "exit": close_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "result": "win" if pnl > 0 else "loss",
                    "exit_reason": "end_of_window",
                    "opened_at": str(pos["entry_date"].date()),
                    "closed_at": str(df_test.index[-1].date()),
                    "strategy": pos["strategy"],
                    "window": idx
                })
                window_trades += 1

        strat_counts = Counter(window_strategies.values())
        window_log.append({
            "window": idx,
            "train_start": str(train_start), "train_end": str(train_end), "test_end": str(test_end),
            "oos_trades": window_trades,
            "dominant_strategy": strat_counts.most_common(1)[0][0] if strat_counts else "?",
            "strategy_breakdown": dict(strat_counts),
        })
        logger.info(
            f"  Window {idx:3d} ({train_start}→{test_end})  "
            f"trades={window_trades}  dominant={strat_counts.most_common(1)[0][0] if strat_counts else '?'}"
        )
        idx += forward_days

    # Compile results
    if not all_oos_trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "total_return_pct": 0.0, "final_capital": portfolio_capital,
            "windows": window_log, "error": "No OOS trades generated",
        }

    df_t   = pd.DataFrame(all_oos_trades)
    wins   = df_t[df_t["result"] == "win"]
    losses = df_t[df_t["result"] == "loss"]
    total_pnl = df_t["pnl"].sum()
    win_rate  = len(wins) / len(df_t) * 100
    gross_w   = wins["pnl"].sum() if len(wins) else 0.0
    gross_l   = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf        = gross_w / gross_l if gross_l > 0 else float("inf")
    wfo_ret   = (portfolio_capital - initial_capital) / initial_capital * 100

    result = {
        "tickers_tested": len(stock_data),
        "total_trades": len(df_t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(min(pf, 99.9), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(wfo_ret, 2),
        "final_capital": round(portfolio_capital, 2),
        "windows_tested": len(window_log),
        "windows": window_log,
        "per_ticker": {
            tkr: {
                "trades": len(sub := df_t[df_t["ticker"] == tkr]),
                "wins": int((sub["result"] == "win").sum()),
                "pnl": round(sub["pnl"].sum(), 2),
            }
            for tkr in df_t["ticker"].unique()
        },
    }

    logger.success(
        f"NGX Dynamic Portfolio WFO: OOS ret={wfo_ret:+.2f}% | "
        f"trades={result['total_trades']} | WR={win_rate:.1f}% | PF={pf:.2f}"
    )

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "ngx_dynamic_portfolio_wfo.json", "w") as f:
            json.dump(result, f, indent=2, default=str)

    return result


def optimize_weights_for_window(
    df: pd.DataFrame,
    symbol: str,
    asset_type: str,
    timeframe: str,
    start_idx: int,
    end_idx: int,
    base_weights: dict[str, float],
    forward_candles: int = 12,
    htf_dfs: dict[str, pd.DataFrame] | None = None,
) -> dict[str, float]:
    """
    Evaluates individual agent signal accuracy over a training window
    and updates base weights proportionally.
    """
    agent_names = ["trend", "momentum", "volume", "volatility", "structure", "orderflow"]
    agent_stats = {name: {"correct": 0, "total": 0} for name in agent_names}
    
    closes = df["close"].values
    
    start_idx = max(20, start_idx)
    for i in range(start_idx, min(end_idx, len(df) - forward_candles)):
        snap = make_historical_snapshot(symbol, asset_type, timeframe, df, i, htf_dfs)
        
        agents_signals = {
            "trend": trend_agent.analyze(snap),
            "momentum": momentum_agent.analyze(snap),
            "volume": volume_agent.analyze(snap),
            "volatility": volatility_agent.analyze(snap),
            "structure": structure_agent.analyze(snap),
            "orderflow": orderflow_agent.analyze(snap),
        }
        
        future_price = closes[i + forward_candles]
        current_price = closes[i]
        price_moved_up = future_price > current_price * 1.002
        price_moved_down = future_price < current_price * 0.998
        
        for name, sig in agents_signals.items():
            if sig.signal == Signal.BUY:
                agent_stats[name]["total"] += 1
                if price_moved_up:
                    agent_stats[name]["correct"] += 1
            elif sig.signal == Signal.SELL:
                agent_stats[name]["total"] += 1
                if price_moved_down:
                    agent_stats[name]["correct"] += 1
                    
    optimized = base_weights.copy()
    for name in agent_names:
        stats = agent_stats[name]
        if stats["total"] >= 5:
            win_rate = stats["correct"] / stats["total"]
            multiplier = win_rate / 0.50
            new_w = base_weights.get(name, 1.0) * multiplier
            optimized[name] = round(max(0.5, min(2.0, new_w)), 2)
            
    return optimized


def run_walk_forward_optimization(
    symbol: str,
    timeframe: str = "4h",
    days: int = 365,
    lookback_days: int = 30,
    forward_days: int = 7,
    initial_capital: float = 10000,
) -> dict:
    """
    Performs rolling walk-forward optimization of agent weights (crypto/CFD)
    or strategy selection (NGX equities).
    NGX symbols are automatically routed to run_ngx_wfo().
    """
    # ── Auto-route NGX symbols ────────────────────────────────────────────
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    if ac == AssetClass.NGX_STOCK:
        return run_ngx_wfo(
            symbol=symbol, days=days,
            lookback_days=lookback_days, forward_days=forward_days,
            initial_capital=initial_capital,
        )
    # ─────────────────────────────────────────────────────────────────────
    from trading_engine.data.market_data import compute_indicators
    import ccxt

    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    # Enable backtest mode: skips live orderbook fetch (lookahead bias + slow)
    settings.is_backtesting = True
    asset_type = "crypto" if ac == AssetClass.CRYPTO else "cfd" if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) else "stock"
    
    # Timeframe to candles per day mapping
    tf_mapping = {
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "4h": 6,
        "1d": 1,
    }
    candles_per_day = tf_mapping.get(timeframe, 6)
    
    # 1. Fetch data
    limit = min(days * candles_per_day, 20000)
    try:
        from trading_engine.data.market_data import load_historical_data
        df = load_historical_data(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        logger.error(f"WFO Data fetch failed: {e}")
        return {"error": str(e)}
        
    df = compute_indicators(df)
    df = df.dropna()
        
    try:
        htf_dfs = prepare_htf_dfs(symbol, timeframe)
    except Exception as e:
        logger.warning(f"Failed to prepare HTF dfs for WFO: {e}")
        htf_dfs = None

    train_size = lookback_days * candles_per_day
    test_size = forward_days * candles_per_day
    
    current_weights = judge.DEFAULT_WEIGHTS.copy()
    
    all_test_trades = []
    wfo_capital = initial_capital
    weights_history = []
    
    # Slide the windows
    start_idx = 0
    while start_idx + train_size + test_size <= len(df):
        train_end = start_idx + train_size
        test_end = train_end + test_size
        
        # Optimize weights on train window
        optimized_w = optimize_weights_for_window(
            df, symbol, asset_type, timeframe,
            start_idx, train_end, current_weights,
            htf_dfs=htf_dfs
        )
        
        # Record weights
        weights_history.append({
            "timestamp": df.index[train_end].isoformat(),
            "weights": optimized_w.copy()
        })
        
        # Test out-of-sample on test window
        test_results = _run_multi_agent_simulation(
            df, symbol, asset_type, timeframe,
            optimized_w, wfo_capital,
            start_idx=train_end,
            end_idx=test_end,
            htf_dfs=htf_dfs
        )
        
        # Accumulate out-of-sample trades
        for t in test_results.get("trades", []):
            all_test_trades.append(t)
            
        wfo_capital = test_results["final_capital"]
        
        # Slide forward
        start_idx += test_size
        
    # Analyze out-of-sample stats
    if not all_test_trades:
        logger.warning("No out-of-sample trades generated during WFO.")
        return {"error": "No trades generated"}
        
    df_trades = pd.DataFrame(all_test_trades)
    wins = df_trades[df_trades["result"] == "win"]
    losses = df_trades[df_trades["result"] == "loss"]
    total_pnl = df_trades["net_pnl"].sum()
    win_rate = len(wins) / len(df_trades) * 100
    
    gross_profits = wins["net_pnl"].sum()
    gross_losses = abs(losses["net_pnl"].sum())
    profit_factor = gross_profits / gross_losses if gross_losses > 0 else float("inf")
    
    # Baseline comparison (run static weights over the exact same out-of-sample period)
    oos_start_idx = train_size
    baseline_results = _run_multi_agent_simulation(
        df, symbol, asset_type, timeframe,
        judge.DEFAULT_WEIGHTS, initial_capital,
        start_idx=oos_start_idx,
        htf_dfs=htf_dfs
    )
    
    wfo_results = {
        "total_trades": len(df_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((wfo_capital - initial_capital) / initial_capital * 100, 2),
        "final_capital": round(wfo_capital, 2),
        "baseline_pnl": baseline_results["total_pnl"],
        "baseline_return_pct": baseline_results["total_return_pct"],
        "baseline_trades": baseline_results["total_trades"],
        "weights_progression": weights_history,
    }
    
    logger.success(f"\n{'='*60}")
    logger.success(f"WALK-FORWARD OPTIMIZATION COMPLETE: {symbol}")
    logger.success(f"{'='*60}")
    logger.success(f"  Dynamic WFO Return:  {wfo_results['total_return_pct']}% (${wfo_results['total_pnl']:+,.2f})")
    logger.success(f"  Baseline Static Return: {wfo_results['baseline_return_pct']}% (${wfo_results['baseline_pnl']:+,.2f})")
    logger.success(f"  Win Rate (WFO):      {wfo_results['win_rate']}% (Trades: {wfo_results['total_trades']})")
    logger.success(f"  Profit Factor (WFO): {wfo_results['profit_factor']:.2f}")
    logger.success(f"{'='*60}\n")
    
    # Save optimized weights to JSON
    out_file = Path(__file__).parent.parent / "optimized_weights.json"
    try:
        with open(out_file, "w") as f:
            json.dump(optimized_w, f, indent=2)
        logger.info(f"Saved latest optimized weights to {out_file}")
    except Exception as e:
        logger.error(f"Could not save optimized weights: {e}")
        
    return wfo_results


def run_backtest(
    symbol: str,
    timeframe: str = "4h",
    days: int = 365,
    initial_capital: float = 10000,
    output_dir: str = None,
    use_multi_agent: bool = True,
) -> dict:
    """
    Main backtest entry point.
    Fetches historical data, runs the specified backtest simulation, and reports results.
    NGX symbols (*/NGX) are automatically routed to the NGX native strategy engine.
    """
    from trading_engine.data.market_data import compute_indicators

    logger.info(f"📊 Running backtest: {symbol} | {timeframe} | {days} days")

    # ── Auto-route NGX symbols to native engine ──────────────────────────
    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    # Enable backtest mode: skips live orderbook fetch (lookahead bias + slow)
    settings.is_backtesting = True
    if ac == AssetClass.NGX_STOCK:
        logger.info(f"Routing {symbol} to NGX native strategy engine")
        return run_ngx_native_backtest(
            symbol=symbol, days=days, initial_capital=initial_capital,
            strategy="map", output_dir=output_dir,
        )
    # ─────────────────────────────────────────────────────────────────────

    tf_mapping = {
        "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1,
    }
    candles_per_day = tf_mapping.get(timeframe, 6)
    limit = min(days * candles_per_day, 20000)
    try:
        from trading_engine.data.market_data import load_historical_data
        df = load_historical_data(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        return {"error": str(e)}

    df = compute_indicators(df)
    df = df.dropna()

    try:
        htf_dfs = prepare_htf_dfs(symbol, timeframe)
    except Exception as e:
        logger.warning(f"Failed to prepare HTF dfs for backtest: {e}")
        htf_dfs = None

    asset_type = "crypto" if ac == AssetClass.CRYPTO else "cfd" if ac in (AssetClass.STOCK_CFD, AssetClass.PRECIOUS_METAL) else "stock"

    if use_multi_agent:
        results = _run_multi_agent_simulation(
            df, symbol, asset_type, timeframe,
            judge.DEFAULT_WEIGHTS, initial_capital,
            htf_dfs=htf_dfs
        )
    else:
        df["signal"] = 0
        buy_cond = (
            (df.get("EMA_20", df["close"]) > df.get("EMA_50", df["close"])) &
            (df.get("EMA_50", df["close"]) > df.get("EMA_200", df["close"])) &
            (df.get("RSI_14", pd.Series(50, index=df.index)) > 50) &
            (df.get("ROC_10", pd.Series(0, index=df.index)) > 0)
        )
        sell_cond = (
            (df.get("EMA_20", df["close"]) < df.get("EMA_50", df["close"])) &
            (df.get("RSI_14", pd.Series(50, index=df.index)) < 50)
        )
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1
        results = _simple_backtest(df, df["signal"], initial_capital)

    logger.success(f"\n{'='*50}")
    logger.success(f"BACKTEST RESULTS: {symbol} {timeframe} ({days}d)")
    logger.success(f"{'='*50}")
    for k, v in results.items():
        if k != "trades":
            logger.success(f"  {k}: {v}")
    logger.success(f"{'='*50}\n")

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        out_file = out / f"backtest_{symbol.replace('/', '_')}_{timeframe}.json"
        with open(out_file, "w") as f:
            json.dump({k: v for k, v in results.items() if k != "trades"}, f, indent=2)
        logger.info(f"Results saved to {out_file}")

    return results


if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description="Backtest Engine with WFO")
    parser.add_argument("symbol", nargs="?", default="BTC/USDT", help="Asset symbol")
    parser.add_argument("days", nargs="?", type=int, default=180, help="Days of historical data")
    parser.add_argument("--wfo", action="store_true", help="Run Walk-Forward Weight Optimization")
    parser.add_argument("--timeframe", default="4h", help="Data timeframe")
    
    args = parser.parse_args()
    
    if args.wfo:
        results = run_walk_forward_optimization(
            symbol=args.symbol,
            timeframe=args.timeframe,
            days=args.days
        )
    else:
        results = run_backtest(
            symbol=args.symbol,
            timeframe=args.timeframe,
            days=args.days,
            use_multi_agent=True
        )

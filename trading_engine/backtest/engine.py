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


def make_historical_snapshot(symbol: str, asset_type: str, timeframe: str, df: pd.DataFrame, i: int) -> MarketSnapshot:
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
    
    return MarketSnapshot(
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
) -> dict:
    """
    Runs row-by-row simulation of the actual quant agents,
    evaluating via Judge and RiskAgent. Incorporates transaction fees
    and dynamic ATR-based stops.
    """
    capital = initial_capital
    trades = []
    open_position = None
    
    # 0.06% entry + 0.06% exit
    entry_fee_rate = 0.0006
    exit_fee_rate = 0.0006

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
                    triggered = True
                    exit_price = stop_loss
                    status = "stopped"
                elif high_price >= take_profit:
                    triggered = True
                    exit_price = take_profit
                    status = "take_profit"
            else: # short
                if high_price >= stop_loss:
                    triggered = True
                    exit_price = stop_loss
                    status = "stopped"
                elif low_price <= take_profit:
                    triggered = True
                    exit_price = take_profit
                    status = "take_profit"
                    
            if triggered:
                if direction == "long":
                    gross_pnl = (exit_price - entry_price) / entry_price * size_usd
                else:
                    gross_pnl = (entry_price - exit_price) / entry_price * size_usd
                    
                exit_fee = size_usd * exit_fee_rate
                net_pnl = gross_pnl - exit_fee
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
                    "net_pnl": net_pnl,
                    "result": "win" if net_pnl > 0 else "loss",
                    "status": status,
                    "opened_at": df.index[open_position["entry_idx"]].isoformat(),
                    "closed_at": df.index[i].isoformat(),
                })
                open_position = None
                
        # Look for entry signals
        if not open_position:
            snap = make_historical_snapshot(symbol, asset_type, timeframe, df, i)
            
            # Run 6 quant agents
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
            verdict = judge.evaluate(signals, agent_weights)
            
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
                    capital -= entry_fee
                    
                    open_position = {
                        "entry_idx": i,
                        "direction": "long" if verdict.decision == Signal.BUY else "short",
                        "entry": price,
                        "size_usd": decision.position_size_usd,
                        "stop_loss": decision.stop_loss,
                        "take_profit": decision.take_profit,
                        "entry_fee": entry_fee,
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


def optimize_weights_for_window(
    df: pd.DataFrame,
    symbol: str,
    asset_type: str,
    timeframe: str,
    start_idx: int,
    end_idx: int,
    base_weights: dict[str, float],
    forward_candles: int = 12,
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
        snap = make_historical_snapshot(symbol, asset_type, timeframe, df, i)
        
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
    Performs rolling walk-forward optimization of agent weights.
    Optimizes weights on a rolling lookback window, then tests out-of-sample.
    """
    from trading_engine.data.market_data import compute_indicators
    import ccxt
    
    logger.info(f"🔄 Starting Walk-Forward Optimization for {symbol} ({days}d)")
    is_crypto = "/" in symbol or symbol.endswith("USDT")
    asset_type = "crypto" if is_crypto else "stock"
    
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
        exchange = ccxt.binance()
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
    except Exception as e:
        logger.error(f"WFO Data fetch failed: {e}")
        return {"error": str(e)}
        
    df = compute_indicators(df)
    df = df.dropna()
        
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
            start_idx, train_end, current_weights
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
            end_idx=test_end
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
        start_idx=oos_start_idx
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
    """
    from trading_engine.data.market_data import compute_indicators
    import ccxt

    logger.info(f"📊 Running backtest: {symbol} | {timeframe} | {days} days")

    tf_mapping = {
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "4h": 6,
        "1d": 1,
    }
    candles_per_day = tf_mapping.get(timeframe, 6)
    limit = min(days * candles_per_day, 20000)
    try:
        exchange = ccxt.binance()
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        return {"error": str(e)}

    df = compute_indicators(df)
    df = df.dropna()

    is_crypto = "/" in symbol or symbol.endswith("USDT")
    asset_type = "crypto" if is_crypto else "stock"

    if use_multi_agent:
        results = _run_multi_agent_simulation(
            df, symbol, asset_type, timeframe,
            judge.DEFAULT_WEIGHTS, initial_capital
        )
    else:
        # Simplified condition-based fallback
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

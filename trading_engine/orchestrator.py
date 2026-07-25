"""
trading_engine/orchestrator.py

Main pipeline: runs all agents in parallel, passes to Judge, then Risk Agent.
Returns a complete TradeSignal with full audit trail.
"""
from __future__ import annotations
import concurrent.futures
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
import json
from loguru import logger

from trading_engine.data.market_data import build_snapshot, MarketSnapshot
from trading_engine.agents import (
    trend_agent, momentum_agent, volume_agent,
    orderflow_agent, volatility_agent, structure_agent,
    sentiment_agent, macro_agent, mean_reversion_agent,
)
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.judge import evaluate as judge_evaluate, JudgeVerdict
from trading_engine.risk_agent import evaluate as risk_evaluate, RiskDecision
from trading_engine.config import settings


@dataclass
class TradeSignal:
    symbol: str
    asset_type: str
    timeframe: str
    timestamp: str
    agent_signals: list[dict]
    verdict: dict
    risk: dict
    final_action: str          # "BUY" | "SELL" | "NO_TRADE"
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    position_size_usd: Optional[float]
    reasoning: str


def _run_agents_parallel(snap: MarketSnapshot) -> list[AgentSignal]:
    """Run agents concurrently using thread pool."""
    if settings.simple_ensemble_enabled:
        agent_fns = [
            momentum_agent.analyze,
            structure_agent.analyze,
        ]
    else:
        agent_fns = [
            trend_agent.analyze,
            momentum_agent.analyze,
            volume_agent.analyze,
            orderflow_agent.analyze,
            volatility_agent.analyze,
            structure_agent.analyze,
            sentiment_agent.analyze,
            macro_agent.analyze,
            mean_reversion_agent.analyze,
        ]

    signals = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fn, snap): fn.__module__.split(".")[-1]
                   for fn in agent_fns}
        for future in concurrent.futures.as_completed(futures):
            agent_name = futures[future]
            try:
                result = future.result(timeout=30)
                signals.append(result)
                logger.info(f"  ✓ {result.agent}: {result.signal.value} ({result.confidence:.0f}%)")
            except Exception as e:
                logger.error(f"  ✗ {agent_name} failed: {e}")

    return signals


def _get_daily_pnl(trader) -> float:
    """
    Return today's total realized PnL (USD) from closed positions.
    A negative value means losses; used by the circuit-breaker in risk_agent.
    Returns 0.0 if the state cannot be read.
    """
    try:
        state = trader._load_state()
        today_utc = datetime.now(timezone.utc).date()
        total = 0.0
        closed_list = getattr(state, "closed_trades", [])
        for pos in closed_list:
            # closed_at may be an ISO string or datetime
            closed_at = pos.closed_at
            if closed_at is None:
                continue
            if isinstance(closed_at, str):
                from datetime import datetime as _dt
                iso_str = closed_at
                if iso_str.endswith("Z"):
                    iso_str = iso_str[:-1] + "+00:00"
                closed_at = _dt.fromisoformat(iso_str)
            if hasattr(closed_at, "date") and closed_at.date() == today_utc:
                total += float(getattr(pos, "pnl_usd", 0.0) or getattr(pos, "pnl", 0.0) or 0.0)
        logger.info(f"  💰 Circuit breaker: today's realized PnL = ${total:,.2f}")
        return total
    except Exception as e:
        logger.warning(f"  Could not compute daily PnL for circuit breaker: {e}")
        return 0.0


def run(
    symbol: str,
    timeframe: str = None,
    portfolio_heat: float = 0.0,
    open_positions: int = 0,
    win_rate: float = 0.50,
    open_position_snaps: dict = None,   # symbol -> MarketSnapshot for correlation check
    daily_pnl_usd: float = 0.0,         # today's realized PnL for circuit-breaker check
    account_size: Optional[float] = None,
    closed_trades: Optional[list] = None,
) -> TradeSignal:
    """
    Full pipeline execution for one symbol.
    Returns TradeSignal with complete audit trail.
    """
    tf = timeframe or settings.timeframe
    logger.info(f"\n{'='*60}")
    logger.info(f"TRADING ENGINE | {symbol} | {tf}")
    logger.info(f"{'='*60}")

    from trading_engine.market_hours import classify_symbol, AssetClass
    ac = classify_symbol(symbol)
    
    # Step 1: Fetch market data + indicators
    logger.info("📊 Fetching market data...")
    snap = build_snapshot(symbol, tf)
    logger.info(f"   Close={snap.close:.4f} | RSI={snap.rsi:.1f} | ATR={snap.atr:.4f}")

    # Step 2: Run all agents in parallel
    logger.info("🤖 Running specialist agents...")
    agent_signals = _run_agents_parallel(snap)

    # Step 3: Judge evaluates
    logger.info("⚖️  Judge evaluating...")
    
    # Try to load optimized weights from Walk-Forward Optimization
    agent_weights = None
    try:
        from trading_engine.market_hours import classify_symbol, AssetClass
        ac = classify_symbol(symbol)
        
        # Map symbol to optimization target category
        if ac == AssetClass.CRYPTO:
            target = "crypto"
        elif ac in (AssetClass.STOCK, AssetClass.STOCK_CFD, AssetClass.NGX_STOCK, AssetClass.BAMBOO_US_STOCK):
            target = "stock"
        else:
            target = "crypto"
            
        from trading_engine.storage import db
        
        # 1. Try to load symbol-specific weights from DB
        db_state = db.get_symbol_state(symbol)
        if db_state and db_state.get("weights"):
            agent_weights = db_state["weights"]
            logger.info(f"   Loaded symbol-specific optimized weights for {symbol} from database.")
        else:
            # 2. Try to load category/target-specific weights from DB
            db_state = db.get_symbol_state(target)
            if db_state and db_state.get("weights"):
                agent_weights = db_state["weights"]
                logger.info(f"   Loaded dynamic optimized weights for {target} from database.")
            else:
                # 3. Fallback to local files
                symbol_cleaned = symbol.replace("/", "_").replace(":", "_").upper()
                opt_weights_path = Path(__file__).parent / f"optimized_weights_{symbol_cleaned}.json"
                
                if opt_weights_path.exists():
                    with open(opt_weights_path) as f:
                        agent_weights = json.load(f)
                    logger.info(f"   Loaded symbol-specific optimized weights for {symbol} ({opt_weights_path.name}).")
                else:
                    opt_weights_path = Path(__file__).parent / f"optimized_weights_{target}.json"
                    if target == "crypto" and not opt_weights_path.exists():
                        # Fallback to general optimized_weights.json for backward compatibility
                        opt_weights_path = Path(__file__).parent / "optimized_weights.json"
                        
                    if opt_weights_path.exists():
                        with open(opt_weights_path) as f:
                            agent_weights = json.load(f)
                        logger.info(f"   Loaded dynamic optimized weights for {target} ({opt_weights_path.name}).")
                    else:
                        logger.info(f"   No optimized weights found in DB or files for {target}. Using static defaults.")
    except Exception as e:
        logger.warning(f"   Failed to resolve/load optimized weights for {symbol}: {e}. Using static defaults.")
            
    verdict: JudgeVerdict = judge_evaluate(agent_signals, agent_weights=agent_weights, symbol=symbol)
    logger.info(f"   Decision: {verdict.decision.value} | Conf={verdict.confidence:.0f}% | "
                f"Agreement={verdict.agreement}/{len(agent_signals)}")

    # Step 4: Risk Agent (with correlation filter)
    logger.info("🛡️  Risk Agent evaluating...")
    risk: RiskDecision = risk_evaluate(
        verdict, snap,
        current_portfolio_heat=portfolio_heat,
        historical_win_rate=win_rate,
        open_positions=open_positions,
        open_position_snaps=open_position_snaps or {},
        daily_pnl_usd=daily_pnl_usd,
        account_size=account_size,
        closed_trades=closed_trades,
    )

    # Step 5: Final decision
    if risk.approved:
        final_action = verdict.decision.value
        logger.success(f"✅ TRADE APPROVED: {final_action} {symbol}")
        logger.success(f"   Entry: {risk.entry_price:.4f} | SL: {risk.stop_loss:.4f} | "
                       f"TP: {risk.take_profit:.4f} | Size: ${risk.position_size_usd:,.0f}")
    else:
        final_action = "NO_TRADE"
        logger.warning(f"❌ TRADE REJECTED ({symbol}): {risk.reason}")

    signal = TradeSignal(
        symbol=symbol,
        asset_type=snap.asset_type,
        timeframe=tf,
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_signals=verdict.agent_reports,
        verdict={
            "decision": verdict.decision.value,
            "confidence": verdict.confidence,
            "agreement": verdict.agreement,
            "disagreement": verdict.disagreement,
            "approved": verdict.approved,
        },
        risk={
            "approved": risk.approved,
            "reason": risk.reason,
            "position_size_pct": risk.position_size_pct,
            "position_size_usd": risk.position_size_usd,
            "stop_loss_pct": risk.stop_loss_pct,
            "take_profit_pct": risk.take_profit_pct,
            "risk_reward": risk.risk_reward,
            "max_loss_usd": risk.max_loss_usd,
            "atr": risk.atr,                    # passed to open_trade for trailing stop ratchet
            "adx": float(getattr(snap, "adx", 0.0) or 0.0),
        },
        final_action=final_action,
        entry_price=risk.entry_price,
        stop_loss=risk.stop_loss if risk.approved else None,
        take_profit=risk.take_profit if risk.approved else None,
        position_size_usd=risk.position_size_usd if risk.approved else None,
        reasoning=verdict.reasoning,
    )

    logger.info(f"{'='*60}\n")
    return signal


def run_all_assets() -> list[TradeSignal]:
    """Scan all configured assets and return signals.
    Passes a shared snapshot cache to enable the correlation filter.
    Also computes today's realized PnL to feed the circuit breaker.
    """
    if settings.trading_mode == "live":
        from trading_engine.execution import live_trader as trader
    else:
        from trading_engine.execution import paper_trader as trader

    results = []
    snap_cache: dict = {}   # symbol -> MarketSnapshot, built as we scan
    all_assets = settings.crypto_assets

    # Filter out any closed assets to prevent pipeline errors / API requests on closed markets
    from trading_engine.market_hours import filter_open_symbols
    all_assets = filter_open_symbols(all_assets)

    # Pre-populate snap_cache with currently-held open positions
    status = trader.get_status()
    portfolio_heat = status["portfolio_heat"] / 100
    open_pos_count = status["open_positions"]
    win_rate = status.get("win_rate", 50) / 100
    account_size = status.get("account_size", settings.account_size)

    # Compute today's realized PnL for the circuit breaker
    daily_pnl = _get_daily_pnl(trader)

    portfolio = trader._load_state()
    closed_trades = portfolio.closed_trades
    running_heat = portfolio_heat
    running_open_count = open_pos_count

    for symbol in all_assets:
        try:
            # Pass snap_cache built so far as the correlation reference
            signal = run(
                symbol,
                portfolio_heat=running_heat,
                open_positions=running_open_count,
                win_rate=win_rate,
                open_position_snaps=snap_cache,
                daily_pnl_usd=daily_pnl,
                account_size=account_size,
                closed_trades=closed_trades,
            )
            results.append(signal)

            # Post signal to dashboard immediately during scan to support real-time updates
            try:
                import requests
                payload = {
                    "symbol": signal.symbol,
                    "asset_type": signal.asset_type,
                    "timeframe": signal.timeframe,
                    "timestamp": signal.timestamp,
                    "agent_signals": signal.agent_signals,
                    "verdict": signal.verdict,
                    "risk": signal.risk,
                    "final_action": signal.final_action,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "position_size_usd": signal.position_size_usd,
                    "reasoning": signal.reasoning,
                }
                requests.post(f"http://localhost:{settings.api_port}/api/signals", json=payload, timeout=1.0)
            except Exception as e_post:
                logger.debug(f"Failed to post real-time signal for {symbol} to dashboard: {e_post}")

            # Update running stats if trade was approved
            if signal.final_action in ("BUY", "SELL"):
                running_open_count += 1
                if signal.risk and signal.risk.get("approved"):
                    stop_loss_pct = float(signal.risk.get("stop_loss_pct", 0.05))
                    size_pct = float(signal.risk.get("position_size_pct", 0.05))
                    running_heat += size_pct * stop_loss_pct

            # After running, add this symbol's snapshot to the cache
            # (so the next symbol in the loop sees it as an existing scan)
            # We rebuild the snap here to capture it — it was built inside run()
            # so we import build_snapshot to get the same object
            try:
                from trading_engine.data.market_data import build_snapshot as _bs
                snap_cache[symbol] = _bs(symbol)
            except Exception:
                pass  # If fetch fails, don't block subsequent assets

        except Exception as e:
            logger.error(f"Pipeline failed for {symbol}: {e}")
    return results

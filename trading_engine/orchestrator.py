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
    sentiment_agent, macro_agent,
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
    """Run all 8 agents concurrently using thread pool."""
    agent_fns = [
        trend_agent.analyze,
        momentum_agent.analyze,
        volume_agent.analyze,
        orderflow_agent.analyze,
        volatility_agent.analyze,
        structure_agent.analyze,
        sentiment_agent.analyze,
        macro_agent.analyze,
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


def run(
    symbol: str,
    timeframe: str = None,
    portfolio_heat: float = 0.0,
    open_positions: int = 0,
    win_rate: float = 0.50,
    open_position_snaps: dict = None,   # symbol -> MarketSnapshot for correlation check
) -> TradeSignal:
    """
    Full pipeline execution for one symbol.
    Returns TradeSignal with complete audit trail.
    """
    tf = timeframe or settings.timeframe
    logger.info(f"\n{'='*60}")
    logger.info(f"TRADING ENGINE | {symbol} | {tf}")
    logger.info(f"{'='*60}")

    # Step 1: Fetch market data + indicators
    logger.info("📊 Fetching market data...")
    snap = build_snapshot(symbol, tf)
    logger.info(f"   Close={snap.close:.4f} | RSI={snap.rsi:.1f} | ATR={snap.atr:.4f}")

    # Step 2: Run all agents in parallel
    logger.info("🤖 Running specialist agents...")
    agent_signals = _run_agents_parallel(snap)

    # Step 3: Judge evaluates
    logger.info("⚖️  Judge evaluating...")
    
    # Try to load optimized weights from Walk-Forward Optimization (only for crypto, as they are optimized on BTC/SOL)
    agent_weights = None
    if snap.asset_type == "crypto":
        opt_weights_path = Path(__file__).parent / "optimized_weights.json"
        if opt_weights_path.exists():
            try:
                with open(opt_weights_path) as f:
                    agent_weights = json.load(f)
                logger.info("   Loaded dynamic walk-forward optimized weights for crypto.")
            except Exception as e:
                logger.warning(f"   Could not load optimized weights: {e}. Using static defaults.")
    else:
        logger.info(f"   Using default static weights for non-crypto asset ({snap.asset_type}).")
            
    verdict: JudgeVerdict = judge_evaluate(agent_signals, agent_weights=agent_weights)
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
        agent_signals=[{
            "agent": s.agent,
            "signal": s.signal.value,
            "confidence": s.confidence,
            "reason": s.reason,
        } for s in agent_signals],
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
    """
    if settings.trading_mode == "live":
        from trading_engine.execution import live_trader as trader
    else:
        from trading_engine.execution import paper_trader as trader

    results = []
    snap_cache: dict = {}   # symbol -> MarketSnapshot, built as we scan
    all_assets = settings.crypto_assets
    if settings.get_massive_api_key:
        all_assets += settings.stock_assets

    # Filter out any closed assets to prevent pipeline errors / API requests on closed markets
    from trading_engine.market_hours import filter_open_symbols
    all_assets = filter_open_symbols(all_assets, extended_stock_hours=settings.extended_cfd_hours)

    # Pre-populate snap_cache with currently-held open positions
    status = trader.get_status()
    portfolio_heat = status["portfolio_heat"] / 100
    open_pos_count = status["open_positions"]
    win_rate = status.get("win_rate", 50) / 100

    for symbol in all_assets:
        try:
            # Pass snap_cache built so far as the correlation reference
            signal = run(
                symbol,
                portfolio_heat=portfolio_heat,
                open_positions=open_pos_count,
                win_rate=win_rate,
                open_position_snaps=snap_cache,
            )
            results.append(signal)

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

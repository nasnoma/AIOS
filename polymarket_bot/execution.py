"""
polymarket_bot/execution.py

Execution engine for Bybit Spot Triangular Arbitrage.
Supports both simulated (paper) and live execution for generic routes.
"""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from loguru import logger

from polymarket_bot.config import settings
from polymarket_bot.state import (
    load_state, save_state, ArbTradeCycle,
    reset_daily_pnl_if_new_day
)
from polymarket_bot.alerts import arbitrage_executed, arbitrage_failed

import ccxt.async_support as ccxt

_exchange = None


def get_bybit_client() -> ccxt.bybit:
    global _exchange
    if _exchange is None:
        _exchange = ccxt.bybit({
            'apiKey': settings.bybit_api_key,
            'secret': settings.bybit_api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            }
        })
        if settings.bybit_testnet:
            _exchange.set_sandbox_mode(True)
    return _exchange


async def close_bybit_client() -> None:
    global _exchange
    if _exchange is not None:
        await _exchange.close()
        _exchange = None


async def execute_arbitrage(opportunity: dict) -> None:
    """
    Executes a triangular arbitrage cycle.
    Handles paper or live modes dynamically based on the opportunity config.
    """
    cycle_id = uuid.uuid4().hex[:8]
    route_name = opportunity.get("route_name", "BTC-ETH")
    direction = opportunity["direction"]
    net_edge = opportunity["net_edge"]
    prices = opportunity["prices"]

    logger.info(f"⚡ [{route_name}] Starting Arbitrage Cycle {cycle_id} ({direction}) | Net Edge: {net_edge:.4%}")

    state = load_state()
    state = reset_daily_pnl_if_new_day(state)

    if settings.trading_mode == "paper":
        # ── PAPER TRADING MODE ──
        # Apply absolute size cap and simulated paper slippage
        size_usdt = min(state.cash * settings.position_size_pct, settings.max_trade_size_usdt)
        realized_net_edge = net_edge - settings.paper_slippage_pct
        pnl = size_usdt * realized_net_edge

        # Update state fields
        state.cash += pnl
        state.account_size = state.cash
        state.total_pnl += pnl
        state.daily_pnl += pnl
        state.cycle_count += 1

        is_win = pnl >= 0
        if is_win:
            state.win_count += 1
            state.avg_win_usd = (
                (state.avg_win_usd * (state.win_count - 1) + pnl) / state.win_count
                if state.win_count > 0 else pnl
            )
        else:
            state.loss_count += 1
            state.avg_loss_usd = (
                (state.avg_loss_usd * (state.loss_count - 1) + pnl) / state.loss_count
                if state.loss_count > 0 else pnl
            )

        # Update route stats
        route_key = f"{route_name}-{direction}"
        if route_key not in state.route_stats:
            state.route_stats[route_key] = {"win_count": 0, "loss_count": 0, "total_pnl": 0.0}
        state.route_stats[route_key]["total_pnl"] += pnl
        if is_win:
            state.route_stats[route_key]["win_count"] += 1
        else:
            state.route_stats[route_key]["loss_count"] += 1

        # Retrieve prices generically
        price_keys = list(prices.keys())
        leg1_p = prices.get(price_keys[0], 0.0) if len(price_keys) > 0 else 0.0
        leg2_p = prices.get(price_keys[1], 0.0) if len(price_keys) > 1 else 0.0
        leg3_p = prices.get(price_keys[2], 0.0) if len(price_keys) > 2 else 0.0

        cycle_record = ArbTradeCycle(
            cycle_id=cycle_id,
            direction=f"{route_name}-{direction}",
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            est_edge_pct=net_edge,
            actual_edge_pct=realized_net_edge,
            size_usdt=size_usdt,
            pnl_usdt=pnl,
            status="completed",
            leg1_price=leg1_p,
            leg2_price=leg2_p,
            leg3_price=leg3_p,
        )

        state.closed_trades.append(cycle_record.to_dict())
        save_state(state)

        logger.success(f"🎉 [{route_name}] Simulated Arb Cycle {cycle_id} Completed! PnL: ${pnl:+.4f} (Simulated Slippage: {settings.paper_slippage_pct:.3%})")
        arbitrage_executed(cycle_id, f"{route_name}-{direction}", size_usdt, pnl, net_edge, mode="PAPER")

    else:
        # ── LIVE TRADING MODE ──
        exchange = get_bybit_client()
        cycle_record = ArbTradeCycle(
            cycle_id=cycle_id,
            direction=f"{route_name}-{direction}",
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at="",
            est_edge_pct=net_edge,
            actual_edge_pct=0.0,
            size_usdt=0.0,
            pnl_usdt=0.0,
            status="failed",
        )

        try:
            # 1. Fetch live USDT balance
            balance = await exchange.fetch_balance()
            free_usdt = float(balance.get('USDT', {}).get('free', 0.0))
            
            # Apply absolute size cap in live mode
            size_usdt = min(free_usdt * settings.position_size_pct, settings.max_trade_size_usdt)
            cycle_record.size_usdt = size_usdt

            if size_usdt < 5.0:
                msg = f"Insufficient USDT balance to trade: {free_usdt:.2f} USDT (min: 5.0)"
                logger.warning(msg)
                arbitrage_failed(cycle_id, f"{route_name}-{direction}", "Balance Check", msg, mode="LIVE")
                return

            logger.info(f"Executing LIVE [{route_name}] {direction} cycle with size {size_usdt:.2f} USDT")

            # 2. Iterate through configured legs dynamically
            free_balance = size_usdt
            for i, leg in enumerate(opportunity["legs"]):
                pair = leg["pair"]
                side = leg["side"]
                curr = leg["currency"]

                # For subsequent legs, retrieve updated balance of target currency
                if i > 0:
                    await asyncio.sleep(0.1)  # small delay for spot book updates
                    balance = await exchange.fetch_balance()
                    free_balance = float(balance.get(curr, {}).get('free', 0.0))

                logger.info(f"Leg {i+1}: Market {side.upper()} {pair} with {free_balance:.6f} {curr}...")
                if side == "buy":
                    await exchange.create_market_buy_order_with_cost(pair, free_balance)
                else:
                    await exchange.create_market_sell_order(pair, free_balance)

            # 3. Compute realized live PnL
            await asyncio.sleep(0.2)
            balance = await exchange.fetch_balance()
            new_usdt = float(balance.get('USDT', {}).get('free', 0.0))
            pnl = new_usdt - free_usdt
            actual_edge = pnl / size_usdt

            # Update state metrics
            state.cash = new_usdt
            state.account_size = new_usdt
            state.total_pnl += pnl
            state.daily_pnl += pnl
            state.cycle_count += 1

            is_win = pnl >= 0
            if is_win:
                state.win_count += 1
                state.avg_win_usd = (
                    (state.avg_win_usd * (state.win_count - 1) + pnl) / state.win_count
                    if state.win_count > 0 else pnl
                )
            else:
                state.loss_count += 1
                state.avg_loss_usd = (
                    (state.avg_loss_usd * (state.loss_count - 1) + pnl) / state.loss_count
                    if state.loss_count > 0 else pnl
                )

            # Update route stats
            route_key = f"{route_name}-{direction}"
            if route_key not in state.route_stats:
                state.route_stats[route_key] = {"win_count": 0, "loss_count": 0, "total_pnl": 0.0}
            state.route_stats[route_key]["total_pnl"] += pnl
            if is_win:
                state.route_stats[route_key]["win_count"] += 1
            else:
                state.route_stats[route_key]["loss_count"] += 1

            cycle_record.completed_at = datetime.now(timezone.utc).isoformat()
            cycle_record.actual_edge_pct = actual_edge
            cycle_record.pnl_usdt = pnl
            cycle_record.status = "completed"

            state.closed_trades.append(cycle_record.to_dict())
            save_state(state)

            logger.success(f"🎉 Live [{route_name}] Arbitrage Completed! PnL: ${pnl:+.4f} USDT")
            arbitrage_executed(cycle_id, f"{route_name}-{direction}", size_usdt, pnl, net_edge, mode="LIVE")

        except Exception as e:
            logger.error(f"❌ Critical Live execution error on cycle {cycle_id}: {e}")
            cycle_record.completed_at = datetime.now(timezone.utc).isoformat()
            cycle_record.reason = str(e)
            state.closed_trades.append(cycle_record.to_dict())
            save_state(state)
            arbitrage_failed(cycle_id, f"{route_name}-{direction}", "Execution Loop", str(e), mode="LIVE")

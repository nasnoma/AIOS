"""
polymarket_bot/execution.py

Execution engine for Bybit Spot Triangular Arbitrage.
Supports both simulated (paper) and live execution.
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

_exchange = None


def get_bybit_client():
    global _exchange
    if _exchange is None:
        import ccxt
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


async def execute_arbitrage(opportunity: dict) -> None:
    """
    Executes a triangular arbitrage cycle.
    Handles paper or live modes.
    """
    cycle_id = uuid.uuid4().hex[:8]
    direction = opportunity["direction"]
    net_edge = opportunity["net_edge"]
    prices = opportunity["prices"]

    logger.info(f"⚡ Starting Arbitrage Cycle {cycle_id} ({direction}) | Net Edge: {net_edge:.4%}")

    state = load_state()
    state = reset_daily_pnl_if_new_day(state)

    if settings.trading_mode == "paper":
        # ── PAPER TRADING MODE ──
        size_usdt = state.cash * settings.position_size_pct
        pnl = size_usdt * net_edge

        # Update state fields
        state.cash += pnl
        state.account_size = state.cash
        state.total_pnl += pnl
        state.daily_pnl += pnl
        state.cycle_count += 1

        is_win = pnl >= 0
        if is_win:
            state.win_count += 1
            # Running Average Win
            state.avg_win_usd = (
                (state.avg_win_usd * (state.win_count - 1) + pnl) / state.win_count
                if state.win_count > 0 else pnl
            )
        else:
            state.loss_count += 1
            # Running Average Loss
            state.avg_loss_usd = (
                (state.avg_loss_usd * (state.loss_count - 1) + pnl) / state.loss_count
                if state.loss_count > 0 else pnl
            )

        cycle_record = ArbTradeCycle(
            cycle_id=cycle_id,
            direction=direction,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            est_edge_pct=net_edge,
            actual_edge_pct=net_edge,
            size_usdt=size_usdt,
            pnl_usdt=pnl,
            status="completed",
            leg1_price=prices.get("BTCUSDT", 0.0) if direction == "FORWARD" else prices.get("ETHUSDT", 0.0),
            leg2_price=prices.get("ETHBTC", 0.0),
            leg3_price=prices.get("ETHUSDT", 0.0) if direction == "FORWARD" else prices.get("BTCUSDT", 0.0),
        )

        state.closed_trades.append(cycle_record.to_dict())
        save_state(state)

        logger.success(f"🎉 Simulated Arb Cycle {cycle_id} Completed! PnL: ${pnl:+.4f}")
        arbitrage_executed(cycle_id, direction, size_usdt, pnl, net_edge, mode="PAPER")

    else:
        # ── LIVE TRADING MODE ──
        exchange = get_bybit_client()
        cycle_record = ArbTradeCycle(
            cycle_id=cycle_id,
            direction=direction,
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
            balance = await asyncio.to_thread(exchange.fetch_balance)
            free_usdt = float(balance.get('USDT', {}).get('free', 0.0))
            size_usdt = free_usdt * settings.position_size_pct
            cycle_record.size_usdt = size_usdt

            if size_usdt < 5.0:
                msg = f"Insufficient USDT balance to trade: {free_usdt:.2f} USDT (min: 5.0)"
                logger.warning(msg)
                arbitrage_failed(cycle_id, direction, "Balance Check", msg, mode="LIVE")
                return

            logger.info(f"Executing LIVE {direction} cycle with size {size_usdt:.2f} USDT")

            if direction == "FORWARD":
                # Leg 1: Buy BTC using USDT
                logger.info("Leg 1: Market Buy BTC/USDT...")
                await asyncio.to_thread(
                    exchange.create_market_buy_order_with_cost,
                    "BTC/USDT",
                    size_usdt
                )

                # Leg 2: Buy ETH using BTC balance
                await asyncio.sleep(0.1)  # tiny delay to allow spot balance to settle
                balance = await asyncio.to_thread(exchange.fetch_balance)
                btc_balance = float(balance.get('BTC', {}).get('free', 0.0))
                logger.info(f"Leg 2: Market Buy ETH/BTC with {btc_balance:.6f} BTC...")
                await asyncio.to_thread(
                    exchange.create_market_buy_order_with_cost,
                    "ETH/BTC",
                    btc_balance
                )

                # Leg 3: Sell ETH for USDT
                await asyncio.sleep(0.1)
                balance = await asyncio.to_thread(exchange.fetch_balance)
                eth_balance = float(balance.get('ETH', {}).get('free', 0.0))
                logger.info(f"Leg 3: Market Sell ETH/USDT for {eth_balance:.6f} ETH...")
                await asyncio.to_thread(
                    exchange.create_market_sell_order,
                    "ETH/USDT",
                    eth_balance
                )

            else:  # REVERSE
                # Leg 1: Buy ETH using USDT
                logger.info("Leg 1: Market Buy ETH/USDT...")
                await asyncio.to_thread(
                    exchange.create_market_buy_order_with_cost,
                    "ETH/USDT",
                    size_usdt
                )

                # Leg 2: Sell ETH for BTC
                await asyncio.sleep(0.1)
                balance = await asyncio.to_thread(exchange.fetch_balance)
                eth_balance = float(balance.get('ETH', {}).get('free', 0.0))
                logger.info(f"Leg 2: Market Sell ETH/BTC for {eth_balance:.6f} ETH...")
                await asyncio.to_thread(
                    exchange.create_market_sell_order,
                    "ETH/BTC",
                    eth_balance
                )

                # Leg 3: Sell BTC for USDT
                await asyncio.sleep(0.1)
                balance = await asyncio.to_thread(exchange.fetch_balance)
                btc_balance = float(balance.get('BTC', {}).get('free', 0.0))
                logger.info(f"Leg 3: Market Sell BTC/USDT for {btc_balance:.6f} BTC...")
                await asyncio.to_thread(
                    exchange.create_market_sell_order,
                    "BTC/USDT",
                    btc_balance
                )

            # 4. Compute actual PnL
            await asyncio.sleep(0.2)
            balance = await asyncio.to_thread(exchange.fetch_balance)
            new_usdt = float(balance.get('USDT', {}).get('free', 0.0))
            pnl = new_usdt - free_usdt
            actual_edge = pnl / size_usdt

            # Update State
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

            cycle_record.completed_at = datetime.now(timezone.utc).isoformat()
            cycle_record.actual_edge_pct = actual_edge
            cycle_record.pnl_usdt = pnl
            cycle_record.status = "completed"

            state.closed_trades.append(cycle_record.to_dict())
            save_state(state)

            logger.success(f"🎉 Live Arbitrage Cycle {cycle_id} Completed! PnL: ${pnl:+.4f} USDT")
            arbitrage_executed(cycle_id, direction, size_usdt, pnl, net_edge, mode="LIVE")

        except Exception as e:
            logger.error(f"❌ Critical Live execution error on cycle {cycle_id}: {e}")
            cycle_record.completed_at = datetime.now(timezone.utc).isoformat()
            cycle_record.reason = str(e)
            state.closed_trades.append(cycle_record.to_dict())
            save_state(state)
            arbitrage_failed(cycle_id, direction, "Execution Loop", str(e), mode="LIVE")

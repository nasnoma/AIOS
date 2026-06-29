"""
polymarket_bot/execution.py

Execution engine for SOL/USDT Spot Grid Market Making.
Handles resting limit order simulation in Paper Mode and live post-only limit orders in Live Mode.
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


async def check_paper_fills(ticker_price: float) -> None:
    """
    Checks active resting paper grid orders against the current price to simulate fills.
    """
    state = load_state()
    state = reset_daily_pnl_if_new_day(state)

    filled_any = False
    retaining_orders = []

    for order in state.open_grid_orders:
        price = float(order["price"])
        size = float(order["size"])
        side = order["side"]
        order_id = order["id"]

        is_filled = False
        if side == "buy" and ticker_price <= price:
            is_filled = True
        elif side == "sell" and ticker_price >= price:
            is_filled = True

        if is_filled:
            filled_any = True
            now_iso = datetime.now(timezone.utc).isoformat()
            
            if side == "buy":
                cost = price * size
                state.cash -= cost
                old_balance = state.asset_balance
                state.asset_balance += size
                # Recalculate average entry price
                if state.asset_balance > 0:
                    state.avg_buy_price = ((old_balance * state.avg_buy_price) + cost) / state.asset_balance
                pnl = 0.0
                route_key = "SOL-BUY"
                logger.success(f"🎉 [Paper Fill] BUY {size:.4f} SOL at {price:.4f} USDT (Avg Entry: {state.avg_buy_price:.2f})")
            else:
                proceeds = price * size
                state.cash += proceeds
                state.asset_balance -= size
                # P&L relative to average buy price
                pnl = (price - state.avg_buy_price) * size
                route_key = "SOL-SELL"
                logger.success(f"🎉 [Paper Fill] SELL {size:.4f} SOL at {price:.4f} USDT | Realized PnL: ${pnl:+.4f} USDT")

                # Update Win/Loss Stats
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

                if route_key not in state.route_stats:
                    state.route_stats[route_key] = {"win_count": 0, "loss_count": 0, "total_pnl": 0.0}
                state.route_stats[route_key]["total_pnl"] += pnl
                if is_win:
                    state.route_stats[route_key]["win_count"] += 1
                else:
                    state.route_stats[route_key]["loss_count"] += 1

            # Log to closed_trades history
            cycle_record = ArbTradeCycle(
                cycle_id=order_id,
                direction=side.upper(),
                started_at=order.get("timestamp", now_iso),
                completed_at=now_iso,
                est_edge_pct=price,
                actual_edge_pct=size,
                size_usdt=price * size,
                pnl_usdt=pnl,
                status="completed",
                leg1_price=price,
            )
            state.closed_trades.append(cycle_record.to_dict())
            arbitrage_executed(order_id, side.upper(), price * size, pnl, price, mode="PAPER")
        else:
            retaining_orders.append(order)

    if filled_any:
        # Calculate new total equity
        state.open_grid_orders = retaining_orders
        state.account_size = state.cash + (state.asset_balance * ticker_price)
        save_state(state)


async def update_resting_grid(target_grid: dict) -> None:
    """
    Cancels existing resting limit orders and places new ones at the target prices.
    Supports both simulated (paper) and live execution.
    """
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    grid_center = target_grid["reservation_price"]

    if settings.trading_mode == "paper":
        logger.info(f"🔄 Replacing PAPER Grid center at {grid_center:.4f} USDT...")
        
        # In Paper mode, reset resting list
        state.open_grid_orders = []
        state.grid_center_price = grid_center
        
        # Load new buy levels
        for level in target_grid["buy_orders"]:
            state.open_grid_orders.append({
                "id": uuid.uuid4().hex[:8],
                "side": "buy",
                "price": level["price"],
                "size": level["size"],
                "timestamp": now_iso
            })
            
        # Load new sell levels
        for level in target_grid["sell_orders"]:
            state.open_grid_orders.append({
                "id": uuid.uuid4().hex[:8],
                "side": "sell",
                "price": level["price"],
                "size": level["size"],
                "timestamp": now_iso
            })
        
        # Enforce equity valuation updates
        state.account_size = state.cash + (state.asset_balance * target_grid["mid_price"])
        save_state(state)
        logger.info(f"🟢 Placed {len(target_grid['buy_orders'])} BUY & {len(target_grid['sell_orders'])} SELL paper grid levels.")

    else:
        # ── LIVE TRADING MODE ──
        exchange = get_bybit_client()
        logger.info(f"🔄 Cancelling & replacing LIVE Bybit orders for SOL/USDT at center {grid_center:.4f}...")
        
        try:
            # 1. Cancel all open orders for SOL/USDT
            await exchange.cancel_all_orders(symbol="SOL/USDT")
            
            # 2. Place new Buy grid levels
            placed_orders = []
            for level in target_grid["buy_orders"]:
                logger.info(f"Placing LIMIT Buy at {level['price']} (Size: {level['size']})")
                order = await exchange.create_order(
                    symbol="SOL/USDT",
                    type="limit",
                    side="buy",
                    amount=level["size"],
                    price=level["price"],
                    params={"timeInForce": "PostOnly"}
                )
                placed_orders.append({
                    "id": order["id"],
                    "side": "buy",
                    "price": level["price"],
                    "size": level["size"],
                    "timestamp": now_iso
                })
                await asyncio.sleep(0.05) # small rate limit safety gap

            # 3. Place new Sell grid levels
            for level in target_grid["sell_orders"]:
                logger.info(f"Placing LIMIT Sell at {level['price']} (Size: {level['size']})")
                order = await exchange.create_order(
                    symbol="SOL/USDT",
                    type="limit",
                    side="sell",
                    amount=level["size"],
                    price=level["price"],
                    params={"timeInForce": "PostOnly"}
                )
                placed_orders.append({
                    "id": order["id"],
                    "side": "sell",
                    "price": level["price"],
                    "size": level["size"],
                    "timestamp": now_iso
                })
                await asyncio.sleep(0.05)

            # 4. Save state
            state.open_grid_orders = placed_orders
            state.grid_center_price = grid_center
            
            # Retrieve live account balances to sync equity
            balances = await exchange.fetch_balance()
            state.cash = float(balances.get('USDT', {}).get('free', 0.0))
            state.asset_balance = float(balances.get('SOL', {}).get('free', 0.0))
            state.account_size = state.cash + (state.asset_balance * target_grid["mid_price"])
            
            save_state(state)
            logger.success(f"🟢 Placed {len(placed_orders)} resting live PostOnly grid orders.")

        except Exception as e:
            logger.error(f"❌ Failed to cancel/replace live orders: {e}")
            arbitrage_failed("GRID-REPLACE", "GRID", 0.0, 0.0, grid_center, mode="LIVE")

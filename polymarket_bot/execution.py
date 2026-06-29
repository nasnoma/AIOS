"""
polymarket_bot/execution.py

Trade executor — paper and live modes.

Paper mode:
  - Fills at current mid-price instantly (zero latency).
  - Tracks virtual portfolio in state.py (JSON).
  - Resolves positions at window close by fetching winning side.

Live mode:
  - Places FOK/GTC limit orders via clob_client.
  - Cancels any unfilled orders 30s before window close.
  - Redemption of winning shares handled post-resolution (Web3).
"""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from polymarket_bot.clob_client import clob_cache
from polymarket_bot.config import settings
from polymarket_bot.market import MarketTokens
from polymarket_bot.risk import RiskDecision
from polymarket_bot.state import (
    OpenPosition, PortfolioState, load_state, reset_daily_pnl_if_new_day, save_state
)
from polymarket_bot.strategy import Signal, SignalType

# Polymarket charges ~1–2¢ per USDC in fees; model conservatively
_FEE_RATE = 0.02   # 2% of position size (includes slippage estimate)


async def execute(
    signal: Signal,
    risk: RiskDecision,
    market: MarketTokens,
) -> Optional[OpenPosition]:
    """
    Execute a trade for an approved signal.
    Returns the opened OpenPosition, or None if execution fails.
    """
    if not risk.approved:
        return None

    if settings.is_live:
        return await _execute_live(signal, risk, market)
    else:
        return await _execute_paper(signal, risk, market)


async def _execute_paper(
    signal: Signal,
    risk: RiskDecision,
    market: MarketTokens,
) -> Optional[OpenPosition]:
    """
    Paper mode: instantly fill at current mid-price, no network call.
    """
    state = load_state()
    state = reset_daily_pnl_if_new_day(state)

    # Determine which side(s) we are buying
    if signal.signal_type == SignalType.SPREAD_ARB:
        side = "BOTH"
    elif signal.signal_type == SignalType.MOMENTUM_LONG:
        side = "YES"
    else:
        side = "NO"

    total_cost = risk.total_size_usd * (1 + _FEE_RATE)
    if state.cash < total_cost:
        logger.warning(f"Paper exec blocked: insufficient cash ({state.cash:.2f} < {total_cost:.2f})")
        return None

    pos = OpenPosition(
        position_id=str(uuid.uuid4())[:8],
        asset=signal.asset,
        token_id_yes=market.token_id_up,
        token_id_no=market.token_id_down,
        signal_type=signal.signal_type.value,
        side=side,
        entry_price_yes=signal.entry_price_yes,
        entry_price_no=signal.entry_price_no,
        size_usd=risk.total_size_usd,
        window_start=market.window_start,
        window_end=market.window_end,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )

    state.cash -= total_cost
    state.positions.append(_pos_to_dict(pos))
    save_state(state)

    logger.success(
        f"📝 PAPER {signal.signal_type.value} | {signal.asset} | side={side} | "
        f"size=${risk.total_size_usd:.2f} | "
        f"YES={signal.entry_price_yes} NO={signal.entry_price_no}"
    )
    return pos


async def _execute_live(
    signal: Signal,
    risk: RiskDecision,
    market: MarketTokens,
) -> Optional[OpenPosition]:
    """
    Live mode: place real limit orders via CLOB.
    """
    placed_orders = []

    if signal.buy_yes and risk.size_usd_yes > 0:
        order_id = await clob_cache.place_limit_order(
            token_id=market.token_id_up,
            side="BUY",
            price=signal.entry_price_yes,
            size=risk.size_usd_yes / signal.entry_price_yes,  # shares = USD / price
            order_type="FOK",
        )
        if order_id:
            placed_orders.append(("YES", order_id))

    if signal.buy_no and risk.size_usd_no > 0:
        order_id = await clob_cache.place_limit_order(
            token_id=market.token_id_down,
            side="BUY",
            price=signal.entry_price_no,
            size=risk.size_usd_no / signal.entry_price_no,
            order_type="FOK",
        )
        if order_id:
            placed_orders.append(("NO", order_id))

    if not placed_orders:
        logger.error(f"Live execution: no orders placed for {signal.asset}")
        return None

    side = "BOTH" if len(placed_orders) == 2 else placed_orders[0][0]
    pos = OpenPosition(
        position_id=str(uuid.uuid4())[:8],
        asset=signal.asset,
        token_id_yes=market.token_id_up,
        token_id_no=market.token_id_down,
        signal_type=signal.signal_type.value,
        side=side,
        entry_price_yes=signal.entry_price_yes,
        entry_price_no=signal.entry_price_no,
        size_usd=risk.total_size_usd,
        window_start=market.window_start,
        window_end=market.window_end,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )

    state = load_state()
    state.positions.append(_pos_to_dict(pos))
    save_state(state)

    logger.success(f"🟢 LIVE {signal.signal_type.value} | {signal.asset} | orders={placed_orders}")
    return pos


async def resolve_expired_positions(current_window_start: int, feed: PriceFeed) -> None:
    """
    Check all open positions. Resolve (close) those whose window has ended.
    Calculates P&L based on the winning side from actual Binance spot strike price resolution.
    """
    state = load_state()
    state = reset_daily_pnl_if_new_day(state)
    changed = False

    for pos_dict in list(state.positions):
        if pos_dict.get("status") != "open":
            continue
        window_end = pos_dict.get("window_end", 0)
        if current_window_start < window_end:
            continue  # Window not yet finished

        # Window is over — determine which side won
        pnl = await _compute_resolution_pnl(pos_dict, feed)
        pos_dict["pnl_usd"] = round(pnl, 4)
        pos_dict["status"] = "closed"
        pos_dict["closed_at"] = datetime.now(timezone.utc).isoformat()

        size = pos_dict.get("size_usd", 0)
        if pnl > 0:
            state.win_count += 1
            # Cumulative moving average for avg win
            state.avg_win_usd = round(
                state.avg_win_usd + (pnl - state.avg_win_usd) / state.win_count, 4
            )
        else:
            state.loss_count += 1
            # Cumulative moving average for avg loss (stored as negative)
            state.avg_loss_usd = round(
                state.avg_loss_usd + (pnl - state.avg_loss_usd) / state.loss_count, 4
            )

        state.total_pnl += pnl
        state.daily_pnl += pnl
        # Return capital to cash (net of fees already deducted at entry)
        state.cash += size + pnl

        state.positions.remove(pos_dict)
        state.closed_trades.append(pos_dict)
        changed = True

        emoji = "✅" if pnl >= 0 else "❌"
        logger.info(
            f"{emoji} Resolved {pos_dict['asset']} | side={pos_dict['side']} | "
            f"size=${size:.2f} | P&L=${pnl:+.2f} | total=${state.total_pnl:+.2f}"
        )

    state.cycle_count += 1
    if changed:
        save_state(state)


async def compute_unrealized_pnl(pos_dict: dict, feed: PriceFeed) -> float:
    """Public helper to compute current unrealized P&L for open positions."""
    return await _compute_resolution_pnl(pos_dict, feed, is_resolution=False)


async def _compute_resolution_pnl(pos_dict: dict, feed: PriceFeed, is_resolution: bool = True) -> float:
    """
    Compute P&L using Binance spot strike prices for deterministic paper resolution (if is_resolution=True)
    or current mid prices for mark-to-market unrealized evaluation (if is_resolution=False).
    """
    side = pos_dict.get("side", "YES")
    size_usd = pos_dict.get("size_usd", 0.0)
    entry_yes = pos_dict.get("entry_price_yes") or 0.5
    entry_no = pos_dict.get("entry_price_no") or 0.5
    asset = pos_dict.get("asset", "")
    window_start = pos_dict.get("window_start", 0)
    window_end = pos_dict.get("window_end", 0)

    token_id_yes = pos_dict.get("token_id_yes", "")
    token_id_no = pos_dict.get("token_id_no", "")
    mid_yes = await clob_cache.get_mid_price(token_id_yes) if token_id_yes else None
    mid_no = await clob_cache.get_mid_price(token_id_no) if token_id_no else None

    # Determine resolution outcome from actual spot feed strike prices
    strike = feed.get_strike(asset, window_start)
    final = feed.get_strike(asset, window_end)

    if strike is not None and final is not None:
        yes_won = final >= strike
    else:
        # Fallback to current mid-prices if strike capture missed (e.g. on startup)
        if mid_yes is not None and mid_no is not None:
            yes_won = mid_yes > mid_no
        else:
            yes_won = entry_yes > 0.5  # Last resort fallback

    if side == "BOTH":
        # Spread arb: one leg wins, one leg loses.
        shares = (size_usd / 2) / ((entry_yes + entry_no) / 2)
        if is_resolution:
            guaranteed_pnl = (1.0 - entry_yes - entry_no) * shares
        else:
            val_yes = mid_yes if mid_yes is not None else entry_yes
            val_no = mid_no if mid_no is not None else entry_no
            guaranteed_pnl = (val_yes + val_no - entry_yes - entry_no) * shares
        return guaranteed_pnl * (1 - _FEE_RATE)

    if side == "YES":
        if is_resolution:
            exit_price = 1.0 if yes_won else 0.0
        else:
            exit_price = mid_yes if mid_yes is not None else entry_yes
        shares = (size_usd / entry_yes) if entry_yes > 0 else 0
        return (exit_price - entry_yes) * shares * (1 - _FEE_RATE)

    if side == "NO":
        if is_resolution:
            exit_price = 0.0 if yes_won else 1.0
        else:
            exit_price = mid_no if mid_no is not None else entry_no
        shares = (size_usd / entry_no) if entry_no > 0 else 0
        return (exit_price - entry_no) * shares * (1 - _FEE_RATE)

    return 0.0


def _pos_to_dict(pos: OpenPosition) -> dict:
    from dataclasses import asdict
    return asdict(pos)

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
    actual_size_usd = 0.0
    fill_price_yes: Optional[float] = None
    fill_price_no: Optional[float] = None

    if signal.buy_yes and risk.size_usd_yes > 0:
        res = await clob_cache.place_limit_order(
            token_id=market.token_id_up,
            side="BUY",
            price=signal.entry_price_yes,
            size=risk.size_usd_yes / signal.entry_price_yes,  # shares = USD / price
            order_type="IOC",
        )
        if res:
            order_id, filled_shares, fill_price_yes = res
            placed_orders.append(("YES", order_id))
            actual_size_usd += filled_shares * fill_price_yes  # use real fill price

    if signal.buy_no and risk.size_usd_no > 0:
        res = await clob_cache.place_limit_order(
            token_id=market.token_id_down,
            side="BUY",
            price=signal.entry_price_no,
            size=risk.size_usd_no / signal.entry_price_no,
            order_type="IOC",
        )
        if res:
            order_id, filled_shares, fill_price_no = res
            placed_orders.append(("NO", order_id))
            actual_size_usd += filled_shares * fill_price_no  # use real fill price

    if not placed_orders:
        logger.info(f"Live execution: no orders filled for {signal.asset} (signals cancelled or expired)")
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
        fill_price_yes=fill_price_yes,
        fill_price_no=fill_price_no,
        size_usd=actual_size_usd,
        window_start=market.window_start,
        window_end=market.window_end,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )

    state = load_state()
    state.positions.append(_pos_to_dict(pos))
    state.cash -= actual_size_usd
    save_state(state)

    logger.success(
        f"🟢 LIVE {signal.signal_type.value} | {signal.asset} | orders={placed_orders} "
        f"| fill_yes={fill_price_yes} fill_no={fill_price_no} | actual_size_usd=${actual_size_usd:.2f}"
    )
    return pos


async def resolve_expired_positions(current_window_start: int, feed) -> None:
    """
    Check all open positions. Resolve (close) those whose window has ended.

    Live mode: places a real SELL order on Polymarket first and uses the actual
    fill price for P&L.  Position is only marked closed after the sell confirms.
    Paper mode: computes P&L from binary Binance-spot outcome (unchanged).
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

        # --- Live mode: sell on Polymarket first ---
        live_exit_price: Optional[float] = None
        if settings.is_live:
            live_exit_price = await _sell_position_live(pos_dict)
            if live_exit_price is None:
                # Sell failed — leave open, retry next cycle
                logger.warning(
                    f"⚠️  SELL failed for {pos_dict['asset']} pos {pos_dict.get('position_id')} "
                    "— position stays open, will retry next cycle"
                )
                continue

        # Window is over — determine P&L
        pnl = await _compute_resolution_pnl(pos_dict, feed, live_exit_price=live_exit_price)
        if pnl is None:
            # Strike data missing and no live exit — skip, retry next cycle
            logger.warning(
                f"⏳ Strike data unavailable for {pos_dict['asset']} — deferring resolution"
            )
            continue

        pos_dict["pnl_usd"] = round(pnl, 4)
        pos_dict["exit_price"] = live_exit_price  # None in paper mode
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


async def _sell_position_live(pos_dict: dict) -> Optional[float]:
    """
    Place an IOC SELL order on Polymarket for an expired position.
    Returns the actual fill price on success, None on failure.

    Strategy: sell the winning/primary leg at current best_bid - 0.01 to ensure
    aggressive fill.  For BOTH positions we sell both legs.
    """
    side = pos_dict.get("side", "YES")
    size_usd = pos_dict.get("size_usd", 0.0)
    entry_yes = pos_dict.get("fill_price_yes") or pos_dict.get("entry_price_yes") or 0.5
    entry_no = pos_dict.get("fill_price_no") or pos_dict.get("entry_price_no") or 0.5
    token_id_yes = pos_dict.get("token_id_yes", "")
    token_id_no = pos_dict.get("token_id_no", "")

    weighted_fill: float = 0.0
    total_usd: float = 0.0

    async def _do_sell(token_id: str, entry_price: float, leg_usd: float) -> Optional[float]:
        """Place IOC sell for one leg; return fill price or None."""
        if not token_id or leg_usd <= 0:
            return None
        book = await clob_cache.get_book(token_id)
        if book is None:
            book = await clob_cache.fetch_book_rest(token_id)
        best_bid = book.best_bid if book else None
        # Sell limit: at best_bid - 0.01 to be aggressive; floor at 0.01
        limit_price = max(0.01, round((best_bid or entry_price) - 0.01, 4))
        shares = round(leg_usd / entry_price, 4)
        if shares <= 0:
            return None
        res = await clob_cache.place_limit_order(
            token_id=token_id,
            side="SELL",
            price=limit_price,
            size=shares,
            order_type="IOC",
        )
        if res is None:
            return None
        _order_id, _filled, fill_price = res
        return fill_price

    if side in ("YES", "BOTH"):
        leg_usd = size_usd / 2 if side == "BOTH" else size_usd
        fp = await _do_sell(token_id_yes, entry_yes, leg_usd)
        if fp is not None:
            weighted_fill += fp * leg_usd
            total_usd += leg_usd
        elif side == "YES":
            return None  # Sell failed

    if side in ("NO", "BOTH"):
        leg_usd = size_usd / 2 if side == "BOTH" else size_usd
        fp = await _do_sell(token_id_no, entry_no, leg_usd)
        if fp is not None:
            weighted_fill += fp * leg_usd
            total_usd += leg_usd
        elif side == "NO":
            return None  # Sell failed

    if total_usd <= 0:
        return None
    return round(weighted_fill / total_usd, 6)  # USD-weighted avg fill price



async def compute_unrealized_pnl(pos_dict: dict, feed) -> float:
    """Public helper to compute current unrealized P&L for open positions."""
    result = await _compute_resolution_pnl(pos_dict, feed, is_resolution=False)
    return result if result is not None else 0.0


async def _compute_resolution_pnl(
    pos_dict: dict,
    feed,
    is_resolution: bool = True,
    live_exit_price: Optional[float] = None,
) -> Optional[float]:
    """
    Compute P&L for a position.

    - live_exit_price: when provided (live mode), use it directly instead of the
      binary 0/1 model.  This is the actual SELL fill price from the CLOB.
    - is_resolution=True + no live_exit_price: paper mode binary outcome (0 or 1).
    - is_resolution=False: mark-to-market unrealized (uses current mid prices).

    Returns None if strike data is missing and we cannot make a reliable determination
    (caller should defer resolution to the next cycle).
    """
    side = pos_dict.get("side", "YES")
    size_usd = pos_dict.get("size_usd", 0.0)
    entry_yes = pos_dict.get("fill_price_yes") or pos_dict.get("entry_price_yes") or 0.5
    entry_no = pos_dict.get("fill_price_no") or pos_dict.get("entry_price_no") or 0.5
    asset = pos_dict.get("asset", "")
    window_start = pos_dict.get("window_start", 0)
    window_end = pos_dict.get("window_end", 0)

    token_id_yes = pos_dict.get("token_id_yes", "")
    token_id_no = pos_dict.get("token_id_no", "")
    mid_yes = await clob_cache.get_mid_price(token_id_yes) if token_id_yes else None
    mid_no = await clob_cache.get_mid_price(token_id_no) if token_id_no else None

    # ── Live mode: use the real SELL fill price ──────────────────────────────
    if live_exit_price is not None:
        # USD-weighted avg exit price; apply to each leg
        if side == "BOTH":
            shares = (size_usd / 2) / ((entry_yes + entry_no) / 2)
            pnl = (live_exit_price * 2 - entry_yes - entry_no) * shares
        elif side == "YES":
            shares = (size_usd / entry_yes) if entry_yes > 0 else 0
            pnl = (live_exit_price - entry_yes) * shares
        else:  # NO
            shares = (size_usd / entry_no) if entry_no > 0 else 0
            pnl = (live_exit_price - entry_no) * shares
        return pnl * (1 - _FEE_RATE)

    # ── Determine binary resolution outcome (paper mode / unrealized) ────────
    strike = feed.get_strike(asset, window_start)
    final = feed.get_strike(asset, window_end)

    if strike is not None and final is not None:
        yes_won = final >= strike
    else:
        # Fallback to current mid-prices if strike capture missed (e.g. on startup)
        if mid_yes is not None and mid_no is not None:
            yes_won = mid_yes > mid_no
        else:
            if is_resolution:
                # Strike data missing and no live exit — refuse to guess
                logger.warning(
                    f"Strike data missing for {asset} (window_start={window_start}) "
                    "and no live exit price — deferring resolution"
                )
                return None
            # Unrealized mark-to-market: use entry side as neutral guess
            yes_won = entry_yes > 0.5

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

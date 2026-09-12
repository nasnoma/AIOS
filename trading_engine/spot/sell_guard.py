"""
Central never-sell-below-buy helpers for the Spot Trading Engine.

Rules:
- A sell price must never be below the relevant buy / cost basis for the lots being sold.
- Live FIFO (and the linked buy fill) beat stale historical cost constants.
- Historical costs are fallback only when live cost is unknown — they must not
  inflate the floor above live FIFO (that pins sells too high and kills cycles).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger


DEFAULT_FEE_FACTOR = 0.0010  # conservative Bybit fee buffer (each side)


def resolve_sell_cost_ref(
    symbol: str,
    *,
    portfolio_avg_cost: float = 0.0,
    units_held: Optional[float] = None,
    sell_qty: Optional[float] = None,
    hist_cost: float = 0.0,
    linked_buy_price: Optional[float] = None,
    current_price: float = 0.0,
) -> float:
    """
    Resolve the minimum cost basis a sell must clear.

    Preference order:
    1) linked_buy_price (exact buy for this cycle), when > 0
    2) FIFO lot cost for the qty being sold (oldest lots first), when available
    3) portfolio avg_cost
    4) hist_cost only if live cost is still unknown
    5) current_price as last-resort floor (never invent a free sell)
    """
    linked = float(linked_buy_price or 0.0)
    if linked > 0:
        return linked

    fifo_avg = 0.0
    fifo_max = 0.0
    try:
        from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis, get_fifo_lot_cost_for_qty

        qty = float(sell_qty or 0.0)
        if qty > 1e-12:
            lot = get_fifo_lot_cost_for_qty(symbol, qty) or {}
            fifo_avg = float(lot.get("avg_cost", 0.0) or 0.0)
            fifo_max = float(lot.get("max_buy_price", 0.0) or 0.0)
        if fifo_avg <= 0:
            fb = get_fifo_cost_basis(symbol, units_held=units_held if units_held and units_held > 0 else None) or {}
            fifo_avg = float(fb.get("avg_cost", 0.0) or 0.0)
            # For full-position exits, use max open lot so no single lot sells at a loss
            # under non-FIFO exchange matching. For partial qty we already used lot helper.
            if qty <= 1e-12:
                fifo_max = float(fb.get("max_buy_price", 0.0) or 0.0)
    except Exception as e:
        logger.debug(f"sell_guard FIFO lookup failed for {symbol}: {e}")

    live = max(float(portfolio_avg_cost or 0.0), fifo_avg, fifo_max)
    if live > 0:
        return live

    hist = float(hist_cost or 0.0)
    if hist > 0:
        return hist

    return float(current_price or 0.0)


def min_fee_proof_sell_price(
    cost_ref: float,
    qty: float,
    *,
    fee_factor: float = DEFAULT_FEE_FACTOR,
    min_net_usd: float = 0.60,
) -> float:
    """Minimum limit sell price that clears cost_ref + fees + min_net_usd."""
    cost_ref = float(cost_ref or 0.0)
    qty = float(qty or 0.0)
    if cost_ref <= 0:
        return 0.0
    if qty <= 1e-12:
        return cost_ref * (1.0 + fee_factor * 2 + 0.009)
    denom = qty * (1.0 - fee_factor)
    if denom <= 0:
        return cost_ref * 1.015
    return (cost_ref * qty * (1.0 + fee_factor) + float(min_net_usd)) / denom


def sell_clears_buy(sell_price: float, cost_ref: float, eps: float = 1e-12) -> bool:
    """True iff sell_price is not below cost_ref (never sell below buy)."""
    if float(cost_ref or 0.0) <= 0:
        return True
    return float(sell_price) + eps >= float(cost_ref)


def enforce_sell_floor(
    sell_price: float,
    cost_ref: float,
    qty: float = 0.0,
    *,
    fee_factor: float = DEFAULT_FEE_FACTOR,
    min_net_usd: float = 0.60,
) -> float:
    """
    Raise sell_price to the never-sell-below-buy / fee-proof floor when needed.
    Returns the safe sell price (may equal input).
    """
    floor = max(
        float(cost_ref or 0.0),
        min_fee_proof_sell_price(cost_ref, qty, fee_factor=fee_factor, min_net_usd=min_net_usd)
        if cost_ref and qty > 0
        else float(cost_ref or 0.0),
    )
    return max(float(sell_price or 0.0), floor)

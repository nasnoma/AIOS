"""
Central never-sell-below-buy + Bybit fee-proof helpers for the Spot Trading Engine.

Rules:
- A sell must never be below the relevant buy / cost basis.
- A completed cycle must remain net-positive after Bybit fees on BOTH sides
  (buy fee + sell fee). Fees must not turn a trade into a loss.
- Live FIFO (and the linked buy fill) beat stale historical cost constants.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger


# Conservative default if config is unavailable (Bybit spot ~0.10% / side)
DEFAULT_FEE_FACTOR = 0.0010
# Extra buffer on top of configured fee_rate for VIP changes / rounding
FEE_SAFETY_MULT = 1.10
DEFAULT_MIN_NET_USD = 0.60


def get_fee_factor() -> float:
    """Per-side fee factor used for floors (config fee_rate with safety buffer)."""
    try:
        from trading_engine.config import spot_settings
        base = float(getattr(spot_settings, "fee_rate", DEFAULT_FEE_FACTOR) or DEFAULT_FEE_FACTOR)
    except Exception:
        base = DEFAULT_FEE_FACTOR
    base = max(base, DEFAULT_FEE_FACTOR)
    return base * FEE_SAFETY_MULT


def estimated_net_pnl(
    sell_price: float,
    cost_ref: float,
    qty: float,
    *,
    fee_factor: Optional[float] = None,
) -> float:
    """Net USD after buy+sell fees: sell*(1-f)*qty - cost*(1+f)*qty."""
    f = float(fee_factor if fee_factor is not None else get_fee_factor())
    sp = float(sell_price or 0.0)
    cp = float(cost_ref or 0.0)
    q = float(qty or 0.0)
    if q <= 0 or sp <= 0 or cp <= 0:
        return 0.0
    return (sp * q * (1.0 - f)) - (cp * q * (1.0 + f))


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

    Preference: linked buy -> FIFO lot cost for qty -> portfolio avg -> hist -> price.
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
            if qty <= 1e-12:
                fifo_max = float(fb.get("max_buy_price", 0.0) or 0.0)
    except Exception as e:
        logger.debug(f"sell_guard FIFO lookup failed for {symbol}: {e}")

    # Prefer FIFO avg for the qty being sold (oldest lots first). Do NOT pin every
    # sell to fifo_max / one expensive lot — that stalls aged-compress and cycles.
    # When qty is unknown, keep max lot as a conservative floor.
    if float(sell_qty or 0.0) > 1e-12 and fifo_avg > 0:
        live = max(float(portfolio_avg_cost or 0.0), fifo_avg)
    else:
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
    fee_factor: Optional[float] = None,
    min_net_usd: float = DEFAULT_MIN_NET_USD,
) -> float:
    """Minimum limit sell price that clears cost_ref + both-side fees + min_net_usd."""
    f = float(fee_factor if fee_factor is not None else get_fee_factor())
    cost_ref = float(cost_ref or 0.0)
    qty = float(qty or 0.0)
    min_net_usd = float(min_net_usd)
    if cost_ref <= 0:
        return 0.0
    if qty <= 1e-12:
        # percent-only floor when qty unknown: 2*fee + small edge
        return cost_ref * (1.0 + (2.0 * f) + 0.003)
    denom = qty * (1.0 - f)
    if denom <= 0:
        return cost_ref * (1.0 + (2.0 * f) + 0.005)
    return (cost_ref * qty * (1.0 + f) + min_net_usd) / denom


def sell_clears_buy(sell_price: float, cost_ref: float, eps: float = 1e-12) -> bool:
    """True iff sell_price is not below cost_ref (gross, before fees)."""
    if float(cost_ref or 0.0) <= 0:
        return True
    return float(sell_price) + eps >= float(cost_ref)


def sell_is_fee_proof(
    sell_price: float,
    cost_ref: float,
    qty: float,
    *,
    fee_factor: Optional[float] = None,
    min_net_usd: float = DEFAULT_MIN_NET_USD,
) -> bool:
    """True iff sell is >= buy AND estimated net after Bybit fees >= min_net_usd."""
    if not sell_clears_buy(sell_price, cost_ref):
        return False
    if float(qty or 0.0) <= 0 or float(cost_ref or 0.0) <= 0:
        # Without qty, require at least 2*fee + tiny edge above cost
        f = float(fee_factor if fee_factor is not None else get_fee_factor())
        return float(sell_price) >= float(cost_ref) * (1.0 + (2.0 * f) + 0.001)
    return estimated_net_pnl(sell_price, cost_ref, qty, fee_factor=fee_factor) + 1e-9 >= float(min_net_usd)


def enforce_sell_floor(
    sell_price: float,
    cost_ref: float,
    qty: float = 0.0,
    *,
    fee_factor: Optional[float] = None,
    min_net_usd: float = DEFAULT_MIN_NET_USD,
) -> float:
    """Raise sell_price to the fee-proof floor (never below buy; never net-negative after fees)."""
    f = float(fee_factor if fee_factor is not None else get_fee_factor())
    floor = max(
        float(cost_ref or 0.0),
        min_fee_proof_sell_price(cost_ref, qty, fee_factor=f, min_net_usd=min_net_usd)
        if cost_ref
        else 0.0,
    )
    return max(float(sell_price or 0.0), floor)

"""
ATOM sell-only exit pin: fee-proof bag floor + HARD $0.50 net.

User 2026-09-22: tighten ATOM closer to fee-proof+$0.50 for a smaller bounce,
never sell at a loss. Pin is recomputed from live FIFO (wallet-trimmed) each build.
"""
from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from trading_engine.spot.sell_guard import (
    HARD_MIN_NET_USD,
    min_fee_proof_sell_price,
    resolve_sell_cost_ref,
)

ATOM_SYMBOL = "ATOM/USDT"
# Allow disable without code edit
_ENV_DISABLE = "SPOT_ATOM_EXIT_PIN"


def atom_pin_enabled() -> bool:
    v = (os.environ.get(_ENV_DISABLE) or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def atom_fee_proof_pin(
    *,
    units_held: float,
    portfolio_avg_cost: float = 0.0,
    sell_qty: Optional[float] = None,
    current_price: float = 0.0,
    min_net_usd: float = 0.50,
) -> float:
    """Minimum limit sell price for ATOM: fee-proof(FIFO bag max) + min_net (default $0.50)."""
    if not atom_pin_enabled():
        return 0.0
    qty = float(sell_qty or 0.0) or max(float(units_held or 0.0), 0.0)
    if qty <= 1e-12 or float(units_held or 0.0) <= 1e-12:
        return 0.0
    cost = resolve_sell_cost_ref(
        ATOM_SYMBOL,
        portfolio_avg_cost=float(portfolio_avg_cost or 0.0),
        units_held=float(units_held or 0.0),
        sell_qty=qty,
        current_price=float(current_price or 0.0),
    )
    if cost <= 0:
        logger.error("[ATOM pin] no live cost_ref — fail-closed (no pin)")
        return 0.0
    net = max(0.50, float(min_net_usd or 0.50), float(HARD_MIN_NET_USD or 0.50))
    pin = float(min_fee_proof_sell_price(cost, qty, min_net_usd=net))
    logger.info(
        f"[ATOM pin] cost_ref=${cost:.4f} qty={qty:.4f} min_net=${net:.2f} → pin=${pin:.4f}"
    )
    return pin

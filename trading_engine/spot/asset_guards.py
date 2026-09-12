"""
Hard asset policy for the Spot engine.

MNT is held solely to earn Bybit's fee discount and must never be sold
(or grid-traded as a normal inventory asset).
"""
from __future__ import annotations

from typing import FrozenSet

# Stablecoins / fee-buffer coins that are never spot-grid sold
NEVER_SELL_SYMBOLS: FrozenSet[str] = frozenset({
    "MNT/USDT",
    "MNT",
    "USDT",
    "USDC",
    "USDC/USDT",
})

FEE_BUFFER_SYMBOLS: FrozenSet[str] = frozenset({
    "MNT/USDT",
    "MNT",
})


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def is_never_sell_symbol(symbol: str) -> bool:
    s = _norm(symbol)
    if s in NEVER_SELL_SYMBOLS:
        return True
    base = s.split("/")[0] if "/" in s else s
    return base in NEVER_SELL_SYMBOLS or f"{base}/USDT" in NEVER_SELL_SYMBOLS


def is_fee_buffer_asset(symbol: str) -> bool:
    s = _norm(symbol)
    if s in FEE_BUFFER_SYMBOLS:
        return True
    base = s.split("/")[0] if "/" in s else s
    return base == "MNT" or s == "MNT/USDT"

"""
trading_engine/agents/carry_agent.py

Delta-Neutral Funding Carry Agent
- Scans live funding rates across perpetual venues
- Detects favorable carry: when funding APY exceeds borrow cost + safety margin
- Returns BUY signal when shorting perp collects funding (funding > 0)
- Returns SELL signal when longing perp collects funding (funding < 0)
- Returns HOLD when carry is too thin to cover costs

This agent does NOT predict price direction. It harvests the structural
flaw in crypto perpetuals: longs persistently overpay funding, and the
short side collects positive expected drift.

Carry math:
  funding_rate (per 8h) → APY = rate × 3 × 365 × 100
  If APY > min_carry_apy + borrow_cost_apy → signal fires
"""
from __future__ import annotations
from typing import Optional
from loguru import logger
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.config import settings


def _fetch_funding_rates(symbol: str) -> dict[str, dict]:
    """
    Fetch current funding rates across all configured venues.
    Returns {venue: {"funding_rate": float, "next_funding_ms": int, "mark_price": float}}
    """
    import ccxt
    venues = {}
    perp_sym = f"{symbol}:USDT" if ":" not in symbol else symbol
    spot_sym = symbol.split(":")[0]

    for ex_id in ["binance", "bybit"]:
        try:
            ex = getattr(ccxt, ex_id)()
            fr = ex.fetch_funding_rate(perp_sym)
            rate = fr.get("fundingRate")
            if rate is not None:
                venues[ex_id] = {
                    "funding_rate": float(rate),
                    "next_funding_ms": fr.get("fundingDatetime"),
                    "mark_price": float(fr.get("markPrice", 0) or 0),
                }
        except Exception as e:
            logger.debug(f"carry_agent: {ex_id} funding fetch failed for {perp_sym}: {e}")
    return venues


def _apy_from_rate(rate: float) -> float:
    """Convert per-8h funding rate to annualized percentage yield."""
    return rate * 3 * 365 * 100  # 3 payments/day × 365 days


def analyze(snap) -> AgentSignal:
    """
    Scan funding rates for carry opportunity.
    Returns AgentSignal with carry-specific metadata.
    """
    symbol = snap.symbol.split(":")[0]

    if not getattr(settings, "carry_enabled", False):
        return AgentSignal(
            agent="carry",
            signal=Signal.HOLD,
            confidence=50.0,
            reason="Carry strategy disabled in config.",
            raw_data={},
        )

    min_apy = float(getattr(settings, "carry_min_apy", 15.0))
    borrow_cost_apy = float(getattr(settings, "carry_borrow_cost_apy", 5.0))
    margin_buffer_pct = float(getattr(settings, "carry_margin_buffer_pct", 0.30))

    venues = _fetch_funding_rates(symbol)
    if not venues:
        return AgentSignal(
            agent="carry",
            signal=Signal.HOLD,
            confidence=50.0,
            reason=f"No funding data available for {symbol}.",
            raw_data={"venues": {}},
        )

    # Find the venue with the best short-leg carry (highest positive funding)
    best_short_venue = None
    best_short_apy = 0.0
    best_short_rate = 0.0
    for ex_id, data in venues.items():
        rate = data["funding_rate"]
        apy = _apy_from_rate(rate)
        if apy > best_short_apy:
            best_short_apy = apy
            best_short_venue = ex_id
            best_short_rate = rate

    # Find the venue with the best long-leg carry (most negative funding = longs collect)
    best_long_venue = None
    best_long_apy = 0.0
    best_long_rate = 0.0
    for ex_id, data in venues.items():
        rate = data["funding_rate"]
        apy = -_apy_from_rate(rate)  # negative funding → long collects
        if apy > best_long_apy:
            best_long_apy = apy
            best_long_venue = ex_id
            best_long_rate = rate

    # Determine which leg is better
    net_short_carry = best_short_apy - borrow_cost_apy
    net_long_carry = best_long_apy - borrow_cost_apy

    # Cross-exchange basis: short on high-funding venue, long on low-funding venue
    cross_basis_apy = 0.0
    cross_legs = None
    if len(venues) >= 2:
        sorted_venues = sorted(venues.items(), key=lambda x: x[1]["funding_rate"], reverse=True)
        high_funding_ex, high_data = sorted_venues[0]
        low_funding_ex, low_data = sorted_venues[-1]
        spread = high_data["funding_rate"] - low_data["funding_rate"]
        cross_basis_apy = _apy_from_rate(spread)
        if cross_basis_apy > min_apy:
            cross_legs = {
                "short_venue": high_funding_ex,
                "long_venue": low_funding_ex,
                "spread_rate": spread,
            }

    # ── Signal logic: pick the best carry structure ───────────────────
    # Priority: cross-exchange basis > single-exchange short > single-exchange long
    best_carry_apy = 0.0
    best_structure = None
    best_direction = Signal.HOLD
    best_reason = ""

    if cross_legs and cross_basis_apy > min_apy:
        best_carry_apy = cross_basis_apy
        best_structure = "cross_exchange_basis"
        best_direction = Signal.BUY  # "open carry position" — we treat as BUY for judge compatibility
        best_reason = (
            f"Cross-exchange basis carry: short {cross_legs['short_venue']} "
            f"(funding {venues[cross_legs['short_venue']]['funding_rate']*100:.4f}%) "
            f"+ long {cross_legs['long_venue']} "
            f"(funding {venues[cross_legs['long_venue']]['funding_rate']*100:.4f}%) "
            f"= {cross_basis_apy:.1f}% APY spread."
        )
    elif net_short_carry > min_apy:
        best_carry_apy = net_short_carry
        best_structure = "short_perp"
        best_direction = Signal.SELL  # short perp to collect positive funding
        best_reason = (
            f"Short-leg carry on {best_short_venue}: funding={best_short_rate*100:.4f}%/8h "
            f"({best_short_apy:.1f}% APY) - {borrow_cost_apy:.1f}% borrow = "
            f"{net_short_carry:.1f}% net APY."
        )
    elif net_long_carry > min_apy:
        best_carry_apy = net_long_carry
        best_structure = "long_perp"
        best_direction = Signal.BUY  # long perp to collect negative funding
        best_reason = (
            f"Long-leg carry on {best_long_venue}: funding={best_long_rate*100:.4f}%/8h "
            f"({best_long_apy:.1f}% APY to long) - {borrow_cost_apy:.1f}% borrow = "
            f"{net_long_carry:.1f}% net APY."
        )
    else:
        best_reason = (
            f"No carry edge: best short {best_short_apy:.1f}% APY, "
            f"best long {best_long_apy:.1f}% APY, basis {cross_basis_apy:.1f}% APY. "
            f"All below {min_apy:.1f}% min threshold."
        )

    # Confidence scales with APY (higher APY = more conviction)
    confidence = min(95.0, 50.0 + best_carry_apy * 0.8) if best_carry_apy > 0 else 50.0

    raw_data = {
        "venues": {k: {"funding_rate": v["funding_rate"], "apy": _apy_from_rate(v["funding_rate"])}
                   for k, v in venues.items()},
        "best_structure": best_structure,
        "best_carry_apy": round(best_carry_apy, 2),
        "cross_basis_apy": round(cross_basis_apy, 2),
        "borrow_cost_apy": borrow_cost_apy,
        "margin_buffer_pct": margin_buffer_pct,
    }
    if cross_legs:
        raw_data["cross_legs"] = cross_legs

    return AgentSignal(
        agent="carry",
        signal=best_direction,
        confidence=round(confidence, 1),
        reason=best_reason,
        raw_data=raw_data,
    )

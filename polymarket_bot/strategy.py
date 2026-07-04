"""
polymarket_bot/strategy.py

Core signal logic — pure functions, no I/O.
Fully unit-testable without any network access.

Two signals:
  SPREAD_ARB   — YES price + NO price < threshold → buy both legs (guaranteed arb)
  MOMENTUM     — BTC moves strongly; underpriced leg detected → directional sniping
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from polymarket_bot.clob_client import OrderBook
from polymarket_bot.config import Settings


class SignalType(str, Enum):
    NO_SIGNAL = "NO_SIGNAL"
    SPREAD_ARB = "SPREAD_ARB"      # Buy both legs
    MOMENTUM_LONG = "MOMENTUM_LONG"  # Buy YES (UP)
    MOMENTUM_SHORT = "MOMENTUM_SHORT"  # Buy NO (DOWN)


@dataclass
class Signal:
    signal_type: SignalType
    asset: str

    # Prices and sizing hints
    buy_yes: bool = False
    buy_no: bool = False
    entry_price_yes: Optional[float] = None
    entry_price_no: Optional[float] = None
    yes_no_sum: Optional[float] = None      # spread arb: combined price
    implied_prob: Optional[float] = None    # momentum: implied prob on favoured side
    momentum_usd: Optional[float] = None   # BTC $ move that triggered signal
    confidence: float = 0.0               # 0–1

    # Window context
    elapsed_s: float = 0.0
    remaining_s: float = 0.0

    @property
    def description(self) -> str:
        if self.signal_type == SignalType.SPREAD_ARB:
            return (
                f"SPREAD_ARB {self.asset}: YES={self.entry_price_yes:.3f} "
                f"NO={self.entry_price_no:.3f} sum={self.yes_no_sum:.3f} "
                f"(profit={1.0 - self.yes_no_sum:.3f}/share)"
            )
        if self.signal_type in (SignalType.MOMENTUM_LONG, SignalType.MOMENTUM_SHORT):
            side = "UP/YES" if self.signal_type == SignalType.MOMENTUM_LONG else "DOWN/NO"
            return (
                f"{self.signal_type.value} {self.asset}: {side} @ {self.entry_price_yes or self.entry_price_no:.3f} "
                f"| momentum=${self.momentum_usd:+.2f} "
                f"| implied_prob={self.implied_prob:.2%} | conf={self.confidence:.0%}"
            )
        return f"NO_SIGNAL {self.asset}"


def evaluate_signals(
    asset: str,
    book_yes: Optional[OrderBook],
    book_no: Optional[OrderBook],
    momentum_usd: Optional[float],
    elapsed_s: float,
    remaining_s: float,
    cfg: Settings,
    cvd_delta: Optional[float] = None,
) -> Signal:
    """
    Main strategy evaluation function.
    Checks signals in priority order: entry window → spread arb → momentum.

    Args:
        asset:          "BTC" | "ETH" | "SOL"
        book_yes:       Current order book for the YES (UP) token.
        book_no:        Current order book for the NO/DOWN token.
        momentum_usd:   BTC price change over lookback window (positive = rising).
        elapsed_s:      Seconds since window start.
        remaining_s:    Seconds until window close.
        cfg:            Strategy configuration (thresholds).

    Returns:
        Signal with signal_type set appropriately.
    """
    _no = Signal(signal_type=SignalType.NO_SIGNAL, asset=asset, elapsed_s=elapsed_s, remaining_s=remaining_s)

    # ── Guard: must be within the entry window ──────────────────────────────
    if not (cfg.entry_window_min_s <= elapsed_s <= cfg.entry_window_max_s):
        return _no

    # ── Require valid order books ───────────────────────────────────────────
    if not book_yes or not book_no:
        return _no

    mid_yes = book_yes.mid_price
    mid_no = book_no.mid_price
    if mid_yes is None or mid_no is None:
        return _no

    # ── Priority 1: Spread Arbitrage ────────────────────────────────────────
    yes_no_sum = mid_yes + mid_no
    if yes_no_sum < cfg.spread_arb_threshold:
        # Both legs must be buyable below max price
        if mid_yes <= cfg.max_entry_price and mid_no <= cfg.max_entry_price:
            guaranteed_profit = 1.0 - yes_no_sum
            confidence = min(1.0, guaranteed_profit / (1.0 - cfg.spread_arb_threshold))
            return Signal(
                signal_type=SignalType.SPREAD_ARB,
                asset=asset,
                buy_yes=True,
                buy_no=True,
                entry_price_yes=mid_yes,
                entry_price_no=mid_no,
                yes_no_sum=round(yes_no_sum, 4),
                confidence=round(confidence, 4),
                elapsed_s=elapsed_s,
                remaining_s=remaining_s,
            )

    # ── Priority 2: Momentum Sniping ────────────────────────────────────────
    if momentum_usd is None:
        return _no

    # Scale momentum threshold based on the asset price (BTC vs ETH)
    threshold = cfg.momentum_threshold_usd
    if asset == "ETH":
        # Scale proportionally (e.g. ~$2.13 for ETH if BTC threshold is $80.00)
        threshold = round(cfg.momentum_threshold_usd * (1600.0 / 60000.0), 2)
        threshold = max(5.00, threshold)  # Require at least $5.00 move on ETH

    abs_momentum = abs(momentum_usd)
    if abs_momentum < threshold:
        return _no

    # ── Time-decay weight ───────────────────────────────────────────────────
    # A move at minute 5 of 60 is weak signal; same move at minute 50 is strong.
    # Block entries in the first 10% of the window (too early — noise dominates).
    # Weight rises linearly from 0.3 at 10% elapsed → 1.0 at 80%+ elapsed.
    window_duration = elapsed_s + remaining_s
    pct_elapsed = elapsed_s / window_duration if window_duration > 0 else 0.0
    
    if window_duration <= 600.0:
        # Short windows (5-10m): no time-decay, early entries are better
        time_weight = 1.0
    else:
        if pct_elapsed < 0.10:
            return _no  # Too early — insufficient price discovery
        time_weight = min(1.0, max(0.3, (pct_elapsed - 0.10) / 0.70))

    # Determine which side is favoured by momentum
    if momentum_usd > 0:
        # Rising asset → UP/YES leg favoured → check if YES is underpriced
        signal_type = SignalType.MOMENTUM_LONG
        entry_price = mid_yes
        implied_prob = mid_yes  # YES price ≈ implied probability of UP
    else:
        # Falling asset → DOWN/NO leg favoured → check if NO is underpriced
        signal_type = SignalType.MOMENTUM_SHORT
        entry_price = mid_no
        implied_prob = mid_no   # NO price ≈ implied probability of DOWN

    # Tighter Filters Live Guardrail:
    # 1. Momentum must be at least the asset threshold (already filtered above)
    # 2. Favoured leg price (implied prob) must be at least 0.50 (i.e. YES >= 0.50 or YES <= 0.50)
    if implied_prob < 0.50:
        return _no

    # Enforce minimum implied probability (min_confidence) strictly to filter out coin flips
    if implied_prob < cfg.min_confidence:
        return _no  # Market doesn't agree strongly enough
    if entry_price > cfg.max_entry_price:
        return _no  # Entry price too high — limited upside

    # Confidence: momentum strength × time-decay weight × probability alignment
    momentum_confidence = min(1.0, abs_momentum / (threshold * 3))
    if implied_prob >= cfg.min_confidence:
        prob_confidence = (implied_prob - cfg.min_confidence) / (1.0 - cfg.min_confidence)
    else:
        prob_confidence = 0.0
    # Apply time_weight: early signals are penalised on hourly, but not on short windows
    combined_confidence = round(((momentum_confidence + prob_confidence) / 2) * time_weight, 4)

    # Enforce minimum signal confidence strictly
    if combined_confidence < cfg.min_signal_confidence:
        return _no

    # ── CVD Confirmation (optional boost/penalty) ───────────────────────────
    # CVD direction should agree with momentum. Agreement boosts confidence;
    # strong disagreement penalises it.
    if cvd_delta is not None and cvd_delta != 0.0:
        cvd_agrees = (momentum_usd > 0 and cvd_delta > 0) or (momentum_usd < 0 and cvd_delta < 0)
        if cvd_agrees:
            combined_confidence = min(1.0, combined_confidence * 1.25)
        else:
            combined_confidence = combined_confidence * 0.85
        combined_confidence = round(combined_confidence, 4)

        # Re-check minimum after CVD adjustment
        if combined_confidence < cfg.min_signal_confidence and not is_strong_momentum:
            return _no

    buy_yes = signal_type == SignalType.MOMENTUM_LONG
    buy_no = signal_type == SignalType.MOMENTUM_SHORT

    return Signal(
        signal_type=signal_type,
        asset=asset,
        buy_yes=buy_yes,
        buy_no=buy_no,
        entry_price_yes=mid_yes if buy_yes else None,
        entry_price_no=mid_no if buy_no else None,
        implied_prob=round(implied_prob, 4),
        momentum_usd=round(momentum_usd, 2),
        confidence=combined_confidence,
        elapsed_s=elapsed_s,
        remaining_s=remaining_s,
    )

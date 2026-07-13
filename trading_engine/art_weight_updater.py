"""
trading_engine/art_weight_updater.py

ART (Automated Reinforcement from Trade-outcomes) weight updater.

After every closed position, each agent that voted is graded:
  - Agent agreed with trade direction AND trade was profitable  → weight × 1.05 (boost)
  - Agent disagreed with direction OR trade was stopped out     → weight × 0.95 (decay)

Weights are bounded to [0.5, 3.0] so no agent is ever silenced or dominates.
Writes atomically to judge_weights.json to prevent corruption on concurrent closes.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from trading_engine.execution.live_trader import Position

WEIGHTS_PATH = Path(__file__).parent / "autoresearch" / "params" / "judge_weights.json"

# ART hyper-parameters
BOOST_FACTOR = 1.05   # winning agent weight multiplier
DECAY_FACTOR = 0.95   # losing agent weight multiplier
MIN_WEIGHT   = 0.5    # floor — ensures every agent retains some voice
MAX_WEIGHT   = 3.0    # ceiling — prevents runaway dominance
MIN_SIGNALS  = 1      # skip update if fewer than this many agent signals stored


def _load_weights() -> dict:
    """Load current weights dict from disk, returning defaults on failure."""
    try:
        with open(WEIGHTS_PATH) as f:
            data = json.load(f)
        return data.get("weights", {})
    except Exception as e:
        logger.warning(f"ART: Could not load weights from {WEIGHTS_PATH}: {e}")
        return {}


def _save_weights(weights: dict) -> None:
    """Atomically write updated weights back to judge_weights.json."""
    try:
        with open(WEIGHTS_PATH) as f:
            full_data = json.load(f)
    except Exception:
        full_data = {"min_agreement": 6, "min_avg_confidence": 59.0}

    full_data["weights"] = weights

    # Atomic write: write to temp file then rename to prevent partial writes
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=WEIGHTS_PATH.parent, prefix=".judge_weights_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(full_data, f, indent=2)
        os.replace(tmp_path, WEIGHTS_PATH)
    except Exception as e:
        logger.error(f"ART: Failed to write weights: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def update_weights_from_outcome(pos: "Position") -> None:
    """
    Grade each agent that voted on this trade and update weights accordingly.

    Called immediately after a position is finalized (win or loss).
    Safe to call even if pos.agent_signals is empty (no-op).
    """
    agent_signals: list = getattr(pos, "agent_signals", [])
    if not agent_signals or len(agent_signals) < MIN_SIGNALS:
        logger.debug(f"ART: Skipping weight update for {pos.symbol} — no agent signals stored.")
        return

    pnl = pos.pnl_usd or 0.0
    profitable = pnl > 0
    direction = pos.direction  # 'long' | 'short'
    expected_signal = "BUY" if direction == "long" else "SELL"

    weights = _load_weights()
    if not weights:
        logger.warning("ART: Empty weights loaded — skipping update.")
        return

    old_weights = dict(weights)
    changes: list[str] = []

    for signal_record in agent_signals:
        agent_name = signal_record.get("agent", "")
        agent_voted = signal_record.get("signal", "HOLD").upper()

        if agent_name not in weights:
            continue  # unknown agent, skip

        agreed = (agent_voted == expected_signal)

        # Grade the agent:
        # - agreed AND profitable  → correct call → boost
        # - agreed AND not profit  → wrong entry despite agreement → decay
        # - disagreed AND profit   → agent was right to warn → boost
        # - disagreed AND not profit → agent correctly warned → boost (it was right)
        if agreed and profitable:
            factor = BOOST_FACTOR
            grade = "✅ correct"
        elif agreed and not profitable:
            factor = DECAY_FACTOR
            grade = "❌ wrong"
        elif not agreed and profitable:
            # Agent said HOLD/opposite but trade still won → agent was overcautious
            factor = DECAY_FACTOR
            grade = "⚠️ overcautious"
        else:
            # Agent disagreed AND trade lost → agent was right to be cautious
            factor = BOOST_FACTOR
            grade = "✅ correctly cautious"

        new_weight = round(
            max(MIN_WEIGHT, min(MAX_WEIGHT, weights[agent_name] * factor)),
            4,
        )
        delta = new_weight - weights[agent_name]
        weights[agent_name] = new_weight
        changes.append(
            f"  {agent_name}: {old_weights[agent_name]:.3f} → {new_weight:.3f} "
            f"({delta:+.3f}) [{grade}]"
        )

    outcome_str = f"WIN +${pnl:.2f}" if profitable else f"LOSS -${abs(pnl):.2f}"
    logger.info(
        f"🧠 ART Weight Update — {pos.symbol} {direction.upper()} [{outcome_str}]\n"
        + "\n".join(changes)
    )

    _save_weights(weights)

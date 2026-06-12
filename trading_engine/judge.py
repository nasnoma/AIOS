"""
trading_engine/judge.py

Judge Agent — Weighted Ensemble Voter
- Receives all 8 agent signals
- Applies confidence-weighted voting (not simple majority)
- Uses per-agent weights that can be updated from historical accuracy
- Optionally calls LLM to produce a human-readable reasoning summary
- Hard threshold: must meet MIN_AGENT_AGREEMENT + MIN_AVG_CONFIDENCE
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Optional
from loguru import logger

from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.config import settings
from trading_engine.utils.llm import call_llm


# Default weights per agent (can be updated from DB track record)
DEFAULT_WEIGHTS = {
    "trend":      1.4,   # High weight — primary direction filter
    "momentum":   1.2,
    "volume":     1.1,
    "orderflow":  1.3,   # High weight for crypto — smart money signal
    "volatility": 0.8,   # Lower weight — regime filter, not direction
    "structure":  1.2,
    "sentiment":  0.9,   # LLM-based — slightly lower trust
    "macro":      1.0,
}


@dataclass
class JudgeVerdict:
    decision: Signal
    confidence: float
    agreement: int           # number of agents agreeing with final decision
    disagreement: int
    weighted_score: float
    reasoning: str
    agent_reports: list[dict]
    approved: bool           # False if thresholds not met


def _llm_explain(agents: list[AgentSignal], decision: Signal, confidence: float) -> str:
    """Call LLM to generate a concise explanation. Fallback to rule-based summary."""
    reports = [{"agent": a.agent, "signal": a.signal.value,
                "confidence": a.confidence, "reason": a.reason} for a in agents]
    prompt = f"""You are JudgeAgent. You never look at charts — only agent reports.

Agent reports:
{json.dumps(reports, indent=2)}

Final decision: {decision.value} with {confidence:.0f}% confidence.

Write a concise 2-sentence explanation of WHY this decision was made,
highlighting the strongest agreeing agents and any notable disagreements.
Be specific about indicator values where mentioned."""

    try:
        return call_llm(prompt)
    except Exception as e:
        logger.warning(f"Judge LLM explanation failed: {e}")

    # Fallback: rule-based summary
    agreeing = [a.agent for a in agents if a.signal == decision]
    return (f"Decision: {decision.value}. Agreeing agents: {', '.join(agreeing)}. "
            f"Weighted confidence: {confidence:.0f}%.")


def evaluate(agents: list[AgentSignal], agent_weights: dict[str, float] = None) -> JudgeVerdict:
    """
    Run the weighted ensemble vote.
    Returns JudgeVerdict with approved=True only if thresholds are met.
    """
    weights = agent_weights or DEFAULT_WEIGHTS

    buy_score = 0.0
    sell_score = 0.0
    hold_score = 0.0
    total_weight = 0.0
    agreement_with = {"BUY": 0, "SELL": 0, "HOLD": 0}

    reports = []
    for agent in agents:
        w = weights.get(agent.agent, 1.0) * agent.weight
        weighted_conf = (agent.confidence / 100) * w

        if agent.signal == Signal.BUY:
            buy_score += weighted_conf
            agreement_with["BUY"] += 1
        elif agent.signal == Signal.SELL:
            sell_score += weighted_conf
            agreement_with["SELL"] += 1
        else:
            hold_score += weighted_conf
            agreement_with["HOLD"] += 1

        total_weight += w
        reports.append({
            "agent": agent.agent,
            "signal": agent.signal.value,
            "confidence": agent.confidence,
            "reason": agent.reason,
            "weight": round(w, 2),
        })

    # Normalize
    if total_weight == 0:
        return JudgeVerdict(
            decision=Signal.HOLD, confidence=0, agreement=0, disagreement=8,
            weighted_score=0, reasoning="No agent data", agent_reports=reports, approved=False
        )

    buy_norm = buy_score / total_weight
    sell_norm = sell_score / total_weight

    # Determine decision
    if buy_norm > sell_norm and buy_norm > hold_score / total_weight:
        decision = Signal.BUY
        confidence = round(buy_norm * 100, 1)
        agreement = agreement_with["BUY"]
        disagreement = len(agents) - agreement
    elif sell_norm > buy_norm and sell_norm > hold_score / total_weight:
        decision = Signal.SELL
        confidence = round(sell_norm * 100, 1)
        agreement = agreement_with["SELL"]
        disagreement = len(agents) - agreement
    else:
        decision = Signal.HOLD
        confidence = round(hold_score / total_weight * 100, 1)
        agreement = agreement_with["HOLD"]
        disagreement = len(agents) - agreement

    # ── Threshold check ────────────────────────────────
    min_agreement = settings.min_agent_agreement
    min_confidence = settings.min_avg_confidence
    avg_raw_confidence = sum(a.confidence for a in agents) / len(agents) if agents else 0

    approved = (
        agreement >= min_agreement
        and avg_raw_confidence >= min_confidence
        and decision != Signal.HOLD
    )

    logger.info(
        f"Judge: {decision.value} | conf={confidence:.1f}% | "
        f"agree={agreement}/{len(agents)} | approved={approved} | "
        f"avg_raw_conf={avg_raw_confidence:.1f}% (min_conf={min_confidence})"
    )

    # Optional LLM explanation
    reasoning = _llm_explain(agents, decision, confidence)

    return JudgeVerdict(
        decision=decision,
        confidence=confidence,
        agreement=agreement,
        disagreement=disagreement,
        weighted_score=buy_norm if decision == Signal.BUY else sell_norm,
        reasoning=reasoning,
        agent_reports=reports,
        approved=approved,
    )

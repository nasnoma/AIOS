"""
polymarket_bot/ai_advisor.py

OpenRouter async LLM advisor — non-blocking, out-of-band.

NEVER called in the trade hot path.
Runs as a background asyncio task every N completed cycles.
Analyses recent P&L and suggests threshold adjustments.
Recommendations are logged (and optionally auto-applied if LLM_AUTO_TUNE=true).
"""
from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from polymarket_bot.config import settings


# ── Structured output schema ──────────────────────────────────────────────────

class ThresholdRecommendation(BaseModel):
    spread_arb_threshold: Optional[float] = Field(
        default=None, ge=0.90, le=1.00,
        description="Recommended spread_arb_threshold. Null = no change."
    )
    momentum_threshold_usd: Optional[float] = Field(
        default=None, ge=1.0,
        description="Recommended momentum_threshold_usd. Null = no change."
    )
    min_confidence: Optional[float] = Field(
        default=None, ge=0.50, le=1.00,
        description="Recommended min_confidence. Null = no change."
    )
    rationale: str = Field(default="", description="Brief explanation of recommendations.")
    risk_level: str = Field(default="medium", description="Current risk assessment: low | medium | high")


_SYSTEM_PROMPT = """You are a quantitative trading advisor for a Polymarket 5-minute binary prediction market bot.
The bot trades BTC/ETH Up/Down contracts using two strategies:
1. Spread Arbitrage: buy both YES+NO legs when their combined price < threshold (e.g. 0.98).
2. Momentum Sniping: buy the underpriced leg when BTC moves strongly in one direction.

You will be given recent performance stats and current strategy parameters.
Provide CONSERVATIVE threshold recommendations to improve win rate and edge.
Only suggest changes if there is clear evidence of suboptimal performance.
Never suggest aggressive parameters that could increase risk.
Return valid JSON matching the ThresholdRecommendation schema."""

_USER_TEMPLATE = """Recent performance (last {cycles} completed windows):
- Win rate: {win_rate:.1%}
- Total P&L: ${total_pnl:+.2f}
- Daily P&L: ${daily_pnl:+.2f}
- Win count: {wins} | Loss count: {losses}
- Trades this session: {trades}

Current strategy parameters:
- spread_arb_threshold: {spread_arb}
- momentum_threshold_usd: ${momentum}
- min_confidence: {confidence:.0%}
- max_entry_price: {max_entry:.0%}
- entry_window: {win_min}s – {win_max}s

Analyse performance and recommend any parameter adjustments.
Only suggest changes if win rate < 52% or you see a clear pattern.
If performance is acceptable, return null for all threshold fields."""


class AiAdvisor:
    """
    Background async LLM advisor using OpenRouter.
    Fire-and-forget: does not block the trade loop.
    """

    def __init__(self):
        self._client: Optional[AsyncOpenAI] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_recommendation: Optional[ThresholdRecommendation] = None

    def _get_client(self) -> Optional[AsyncOpenAI]:
        if not settings.openrouter_api_key or settings.openrouter_api_key.startswith("sk-or-..."):
            return None
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=settings.openrouter_api_key,
            )
        return self._client

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._advisor_loop(), name="ai_advisor")
        logger.info(f"AiAdvisor started (interval: every {settings.llm_advisor_interval_cycles} cycles)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _advisor_loop(self) -> None:
        """
        Polls cycle_count from state.py.
        Fires LLM call every LLM_ADVISOR_INTERVAL_CYCLES completed windows.
        """
        from polymarket_bot.state import load_state
        last_cycle = 0
        interval = settings.llm_advisor_interval_cycles

        while self._running:
            await asyncio.sleep(30)  # Check every 30s
            try:
                state = load_state()
                if state.cycle_count - last_cycle >= interval:
                    last_cycle = state.cycle_count
                    await self._run_analysis(state)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"AiAdvisor loop error: {e}")

    async def _run_analysis(self, state) -> None:
        client = self._get_client()
        if not client:
            logger.debug("AiAdvisor: OpenRouter not configured — skipping LLM call")
            return

        total_trades = state.win_count + state.loss_count
        win_rate = state.win_count / total_trades if total_trades > 0 else 0.0

        user_msg = _USER_TEMPLATE.format(
            cycles=settings.llm_advisor_interval_cycles,
            win_rate=win_rate,
            total_pnl=state.total_pnl,
            daily_pnl=state.daily_pnl,
            wins=state.win_count,
            losses=state.loss_count,
            trades=total_trades,
            spread_arb=settings.spread_arb_threshold,
            momentum=settings.momentum_threshold_usd,
            confidence=settings.min_confidence,
            max_entry=settings.max_entry_price,
            win_min=settings.entry_window_min_s,
            win_max=settings.entry_window_max_s,
        )

        logger.info(f"AiAdvisor: Requesting LLM analysis (model={settings.llm_model})...")
        try:
            completion = await client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=512,
            )
            raw = (completion.choices[0].message.content or "{}").strip()
            # Extract JSON block if surrounded by markdown code fences
            if "```" in raw:
                import re
                match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
                if match:
                    raw = match.group(1).strip()
            data = json.loads(raw)
            rec = ThresholdRecommendation(**data)
            self._last_recommendation = rec

            logger.info(
                f"🤖 AiAdvisor recommendation | "
                f"spread_arb={rec.spread_arb_threshold} | "
                f"momentum=${rec.momentum_threshold_usd} | "
                f"min_conf={rec.min_confidence} | "
                f"risk={rec.risk_level}"
            )
            logger.info(f"   Rationale: {rec.rationale}")

            if settings.llm_auto_tune:
                self._apply_recommendations(rec)
            else:
                logger.info("   LLM_AUTO_TUNE=false — recommendation logged only, not applied.")

        except Exception as e:
            logger.warning(f"AiAdvisor: LLM call failed: {e}")
            if "raw" in locals():
                logger.debug(f"AiAdvisor raw LLM output was: {raw}")

    def _apply_recommendations(self, rec: ThresholdRecommendation) -> None:
        """Apply LLM recommendations to live settings (only if auto_tune enabled)."""
        if rec.spread_arb_threshold is not None:
            old = settings.spread_arb_threshold
            settings.spread_arb_threshold = rec.spread_arb_threshold
            logger.info(f"   Auto-tuned spread_arb_threshold: {old} → {rec.spread_arb_threshold}")
        if rec.momentum_threshold_usd is not None:
            old = settings.momentum_threshold_usd
            settings.momentum_threshold_usd = rec.momentum_threshold_usd
            logger.info(f"   Auto-tuned momentum_threshold_usd: {old} → {rec.momentum_threshold_usd}")
        if rec.min_confidence is not None:
            old = settings.min_confidence
            settings.min_confidence = rec.min_confidence
            logger.info(f"   Auto-tuned min_confidence: {old} → {rec.min_confidence}")

    @property
    def last_recommendation(self) -> Optional[ThresholdRecommendation]:
        return self._last_recommendation

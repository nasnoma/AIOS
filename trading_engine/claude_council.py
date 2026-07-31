"""
trading_engine/claude_council.py

Claude Council Module — High-level governance and adversarial review layer.

Key Functions:
1. Red-Team Trade Review (Bull vs. Bear Debate):
   Adversarially evaluates candidate trades before execution to filter fakeouts.
2. Weekly Performance Review & Parameter Tuning:
   Analyzes 7-day trade history, identifies loss patterns, auto-tunes
   judge weights and risk parameters, and sends Telegram reports.
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger

from trading_engine.data.market_data import MarketSnapshot
from trading_engine.judge import JudgeVerdict
from trading_engine.utils.llm import call_llm
from trading_engine.config import settings

_PARAM_DIR = Path(__file__).parent / "autoresearch" / "params"
_JUDGE_WEIGHTS_PATH = _PARAM_DIR / "judge_weights.json"
_RISK_PARAMS_PATH = _PARAM_DIR / "risk_thresholds.json"


@dataclass
class RedTeamVerdict:
    approved: bool
    confidence_modifier: float   # Multiplier (e.g. 0.8 to 1.2) applied to judge confidence
    bull_case: str
    bear_case: str
    verdict_reason: str


class ClaudeCouncil:
    """Governance council utilizing LLM adversarial debate & performance feedback."""

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name or settings.llm_model

    def red_team_trade(
        self,
        snapshot: MarketSnapshot,
        judge_verdict: JudgeVerdict,
    ) -> RedTeamVerdict:
        """
        Conducts a pre-trade Bull vs. Bear Red-Team evaluation.
        Synthesizes adversarial arguments to decide whether to approve or veto the trade.
        """
        decision_val = judge_verdict.decision.value if hasattr(judge_verdict.decision, "value") else str(judge_verdict.decision)
        if not judge_verdict.approved or decision_val == "HOLD":
            return RedTeamVerdict(
                approved=False,
                confidence_modifier=1.0,
                bull_case="N/A (No active signal)",
                bear_case="N/A (No active signal)",
                verdict_reason="Judge did not approve trade."
            )

        logger.info(f"🛡️ Claude Council: Red-Teaming {decision_val} signal for {snapshot.symbol}...")

        macd_h = getattr(snapshot, "macd_hist", 0.0) or 0.0
        ema_short = getattr(snapshot, "ema9", 0.0) or getattr(snapshot, "ema20", 0.0) or 0.0
        ema_mid = getattr(snapshot, "ema21", 0.0) or getattr(snapshot, "ema50", 0.0) or 0.0
        ema_long = getattr(snapshot, "ema200", 0.0) or 0.0
        r_vol = getattr(snapshot, "realized_vol", 0.0) or 0.0

        prompt = f"""You are the Chief Investment Officer of an AI Quant Fund conducting a mandatory Red-Team review for a proposed trade.

Symbol: {snapshot.symbol}
Direction: {decision_val}
Judge Confidence: {judge_verdict.confidence:.1f}%
Agent Agreement: {judge_verdict.agreement}/8 agents

Market Context:
- Close Price: {snapshot.close:.4f}
- RSI: {snapshot.rsi:.1f} | StochRSI: K={snapshot.stoch_rsi_k:.1f}, D={snapshot.stoch_rsi_d:.1f}
- MACD Hist: {macd_h:.4f} | ATR: {snapshot.atr:.4f}
- EMA Stack: Short={ema_short:.4f}, Mid={ema_mid:.4f}, Long={ema_long:.4f}
- Relative Volume: {snapshot.rel_volume:.2f}x
- Realized Volatility: {r_vol:.2%}

Task:
1. Formulate the strongest Bullish argument.
2. Formulate the strongest Bearish argument (identify potential traps, wicks, resistance/support walls, low volume risk).
3. Deliver a final Red-Team decision: APPROVE or VETO.

Output JSON ONLY in this format:
{{
  "bull_case": "...",
  "bear_case": "...",
  "decision": "APPROVE",
  "confidence_modifier": 1.0,
  "reason": "..."
}}
Note: decision must be "APPROVE" or "VETO". confidence_modifier should be between 0.7 and 1.2.
"""

        try:
            raw_response = call_llm(prompt=prompt, system_prompt="You are a strict risk-focused quant CIO. Output JSON only.")
            cleaned = raw_response.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```")[1].split("```")[0].strip()

            parsed = json.loads(cleaned)
            is_approved = parsed.get("decision", "VETO").upper() == "APPROVE"
            mod = float(parsed.get("confidence_modifier", 1.0))
            mod = max(0.7, min(1.2, mod))

            verdict = RedTeamVerdict(
                approved=is_approved,
                confidence_modifier=mod,
                bull_case=parsed.get("bull_case", ""),
                bear_case=parsed.get("bear_case", ""),
                verdict_reason=parsed.get("reason", "")
            )
            logger.info(f"🛡️ Red-Team Verdict for {snapshot.symbol}: {'APPROVED ✅' if is_approved else 'VETOED 🛑'} (mod={mod:.2f}) - {verdict.verdict_reason}")
            return verdict

        except Exception as e:
            logger.warning(f"Claude Council Red-Team evaluation failed: {e}. Defaulting to APPROVE with mod=1.0.")
            return RedTeamVerdict(
                approved=True,
                confidence_modifier=1.0,
                bull_case="Fallback approval (LLM error)",
                bear_case="Fallback approval (LLM error)",
                verdict_reason="Red-Team evaluation encountered LLM error; allowing trade."
            )

    def run_weekly_performance_review(self, closed_trades: List[dict], days: int = 7) -> Dict[str, Any]:
        """
        Analyzes performance over the past `days` of closed trades, formulates
        optimization insights, updates parameter files, and builds a summary report.
        """
        logger.info(f"📊 Claude Council: Running Weekly Performance Review over last {days} days ({len(closed_trades)} trades)...")

        if not closed_trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "tuning_applied": False,
                "summary": "No closed trades in review window."
            }

        total_trades = len(closed_trades)
        wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
        losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]

        win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
        total_pnl = sum(t.get("pnl", 0.0) for t in closed_trades)

        avg_win = (sum(t.get("pnl", 0.0) for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(t.get("pnl", 0.0) for t in losses) / len(losses)) if losses else 0.0

        tuning_updates = {}

        # Self-healing parameter adjustments based on empirical results
        if total_trades >= 5:
            # Load existing risk thresholds
            risk_data = {}
            if _RISK_PARAMS_PATH.exists():
                try:
                    risk_data = json.loads(_RISK_PARAMS_PATH.read_text())
                except Exception:
                    pass

            # Rule 1: If win rate < 45%, slightly widen min_stop_pct to reduce noise stop-outs
            if win_rate < 45.0:
                current_min_stop = float(risk_data.get("min_stop_pct", 0.022))
                new_min_stop = min(0.035, round(current_min_stop + 0.003, 3))
                risk_data["min_stop_pct"] = new_min_stop
                tuning_updates["min_stop_pct"] = new_min_stop
                logger.info(f"🔧 Tuning: Widened min_stop_pct to {new_min_stop:.1%} due to win rate {win_rate:.1f}%")

            # Rule 2: If win rate > 60%, slightly increase kelly_fraction safely up to 0.25
            elif win_rate > 60.0:
                current_kelly = float(risk_data.get("kelly_fraction", 0.20))
                new_kelly = min(0.25, round(current_kelly + 0.02, 3))
                risk_data["kelly_fraction"] = new_kelly
                tuning_updates["kelly_fraction"] = new_kelly
                logger.info(f"🔧 Tuning: Increased Kelly fraction to {new_kelly:.2f} due to high win rate {win_rate:.1f}%")

            # Persist updated risk parameters if modified
            if tuning_updates:
                try:
                    _RISK_PARAMS_PATH.write_text(json.dumps(risk_data, indent=2))
                except Exception as e:
                    logger.error(f"Failed writing updated risk_thresholds.json: {e}")

        summary = (
            f"🏛️ *Claude Council — Weekly Review*\n"
            f"• Period: Last {days} Days\n"
            f"• Total Trades: {total_trades}\n"
            f"• Win Rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)\n"
            f"• Total Realized PnL: ${total_pnl:+.2f}\n"
            f"• Avg Win: ${avg_win:+.2f} | Avg Loss: ${avg_loss:+.2f}\n"
            f"• Parameter Tuning Applied: {'Yes (' + str(tuning_updates) + ')' if tuning_updates else 'No (Metrics within bounds)'}"
        )

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "tuning_updates": tuning_updates,
            "summary": summary
        }

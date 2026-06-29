"""
polymarket_bot/autoresearch_loop.py

Karpathy-style autonomous parameter optimization loop for the Polymarket bot.

How it works:
  1. Load current best params from best_params.json (or .env defaults)
  2. Run backtester → get score + metrics
  3. Call OpenRouter LLM → propose new parameter set
  4. Run backtester with proposed params
  5. If score improves: save as new best, append to history
  6. Repeat N iterations

Usage:
  python -m polymarket_bot.autoresearch_loop
  python -m polymarket_bot.autoresearch_loop --iterations 20 --assets BTC ETH
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from openai import OpenAI

from polymarket_bot.backtester import run_backtest
from polymarket_bot.config import Settings

# ── Paths ─────────────────────────────────────────────────────────────────────
_DIR = Path(__file__).parent
BEST_PARAMS_FILE = _DIR / "best_params.json"
HISTORY_FILE = _DIR / "autoresearch_history.json"

# ── Parameter bounds ──────────────────────────────────────────────────────────
PARAM_BOUNDS: Dict[str, tuple] = {
    "momentum_threshold_usd": (5.0,  50.0),
    "min_confidence":         (0.50,  0.75),
    "min_signal_confidence":  (0.20,  0.65),
    "entry_window_min_s":     (30,   120),
    "entry_window_max_s":     (150,  270),
}

DEFAULT_PARAMS: Dict[str, Any] = {
    "momentum_threshold_usd": 10.0,
    "min_confidence":         0.57,
    "min_signal_confidence":  0.40,
    "entry_window_min_s":     45,
    "entry_window_max_s":     270,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_best() -> Dict[str, Any]:
    if BEST_PARAMS_FILE.exists():
        return json.loads(BEST_PARAMS_FILE.read_text())
    return DEFAULT_PARAMS.copy()


def _save_best(params: Dict[str, Any], score: float, metrics: dict) -> None:
    data = {"params": params, "score": score, "metrics": metrics,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    BEST_PARAMS_FILE.write_text(json.dumps(data, indent=2))


def _append_history(record: dict) -> None:
    history: List[dict] = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())
    history.append(record)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _clamp_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp all parameter values to their allowed bounds."""
    clamped = {}
    for k, v in params.items():
        if k not in PARAM_BOUNDS:
            continue
        lo, hi = PARAM_BOUNDS[k]
        if isinstance(lo, int):
            clamped[k] = int(max(lo, min(hi, int(v))))
        else:
            clamped[k] = round(float(max(lo, min(hi, float(v)))), 4)
    return clamped


def _build_cfg(params: Dict[str, Any]) -> Settings:
    """Build a Settings object from a param dict (overrides .env values)."""
    return Settings(
        polymarket_private_key="",
        openrouter_api_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        spread_arb_threshold=0.98,
        max_risk_per_trade_pct=0.01,
        max_concurrent_positions=3,
        max_daily_loss_usd=100.0,
        momentum_threshold_usd=params["momentum_threshold_usd"],
        min_confidence=params["min_confidence"],
        min_signal_confidence=params["min_signal_confidence"],
        entry_window_min_s=params["entry_window_min_s"],
        entry_window_max_s=params["entry_window_max_s"],
    )


def _metrics_from_result(result) -> dict:
    return {
        "total_trades": result.total_trades,
        "win_rate": round(result.win_rate, 4),
        "net_pnl": result.net_pnl,
        "max_drawdown_pct": round(result.max_drawdown_pct, 4),
        "score": result.score,
    }


# ── LLM proposal ─────────────────────────────────────────────────────────────

def _propose_params(
    client: OpenAI,
    llm_model: str,
    current_params: Dict[str, Any],
    current_score: float,
    current_metrics: dict,
    history: List[dict],
) -> Dict[str, Any]:
    """Ask the LLM to propose a new parameter set."""

    compact_history = [
        {
            "iteration": h.get("iteration"),
            "params": h.get("params"),
            "score": h.get("score"),
            "metrics": h.get("metrics"),
            "improved": h.get("improved"),
        }
        for h in history[-10:]  # last 10 only
    ]

    system_prompt = (
        "You are an expert quantitative trading researcher. "
        "Your goal is to find parameters that maximize the trading score for a "
        "5-minute binary options momentum bot on Polymarket BTC/ETH markets. "
        "You MUST respond ONLY with a raw JSON object — no markdown, no explanation. "
        "The JSON must have exactly two keys: 'reasoning' (string) and 'params' (object)."
    )

    prompt = f"""
Current best parameters:
{json.dumps(current_params, indent=2)}

Current backtest score: {current_score:.4f}

Current metrics:
{json.dumps(current_metrics, indent=2)}

Parameter bounds (min, max):
{json.dumps({k: list(v) for k, v in PARAM_BOUNDS.items()}, indent=2)}

Recent optimization history (last 10 iterations):
{json.dumps(compact_history, indent=2)}

Instructions:
1. Analyse the metrics. Low win_rate (<0.52) means the signal is picking noise — consider raising thresholds.
   High max_drawdown means sizing or entry is too aggressive. Low trade count means thresholds are too tight.
2. Propose a new parameter set likely to improve the score.
3. Respond ONLY with this exact JSON format (no markdown):
{{
  "reasoning": "brief analysis and rationale",
  "params": {{
    "momentum_threshold_usd": 15.0,
    "min_confidence": 0.57,
    "min_signal_confidence": 0.40,
    "entry_window_min_s": 45,
    "entry_window_max_s": 270
  }}
}}
"""

    response = client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=600,
        extra_headers={
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "Polymarket Autoresearch",
        },
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if model included them
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            l for l in lines
            if not l.startswith("```")
        ).strip()

    parsed = json.loads(raw)
    reasoning = parsed.get("reasoning", "")
    proposed = _clamp_params(parsed["params"])
    logger.info(f"LLM reasoning: {reasoning}")
    return proposed


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_loop(
    assets: List[str],
    iterations: int,
    openrouter_api_key: str,
    llm_model: str,
    max_bars: Optional[int] = None,
) -> None:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_api_key,
    )

    # Load existing best
    best_data = _load_best()
    best_params: Dict[str, Any] = best_data.get("params", best_data)  # handle both formats
    best_score: float = best_data.get("score", -999.0)

    logger.info(f"Starting autoresearch | {iterations} iterations | assets={assets}")
    logger.info(f"Current best score: {best_score:.4f} | params: {best_params}")

    # Baseline backtest
    cfg = _build_cfg(best_params)
    baseline = run_backtest(assets=assets, cfg=cfg, max_bars=max_bars)
    best_score = baseline.score
    best_metrics = _metrics_from_result(baseline)
    logger.info(
        f"Baseline | trades={baseline.total_trades} wr={baseline.win_rate:.1%} "
        f"pnl=${baseline.net_pnl:+.2f} dd={baseline.max_drawdown_pct:.1%} "
        f"score={baseline.score:.4f}"
    )

    # Always persist the baseline if it beats the stored score
    if best_score > best_data.get("score", -999.0):
        _save_best(best_params, best_score, best_metrics)
        logger.info(f"Baseline saved to {BEST_PARAMS_FILE}")

    history: List[dict] = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())

    for i in range(1, iterations + 1):
        logger.info(f"\n── Iteration {i}/{iterations} ──────────────────────────")

        # Propose new params
        try:
            proposed = _propose_params(
                client=client,
                llm_model=llm_model,
                current_params=best_params,
                current_score=best_score,
                current_metrics=best_metrics,
                history=history,
            )
        except Exception as e:
            logger.warning(f"LLM proposal failed: {e} — skipping iteration")
            continue

        logger.info(f"Proposed: {proposed}")

        # Evaluate proposed params
        try:
            cfg = _build_cfg(proposed)
            result = run_backtest(assets=assets, cfg=cfg, max_bars=max_bars)
        except Exception as e:
            logger.warning(f"Backtest failed: {e} — skipping iteration")
            continue

        metrics = _metrics_from_result(result)
        improved = result.score > best_score

        logger.info(
            f"Result | trades={result.total_trades} wr={result.win_rate:.1%} "
            f"pnl=${result.net_pnl:+.2f} dd={result.max_drawdown_pct:.1%} "
            f"score={result.score:.4f} | {'✅ IMPROVED' if improved else '❌ No improvement'}"
        )

        record = {
            "iteration": i,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "params": proposed,
            "score": result.score,
            "metrics": metrics,
            "improved": improved,
        }
        history.append(record)
        _append_history(record)

        if improved:
            best_params = proposed
            best_score = result.score
            best_metrics = metrics
            _save_best(best_params, best_score, best_metrics)
            logger.success(f"New best score: {best_score:.4f} | params saved to {BEST_PARAMS_FILE}")

    logger.info(f"\n═══ Autoresearch complete ═══")
    logger.info(f"Best score : {best_score:.4f}")
    logger.info(f"Best params: {json.dumps(best_params, indent=2)}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket autoresearch loop")
    parser.add_argument("--iterations", type=int, default=10, help="Number of LLM iterations")
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH"], help="Assets to backtest")
    parser.add_argument("--max-bars", type=int, default=None, help="Limit bars per asset (for speed)")
    args = parser.parse_args()

    from polymarket_bot.config import settings as _s
    if not _s.openrouter_api_key:
        logger.error("OPENROUTER_API_KEY not set in polymarket_bot/.env")
        sys.exit(1)

    run_loop(
        assets=args.assets,
        iterations=args.iterations,
        openrouter_api_key=_s.openrouter_api_key,
        llm_model=_s.llm_model,
        max_bars=args.max_bars,
    )

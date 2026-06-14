#!/usr/bin/env python
"""
trading_engine/autoresearch/run_evolution.py

Unified Autonomous Evolution Loop (production Karpathy Loop).

Replaces run_autoresearch_loop.py and run_prompt_evolution_loop.py.

Strategy:
  1. Run harness → establish baseline fitness
  2. LLM researcher reads program.md + current param file → proposes ONE change
  3. Apply change (backup original)
  4. Run harness → measure candidate fitness
  5. If improved AND all guardrails pass → keep (write evolution_history.jsonl)
     Else → revert backup
  6. Repeat for N iterations

Targets (selectable via --target):
  - weights    → params/judge_weights.json
  - risk       → params/risk_thresholds.json
  - specialists → params/specialist_configs.json
  - sentiment  → prompts/sentiment_prompt.md
  - macro      → prompts/macro_prompt.md

Usage examples:
  # Evolve Judge weights for 20 iterations overnight:
  python -m trading_engine.autoresearch.run_evolution --target weights --iterations 20

  # Evolve Risk thresholds for 10 iterations:
  python -m trading_engine.autoresearch.run_evolution --target risk --iterations 10

  # Evolve all targets in round-robin for 30 iterations:
  python -m trading_engine.autoresearch.run_evolution --target all --iterations 30
"""
from __future__ import annotations

import sys
import os
import json
import shutil
import argparse
import itertools
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger
from openai import OpenAI

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.append(str(PROJECT_ROOT))

from trading_engine.config import settings
from trading_engine.autoresearch.harness import run_harness, DEFAULT_SYMBOLS, DEFAULT_TIMEFRAME

AUTORESEARCH_DIR = Path(__file__).parent
PARAMS_DIR = AUTORESEARCH_DIR / "params"
PROMPTS_DIR = PROJECT_ROOT / "trading_engine" / "prompts"
PROGRAM_MD = AUTORESEARCH_DIR / "program.md"
HISTORY_FILE = AUTORESEARCH_DIR / "evolution_history.jsonl"

# Mapping of target name → file path
TARGET_FILES = {
    "weights":     PARAMS_DIR / "judge_weights.json",
    "risk":        PARAMS_DIR / "risk_thresholds.json",
    "specialists": PARAMS_DIR / "specialist_configs.json",
    "sentiment":   PROMPTS_DIR / "sentiment_prompt.md",
    "macro":       PROMPTS_DIR / "macro_prompt.md",
}

# Prompt targets (sentiment/macro) are NOT included — prompt changes cannot affect the
# deterministic backtest harness, so evolving them produces no fitness gradient.
ALL_TARGETS = ["weights", "risk", "specialists"]


# ── Structured random perturbation (for JSON param targets) ───────────────────

import random

_WEIGHT_RANGES = {
    "trend":      (0.5, 2.5),
    "momentum":   (0.5, 2.0),
    "volume":     (0.5, 1.8),
    "orderflow":  (0.8, 2.5),
    "volatility": (0.3, 1.2),
    "structure":  (0.5, 1.8),
    "sentiment":  (0.3, 1.2),
    "macro":      (0.3, 1.2),
}

_RISK_RANGES = {
    "kelly_fraction":      (0.10, 0.40),
    "max_portfolio_heat":  (0.08, 0.25),
    "atr_stop_multiplier": (1.5, 3.5),
    "corr_soft_threshold": (0.60, 0.85),
    "corr_hard_threshold": (0.82, 0.95),
    "max_position_pct":    (0.05, 0.15),
}

# Indicator period ranges for specialist_configs.json
_SPECIALIST_RANGES = {
    "ema_fast":       (8,   30),
    "ema_slow":       (30,  80),
    "ema_trend":      (100, 250),
    "rsi_period":     (7,   21),
    "atr_period":     (7,   21),
    "bb_period":      (14,  30),
    "roc_period":     (5,   20),
    "rel_vol_period": (10,  40),
}


def _perturb_weights(current_json: dict, iteration: int) -> dict:
    """
    Hybrid perturbation strategy for judge_weights.json.
    5 strategies (mod 5) — wider deltas than before to break out of plateau.
    """
    import copy
    new = copy.deepcopy(current_json)
    w = new.setdefault("weights", {})

    strategy = iteration % 5
    rng = random.Random(iteration * 7 + 13)

    if strategy == 0:
        # Wider nudge: tweak 3-5 random weights by ±25-45%
        keys = rng.sample(list(_WEIGHT_RANGES.keys()), k=rng.randint(3, 5))
        for k in keys:
            lo, hi = _WEIGHT_RANGES[k]
            delta = rng.uniform(0.25, 0.45) * rng.choice([-1, 1])
            w[k] = round(max(lo, min(hi, w.get(k, 1.0) + delta)), 2)

    elif strategy == 1:
        # Large jump: resample 2-4 weights from full range
        keys = rng.sample(list(_WEIGHT_RANGES.keys()), k=rng.randint(2, 4))
        for k in keys:
            lo, hi = _WEIGHT_RANGES[k]
            w[k] = round(rng.uniform(lo, hi), 2)

    elif strategy == 2:
        # Threshold shift: change min_agreement AND min_avg_confidence together
        new["min_agreement"]      = rng.choice([4, 5, 6])
        new["min_avg_confidence"] = round(rng.uniform(40, 62), 0)

    elif strategy == 3:
        # Rebalance: boost one agent, reduce its complement (bigger delta)
        pairs = [("trend", "volatility"), ("momentum", "macro"),
                 ("orderflow", "sentiment"), ("volume", "structure")]
        a, b = rng.choice(pairs)
        delta = rng.uniform(0.30, 0.70)
        lo_a, hi_a = _WEIGHT_RANGES[a]
        lo_b, hi_b = _WEIGHT_RANGES[b]
        w[a] = round(max(lo_a, min(hi_a, w.get(a, 1.0) + delta)), 2)
        w[b] = round(max(lo_b, min(hi_b, w.get(b, 1.0) - delta)), 2)

    else:
        # Full reset: resample all weights + thresholds simultaneously
        for k, (lo, hi) in _WEIGHT_RANGES.items():
            w[k] = round(rng.uniform(lo, hi), 2)
        new["min_agreement"]      = rng.choice([4, 5, 6])
        new["min_avg_confidence"] = round(rng.uniform(40, 65), 0)

    return new


def _perturb_risk(current_json: dict, iteration: int) -> dict:
    """Structured perturbation for risk_thresholds.json."""
    import copy
    new = copy.deepcopy(current_json)
    rng = random.Random(iteration * 11 + 7)

    # Perturb 1-2 risk params
    keys = rng.sample(list(_RISK_RANGES.keys()), k=rng.randint(1, 2))
    for k in keys:
        lo, hi = _RISK_RANGES[k]
        current_val = new.get(k, (lo + hi) / 2)
        if rng.random() > 0.4:
            # Nudge ±15-30%
            delta = rng.uniform(0.10, 0.30) * rng.choice([-1, 1]) * current_val
            new[k] = round(max(lo, min(hi, current_val + delta)), 3)
        else:
            # Jump to random point in range
            new[k] = round(rng.uniform(lo, hi), 3)

    return new


def _perturb_specialists(current_json: dict, iteration: int) -> dict:
    """Structured perturbation for specialist_configs.json (indicator periods)."""
    import copy
    new = copy.deepcopy(current_json)
    # Strip metadata
    new = {k: v for k, v in new.items() if not k.startswith("_")}
    rng = random.Random(iteration * 17 + 3)

    strategy = iteration % 3
    if strategy == 0:
        # Nudge 1-2 params ±10-25%
        keys = rng.sample(list(_SPECIALIST_RANGES.keys()), k=rng.randint(1, 2))
        for k in keys:
            lo, hi = _SPECIALIST_RANGES[k]
            current_val = new.get(k, int((lo + hi) / 2))
            delta = rng.uniform(0.10, 0.25) * rng.choice([-1, 1]) * current_val
            new[k] = int(max(lo, min(hi, round(current_val + delta))))
    elif strategy == 1:
        # Jump: resample 1 param from full range
        k = rng.choice(list(_SPECIALIST_RANGES.keys()))
        lo, hi = _SPECIALIST_RANGES[k]
        new[k] = int(rng.uniform(lo, hi))
    else:
        # Swap: move ema_fast closer to/further from ema_slow
        ema_fast = new.get("ema_fast", 20)
        ema_slow = new.get("ema_slow", 50)
        direction = rng.choice([-1, 1])
        new["ema_fast"] = int(max(8, min(ema_slow - 5, ema_fast + direction * rng.randint(2, 6))))

    return new


def _propose_json_mutation(target: str, current_content: str, iteration: int) -> str:
    """
    For JSON targets: use structured perturbation (guaranteed diversity).
    No LLM needed — faster, cheaper, and more diverse than asking an LLM to tweak numbers.
    """
    try:
        current = json.loads(current_content)
        # Strip _comment keys for cleaner processing
        current = {k: v for k, v in current.items() if not k.startswith("_")}
    except Exception:
        current = {}

    if target == "weights":
        proposed = _perturb_weights(current, iteration)
    elif target == "risk":
        proposed = _perturb_risk(current, iteration)
    else:  # specialists
        proposed = _perturb_specialists(current, iteration)

    return json.dumps(proposed, indent=2)


# ── LLM researcher (used for PROMPT targets only) ─────────────────────────────

def query_researcher(
    target: str,
    current_content: str,
    current_fitness: float,
    val_metrics: dict,
    history_summary: str,
    iteration: int = 1,
) -> str:
    """
    For JSON param targets: uses structured random perturbation (fast, diverse).
    For prompt targets (sentiment/macro): uses LLM with high temperature for creativity.
    """
    # JSON targets: pure perturbation, no LLM cost
    if target in ("weights", "risk", "specialists"):
        return _propose_json_mutation(target, current_content, iteration)

    # Prompt targets: LLM with enforced diversity
    if not settings.openrouter_api_key:
        raise ValueError("openrouter_api_key is not set. Cannot run prompt evolution.")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
    )

    program_content = PROGRAM_MD.read_text() if PROGRAM_MD.exists() else ""
    is_json = target in ("weights", "risk", "specialists")
    format_instruction = (
        "Output ONLY the complete, valid JSON object for the updated file. No backticks, no comments outside JSON."
        if is_json else
        "Output ONLY the raw text of the updated prompt. No backticks, no explanations, no headers."
    )

    system_prompt = (
        "You are an expert quantitative trading researcher and prompt engineer. "
        "You improve a live multi-agent trading system by proposing targeted, evidence-based changes "
        "to its configuration files. You are scientific: you study the metrics, identify weaknesses, "
        "and make one focused change per iteration. You never break constraints.\n\n"
        f"SYSTEM GOALS AND CONSTRAINTS:\n{program_content}"
    )

    user_prompt = f"""
We are optimizing: `{target}` (target file: {TARGET_FILES[target].name})

Current file content:
--------------------------------------------------
{current_content}
--------------------------------------------------

Current best fitness score: {current_fitness:+.4f}  (higher = better, target > 1.5)

Latest validation metrics:
{json.dumps(val_metrics, indent=2)}

Recent evolution history (last 5 iterations):
{history_summary}

Your task:
1. Study the metrics above. Identify the single biggest weakness limiting performance.
2. Propose ONE targeted change to improve fitness without breaking any guardrails.
3. {format_instruction}
"""

    logger.info(f"  Querying LLM researcher to evolve: {target}...")
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.25,
        max_tokens=2000,
        extra_headers={
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "Antigravity Trading System",
        },
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if the model wrapped the output
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]  # drop opening fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    return raw


# ── Validation helpers ─────────────────────────────────────────────────────────

def validate_json_target(target: str, content: str) -> tuple[bool, str]:
    """Ensure proposed JSON content is parseable and contains required keys."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"

    required_keys = {
        "weights":     ["weights"],
        "risk":        ["kelly_fraction", "max_portfolio_heat"],
        "specialists": ["ema_fast", "rsi_period"],   # flat period format
    }
    for key in required_keys.get(target, []):
        if key not in data:
            return False, f"Missing required key: '{key}'"

    # Range checks for weights
    if target == "weights":
        for agent, w in data.get("weights", {}).items():
            if not (0.1 <= float(w) <= 3.0):
                return False, f"Weight for '{agent}' out of range [0.1, 3.0]: {w}"
        min_ag = data.get("min_agreement", 5)
        if not (3 <= int(min_ag) <= 7):
            return False, f"min_agreement out of range [3, 7]: {min_ag}"

    # Range checks for risk
    if target == "risk":
        kf = float(data.get("kelly_fraction", 0.25))
        if not (0.10 <= kf <= 0.50):
            return False, f"kelly_fraction out of range [0.10, 0.50]: {kf}"
        heat = float(data.get("max_portfolio_heat", 0.15))
        if not (0.05 <= heat <= 0.30):
            return False, f"max_portfolio_heat out of range [0.05, 0.30]: {heat}"

    # Range checks for specialists (indicator periods must stay in safe bounds)
    if target == "specialists":
        checks = {
            "ema_fast":       (5,   40),
            "ema_slow":       (20,  100),
            "ema_trend":      (50,  300),
            "rsi_period":     (5,   25),
            "atr_period":     (5,   25),
            "bb_period":      (10,  40),
            "roc_period":     (3,   25),
            "rel_vol_period": (5,   60),
        }
        for param, (lo, hi) in checks.items():
            if param in data:
                val = int(data[param])
                if not (lo <= val <= hi):
                    return False, f"Specialist param '{param}' out of range [{lo}, {hi}]: {val}"
        # Sanity: ema_fast must be less than ema_slow
        ema_fast = int(data.get("ema_fast", 20))
        ema_slow = int(data.get("ema_slow", 50))
        if ema_fast >= ema_slow:
            return False, f"ema_fast ({ema_fast}) must be < ema_slow ({ema_slow})"

    return True, "OK"


def validate_prompt_target(target: str, content: str) -> tuple[bool, str]:
    """Ensure required template placeholders are present in prompt files."""
    required_placeholders = {
        "sentiment": ["{symbol}", "{combined_context}"],
        "macro":     ["{symbol}", "{asset_type}"],
    }
    for ph in required_placeholders.get(target, []):
        if ph not in content:
            return False, f"Missing required placeholder: {ph}"
    if len(content.strip()) < 50:
        return False, "Proposed prompt is too short (< 50 chars)"
    return True, "OK"


# ── History helpers ────────────────────────────────────────────────────────────

def append_history(record: dict) -> None:
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_recent_history(n: int = 5) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text().strip().split("\n")
    records = []
    for line in lines[-n:]:
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def format_history_summary(records: list[dict]) -> str:
    if not records:
        return "No history yet — this is the first iteration."
    lines = []
    for r in records:
        status = "✅ ACCEPTED" if r.get("improved") else "❌ REJECTED"
        lines.append(
            f"  Iter {r.get('iteration', '?')} | target={r.get('target')} | "
            f"fitness={r.get('fitness', 0):+.4f} | {status} | reason={r.get('reason', '')}"
        )
    return "\n".join(lines)


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_single_target(
    target: str,
    iterations: int,
    symbols: list[str],
    timeframe: str,
    train_days: int,
    val_days: int,
    iteration_offset: int = 0,
) -> float:
    """
    Runs the evolution loop for a single target file.
    Returns the best fitness achieved.
    """
    target_path = TARGET_FILES[target]
    is_json = target in ("weights", "risk", "specialists")

    logger.info(f"\n{'='*72}")
    logger.info(f" TARGET: {target} → {target_path.name}")
    logger.info(f"{'='*72}")

    # ── Baseline ──────────────────────────────────────────────────────────────
    logger.info("Running baseline harness...")
    baseline = run_harness(symbols=symbols, timeframe=timeframe,
                           train_days=train_days, val_days=val_days)
    best_fitness = baseline["overall_fitness"]
    best_content = target_path.read_text()

    # Aggregate val metrics for the researcher
    agg_val = {}
    for sym, res in baseline.get("assets", {}).items():
        if "error" not in res:
            agg_val[sym] = res.get("val", {})

    logger.success(f"Baseline fitness: {best_fitness:+.4f}")

    for it in range(1, iterations + 1):
        global_it = iteration_offset + it
        logger.info(f"\n--- Evolution Cycle {it}/{iterations} (global #{global_it}) | target={target} ---")

        history_records = load_recent_history(5)
        history_summary = format_history_summary(history_records)

        try:
            # ── 1. Query researcher ───────────────────────────────────────────
            proposed = query_researcher(
                target=target,
                current_content=best_content,
                current_fitness=best_fitness,
                val_metrics=agg_val,
                history_summary=history_summary,
                iteration=global_it,
            )

            # ── 2. Validate proposed change ───────────────────────────────────
            if is_json:
                valid, reason = validate_json_target(target, proposed)
            else:
                valid, reason = validate_prompt_target(target, proposed)

            if not valid:
                logger.warning(f"  Proposed change failed validation: {reason}. Skipping.")
                append_history({
                    "iteration": global_it, "target": target,
                    "fitness": best_fitness, "improved": False,
                    "reason": f"validation_failed: {reason}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                continue

            # ── 3. Backup + apply ─────────────────────────────────────────────
            backup_path = target_path.with_suffix(target_path.suffix + ".bak")
            shutil.copy2(target_path, backup_path)
            target_path.write_text(proposed)

            # ── 4. Run harness with new params ────────────────────────────────
            logger.info("  Running harness with proposed change...")
            res = run_harness(symbols=symbols, timeframe=timeframe,
                              train_days=train_days, val_days=val_days)
            candidate_fitness = res["overall_fitness"]
            all_guardrails_ok = res.get("overall_guardrails_passed", True)

            # ── 5. Accept or revert ───────────────────────────────────────────
            improved = (
                candidate_fitness > best_fitness
                and all_guardrails_ok
            )

            if improved:
                best_fitness = candidate_fitness
                best_content = proposed
                backup_path.unlink(missing_ok=True)

                # Update aggregated val metrics for next iteration
                agg_val = {}
                for sym, sres in res.get("assets", {}).items():
                    if "error" not in sres:
                        agg_val[sym] = sres.get("val", {})

                logger.success(
                    f"  🎉 ACCEPTED: fitness {best_fitness:+.4f} "
                    f"(guardrails={'✅' if all_guardrails_ok else '❌'})"
                )
                reason = "improved"
            else:
                # Revert
                shutil.copy2(backup_path, target_path)
                backup_path.unlink(missing_ok=True)
                reject_reason = (
                    "guardrails_failed" if not all_guardrails_ok
                    else f"no_improvement ({candidate_fitness:+.4f} <= {best_fitness:+.4f})"
                )
                logger.warning(f"  ❌ REJECTED: {reject_reason}")
                reason = reject_reason

            append_history({
                "iteration": global_it,
                "target": target,
                "fitness": candidate_fitness,
                "best_fitness": best_fitness,
                "improved": improved,
                "reason": reason,
                "guardrails_passed": all_guardrails_ok,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "proposed_snippet": proposed[:300],  # first 300 chars for reference
            })

        except Exception as e:
            logger.error(f"  Error in cycle {it}: {e}")
            # Safety: ensure we're on best content
            target_path.write_text(best_content)
            append_history({
                "iteration": global_it, "target": target,
                "fitness": best_fitness, "improved": False,
                "reason": f"error: {str(e)[:200]}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    logger.success(f"\nTarget '{target}' complete. Best fitness: {best_fitness:+.4f}")
    return best_fitness


def main():
    parser = argparse.ArgumentParser(
        description="Unified Autonomous Evolution Loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Targets:
  weights     → params/judge_weights.json   (ensemble vote weights)
  risk        → params/risk_thresholds.json  (Kelly, heat, ATR, correlation)
  specialists → params/specialist_configs.json (indicator parameters)
  sentiment   → prompts/sentiment_prompt.md
  macro       → prompts/macro_prompt.md
  all         → round-robin across all targets

Examples:
  python -m trading_engine.autoresearch.run_evolution --target weights --iterations 20
  python -m trading_engine.autoresearch.run_evolution --target all --iterations 50
        """
    )
    parser.add_argument("--target", default="weights",
                        choices=list(TARGET_FILES.keys()) + ["all"],
                        help="Which file to evolve (default: weights)")
    parser.add_argument("--iterations", type=int, default=10,
                        help="Number of evolution cycles (default: 10)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated symbols to backtest")
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME,
                        help=f"Candle timeframe (default: {DEFAULT_TIMEFRAME})")
    parser.add_argument("--train-days", type=int, default=90,
                        help="Training period days (default: 90)")
    parser.add_argument("--val-days", type=int, default=45,
                        help="Validation period days (default: 45)")
    args = parser.parse_args()

    # Disable live exchange auth for backtest-only runs
    settings.crypto_testnet = False
    settings.bybit_api_key = ""
    settings.bybit_api_secret = ""

    symbols = [s.strip() for s in args.symbols.split(",")]

    logger.info("=" * 72)
    logger.info("     AUTONOMOUS EVOLUTION LOOP  —  Karpathy Loop")
    logger.info("=" * 72)
    logger.info(f"  target={args.target} | iterations={args.iterations}")
    logger.info(f"  symbols={symbols} | timeframe={args.timeframe}")
    logger.info(f"  train={args.train_days}d | val={args.val_days}d")
    logger.info("=" * 72)

    harness_kwargs = dict(
        symbols=symbols,
        timeframe=args.timeframe,
        train_days=args.train_days,
        val_days=args.val_days,
    )

    if args.target == "all":
        # Round-robin across all targets
        target_cycle = itertools.cycle(ALL_TARGETS)
        target_counts = {t: 0 for t in ALL_TARGETS}
        per_target = max(1, args.iterations // len(ALL_TARGETS))
        total_done = 0

        for _ in range(args.iterations):
            target = next(target_cycle)
            run_single_target(
                target=target,
                iterations=1,
                iteration_offset=total_done,
                **harness_kwargs,
            )
            target_counts[target] += 1
            total_done += 1
    else:
        run_single_target(
            target=args.target,
            iterations=args.iterations,
            iteration_offset=0,
            **harness_kwargs,
        )

    logger.info("\n" + "=" * 72)
    logger.info("  EVOLUTION COMPLETE")
    logger.info("=" * 72)

    # Print final summary from history
    recent = load_recent_history(args.iterations)
    accepted = [r for r in recent if r.get("improved")]
    logger.success(f"  Accepted improvements: {len(accepted)} / {len(recent)}")
    for r in accepted:
        logger.success(f"    → {r['target']} | fitness={r['fitness']:+.4f} | iter={r['iteration']}")


if __name__ == "__main__":
    main()

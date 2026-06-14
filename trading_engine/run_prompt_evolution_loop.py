#!/usr/bin/env python
"""
trading_engine/run_prompt_evolution_loop.py

Autonomous Prompt Evolution Loop ("Karpathy Loop" for agent prompts).
Iteratively prompts the LLM researcher to modify the markdown prompt templates
for Sentiment and Macro agents, runs backtests with real LLM calls over a small,
cost-effective window, and saves improvements.
"""
from __future__ import annotations
import os
import sys
import json
import subprocess
import argparse
import shutil
from pathlib import Path
from loguru import logger
from openai import OpenAI

# Add project root directory to path
PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.append(str(PROJECT_ROOT))

from trading_engine.config import settings

PROMPTS_DIR = PROJECT_ROOT / "trading_engine" / "prompts"
HISTORY_FILE = PROJECT_ROOT / "trading_engine" / "prompt_evolution_history.json"

def run_harness(symbols: str, timeframe: str, train_days: int, val_days: int) -> dict:
    """Executes the autoresearch harness with real LLM calls and returns results."""
    cmd = [
        str(PROJECT_ROOT / "trading_engine" / "venv" / "bin" / "python"),
        "-m", "trading_engine.scratch.autoresearch_harness",
        "--symbols", symbols,
        "--timeframe", timeframe,
        "--train-days", str(train_days),
        "--val-days", str(val_days),
        "--real-llm"  # Enable real LLM calls during backtest
    ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=str(PROJECT_ROOT))
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"Harness run failed: {e.stderr}")
        raise e
    except json.JSONDecodeError as e:
        logger.error("Failed to decode JSON from harness output.")
        raise e

def query_llm_for_prompt(
    prompt_name: str,
    current_prompt_content: str,
    current_fitness: float,
    performance_metrics: dict,
    history: list[dict]
) -> str:
    """Queries OpenRouter to propose optimized markdown prompt instructions."""
    if not settings.openrouter_api_key:
        raise ValueError("OpenRouter API key is missing. Cannot run autonomous prompt optimization.")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
    )

    system_prompt = (
        "You are an expert quantitative trading prompt engineer and developer. "
        "Your task is to refine the system instructions (prompt template) of a trading specialist agent "
        "to improve its out-of-sample decision-making accuracy and Sortino ratio. "
        "You must output ONLY the refined prompt text. Do not wrap it in markdown codeblocks (like ```markdown ... ```) "
        "or include any conversational headers or footers."
    )

    prompt = f"""
    We are optimizing the prompt template for the specialist agent: '{prompt_name}'
    
    Current Prompt Template Content:
    --------------------------------------------------
    {current_prompt_content}
    --------------------------------------------------

    Current Backtest Fitness Score: {current_fitness}
    
    Detailed Performance Metrics:
    {json.dumps(performance_metrics, indent=2)}

    Instructions:
    1. Identify any flaws or limitations in the current prompt instructions (e.g. poor risk management, lack of specific rules, confusing formats).
    2. Revise the instructions/rules in the prompt template to improve its signal generation.
    3. Make sure to preserve any parameter placeholders like {{symbol}}, {{combined_context}}, {{dxy_trend}}, {{risk_mode}}, or {{asset_type}} so the agent python code can format them at runtime!
    4. Respond ONLY with the raw text of the updated prompt template (do not include your reasoning, explanations, or backticks).
    """

    logger.info(f"Querying LLM researcher to evolve prompt: {prompt_name}...")
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1500,
        extra_headers={
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "Antigravity Trading System",
        }
    )
    
    updated_content = response.choices[0].message.content.strip()
    
    # Strip code block wrappers if generated
    if updated_content.startswith("```"):
        lines = updated_content.split("\n")
        if lines[0].startswith("```") or lines[0].startswith("```markdown"):
            lines = lines[1:]
        if lines[-1].strip() == "```":
            lines = lines[:-1]
        updated_content = "\n".join(lines).strip()
        
    return updated_content

def main():
    parser = argparse.ArgumentParser(description="Autonomous Prompt Evolution Loop")
    parser.add_argument("--iterations", type=int, default=5, help="Number of cycles to run")
    parser.add_argument("--target-prompt", default="sentiment_prompt.md", choices=["sentiment_prompt.md", "macro_prompt.md"], help="Target prompt file to optimize")
    parser.add_argument("--symbols", default="BTC/USDT", help="Backtest symbols")
    parser.add_argument("--timeframe", default="1d", help="Backtest timeframe")
    parser.add_argument("--train-days", type=int, default=30, help="Training period days")
    parser.add_argument("--val-days", type=int, default=15, help="Validation period days")
    args = parser.parse_args()

    logger.info("================================================================================")
    logger.info("                 AUTONOMOUS PROMPT EVOLUTION LOOP (KARPATHY LOOP)")
    logger.info("================================================================================")
    logger.info(f"Target Prompt: {args.target_prompt} | Symbols: {args.symbols} | Timeframe: {args.timeframe}")
    logger.info(f"Train/Val: {args.train_days}d / {args.val_days}d")
    logger.info("================================================================================")

    target_path = PROMPTS_DIR / args.target_prompt
    if not target_path.exists():
        logger.error(f"Target prompt file not found at {target_path}")
        sys.exit(1)

    # Establish baseline
    logger.info("Running baseline backtest (using real LLM calls)...")
    baseline_res = run_harness(args.symbols, args.timeframe, args.train_days, args.val_days)
    best_fitness = baseline_res["overall_fitness"]
    logger.success(f"Baseline established! Fitness Score: {best_fitness:.4f}")

    # Load starting prompt content
    best_prompt_content = target_path.read_text()

    # Load history
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass

    history.append({
        "iteration": 0,
        "prompt_file": args.target_prompt,
        "prompt_content": best_prompt_content,
        "fitness": best_fitness,
        "metrics": baseline_res["assets"],
        "improved": True
    })

    # Run Loop
    for it in range(1, args.iterations + 1):
        logger.info(f"\n--- Prompt Evolution Cycle {it}/{args.iterations} ---")
        
        try:
            # 1. Ask LLM to optimize prompt
            proposed_content = query_llm_for_prompt(
                prompt_name=args.target_prompt,
                current_prompt_content=best_prompt_content,
                current_fitness=best_fitness,
                performance_metrics=history[-1]["metrics"],
                history=history
            )
            
            # Verify placeholders are preserved
            if args.target_prompt == "sentiment_prompt.md":
                if "{symbol}" not in proposed_content or "{combined_context}" not in proposed_content:
                    logger.warning("Proposed prompt is missing required placeholders ({symbol} or {combined_context}). Skipping.")
                    continue
            elif args.target_prompt == "macro_prompt.md":
                if "{symbol}" not in proposed_content or "{asset_type}" not in proposed_content:
                    logger.warning("Proposed prompt is missing required placeholders ({symbol} or {asset_type}). Skipping.")
                    continue

            # Backup current prompt
            backup_path = target_path.with_suffix(".md.bak")
            shutil.copy2(target_path, backup_path)
            
            # 2. Write proposed prompt
            with open(target_path, "w") as f:
                f.write(proposed_content)
                
            # 3. Run backtest harness
            logger.info("Running backtest with proposed prompt...")
            res = run_harness(args.symbols, args.timeframe, args.train_days, args.val_days)
            proposed_fitness = res["overall_fitness"]
            
            # 4. Compare results
            improved = proposed_fitness > best_fitness
            if improved:
                best_fitness = proposed_fitness
                best_prompt_content = proposed_content
                logger.success(f"🎉 SUCCESS: Fitness improved from {best_fitness:.4f} to {proposed_fitness:.4f}!")
                # Remove backup
                if backup_path.exists():
                    backup_path.unlink()
            else:
                logger.warning(f"❌ REJECTED: Fitness ({proposed_fitness:.4f}) did not exceed best ({best_fitness:.4f})")
                # Revert
                shutil.copy2(backup_path, target_path)
                if backup_path.exists():
                    backup_path.unlink()

            # Record iteration details
            history.append({
                "iteration": it,
                "prompt_file": args.target_prompt,
                "prompt_content": proposed_content,
                "fitness": proposed_fitness,
                "metrics": res["assets"],
                "improved": improved
            })
            
            # Save history log
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)

        except Exception as e:
            logger.error(f"Error in cycle {it}: {e}")
            # Ensure we are reverted to best prompt in case of failure
            with open(target_path, "w") as f:
                f.write(best_prompt_content)
            continue

    logger.info("\n================================================================================")
    logger.info("                         PROMPT OPTIMIZATION COMPLETE")
    logger.info("================================================================================")
    logger.success(f"Final Best Fitness Score: {best_fitness:.4f}")
    logger.success(f"Optimal prompt saved to {target_path}")

if __name__ == "__main__":
    main()

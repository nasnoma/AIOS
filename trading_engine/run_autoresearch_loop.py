#!/usr/bin/env python
"""
trading_engine/run_autoresearch_loop.py

Autonomous Weight Optimization Loop ("Karpathy Loop" for trading).
Iteratively prompts the LLM to propose optimized specialist agent weights,
runs walk-forward backtests, and saves the configuration that maximizes fitness.
"""
from __future__ import annotations
import os
import sys
import json
import subprocess
import argparse
from pathlib import Path
from loguru import logger
from openai import OpenAI

# Add project root directory to path
PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.append(str(PROJECT_ROOT))

from trading_engine.config import settings

WEIGHTS_FILE = PROJECT_ROOT / "trading_engine" / "optimized_weights.json"
HISTORY_FILE = PROJECT_ROOT / "trading_engine" / "autoresearch_history.json"

def run_harness(symbols: str, timeframe: str, train_days: int, val_days: int, target: str = "crypto") -> dict:
    """Executes the autoresearch harness and returns parsed JSON results."""
    cmd = [
        str(PROJECT_ROOT / "trading_engine" / "venv" / "bin" / "python"),
        "-m", "trading_engine.scratch.autoresearch_harness",
        "--symbols", symbols,
        "--timeframe", timeframe,
        "--train-days", str(train_days),
        "--val-days", str(val_days),
        "--target", target
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

def query_llm_for_weights(
    current_weights: dict,
    current_fitness: float,
    performance_metrics: dict,
    history: list[dict]
) -> dict:
    """Queries OpenRouter to propose optimized weights based on current metrics and history."""
    if not settings.openrouter_api_key:
        raise ValueError("OpenRouter API key is missing. Cannot run autonomous optimization.")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
    )

    # Clean history to keep prompt size reasonable
    compact_history = []
    for h in history[-10:]:  # Last 10 attempts
        compact_history.append({
            "iteration": h.get("iteration"),
            "weights": h.get("weights"),
            "fitness": h.get("fitness"),
            "outcome": "Improved" if h.get("improved") else "No Improvement"
        })

    system_prompt = (
        "You are an expert quantitative trading developer and reinforcement learning agent. "
        "Your goal is to optimize the weights of a multi-agent voting system to maximize out-of-sample trading performance. "
        "You must respond ONLY with a raw JSON block containing 'reasoning' and 'weights'. "
        "The weights must be positive floats between 0.1 and 3.0. Do not use markdown backticks in your response."
    )

    prompt = f"""
    Current Agent Weights:
    {json.dumps(current_weights, indent=2)}

    Current Backtest Fitness Score: {current_fitness}
    
    Detailed Performance Metrics:
    {json.dumps(performance_metrics, indent=2)}

    Optimization History (Last 10 Runs):
    {json.dumps(compact_history, indent=2)}

    Instructions:
    1. Analyze which agents (trend, momentum, volume, orderflow, volatility, structure, sentiment, macro) are overweighted or underweighted based on the metrics.
    2. Propose a new set of weights that you believe will improve the composite fitness score.
    3. Output your response exactly in this JSON format (no other text, no markdown codeblocks):
    {{
      "reasoning": "your brief analytical reasoning here",
      "weights": {{
        "trend": 1.0,
        "momentum": 1.0,
        "volume": 1.0,
        "orderflow": 1.0,
        "volatility": 1.0,
        "structure": 1.0,
        "sentiment": 1.0,
        "macro": 1.0
      }}
    }}
    """

    logger.info("Querying LLM researcher for weight updates...")
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4,
        max_tokens=800,
        extra_headers={
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "Antigravity Trading System",
        }
    )
    
    raw_content = response.choices[0].message.content.strip()
    
    # Strip markdown code block wrappers if the model included them anyway
    if raw_content.startswith("```"):
        lines = raw_content.split("\n")
        if lines[0].startswith("```json") or lines[0].startswith("```"):
            lines = lines[1:]
        if lines[-1].strip() == "```":
            lines = lines[:-1]
        raw_content = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw_content)
        # Validate weights structure
        proposed_weights = parsed["weights"]
        required_agents = ["trend", "momentum", "volume", "orderflow", "volatility", "structure", "sentiment", "macro"]
        for agent in required_agents:
            if agent not in proposed_weights:
                proposed_weights[agent] = 1.0
            # Bound proposed weight
            proposed_weights[agent] = max(0.1, min(3.0, float(proposed_weights[agent])))
        return parsed
    except Exception as e:
        logger.error(f"Failed to parse LLM response: {raw_content}. Error: {e}")
        raise e

def main():
    parser = argparse.ArgumentParser(description="Autonomous Weight Optimization Loop")
    parser.add_argument("--iterations", type=int, default=10, help="Number of optimization cycles to run")
    parser.add_argument("--symbols", default="BTC/USDT,SOL/USDT", help="Backtest symbols")
    parser.add_argument("--timeframe", default="4h", help="Backtest timeframe")
    parser.add_argument("--train-days", type=int, default=180, help="Training period days")
    parser.add_argument("--val-days", type=int, default=90, help="Validation period days")
    parser.add_argument("--target", default="crypto", help="Weight optimization target")
    parser.add_argument("--symbol-specific", action="store_true", help="Optimize and save weights specifically for a single symbol")
    args = parser.parse_args()

    # Determine target name and file paths
    target = args.target
    if args.symbol_specific:
        symbols_list = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if len(symbols_list) == 1:
            target = symbols_list[0].replace("/", "_").replace(":", "_").upper()
        else:
            raise ValueError("Can only use --symbol-specific with a single symbol.")

    logger.info("================================================================================")
    logger.info(f"       AUTONOMOUS TRADING ENGINE OPTIMIZATION LOOP ({target.upper()})")
    logger.info("================================================================================")
    
    weights_file = PROJECT_ROOT / "trading_engine" / f"optimized_weights_{target}.json"
    history_file = PROJECT_ROOT / "trading_engine" / f"autoresearch_history_{target}.json"

    # Ensure baseline weights file exists
    if not weights_file.exists():
        if args.target == "crypto" or args.symbol_specific:
            old_weights_file = PROJECT_ROOT / "trading_engine" / "optimized_weights.json"
            if old_weights_file.exists():
                import shutil
                try:
                    shutil.copy(old_weights_file, weights_file)
                    logger.info(f"Copied existing optimized_weights.json to {weights_file}")
                except Exception as e:
                    logger.warning(f"Could not copy optimized_weights.json: {e}")
            
    if not weights_file.exists():
        logger.info(f"No optimized weights file found for {target}. Initializing with DEFAULT_WEIGHTS.")
        from trading_engine import judge
        with open(weights_file, "w") as f:
            json.dump(judge.DEFAULT_WEIGHTS, f, indent=2)

    # Load starting weights
    with open(weights_file) as f:
        best_weights = json.load(f)

    # Establish baseline performance
    logger.info("Running baseline backtest...")
    baseline_res = run_harness(args.symbols, args.timeframe, args.train_days, args.val_days, target)
    best_fitness = baseline_res["overall_fitness"]
    logger.success(f"Baseline established! Fitness Score: {best_fitness:.4f}")

    # Load or initialize history
    history = []
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
        except Exception:
            pass

    history.append({
        "iteration": 0,
        "weights": best_weights.copy(),
        "fitness": best_fitness,
        "metrics": baseline_res["assets"],
        "reasoning": "Baseline configuration",
        "improved": True
    })

    # Run Loop
    for it in range(1, args.iterations + 1):
        logger.info(f"\n--- Optimization Cycle {it}/{args.iterations} ---")
        
        try:
            # 1. Query LLM for new weights
            proposal = query_llm_for_weights(
                current_weights=best_weights,
                current_fitness=best_fitness,
                performance_metrics=history[-1]["metrics"],
                history=history
            )
            proposed_weights = proposal["weights"]
            reasoning = proposal["reasoning"]
            
            logger.info(f"LLM proposed changes: {reasoning}")
            logger.info(f"Proposed weights: {proposed_weights}")
            
            # 2. Write proposed weights
            with open(weights_file, "w") as f:
                json.dump(proposed_weights, f, indent=2)
                
            # 3. Run backtest harness
            logger.info("Running backtest with proposed weights...")
            res = run_harness(args.symbols, args.timeframe, args.train_days, args.val_days, target)
            proposed_fitness = res["overall_fitness"]
            
            # 4. Compare results
            improved = proposed_fitness > best_fitness
            if improved:
                best_fitness = proposed_fitness
                best_weights = proposed_weights.copy()
                logger.success(f"🎉 SUCCESS: Fitness improved from {best_fitness:.4f} to {proposed_fitness:.4f}!")
            else:
                logger.warning(f"❌ REJECTED: Fitness ({proposed_fitness:.4f}) did not exceed best ({best_fitness:.4f})")
                # Revert
                with open(weights_file, "w") as f:
                    json.dump(best_weights, f, indent=2)

            # Record iteration details
            history.append({
                "iteration": it,
                "weights": proposed_weights,
                "fitness": proposed_fitness,
                "metrics": res["assets"],
                "reasoning": reasoning,
                "improved": improved
            })
            
            # Save history log
            with open(history_file, "w") as f:
                json.dump(history, f, indent=2)

        except Exception as e:
            logger.error(f"Error in cycle {it}: {e}")
            # Ensure we are reverted to best weights in case of failure
            with open(weights_file, "w") as f:
                json.dump(best_weights, f, indent=2)
            continue

    logger.info("\n================================================================================")
    logger.info("                         OPTIMIZATION LOOP COMPLETE")
    logger.info("================================================================================")
    logger.success(f"Final Best Fitness Score: {best_fitness:.4f}")
    logger.success(f"Optimal weights written to {weights_file}")

if __name__ == "__main__":
    main()

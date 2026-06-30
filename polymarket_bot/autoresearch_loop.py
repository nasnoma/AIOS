"""
polymarket_bot/autoresearch_loop.py

Karpathy-style parameter optimization loop for the CEX-DEX Arbitrage Bot.
Runs experiments by adjusting thresholds, running the bot in paper mode,
and analyzing state file metrics to score and find optimal parameters.
"""
import json
import time
import subprocess
import os
from pathlib import Path
from datetime import datetime
from loguru import logger

# Paths
STATE_FILE = Path(__file__).parent / "paper_state.json"
DOTENV_FILE = Path(__file__).parent.parent / ".env"

class ArbitrageAutoresearch:
    def __init__(self, run_time_seconds: int = 600):
        self.run_time_seconds = run_time_seconds
        self.best_params = {"min_spread": 0.0018, "score": -float('inf')}
        self.iteration = 0

    def load_dotenv(self) -> dict:
        """Helper to read current .env values."""
        dotenv_data = {}
        if DOTENV_FILE.exists():
            with open(DOTENV_FILE, "r") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        dotenv_data[k.strip()] = v.strip().strip('"').strip("'")
        return dotenv_data

    def write_dotenv(self, data: dict):
        """Helper to write back to .env."""
        with open(DOTENV_FILE, "w") as f:
            for k, v in data.items():
                f.write(f'{k}="{v}"\n')

    def reset_state_for_experiment(self):
        """Resets PnL and trade history in state file to start fresh for the run."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    state_data = json.load(f)
                
                # Clear active session stats
                state_data["closed_trades"] = []
                state_data["total_pnl"] = 0.0
                state_data["daily_pnl"] = 0.0
                state_data["win_count"] = 0
                state_data["loss_count"] = 0
                state_data["cycle_count"] = 0
                state_data["max_drawdown_paused"] = False
                state_data["total_actual_pnl"] = 0.0
                state_data["total_expected_pnl"] = 0.0
                state_data["total_slippage_usd"] = 0.0
                
                with open(STATE_FILE, "w") as f:
                    json.dump(state_data, f, indent=4)
                logger.info("✅ Reset state history for the new experiment.")
            except Exception as e:
                logger.warning(f"⚠️ Failed to reset state: {e}")

    def run_experiment(self, params: dict) -> float:
        """Run the bot in a subprocess with specific parameters for a fixed time."""
        logger.info(f"🧪 [Iter {self.iteration}] Testing parameters: {params}")

        # Update .env config
        env_data = self.load_dotenv()
        env_data["MIN_ARBITRAGE_SPREAD_PCT"] = str(params["min_spread"])
        env_data["TRADE_SIZE_USDT"] = str(params["trade_size"])
        env_data["TRADING_MODE"] = "paper"
        self.write_dotenv(env_data)

        # Clear state file stats
        self.reset_state_for_experiment()

        # Run bot
        bot_cmd = ["python", "-m", "polymarket_bot.main"]
        logger.info(f"⏳ Running paper scanner for {self.run_time_seconds} seconds...")
        
        try:
            # Launch bot process
            proc = subprocess.Popen(bot_cmd, cwd=str(DOTENV_FILE.parent))
            
            # Wait for execution duration
            time.sleep(self.run_time_seconds)
            
            # Terminate bot process
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("⏹️ Stopped bot process.")
            
            # Evaluate performance
            score = self.evaluate_run()
            return score
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning("⚠️ Bot process timeout expired and was force-killed.")
            return -1000.0
        except Exception as e:
            logger.error(f"❌ Subprocess failed: {e}")
            return -1000.0

    def evaluate_run(self) -> float:
        """
        Parses state file and returns a score for the run.
        Score = Net actual PnL * Win Rate / (1 + Max Drawdown)
        """
        if not STATE_FILE.exists():
            logger.warning("⚠️ No state file found post-run. Score = 0")
            return 0.0

        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)

            trades_count = data.get("cycle_count", 0)
            if trades_count == 0:
                logger.info("ℹ️ No trades executed during this run. Score = 0")
                return 0.0

            actual_pnl = data.get("total_actual_pnl", 0.0)
            win_count = data.get("win_count", 0)
            win_rate = win_count / trades_count if trades_count > 0 else 0.0

            # Calculate drawdown
            account_size = data.get("account_size", 500.0)
            peak_size = data.get("peak_account_size", 500.0)
            drawdown = (peak_size - account_size) / peak_size if peak_size > 0 else 0.0

            # Score formula penalizing drawdown and favoring high win-rate & high net PnL
            score = (actual_pnl * win_rate) / (1.0 + drawdown)
            
            # Add a small penalty if bot over-traded and got high slippage
            slippage_usd = data.get("total_slippage_usd", 0.0)
            score -= (slippage_usd * 0.1)

            logger.info(f"📊 Run Results | Trades: {trades_count} | Net PnL: ${actual_pnl:+.4f} | Win Rate: {win_rate:.1%} | Drawdown: {drawdown:.2%} | Score: {score:.4f}")
            return score

        except Exception as e:
            logger.error(f"❌ Failed to parse state for evaluation: {e}")
            return -1000.0

    def optimize(self, steps: int = 5):
        """Vary parameters sequentially and keep track of the best setting."""
        logger.info("🎯 Starting Autoresearch Optimization Loop...")
        
        # Backup original .env to restore after experiments
        original_env = self.load_dotenv()

        try:
            for i in range(steps):
                self.iteration = i + 1
                
                # Propose candidate parameters (Karpathy-style exploration)
                # Try spread thresholds between 0.12% (0.0012) and 0.28% (0.0028)
                min_spread = round(0.0012 + (i * 0.0004), 4)
                trade_size = 25.0 + (i * 5.0)  # Try sizes between $25 and $45
                
                candidate = {
                    "min_spread": min_spread,
                    "trade_size": trade_size
                }

                score = self.run_experiment(candidate)

                if score > self.best_params["score"]:
                    self.best_params = {
                        "min_spread": min_spread,
                        "trade_size": trade_size,
                        "score": score
                    }
                    logger.success(f"🔥 New best parameters found! {self.best_params}")

            logger.success(f"🏆 Optimization complete! Best config: {self.best_params}")
        finally:
            # Restore original .env
            self.write_dotenv(original_env)
            logger.info("✅ Restored original environment settings.")

if __name__ == "__main__":
    # Runs 4 experiments, each running the bot in paper mode for 20 seconds
    researcher = ArbitrageAutoresearch(run_time_seconds=20)
    researcher.optimize(steps=4)

import sys
sys.path.append("/Users/nasir.noma/claude_projects/AIOS")

import os
from loguru import logger
from trading_engine.config import settings

# Force paper mode to be safe
settings.trading_mode = "paper"

from trading_engine.orchestrator import run_all_assets

logger.info("Running run_all_assets()...")
try:
    results = run_all_assets()
    logger.info(f"Completed run_all_assets. Got {len(results)} results:")
    for r in results:
        logger.info(f"Symbol: {r.symbol} | Final Action: {r.final_action} | Verdict: {r.verdict['decision']}")
except Exception as e:
    logger.exception("Failed to run run_all_assets:")

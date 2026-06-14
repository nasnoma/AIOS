#!/bin/bash
# Start the FastAPI dashboard API server in the background
echo "Starting Dashboard API server..."
python -m trading_engine.api.server &

# Start the Trading Engine Scheduler in the foreground
echo "Starting Trading Engine Scheduler..."
python -m trading_engine.scheduler

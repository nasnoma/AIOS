#!/bin/bash
# Start the copy trading intelligence runner in the background
echo "Starting Copy Trading runner..."
python -m trading_engine.copy_trading.runner &

# Start the Trading Engine Scheduler in the background
echo "Starting Trading Engine Scheduler..."
python -m trading_engine.scheduler &

# Start the FastAPI dashboard API server in the foreground with exec
echo "Starting Dashboard API server..."
exec python -m trading_engine.api.server

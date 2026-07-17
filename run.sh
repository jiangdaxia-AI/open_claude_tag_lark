#!/bin/bash
# Auto-restart wrapper for the ocl gateway.
# If the process exits (e.g. watchdog detects a dead WebSocket), restart it.
cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
    echo "[$(date)] Starting ocl gateway..."
    python -m ocl
    echo "[$(date)] ocl gateway exited (code $?), restarting in 3s..."
    sleep 3
done

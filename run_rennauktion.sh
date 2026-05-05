#!/bin/bash
# run_rennauktion.sh — RennAuktion auction scrape every 300s
# Scheduled via launchd com.rennauktion.scrape

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/rennauktion_cron.log"
PYTHON="/usr/bin/python3"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') RennAuktion scrape ===" >> "$LOG"
cd "$SCRIPT_DIR"

"$PYTHON" rennauktion/main.py "$@" >> "$LOG" 2>&1 || echo "=== rennauktion/main.py exited $? ===" >> "$LOG"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ===" >> "$LOG"

# Rotate log if over 5MB
if [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "${LOG}.1"
fi

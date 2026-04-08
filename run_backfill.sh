#!/bin/bash
# Launch backfill in background, logging to logs/backfill_run.log
cd "$(dirname "$0")"
mkdir -p logs
/opt/homebrew/bin/python3.11 backfill_comps.py >> logs/backfill_run.log 2>&1

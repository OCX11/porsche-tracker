#!/bin/bash
# run_rennmarkt.sh — RennMarkt retail scrape every 120s
# Scheduled via launchd com.rennmarkt.scrape

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/rennmarkt_cron.log"
PYTHON="/usr/bin/python3"

DOW="$(date '+%u')"   # 1=Mon … 7=Sun
DOM="$(date '+%-d')"  # Day of month

echo "=== $(date '+%Y-%m-%d %H:%M:%S') RennMarkt scrape ===" >> "$LOG"
cd "$SCRIPT_DIR"

# Main retail scrape (every run)
"$PYTHON" rennmarkt/main.py "$@" >> "$LOG" 2>&1 || echo "=== rennmarkt/main.py exited $? ===" >> "$LOG"

# Supplemental enrichment (every run)
"$PYTHON" enrich_listings.py >> "$LOG" 2>&1 || echo "=== enrich_listings.py exited $? ===" >> "$LOG"

# Monday: VIN enrichment + weekly report
if [ "$DOW" -eq 1 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Monday: VIN enrich + weekly report ===" >> "$LOG"
    "$PYTHON" enrich_bat_vins.py >> "$LOG" 2>&1 || echo "=== enrich_bat_vins.py exited $? ===" >> "$LOG"
    "$PYTHON" weekly_report.py >> "$LOG" 2>&1 || echo "=== weekly_report.py exited $? ===" >> "$LOG"
fi

# Month start: monthly report + hagerty
if [ "$DOM" -eq 1 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Month start: monthly report + hagerty ===" >> "$LOG"
    "$PYTHON" monthly_report.py >> "$LOG" 2>&1 || echo "=== monthly_report.py exited $? ===" >> "$LOG"
    "$PYTHON" -c "import sys; sys.path.insert(0,'.'); import comp_scraper; comp_scraper.run_hagerty_scrape()" >> "$LOG" 2>&1 || echo "=== hagerty exited $? ===" >> "$LOG"
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ===" >> "$LOG"

# Rotate log if over 5MB
if [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "${LOG}.1"
fi

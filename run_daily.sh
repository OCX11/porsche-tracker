#!/bin/bash
# Run script for Porsche competitor inventory tracker
# Scheduled via launchd (com.porschetracker.scrape.plist)
# Peak: every 20 min (7AM–10:40PM)  Off-peak: every 60 min (11PM–6AM)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/cron.log"

# Absolute path to the Xcode CLTools Python 3.9 that has all packages installed.
# /usr/bin/python3 is the Xcode shim → Python 3.9 with site-packages at
# ~/Library/Python/3.9/lib/python/site-packages (requests, bs4, playwright, etc.)
# Do NOT use `python3` bare — Homebrew may shadow it with a bare interpreter that has no packages.
PYTHON="/usr/bin/python3"

DOW="$(date '+%u')"     # 1=Mon … 7=Sun
DOM="$(date '+%-d')"    # Day of month (no leading zero)

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Starting scrape ===" >> "$LOG"
cd "$SCRIPT_DIR"

# ── Main scrape + daily report (every run) ─────────────────────────────────
# Each step runs independently — a failure in one does not abort the others.
"$PYTHON" main.py >> "$LOG" 2>&1 || echo "=== main.py exited $? ===" >> "$LOG"
"$PYTHON" enrich_listings.py >> "$LOG" 2>&1 || echo "=== enrich_listings.py exited $? ===" >> "$LOG"
"$PYTHON" enrich_rennlist.py >> "$LOG" 2>&1 || echo "=== enrich_rennlist.py exited $? ===" >> "$LOG"
"$PYTHON" notify_gunther.py >> "$LOG" 2>&1 || echo "=== notify_gunther.py exited $? ===" >> "$LOG"

# ── Sold-comp scrape + VIN enrichment + weekly report (every Monday) ────────
if [ "$DOW" -eq 1 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Monday: comp scrape + VIN enrich + weekly report ===" >> "$LOG"
    "$PYTHON" main.py --comps >> "$LOG" 2>&1 || echo "=== main.py --comps exited $? ===" >> "$LOG"
    "$PYTHON" enrich_bat_vins.py >> "$LOG" 2>&1 || echo "=== enrich_bat_vins.py exited $? ===" >> "$LOG"
    "$PYTHON" main.py --weekly >> "$LOG" 2>&1 || echo "=== main.py --weekly exited $? ===" >> "$LOG"
fi

# ── Monthly report (1st of each month) ─────────────────────────────────────
if [ "$DOM" -eq 1 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Month start: comp scrape + monthly report ===" >> "$LOG"
    "$PYTHON" main.py --comps >> "$LOG" 2>&1 || echo "=== main.py --comps exited $? ===" >> "$LOG"
    "$PYTHON" main.py --monthly >> "$LOG" 2>&1 || echo "=== main.py --monthly exited $? ===" >> "$LOG"
    "$PYTHON" main.py --hagerty >> "$LOG" 2>&1 || echo "=== main.py --hagerty exited $? ===" >> "$LOG"
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ===" >> "$LOG"

# Keep log under 5MB (rotate if exceeded)
if [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "${LOG}.1"
fi

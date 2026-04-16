#!/bin/bash
# watch_code.sh — watches scraper.log and git log for changes
# Writes a trigger file when something new happens so PM thread can detect it
#
# Usage: ./watch_code.sh &
# It runs in background, updating /tmp/pt_code_done.txt whenever scraper.log changes

LOG="/Users/claw/porsche-tracker/logs/scraper.log"
TRIGGER="/tmp/pt_code_done.txt"

echo "Watching $LOG for changes..."
LAST_LINE=""

while true; do
    CURRENT_LINE=$(tail -1 "$LOG" 2>/dev/null)
    if [ "$CURRENT_LINE" != "$LAST_LINE" ]; then
        LAST_LINE="$CURRENT_LINE"
        TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
        echo "$TIMESTAMP | $CURRENT_LINE" >> "$TRIGGER"
    fi
    sleep 10
done

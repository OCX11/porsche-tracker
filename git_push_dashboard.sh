#!/bin/bash
# Pushes updated dashboard files to GitHub Pages every 2 minutes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/git_push.log"
cd "$SCRIPT_DIR"

# Always refresh gh token to avoid credential expiry breaking pushes
TOKEN=$(/opt/homebrew/bin/gh auth token 2>/dev/null)
if [ -n "$TOKEN" ]; then
    git remote set-url origin "https://$TOKEN@github.com/OCX11/PTOX11.git"
fi

git add docs/index.html docs/auctions.html docs/search_data.json \
        docs/calculator_data.json \
        docs/daily_report.html docs/market_report.html \
        docs/weekly_report.html docs/monthly_report.html 2>> "$LOG"

if git diff --cached --quiet; then
    git remote set-url origin https://github.com/OCX11/PTOX11.git
    exit 0
fi

git commit -m "Dashboard update $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1

# Reset to clean URL (no token in remote)
git remote set-url origin https://github.com/OCX11/PTOX11.git

echo "=== $(date '+%Y-%m-%d %H:%M:%S') pushed ===" >> "$LOG"

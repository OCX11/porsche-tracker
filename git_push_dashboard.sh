#!/bin/bash
# Pushes updated dashboard files to GitHub Pages every 2 minutes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/git_push.log"
cd "$SCRIPT_DIR"

# ── Guard: validate docs/index.html JS before pushing ────────────────────────
# Extracts the main <script> block and runs node --check on it.
# If syntax is broken, aborts push and logs the error — never ships a blank page.
DASHBOARD="$SCRIPT_DIR/docs/index.html"
if [ -f "$DASHBOARD" ]; then
  GUARD_TMP="/tmp/rennmarkt_guard_$$.js"
  # Stub out browser globals so node --check sees valid references
  cat > "$GUARD_TMP" << 'STUB'
const document={getElementById:()=>({value:'',checked:false,textContent:'',innerHTML:'',insertAdjacentHTML:()=>{},classList:{add:()=>{},remove:()=>{}},children:{length:0},addEventListener:()=>{},style:{}}),querySelector:()=>null,querySelectorAll:()=>({forEach:()=>{},length:0}),body:{insertAdjacentHTML:()=>{}},addEventListener:()=>{}};
const location={hostname:'www.rennmarkt.net',hash:''};
const window={location,addEventListener:()=>{}};
const localStorage={getItem:()=>null,setItem:()=>{}};
STUB
  # Append the second <script> block (main JS) from the dashboard
  python3 -c "
import re, sys
html = open('$DASHBOARD').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
if len(scripts) >= 2:
    sys.stdout.write(scripts[1])
else:
    sys.exit(1)
" >> "$GUARD_TMP" 2>/dev/null
  GUARD_EXIT=$?
  if [ $GUARD_EXIT -ne 0 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') GUARD: could not extract script from dashboard — skipping push ===" >> "$LOG"
    rm -f "$GUARD_TMP"
    exit 1
  fi
  if ! node --check "$GUARD_TMP" 2>> "$LOG"; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') GUARD: JS syntax error in docs/index.html — push aborted ===" >> "$LOG"
    rm -f "$GUARD_TMP"
    exit 1
  fi
  rm -f "$GUARD_TMP"
fi

# ── Push ──────────────────────────────────────────────────────────────────────
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

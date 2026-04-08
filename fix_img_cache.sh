#!/bin/bash
# Delete fake HTML files that were incorrectly downloaded as images,
# then regenerate the dashboard so it copies real images from static/img_cache/
set -e
cd "$(dirname "$0")"

echo "=== Cleaning docs/img_cache/ ==="
if [ -d docs/img_cache ]; then
    # Remove HTML files masquerading as images
    for f in docs/img_cache/*.jpg docs/img_cache/*.png docs/img_cache/*.webp; do
        [ -f "$f" ] || continue
        if file "$f" | grep -q "HTML"; then
            echo "  Removing fake: $f"
            rm -f "$f"
        fi
    done
    echo "  Done."
else
    echo "  docs/img_cache/ doesn't exist yet — will be created by dashboard."
fi

echo ""
echo "=== Checking static/img_cache/ (scraper-downloaded real images) ==="
count=$(ls static/img_cache/ 2>/dev/null | wc -l | tr -d ' ')
echo "  $count files in static/img_cache/"

echo ""
echo "=== Regenerating dashboard ==="
python3 new_dashboard.py

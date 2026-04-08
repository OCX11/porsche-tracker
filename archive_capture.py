"""
archive_capture.py
------------------
Background service that captures listing pages (HTML + screenshot) for
newly seen listings where html_path IS NULL.

Processes up to 20 listings per run, sleeping 3s between each to be polite.
Designed to be run on a schedule (every 10 minutes via launchd).

Saves to:
  archive/html/YYYY/{listing_id}_{safe_id}.html
  archive/screenshots/YYYY/{listing_id}.png
"""

import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "logs" / "archive_capture.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
from db import get_conn, init_db, update_listing_paths

# ── Config ────────────────────────────────────────────────────────────────────
BATCH_SIZE        = 20
SLEEP_BETWEEN     = 3      # seconds between captures
REQUEST_TIMEOUT   = 15     # seconds for HTML fetch
SCREENSHOT_WIDTH  = 1400   # max viewport width for screenshots
SCREENSHOT_HEIGHT = 900

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [capture] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _archive_dir(subdir: str, year: int) -> Path:
    d = BASE_DIR / "archive" / subdir / str(year)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(vin: str, listing_id: int) -> str:
    """Return vin if available, else the listing id as string."""
    if vin:
        return re.sub(r"[^A-Za-z0-9]", "_", vin)[:17]
    return str(listing_id)


def _year_from_listing(year) -> int:
    try:
        y = int(year)
        return y if 1960 <= y <= 2030 else datetime.now().year
    except (TypeError, ValueError):
        return datetime.now().year


# ── HTML capture ──────────────────────────────────────────────────────────────

def capture_html(listing_id: int, url: str, year: int, vin: str) -> str:
    """
    Fetch listing HTML with requests.
    Returns relative path on success, 'FAILED' on error.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            log.warning("  HTML fetch %s → HTTP %s", url[:80], resp.status_code)
            return "FAILED"

        html_dir  = _archive_dir("html", year)
        fname     = f"{listing_id}_{_safe_id(vin, listing_id)}.html"
        full_path = html_dir / fname
        full_path.write_bytes(resp.content)

        rel_path = str(full_path.relative_to(BASE_DIR))
        log.debug("  HTML saved → %s (%d bytes)", rel_path, len(resp.content))
        return rel_path

    except requests.exceptions.Timeout:
        log.warning("  HTML fetch timeout: %s", url[:80])
        return "FAILED"
    except Exception as exc:
        log.warning("  HTML fetch error: %s — %s", url[:80], exc)
        return "FAILED"


# ── Screenshot capture ────────────────────────────────────────────────────────

def capture_screenshot(listing_id: int, url: str, year: int) -> str:
    """
    Take a full-page screenshot with Playwright headless Chromium.
    Returns relative path on success, empty string on error.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        shot_dir  = _archive_dir("screenshots", year)
        fname     = f"{listing_id}.png"
        full_path = shot_dir / fname

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context(
                viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
                user_agent=UA,
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(2_000)   # let JS render
            except PWTimeout:
                log.warning("  Screenshot navigation timeout: %s", url[:80])
                # Take a screenshot of whatever loaded
            page.screenshot(path=str(full_path), full_page=True)
            browser.close()

        rel_path = str(full_path.relative_to(BASE_DIR))
        log.debug("  Screenshot saved → %s (%d bytes)", rel_path, full_path.stat().st_size)
        return rel_path

    except Exception as exc:
        log.warning("  Screenshot error for %s: %s", url[:80], exc)
        return ""


# ── Main batch ────────────────────────────────────────────────────────────────

def run_batch():
    init_db()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, listing_url, year, make, model, trim, dealer, vin
            FROM   listings
            WHERE  html_path IS NULL
              AND  listing_url IS NOT NULL
              AND  listing_url != ''
              AND  listing_url != 'FAILED'
            ORDER BY date_first_seen DESC
            LIMIT  ?
        """, (BATCH_SIZE,)).fetchall()

    if not rows:
        log.info("No listings need capturing — all up to date")
        return

    log.info("Capturing %d listing(s)", len(rows))
    captured = failed = 0

    for row in rows:
        lid   = row["id"]
        url   = row["listing_url"]
        year  = _year_from_listing(row["year"])
        vin   = row["vin"] or ""
        desc  = f"{row['year'] or '?'} {row['model'] or ''} {row['trim'] or ''}".strip()

        log.info("Capturing id=%d  %s  [%s]  %s", lid, desc, row["dealer"], url[:70])

        html_path = capture_html(lid, url, year, vin)
        shot_path = ""

        if html_path != "FAILED":
            shot_path = capture_screenshot(lid, url, year)
            captured += 1
        else:
            failed += 1

        with get_conn() as conn:
            update_listing_paths(conn, lid, html_path=html_path,
                                 screenshot_path=shot_path if shot_path else None)

        if html_path != "FAILED":
            parts = []
            if html_path:
                parts.append("html")
            if shot_path:
                parts.append("screenshot")
            log.info("  CAPTURED id=%d %s [%s] → %s saved",
                     lid, desc, row["dealer"], "+".join(parts) if parts else "nothing")
        else:
            log.warning("  FAILED   id=%d %s [%s]", lid, desc, row["dealer"])

        if row is not rows[-1]:
            time.sleep(SLEEP_BETWEEN)

    log.info("Batch done — captured=%d  failed=%d", captured, failed)


if __name__ == "__main__":
    run_batch()

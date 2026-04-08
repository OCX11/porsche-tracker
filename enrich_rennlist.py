#!/usr/bin/python3
"""
enrich_rennlist.py
------------------
Fetches the Rennlist marketplace listing index, parses individual listing URLs
and image URLs from each card's JSON-LD <script> block, then updates inventory.db
for active Rennlist listings that are missing either field (or still carry the
search-page URL as listing_url).

Matching strategy: (year, price) — the only fields reliably present in both
the Distill-polled DB rows and the Rennlist JSON-LD.

Run from ~/porsche-tracker/ via run_daily.sh.
"""

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "logs" / "enrich_rennlist.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [enrich_rennlist] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))
from db import get_conn, init_db

_RENNLIST_BASE = "https://rennlist.com/forums/market/vehicles"
_SEARCH_URL_FRAGMENT = "rennlist.com/forums/market/vehicles"


def _chrome_cookies():
    """Return a requests CookieJar for rennlist.com from Chrome's local store."""
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="rennlist.com")
        log.info("Loaded %d Chrome cookies for rennlist.com", sum(1 for _ in cj))
        return cj
    except Exception as e:
        log.warning("Could not load Chrome cookies: %s", e)
        return None


def _fetch_page(page: int, cj):
    """Fetch one marketplace index page. Returns BeautifulSoup or None."""
    url = f"{_RENNLIST_BASE}?page={page}" if page > 1 else _RENNLIST_BASE
    try:
        r = requests.get(
            url,
            cookies=cj,
            proxies={},   # bypass all system/configured proxies (Rennlist blocks proxy IPs)
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://rennlist.com/forums/market/",
            },
            timeout=30,
            allow_redirects=True,
        )
        r.raise_for_status()
        log.info("page %d → HTTP %d (%d bytes)", page, r.status_code, len(r.content))
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning("Fetch error page %d: %s", page, e)
        return None


def _parse_listings(soup) -> list[dict]:
    """
    Extract (year, price, listing_url, image_url) from all Car JSON-LD blocks
    on a single marketplace index page.
    """
    results = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            t = item.get("@type", "")
            if isinstance(t, list):
                t = " ".join(t)
            if "Car" not in t and "Vehicle" not in t:
                continue

            # year
            year = None
            for key in ("modelDate", "vehicleModelDate"):
                try:
                    year = int(item.get(key) or 0) or None
                except (TypeError, ValueError):
                    pass
                if year:
                    break

            # price
            price = None
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                try:
                    price = int(offers.get("price") or 0) or None
                except (TypeError, ValueError):
                    pass

            # listing URL
            listing_url = (item.get("url") or "").strip() or None

            # image — upgrade thumbnail to larger size if available
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            image = (image or "").strip() or None
            if image and "160x120" in image:
                image = image.replace("160x120", "800x600")

            if not year and not price:
                continue

            results.append({
                "year":        year,
                "price":       price,
                "listing_url": listing_url,
                "image_url":   image,
            })

    return results


def _needs_enrichment(row) -> tuple[bool, bool]:
    """Return (needs_url, needs_image) for a DB row."""
    url = row["listing_url"] or ""
    needs_url   = not url or _SEARCH_URL_FRAGMENT in url
    needs_image = not row["image_url"]
    return needs_url, needs_image


def main():
    log.info("=" * 60)
    log.info("enrich_rennlist starting")

    init_db()
    cj = _chrome_cookies()
    if cj is None:
        log.error("No cookies — cannot authenticate to Rennlist. Exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 1. Fetch all Rennlist marketplace pages and collect (year, price) → URLs
    # ------------------------------------------------------------------
    # Key: (year, price); value: first matching card dict.
    # If multiple cards share (year, price) the first one wins (rare collision).
    scraped: dict[tuple, dict] = {}
    page = 1
    max_pages = 40

    while page <= max_pages:
        soup = _fetch_page(page, cj)
        if soup is None:
            log.warning("Failed to fetch page %d — stopping pagination", page)
            break

        ld_scripts = soup.find_all("script", type="application/ld+json")
        car_block_count = sum(
            1 for t in ld_scripts
            if t.string and '"@type":"Car"' in t.string
        )
        if car_block_count == 0:
            log.info("Page %d has no Car listings — end of results", page)
            break

        cards = _parse_listings(soup)
        log.info("Page %d → %d Car listings parsed", page, len(cards))

        for card in cards:
            key = (card["year"], card["price"])
            if key not in scraped:
                scraped[key] = card

        page += 1

        import time
        time.sleep(0.8)

    log.info("Total unique (year, price) keys scraped: %d", len(scraped))

    # ------------------------------------------------------------------
    # 2. Load active Rennlist DB rows that need enrichment
    # ------------------------------------------------------------------
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, year, price, listing_url, image_url
               FROM listings
               WHERE dealer='Rennlist' AND status='active'
                 AND (
                     image_url IS NULL
                     OR listing_url IS NULL
                     OR listing_url LIKE ?
                 )""",
            (f"%{_SEARCH_URL_FRAGMENT}%",)
        ).fetchall()

        if not rows:
            log.info("No Rennlist rows need enrichment. Done.")
            return

        log.info("%d Rennlist rows need enrichment", len(rows))

        updated_url   = 0
        updated_image = 0
        unmatched     = 0

        for row in rows:
            key = (row["year"], row["price"])
            card = scraped.get(key)
            if card is None:
                log.debug("No scraped card for key %s (id=%d)", key, row["id"])
                unmatched += 1
                continue

            needs_url, needs_image = _needs_enrichment(row)
            new_url   = card["listing_url"] if needs_url   else None
            new_image = card["image_url"]   if needs_image else None

            if not new_url and not new_image:
                continue

            conn.execute(
                """UPDATE listings
                   SET listing_url = CASE WHEN ? IS NOT NULL THEN ? ELSE listing_url END,
                       image_url   = CASE WHEN ? IS NOT NULL THEN ? ELSE image_url   END
                   WHERE id = ?""",
                (new_url, new_url, new_image, new_image, row["id"])
            )

            parts = []
            if new_url:
                parts.append(f"url → {new_url}")
                updated_url += 1
            if new_image:
                parts.append("image updated")
                updated_image += 1
            log.info("  id=%-6d  %s %s  $%s  —  %s",
                     row["id"], row["year"] or "?",
                     "", f"{row['price']:,}" if row["price"] else "?",
                     "  |  ".join(parts))

    log.info(
        "Done — url_updated=%d  image_updated=%d  unmatched=%d",
        updated_url, updated_image, unmatched,
    )


if __name__ == "__main__":
    main()

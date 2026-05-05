"""
backfill_cnb_comps.py — One-time backfill of Cars & Bids sold Porsche comps.

Strategy:
1. Fetch https://carsandbids.com/cab-sitemap/auctions.xml to get all Porsche auction URLs
2. Filter to 911/Cayman/Boxster/718 (exclude Cayenne/Macan/Panamera/Taycan)
3. Skip URLs already in sold_comps
4. Use Playwright to scrape each auction page via the v2/autos/auctions/{id} API
   (Playwright has the auth context; direct API calls are blocked)
5. Parse: year, model, trim, mileage, transmission, sold_price, sold_date
6. Write to sold_comps via db.upsert_sold_comp

Rate limit: 0.5s between pages. ~500 valid pages = ~5-8 min total.
Run once; subsequent runs are idempotent (skip existing URLs).
"""
import re
import json
import time
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core.db as db
from shared.scraper_utils import _parse_ymmt, _is_valid_listing, YEAR_MIN, YEAR_MAX

log = logging.getLogger(__name__)

_CNB_BASE = "https://carsandbids.com"
_SITEMAP  = "https://carsandbids.com/cab-sitemap/auctions.xml"

_EXCLUDED_MODELS = frozenset({'cayenne', 'macan', 'panamera', 'taycan', '918'})
_TARGET_MODELS   = frozenset({'911', 'cayman', 'boxster', '718', '930', '964', '993',
                               '996', '997', '991', '992', 'gt3', 'gt4', 'turbo', 'spyder'})


def _is_target_url(url):
    """Quick URL-based filter before fetching the page."""
    low = url.lower()
    if any(x in low for x in ('cayenne', 'macan', 'panamera', 'taycan', '918-spyder')):
        return False
    return True


def _get_all_porsche_urls():
    """Fetch sitemap and return all Porsche auction URLs."""
    from curl_cffi import requests as cr
    r = cr.get(_SITEMAP, impersonate="safari17_0", timeout=30)
    if r.status_code != 200:
        log.error("Sitemap fetch failed: %d", r.status_code)
        return []
    urls = re.findall(r'<loc>(https://carsandbids\.com/auctions/[^<]+)</loc>', r.text)
    porsche = [u for u in urls if 'porsche' in u.lower()]
    log.info("Sitemap: %d total auctions, %d Porsche", len(urls), len(porsche))
    return porsche


def _get_known_urls():
    """Return set of C&B listing URLs already in sold_comps."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT listing_url FROM sold_comps WHERE source='Cars and Bids' AND listing_url IS NOT NULL"
    ).fetchall()
    conn.close()
    return {r[0].rstrip('/') for r in rows}


def _parse_title_cnb(title):
    """Parse 'YYYY Porsche Model Trim' from C&B title."""
    m = re.match(r'(\d{4})\s+Porsche\s+(.*)', title, re.I)
    if not m:
        return None, None, None
    year = int(m.group(1))
    rest = m.group(2).strip()
    parts = rest.split(None, 1)
    model = parts[0] if parts else rest
    trim  = parts[1] if len(parts) > 1 else None
    return year, model, trim


def _parse_mileage(text):
    if not text:
        return None
    m = re.search(r'([\d,]+)', str(text))
    return int(m.group(1).replace(',', '')) if m else None


def scrape_ended_auction(page, url):
    """
    Load one C&B ended auction page via Playwright and extract comp data.
    Returns dict or None.
    """
    try:
        auction_info = {}

        def on_response(resp):
            rurl = resp.url
            ct = resp.headers.get('content-type', '')
            if 'json' in ct and 'v2/autos/auctions/' in rurl:
                try:
                    body = resp.json()
                    if isinstance(body, dict) and 'stats' in body and 'listing' in body:
                        auction_info['data'] = body
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1.0)
        page.remove_listener("response", on_response)

        if not auction_info.get('data'):
            return None

        data    = auction_info['data']
        stats   = data.get('stats', {})
        listing = data.get('listing', {})

        sale_price = stats.get('sale_amount')
        if not sale_price:
            return None  # Not sold (reserve not met or still live)

        title = listing.get('title', '')
        year, model, trim = _parse_title_cnb(title)
        if not year or not model:
            return None

        # Filter excluded models
        model_low = model.lower()
        if any(x in model_low for x in _EXCLUDED_MODELS):
            return None
        if not any(x in model_low for x in _TARGET_MODELS):
            return None

        if not (YEAR_MIN <= year <= YEAR_MAX):
            return None

        mileage = _parse_mileage(listing.get('mileage'))
        tx_raw  = listing.get('transmission')
        transmission = None
        if tx_raw == 2:
            transmission = 'Manual'
        elif tx_raw == 1:
            transmission = 'PDK'

        sold_date = None
        ended_raw = stats.get('auction_end') or data.get('auction_end')
        if ended_raw:
            try:
                dt = datetime.fromisoformat(ended_raw.replace('Z', '+00:00'))
                sold_date = dt.strftime('%Y-%m-%d')
            except Exception:
                sold_date = str(ended_raw)[:10]

        img = listing.get('main_photo', {})
        image_url = None
        if isinstance(img, dict) and img.get('base_url') and img.get('path'):
            image_url = f"https://{img['base_url']}/{img['path']}"

        return dict(
            year=year, make='Porsche', model=model, trim=trim,
            mileage=mileage, transmission=transmission,
            sold_price=sale_price, sold_date=sold_date,
            listing_url=url, image_url=image_url,
            title=title, source='Cars and Bids',
        )

    except Exception as e:
        log.debug("scrape_ended_auction error %s: %s", url, e)
        return None


def run_backfill(max_pages=None, delay=0.6):
    """
    Main backfill entry point.
    max_pages: limit for testing (None = all).
    """
    db.init_db()
    conn = db.get_conn()

    log.info("Fetching C&B Porsche auction URLs from sitemap...")
    all_urls = _get_all_porsche_urls()
    target_urls = [u for u in all_urls if _is_target_url(u)]
    log.info("Target URLs (after model filter): %d", len(target_urls))

    known_urls = _get_known_urls()
    new_urls = [u for u in target_urls if u.rstrip('/') not in known_urls]
    log.info("New URLs (not in DB): %d / %d", len(new_urls), len(target_urls))

    if max_pages:
        new_urls = new_urls[:max_pages]
        log.info("Capped to %d for this run", max_pages)

    if not new_urls:
        log.info("Nothing to backfill — all C&B Porsche auctions already in DB")
        return 0

    from playwright.sync_api import sync_playwright
    saved = 0
    skipped = 0
    errors  = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        for i, url in enumerate(new_urls):
            try:
                comp = scrape_ended_auction(page, url)
                if comp:
                    db.upsert_sold_comp(
                        conn,
                        source='Cars and Bids',
                        year=comp['year'], make=comp['make'],
                        model=comp['model'], trim=comp['trim'],
                        mileage=comp['mileage'], sold_price=comp['sold_price'],
                        sold_date=comp['sold_date'], listing_url=comp['listing_url'],
                        image_url=comp.get('image_url'), title=comp.get('title'),
                        transmission=comp.get('transmission'),
                    )
                    conn.commit()
                    saved += 1
                    log.info("[%d/%d] SAVED: %s %s $%s",
                             i+1, len(new_urls), comp['year'], comp['title'], comp['sold_price'])
                else:
                    skipped += 1
                    log.debug("[%d/%d] skipped: %s", i+1, len(new_urls), url[-50:])

            except Exception as e:
                errors += 1
                log.warning("[%d/%d] error %s: %s", i+1, len(new_urls), url[-40:], e)

            if (i + 1) % 25 == 0:
                log.info("Progress: %d/%d  saved=%d skipped=%d errors=%d",
                         i+1, len(new_urls), saved, skipped, errors)

            time.sleep(delay)

        browser.close()

    conn.close()
    log.info("C&B backfill complete: saved=%d skipped=%d errors=%d", saved, skipped, errors)
    return saved


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None, help="Max pages to scrape (for testing)")
    parser.add_argument("--delay", type=float, default=0.6, help="Delay between pages (sec)")
    args = parser.parse_args()
    run_backfill(max_pages=args.max, delay=args.delay)

"""
rennauktion/scrapers/bat.py — Bring a Trailer scraper.
Extracted from scraper.py. Direct requests (no Playwright needed).
"""
import re
import logging
from datetime import datetime as _dt

import requests as _req
from bs4 import BeautifulSoup

from shared.scraper_utils import (
    SESSION, _clean, _int, _parse_ymmt, _is_valid_listing, _dedupe,
)

log = logging.getLogger(__name__)

DEALER_NAME = "Bring a Trailer"

BAT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer":    "https://www.google.com/",
    "Accept":     "text/html,application/xhtml+xml",
}


def scrape_bat():
    """bringatrailer.com/porsche/ — active auctions via direct requests.

    BaT server-renders all listing cards in the initial HTML.
    Direct requests gets all 100+ cards before JS runs.
    """
    cars = []
    try:
        resp = _req.get("https://bringatrailer.com/porsche/",
                        headers=BAT_HEADERS, timeout=30)
        if resp.status_code != 200:
            log.warning("BaT scraper: HTTP %d — returning []", resp.status_code)
            return []
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning("BaT scraper: request failed: %s", e)
        return []

    for card in soup.select("div.listing-card"):
        a = card.select_one("h3 > a") or card.select_one("h2 > a") or card.select_one("a[href]")
        if not a:
            continue
        title = _clean(a.get_text()) or ""
        url = a.get("href", "")
        if url and not url.startswith("http"):
            url = "https://bringatrailer.com" + url

        mileage = None
        mm = re.search(r"([\d,]+)(k)?-Mile", title, re.I)
        if mm:
            val = int(mm.group(1).replace(",", ""))
            mileage = val * 1000 if mm.group(2) else val

        clean_title = re.sub(r"[\d,]+k?-Mile\s+", "", title, flags=re.I).strip()
        year, make, model, trim = _parse_ymmt(clean_title)

        bid_el = card.select_one("span.bid-formatted, [class*='bid-amount'], [class*='current-bid']")
        price = _int(bid_el.get_text()) if bid_el else None

        img = card.select_one("div.thumbnail img, .listing-thumbnail img, img[src]")
        image_url = None
        if img:
            image_url = (img.get("src") or img.get("data-src") or "").split("?")[0] or None

        auction_ends_at = None
        ts_end = card.get("data-timestamp_end")
        if ts_end:
            try:
                auction_ends_at = _dt.utcfromtimestamp(int(ts_end)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OSError):
                pass

        if not year:
            continue
        c = dict(year=year, make=make or "Porsche", model=model, trim=trim,
                 mileage=mileage, price=price, vin=None, listing_url=url,
                 image_url=image_url, auction_ends_at=auction_ends_at)
        if _is_valid_listing(c):
            cars.append(c)

    log.info("BaT scraper: %d valid from %d cards",
             len(cars), len(soup.select("div.listing-card")))
    return _dedupe(cars)


def fetch_bat_sold_price(url):
    """Fetch a BaT listing page and parse the final hammer price.
    Returns int price or None.
    """
    try:
        r = SESSION.get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        m = re.search(r"[Ss]old\s+for\s+\$\s*([\d,]+)", r.text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as exc:
        log.debug("fetch_bat_sold_price error %s: %s", url, exc)
    return None

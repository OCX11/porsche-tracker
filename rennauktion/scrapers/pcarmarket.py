"""
rennauktion/scrapers/pcarmarket.py — pcarmarket scraper.
Extracted from scraper.py. Playwright required.
Scrapes /auctions/ and /marketplace pages.
"""
import re
import logging
from datetime import datetime as _dt, timedelta, timezone

from bs4 import BeautifulSoup

from shared.scraper_utils import (
    _playwright_available, _pw_launch, _stealth_page,
    _int, _parse_ymmt, _is_valid_listing, _dedupe,
)

log = logging.getLogger(__name__)

DEALER_NAME = "pcarmarket"


def _parse_pcar_relative_time(text):
    """Parse pcarmarket 'Ends In' value to ISO UTC string.
    Handles: '45M', '2H 56M', '1D 4H 32M'.
    Returns ISO UTC string or None.
    """
    days = hours = mins = 0
    for token in text.upper().split():
        token = token.strip()
        if token.endswith("D") and token[:-1].isdigit():
            days = int(token[:-1])
        elif token.endswith("H") and token[:-1].isdigit():
            hours = int(token[:-1])
        elif token.endswith("M") and token[:-1].isdigit():
            mins = int(token[:-1])
    if days == 0 and hours == 0 and mins == 0:
        return None
    ends = _dt.now(timezone.utc) + timedelta(days=days, hours=hours, minutes=mins)
    return ends.strftime("%Y-%m-%dT%H:%M:%SZ")


def scrape_pcarmarket():
    """pcarmarket.com — Porsche auction + marketplace, Playwright required.
    Scrapes two pages:
      /auctions/   — time-limited auctions
      /marketplace — buy-now listings
    """
    if not _playwright_available():
        log.warning("pcarmarket scraper requires Playwright")
        return []

    BASE = "https://www.pcarmarket.com"
    PAGES = ["/auctions/", "/marketplace"]

    _CHASSIS = {
        "992": "911", "992.1": "911", "992.2": "911",
        "991": "911", "991.1": "911", "991.2": "911",
        "993": "911", "964": "911", "930": "911",
        "996": "911", "997": "911", "997.1": "911", "997.2": "911",
        "911": "911",
        "986": "Boxster", "987": "Boxster", "987.2": "Boxster",
        "981": "Boxster", "982": "Boxster",
        "987c": "Cayman", "981c": "Cayman", "982c": "Cayman",
        "boxster": "Boxster", "cayman": "Cayman", "718": "718",
    }

    cars = []
    seen = set()

    def _parse_html(html):
        soup = BeautifulSoup(html, "lxml")
        for ns in soup.find_all("noscript"):
            ns.decompose()
        for a in soup.select("a[href*='/auction/']"):
            href = a.get("href", "")
            url = href if href.startswith("http") else f"{BASE}{href}"
            text = a.get_text(" ", strip=True)

            text = re.sub(r"^SAVE\s+LISTING\s*", "", text, flags=re.I).strip()
            text = re.sub(r"^MarketPlace:\s*", "", text, flags=re.I).strip()
            text = re.sub(r"\s+ENDS\s+IN\b.*$", "", text, flags=re.I).strip()
            text = re.sub(r"\s+HIGH\s+BID\b.*$", "", text, flags=re.I).strip()
            text = re.sub(r"\s+MARKETPLACE\b.*$", "", text, flags=re.I).strip()
            text = re.sub(r"\s*[—–-]\s*(Active|Sold|Pending).*$", "", text, flags=re.I).strip()

            if not text:
                continue

            if not re.match(r"^\d{4}\s", text):
                m = re.search(r"\b(\d{4})\s", text)
                if m:
                    text = text[m.start():]
                else:
                    continue

            year, make, model, trim = _parse_ymmt(text)
            if not year:
                continue

            if model:
                canonical = _CHASSIS.get(model) or _CHASSIS.get(model.lower())
                if canonical:
                    model = canonical

            key = url or f"{year}{model}{trim}"
            if key in seen:
                continue
            seen.add(key)

            img_tag = a.find("img")
            image_url = None
            if img_tag:
                for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                    val = (img_tag.get(attr) or "").strip()
                    if val and "cloudfront.net" in val:
                        image_url = val
                        break
                if not image_url:
                    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                        val = (img_tag.get(attr) or "").strip()
                        if val and val.startswith("http") and not val.startswith("data:"):
                            image_url = val
                            break

            price = None
            price_span = a.select_one("span.pcar-auction-info__price")
            if price_span:
                raw_price = price_span.get_text(strip=True)
                digits = re.sub(r"[^\d]", "", raw_price)
                if digits:
                    price = int(digits)

            auction_ends_at = None
            ends_label = a.find(string=re.compile(r"ends\s+in", re.I))
            if ends_label:
                val_el = a.select_one(".pcar-auction-info__value")
                if val_el:
                    auction_ends_at = _parse_pcar_relative_time(val_el.get_text(strip=True))

            c = dict(year=year, make=make or "Porsche", model=model, trim=trim,
                     mileage=None, price=price, vin=None, url=url, image_url=image_url,
                     auction_ends_at=auction_ends_at)
            if _is_valid_listing(c):
                cars.append(c)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _pw_launch(p)
            pg = _stealth_page(browser)

            MAX_PAGES = 6
            for page_num in range(1, MAX_PAGES + 1):
                suffix = "" if page_num == 1 else f"?page={page_num}"
                url = f"{BASE}/auctions/{suffix}"
                try:
                    pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        pg.wait_for_selector("a[href*='/auction/']", timeout=10000)
                    except Exception:
                        pass
                    pg.wait_for_timeout(2000)
                    html = pg.content()
                    from bs4 import BeautifulSoup as _BS
                    _s = _BS(html, "lxml")
                    for _ns in _s.find_all("noscript"):
                        _ns.decompose()
                    link_count = len(_s.select("a[href*='/auction/']"))
                    if link_count < 3:
                        log.info("pcarmarket: /auctions/ page %d empty — stopping", page_num)
                        break
                    before = len(cars)
                    _parse_html(html)
                    log.info("pcarmarket: /auctions/ page %d (%d links) → +%d cars",
                             page_num, link_count, len(cars) - before)
                except Exception as pe:
                    log.warning("pcarmarket: error on /auctions/ page %d: %s", page_num, pe)
                    break

            try:
                pg.goto(f"{BASE}/marketplace",
                        wait_until="domcontentloaded", timeout=45000)
                try:
                    pg.wait_for_selector("a[href*='/auction/']", timeout=10000)
                except Exception:
                    pass
                pg.wait_for_timeout(2000)
                before = len(cars)
                _parse_html(pg.content())
                log.info("pcarmarket: /marketplace → +%d cars", len(cars) - before)
            except Exception as pe:
                log.warning("pcarmarket: error on /marketplace: %s", pe)

            browser.close()
    except Exception as e:
        log.warning("pcarmarket scraper error: %s", e)

    if not cars:
        log.warning("pcarmarket: 0 results — all pages returned nothing")
    return _dedupe(cars)

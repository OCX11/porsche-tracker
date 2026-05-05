"""
scraper_cnb.py — Cars & Bids Playwright scraper for Porsche listings.

Fetches https://carsandbids.com using Playwright headless Chromium (no proxy needed).
Parses auction cards, extracts bids, mileage, end times, and images.
"""
import re
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME  = "Cars and Bids"
YEAR_MIN     = 1984
YEAR_MAX     = 2024  # HARD RULE: do not change — owner decision required

_BASE_URL  = "https://carsandbids.com"
_INDEX_URL = "https://carsandbids.com/?make=Porsche"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_price(text):
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_mileage(text):
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)\s*mi", text, re.I)
    if m:
        digits = m.group(1).replace(",", "")
        return int(digits) if digits else None
    return None


def _parse_title(title):
    """Parse 'YYYY Make Model Trim' from C&B auction title."""
    m = re.match(r"(\d{4})\s+Porsche\s+(.*)", title, re.I)
    if not m:
        return {"year": None, "model": None, "trim": None}
    year = int(m.group(1))
    rest = m.group(2).strip()
    # Split model from trim on first space-separated word
    parts = rest.split(None, 1)
    model = parts[0] if parts else rest
    trim  = parts[1] if len(parts) > 1 else None
    return {"year": year, "model": model, "trim": trim}


def _is_valid(car):
    year = car.get("year")
    if not year:
        return False
    if not (YEAR_MIN <= year <= YEAR_MAX):
        return False
    return True


def _fetch_html():
    """Return rendered HTML for the C&B Porsche page via Playwright."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.goto(_INDEX_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)

            # Scroll to trigger lazy-loading of all auction cards
            for i in range(12):
                page.evaluate("window.scrollTo(0, %d)" % (i * 2000))
                time.sleep(0.25)
            time.sleep(1)

            # Stamp .ticking countdown text as a plain data attribute so BS4 can read it.
            # C&B renders the countdown via React — text is in the live DOM but not
            # in the static HTML snapshot. We read it now and stamp it onto the <li>.
            page.evaluate(
                "document.querySelectorAll('li.auction-item').forEach(function(li) {"
                "  var tick = li.querySelector('.ticking');"
                "  if (tick && tick.textContent.trim()) {"
                "    li.setAttribute('data-ticking-text', tick.textContent.trim());"
                "  }"
                "});"
            )
            time.sleep(0.3)
            return page.content()
        finally:
            browser.close()


def _parse_cnb_countdown(text):
    """Parse C&B countdown text to ISO UTC string.

    Handles multiple formats:
      - '4:32:15'          (HH:MM:SS)
      - '2d 04:32:15'      (Xd HH:MM:SS)
      - '3 Days'            (human-readable)
      - '4 Hours'           (human-readable)
      - '15 Minutes'        (human-readable)
      - 'Ended'             (auction over)
    """
    if not text:
        return None
    text = text.strip()

    # "Ended" or similar
    if text.lower() in ("ended", "sold", "reserve not met"):
        return None

    # Xd HH:MM:SS
    m = re.match(r"(\d+)d\s+(\d+):(\d+):(\d+)", text, re.I)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(
            days=int(m.group(1)), hours=int(m.group(2)), minutes=int(m.group(3))
        )
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")

    # HH:MM:SS
    m = re.match(r"(\d+):(\d+):(\d+)", text)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(
            hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3))
        )
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")

    # "N Days" / "N Day"
    m = re.match(r"(\d+)\s+days?", text, re.I)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(days=int(m.group(1)))
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")

    # "N Hours" / "N Hour"
    m = re.match(r"(\d+)\s+hours?", text, re.I)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(hours=int(m.group(1)))
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")

    # "N Minutes" / "N Minute" / "N Min"
    m = re.match(r"(\d+)\s+min", text, re.I)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(minutes=int(m.group(1)))
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


def _parse_cards(html):
    soup = BeautifulSoup(html, "html.parser")

    all_items = soup.find_all("li", class_="auction-item")
    items = [li for li in all_items if "heroup" not in li.get("class", [])]
    log.info("C&B: found %d auction cards on page (%d heroup excluded)",
             len(items), len(all_items) - len(items))

    listings = []
    seen_urls = set()

    for item in items:
        a = item.find("a", href=re.compile(r"/auctions/"))
        if not a:
            continue

        href = a.get("href", "")
        url = href if href.startswith("http") else _BASE_URL + href
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = a.get("title", "").strip()
        if not title:
            continue

        img_tag = a.find("img")
        image_url = img_tag.get("src") if img_tag else None

        bid_span = item.find(class_="bid-value")
        price = _parse_price(bid_span.get_text() if bid_span else "")

        subtitle = item.find("p", class_="auction-subtitle")
        mileage = _parse_mileage(subtitle.get_text() if subtitle else "")

        # --- Auction end time ---
        auction_ends_at = None

        # Primary: li.time-left > span.value (current C&B markup as of Apr 2026)
        time_left_li = item.find("li", class_="time-left")
        if time_left_li:
            val_span = time_left_li.find("span", class_="value")
            if val_span:
                auction_ends_at = _parse_cnb_countdown(val_span.get_text(strip=True))

        # Fallback 1: .ticking span (legacy C&B markup)
        if not auction_ends_at:
            ticking = item.find("span", class_="ticking")
            if ticking:
                auction_ends_at = _parse_cnb_countdown(ticking.get_text(strip=True))

        # Fallback 2: stamped by our JS injection before snapshot
        if not auction_ends_at:
            ticking_text = item.get("data-ticking-text")
            if ticking_text:
                auction_ends_at = _parse_cnb_countdown(ticking_text)

        # Fallback 3: any span/div with "time" in class
        if not auction_ends_at:
            for el in item.find_all(["span", "div"]):
                cls = " ".join(el.get("class") or [])
                if "time" in cls.lower():
                    el_text = el.get_text(strip=True)
                    if el_text:
                        auction_ends_at = _parse_cnb_countdown(el_text)
                        if auction_ends_at:
                            break

        # Fallback 4: Unix timestamp in data attributes on the li
        if not auction_ends_at:
            for attr in ("data-time", "data-end", "data-expires", "data-ends-at"):
                val = item.get(attr)
                if val:
                    try:
                        ts = int(val)
                        ends = datetime.fromtimestamp(ts, tz=timezone.utc)
                        auction_ends_at = ends.strftime("%Y-%m-%dT%H:%M:%SZ")
                        break
                    except (ValueError, OSError):
                        pass

        parsed = _parse_title(title)
        parsed.update({
            "mileage":        mileage,
            "price":          price,
            "vin":            None,
            "url":            url,
            "image_url":      image_url,
            "auction_ends_at": auction_ends_at,
        })

        if not _is_valid(parsed):
            log.debug("C&B: filtered out '%s'", title)
            continue

        log.info("C&B: %s %s %s | bid:$%s | ends:%s | %s",
                 parsed.get("year"), parsed.get("model"),
                 parsed.get("trim") or "", parsed.get("price"),
                 auction_ends_at or "unknown", url)
        listings.append(parsed)

    return listings


def fetch_cnb_sold_price(url):
    """Fetch a Cars and Bids listing page and parse the final hammer price.

    C&B shows 'Sold for $XX,XXX' on the closed auction page.
    Returns int price or None. All errors are swallowed — must not
    break the scrape cycle on failure.
    """
    try:
        import requests as _req
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = _req.get(url, headers=headers, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        m = re.search(r"[Ss]old\s+for\s+\$\s*([\d,]+)", r.text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as exc:
        log.debug("fetch_cnb_sold_price error %s: %s", url, exc)
    return None


def scrape_cnb():
    """Public entry point — returns list of active Porsche auction dicts."""
    log.info("Scraping Cars and Bids\u2026")
    try:
        html = _fetch_html()
    except Exception as e:
        log.warning("C&B: Playwright fetch failed: %s", e)
        return []

    listings = _parse_cards(html)
    log.info("C&B: returning %d active Porsche listings", len(listings))
    return listings

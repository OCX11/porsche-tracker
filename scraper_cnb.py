"""
Standalone Cars & Bids active listings scraper.

Fetches https://carsandbids.com using Playwright headless Chromium (no proxy needed).
Scrolls the page to trigger lazy-loading, then parses all active auction cards.

Returns a list of {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
No state file — fetch and return every run (~10-20 active Porsche listings).
VIN is not available on the listing index page; returned as None.
source_category is AUCTION (wired via DEALERS list name "Cars and Bids" which maps to
the "cars & bids" / "carsandbids" pattern in db.source_category()).
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME = "Cars and Bids"
_BASE_URL = "https://carsandbids.com"
_INDEX_URL = "https://carsandbids.com"

# ---------------------------------------------------------------------------
# Filters — must stay in sync with scraper.py
# ---------------------------------------------------------------------------
YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not change — owner decision required
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718"})
_BLOCKED_MODELS = frozenset({"cayenne", "macan", "panamera", "taycan", "918"})

_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-2]\d)\b")
_PRICE_RE = re.compile(r"\$\s*([\d,]+)")
_MILES_RE = re.compile(r"~?([\d,]+)\s*[Mm]iles?")


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _int(s):
    if s is None:
        return None
    s = re.sub(r"[^\d]", "", str(s))
    return int(s) if s else None


def _parse_title(title):
    """Extract year/make/model/trim from a C&B title like '2015 Porsche 911 GT3'."""
    result = {"year": None, "make": "Porsche", "model": None, "trim": None}
    if not title:
        return result

    m = _YEAR_RE.search(title)
    if not m:
        return result
    result["year"] = int(m.group(1))

    # Strip leading year + optional make
    rest = title[m.end():].strip()
    rest = re.sub(r"^Porsche\s+", "", rest, flags=re.I).strip()

    # Match model tokens — longest first to catch "718 Cayman" before "718"
    _MODELS = [
        ("718 Cayman", "718"),
        ("718 Boxster", "718"),
        ("718", "718"),
        ("911 GT3 RS", "911"),   # keep full trim intact below
        ("911", "911"),
        ("Cayman", "Cayman"),
        ("Boxster", "Boxster"),
    ]
    for token, canonical in _MODELS:
        if re.match(r"^" + re.escape(token) + r"\b", rest, re.I):
            result["model"] = canonical
            after = rest[len(token):].strip()
            after = re.sub(r"^[\s'\"\-\u2013\u2014,]+", "", after).strip()
            after = re.sub(r"[\s'\"]+$", "", after).strip()
            if after:
                if len(after) > 60:
                    after = after[:60].rsplit(" ", 1)[0].strip()
                result["trim"] = after or None
            break

    return result


def _parse_mileage(subtitle):
    """Extract mileage from subtitle like '~29,300 Miles, 6-Speed Manual...'."""
    if not subtitle:
        return None
    m = _MILES_RE.search(subtitle)
    if not m:
        return None
    v = _int(m.group(1))
    if v and 0 < v < 999999:
        return v
    return None


def _parse_price(bid_text):
    """Parse price from '$51,500'."""
    m = _PRICE_RE.search(bid_text or "")
    if not m:
        return None
    v = _int(m.group(1))
    if v and v >= 100:
        return v
    return None


def _is_valid(car):
    model = (car.get("model") or "").lower()
    year = car.get("year")
    if not model:
        return False
    if any(b in model for b in _BLOCKED_MODELS):
        return False
    if not any(g in model for g in _ALLOWED_MODELS):
        return False
    if year and not (YEAR_MIN <= year <= YEAR_MAX):
        return False
    return True


# ---------------------------------------------------------------------------
# Playwright fetch + parse
# ---------------------------------------------------------------------------

def _fetch_html():
    """Return rendered HTML for the C&B homepage via Playwright (with scroll)."""
    from playwright.sync_api import sync_playwright
    import time
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

            # Scroll through the full page to trigger lazy-loading of all cards
            for i in range(12):
                page.evaluate("window.scrollTo(0, %d)" % (i * 2000))
                time.sleep(0.25)
            time.sleep(1)

            return page.content()
        finally:
            browser.close()


def _parse_cnb_countdown(text):
    """Parse C&B countdown to ISO UTC string.
    Handles '4:32:15' (HH:MM:SS) and '2d 04:32:15' (Xd HH:MM:SS).
    Returns ISO UTC string or None.
    """
    if not text:
        return None
    text = text.strip()
    m = re.match(r"(\d+)d\s+(\d+):(\d+):(\d+)", text, re.I)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(
            days=int(m.group(1)), hours=int(m.group(2)), minutes=int(m.group(3))
        )
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")
    m = re.match(r"(\d+):(\d+):(\d+)", text)
    if m:
        ends = datetime.now(timezone.utc) + timedelta(
            hours=int(m.group(1)), minutes=int(m.group(2))
        )
        return ends.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _parse_cards(html):
    soup = BeautifulSoup(html, "html.parser")

    # All <li class="auction-item"> — exclude "heroup" hero-carousel items
    all_items = soup.find_all("li", class_="auction-item")
    items = [li for li in all_items if "heroup" not in li.get("class", [])]
    log.info("C&B: found %d auction cards on page (%d heroup excluded)",
             len(items), len(all_items) - len(items))

    listings = []
    seen_urls = set()

    for item in items:
        # Main link — <a class="hero" href="/auctions/...">
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

        # Image — first <img> inside the hero <a>
        img_tag = a.find("img")
        image_url = img_tag.get("src") if img_tag else None

        # Current bid — <span class="bid-value">
        bid_span = item.find(class_="bid-value")
        price = _parse_price(bid_span.get_text() if bid_span else "")

        # Mileage — <p class="auction-subtitle">
        subtitle = item.find("p", class_="auction-subtitle")
        mileage = _parse_mileage(subtitle.get_text() if subtitle else "")

        # Auction end time — span.ticking primary, with multi-format + fallbacks
        auction_ends_at = None
        ticking = item.find("span", class_="ticking")
        if ticking:
            auction_ends_at = _parse_cnb_countdown(ticking.get_text(strip=True))
        # Fallback: any span/div with "time" in class name containing digits
        if not auction_ends_at:
            for el in item.find_all(["span", "div"]):
                cls = " ".join(el.get("class") or [])
                if "time" in cls.lower():
                    el_text = el.get_text(strip=True)
                    if re.search(r"\d+:\d+", el_text):
                        auction_ends_at = _parse_cnb_countdown(el_text)
                        if auction_ends_at:
                            break
        # Fallback: data attribute on the li (Unix timestamp)
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
            "mileage": mileage,
            "price": price,
            "vin": None,
            "url": url,
            "image_url": image_url,
            "auction_ends_at": auction_ends_at,
        })

        if not _is_valid(parsed):
            log.debug("C&B: filtered out '%s'", title)
            continue

        log.info("C&B: %s %s %s | bid:$%s | %s",
                 parsed.get("year"), parsed.get("model"),
                 parsed.get("trim") or "", parsed.get("price"), url)
        listings.append(parsed)

    return listings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_cnb():
    """Scrape Cars & Bids active Porsche auction listings.

    Returns a list of {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
    """
    try:
        html = _fetch_html()
    except Exception as e:
        log.warning("C&B: Playwright fetch failed: %s", e)
        return []

    try:
        listings = _parse_cards(html)
    except Exception as e:
        log.warning("C&B: parse error: %s", e)
        return []

    log.info("C&B: returning %d active Porsche listings", len(listings))
    return listings

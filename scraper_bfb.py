"""
Standalone Built for Backroads scraper.

Fetches https://www.builtforbackroads.com/cars/porsche using Playwright
headless Chromium (no proxy needed — small site, no bot detection).

Returns a list of {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
No state file — fetch and return every run (~10 active listings).
Mileage/VIN not available on the listing index page; both returned as None.
"""
import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME = "Built for Backroads"
_INDEX_URL = "https://www.builtforbackroads.com/cars/porsche"
_BASE_URL = "https://www.builtforbackroads.com"

# ---------------------------------------------------------------------------
# Filters (must stay in sync with scraper.py)
# ---------------------------------------------------------------------------
YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not change — owner decision required
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718"})
_BLOCKED_MODELS = frozenset({"cayenne", "macan", "panamera", "taycan", "918"})

_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-2]\d)\b")
_PRICE_RE = re.compile(r"\$\s*([\d,]+)", re.I)


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _int(s):
    if s is None:
        return None
    s = re.sub(r"[^\d]", "", str(s))
    return int(s) if s else None


def _parse_title(title: str) -> dict:
    """Extract year/make/model/trim from a BfB title like '2016 Porsche Cayman GT4'."""
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

    # Match model tokens (longest first to catch "Cayman GTS" before "Cayman")
    _MODELS = [
        ("718", "718"),
        ("911T", "911"),   # e.g. "1973 Porsche 911T"
        ("911", "911"),
        ("Cayman", "Cayman"),
        ("Boxster", "Boxster"),
    ]
    for token, canonical in _MODELS:
        if re.match(rf"^{re.escape(token)}\b", rest, re.I):
            result["model"] = canonical
            after = rest[len(token):].strip()
            # Strip leading punctuation/quotes/dash
            after = re.sub(r"^[\s'\"\-–—,]+", "", after).strip()
            after = re.sub(r"[\s'\"]+$", "", after).strip()
            # Cap trim at 60 chars
            if after:
                if len(after) > 60:
                    after = after[:60].rsplit(" ", 1)[0].strip()
                result["trim"] = after or None
            break

    return result


def _is_valid(car: dict) -> bool:
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


def _extract_price(meta_text: str):
    """Parse price from meta text like 'Asking $145,000' or 'Listed For $45,000'."""
    m = _PRICE_RE.search(meta_text or "")
    if not m:
        return None
    v = _int(m.group(1))
    if v and 1_000 <= v < 2_000_000:
        return v
    return None


# ---------------------------------------------------------------------------
# Playwright fetch + parse
# ---------------------------------------------------------------------------

def _fetch_html() -> str:
    """Return rendered HTML for the BfB Porsche page via Playwright."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(_INDEX_URL, wait_until="networkidle", timeout=45000)
            try:
                page.wait_for_selector("div.group.w-full", timeout=10000)
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


def _parse_cards(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.group.w-full")
    log.info("BfB: found %d cards on page", len(cards))

    listings = []
    seen_urls = set()

    for card in cards:
        # Skip sold listings — sold cards have a <span>Sold</span> badge
        overlay_spans = [
            s.get_text(strip=True)
            for s in card.find_all("span")
        ]
        if "Sold" in overlay_spans:
            continue

        # Listing URL (first <a href="/listing/...">)
        a_listing = card.find("a", href=lambda h: h and "/listing/" in h)
        if not a_listing:
            continue
        href = a_listing.get("href", "")
        url = href if href.startswith("http") else f"{_BASE_URL}{href}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Title from <h2>
        h2 = card.find("h2")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            continue

        # Image URL from <img src="..."> inside the image <a>
        img = a_listing.find("img")
        image_url = None
        if img:
            image_url = img.get("src") or None

        # Price from the meta <a> (gray text with "Asking $..." or "Listed For $...")
        meta_a = card.find("a", class_=lambda c: c and "text-gray-400" in c)
        meta_text = meta_a.get_text(" ", strip=True) if meta_a else ""
        price = _extract_price(meta_text)

        parsed = _parse_title(title)
        parsed.update({
            "mileage": None,
            "price": price,
            "vin": None,
            "listing_url": url,
            "image_url": image_url,
        })

        if not _is_valid(parsed):
            log.debug("BfB: filtered out '%s'", title)
            continue

        log.info("BfB: %s %s %s | $%s | %s",
                 parsed.get("year"), parsed.get("model"),
                 parsed.get("trim") or "", parsed.get("price"), url)
        listings.append(parsed)

    return listings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_bfb() -> list:
    """Scrape Built for Backroads Porsche listings.

    Returns a list of {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
    """
    try:
        html = _fetch_html()
    except Exception as e:
        log.warning("BfB: Playwright fetch failed: %s", e)
        return []

    try:
        listings = _parse_cards(html)
    except Exception as e:
        log.warning("BfB: parse error: %s", e)
        return []

    log.info("BfB: returning %d active Porsche listings", len(listings))
    return listings

"""
Standalone Cars.com scraper for Porsche listings.

Strategy (in order of preference):
  1. requests → Cars.com search page (parses JSON-LD + HTML listing cards)
  2. headless Playwright + proxy (fallback if requests is blocked)

Cars.com embeds structured data in <script type="application/ld+json"> blocks
AND renders listing cards with data-listing-id attributes.  We prefer the
JSON-LD path (clean structured data); fall back to HTML card parsing.

Mirrors proxy/stealth patterns from scraper_autotrader.py exactly.
"""
import re
import json
import logging
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME = "cars.com"

_SEARCH_BASE = (
    "https://www.cars.com/shopping/results/"
    "?dealer_id=&keyword=&list_price_max=&list_price_min="
    "&makes[]=porsche&maximum_distance=all&mileage_max="
    "&models[]=&sort=listed_at_desc&stock_type=used"
    "&year_max=&year_min=&zip="
)
_PAGE_SIZE = 20        # Cars.com default; &page_size=20 already in URL
_BASE_URL  = "https://www.cars.com"

_STATE_FILE = Path.home() / "porsche-tracker" / "data" / "carscom_state.json"

# ---------------------------------------------------------------------------
# Import filter from scraper.py
# ---------------------------------------------------------------------------
try:
    from scraper import _is_valid_listing
except Exception:
    def _is_valid_listing(car):
        return True

# ---------------------------------------------------------------------------
# Proxy config (mirrors scraper_autotrader.py exactly)
# ---------------------------------------------------------------------------
_PROXY_CFG = {}
_PROXY_URL = ""
_PROXY_DEAD = False

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
})


def _load_proxy():
    global _PROXY_CFG, _PROXY_URL
    script_dir = Path(__file__).resolve().parent
    p = script_dir
    for _ in range(6):
        cand = p / "data" / "proxy_config.json"
        try:
            with open(cand) as f:
                cfg = json.load(f)
            if cfg.get("enabled") and cfg.get("proxy_url"):
                _PROXY_CFG = cfg
                _PROXY_URL = cfg["proxy_url"]
                _SESSION.proxies.update({"http": _PROXY_URL, "https": _PROXY_URL})
                log.info("Proxy enabled: %s:%s (from %s)",
                         cfg.get("host"), cfg.get("port"), cand)
                return
        except Exception:
            pass
        p = p.parent


_load_proxy()


def _disable_proxy():
    """Log proxy failure — does NOT fall back to direct. Scrape will be skipped this cycle."""
    global _PROXY_DEAD
    if not _PROXY_DEAD:
        _PROXY_DEAD = True
        log.warning("Proxy unavailable — cars.com scrape will be skipped this cycle (no naked-IP fallback)")


def _pw_proxy():
    """Return Playwright proxy dict if proxy is alive, else None."""
    if not _PROXY_URL or not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        return None
    return {
        "server": "{}://{}:{}".format(
            _PROXY_CFG["protocol"], _PROXY_CFG["host"], _PROXY_CFG["port"]
        ),
        "username": _PROXY_CFG["username"],
        "password": _PROXY_CFG["password"],
    }


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------
def _int(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _clean(s):
    if not s:
        return None
    return re.sub(r"\s+", " ", str(s)).strip() or None


def _is_blocked(html):
    """
    Return True only if the response is clearly a block/CAPTCHA page.
    Must be a SHORT page (real Cars.com pages are 500KB+) AND contain
    block indicators. Never flag large pages as blocked.
    """
    if not html:
        return True
    if len(html) > 50_000:
        # Large page — can't be a simple block page
        return False
    lower = html.lower()
    return (
        "access denied" in lower
        or "captcha" in lower
        or "cf-error" in lower
        or "cloudflare" in lower and "ray id" in lower
        or ("upstream connect error" in lower)
    )


def _looks_valid(html):
    """Return True if the page looks like a real Cars.com search results page."""
    return bool(html) and (
        "data-listing-id" in html
        or "vehicle-card" in html
        or "listings-page" in html
    )


# ---------------------------------------------------------------------------
# JSON-LD extraction (primary path)
# ---------------------------------------------------------------------------
def _extract_json_ld_listings(html):
    """
    Extract listings from <script type="application/ld+json"> blocks.
    Cars.com embeds ItemList or individual Car entries in LD+JSON.
    Returns list of raw dicts, or [].
    """
    results = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            # ItemList with itemListElement
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                for elem in data.get("itemListElement", []):
                    item = elem.get("item") or elem
                    if isinstance(item, dict):
                        results.append(item)
            # Single Car/Vehicle
            elif isinstance(data, dict) and data.get("@type") in ("Car", "Vehicle"):
                results.append(data)
            # List of items
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") in ("Car", "Vehicle"):
                        results.append(item)
    except Exception as e:
        log.debug("JSON-LD extraction error: %s", e)
    return results


def _parse_json_ld_item(item, listing_id=None):
    """
    Parse one JSON-LD Car/Vehicle dict into our listing dict.
    """
    if not isinstance(item, dict):
        return None

    name = _clean(item.get("name") or "")
    year = None
    make = "Porsche"
    model = None
    trim = None

    # name is often "2019 Porsche 911 Carrera S"
    if name:
        m = re.match(r"(\d{4})\s+(\S+)\s+(\S+)\s*(.*)", name)
        if m:
            year = _int(m.group(1))
            make = _clean(m.group(2)) or "Porsche"
            model = _clean(m.group(3))
            trim = _clean(m.group(4)) or None

    # Explicit fields override name parse
    year = year or _int(item.get("vehicleModelDate") or item.get("modelDate"))
    make = _clean(item.get("brand", {}).get("name") if isinstance(item.get("brand"), dict)
                  else item.get("brand")) or make
    model = _clean(item.get("model")) or model
    trim = _clean(item.get("vehicleConfiguration") or item.get("trim")) or trim

    mileage_obj = item.get("mileageFromOdometer") or {}
    if isinstance(mileage_obj, dict):
        mileage = _int(mileage_obj.get("value"))
    else:
        mileage = _int(mileage_obj)

    # Offers block for price
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _int(
        offers.get("price")
        or item.get("price")
    )

    vin = _clean(item.get("vehicleIdentificationNumber") or item.get("vin"))

    # URL
    url = _clean(item.get("url") or item.get("@id") or "")
    if url and not url.startswith("http"):
        url = _BASE_URL + url
    if listing_id and not url:
        url = _BASE_URL + "/vehicledetail/{}/".format(listing_id)

    # Image
    image = item.get("image") or ""
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl") or ""
    image_url = _clean(str(image)) if image else None
    if image_url and not image_url.startswith("http"):
        image_url = None

    # Seller type: Cars.com sells both dealer and private party
    seller = _clean(str(item.get("seller") or item.get("offeredBy") or ""))
    if seller and "private" in seller.lower():
        seller_type = "private"
    else:
        seller_type = "dealer"

    return {
        "year": year,
        "make": make,
        "model": model,
        "trim": trim,
        "mileage": mileage,
        "price": price,
        "vin": vin,
        "url": url,
        "image_url": image_url,
        "seller_type": seller_type,
    }


# ---------------------------------------------------------------------------
# HTML card extraction — reads data-vehicle-details JSON attribute on <fuse-card>
# ---------------------------------------------------------------------------
def _extract_card_listings(html):
    """
    Cars.com renders listings as <fuse-card data-listing-id="..." data-vehicle-details='{...}'>
    The data-vehicle-details attribute is a complete JSON object with all fields we need:
      year, make, model, trim, vin, price, mileage, primaryThumbnail, listingId, seller

    No CSS selector guessing needed — just parse the JSON attribute directly.
    Each listing_id appears twice in the DOM (card + a nested element) so we dedup by id.
    """
    results = []
    seen_ids = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("[data-vehicle-details]")

        for card in cards:
            listing_id = card.get("data-listing-id", "")
            if listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            # Parse the JSON blob — this has everything
            raw_json = card.get("data-vehicle-details", "")
            try:
                data = json.loads(raw_json)
            except Exception:
                continue

            year  = _int(data.get("year"))
            make  = _clean(data.get("make")) or "Porsche"
            model = _clean(data.get("model"))
            trim  = _clean(data.get("trim"))
            vin   = _clean(data.get("vin"))
            price = _int(data.get("price"))
            mileage = _int(data.get("mileage"))

            # Primary thumbnail — high-res version of primaryThumbnail
            thumb = _clean(data.get("primaryThumbnail") or "")
            # primaryThumbnail uses /in/v2/ path — swap to /large/in/v2/ for full size
            if thumb and "/in/v2/" in thumb and "/large/in/v2/" not in thumb:
                image_url = thumb.replace("/in/v2/", "/large/in/v2/")
            else:
                image_url = thumb or None

            # Per-listing URL
            lid = listing_id or data.get("listingId", "")
            url = "{}/vehicledetail/{}/".format(_BASE_URL, lid) if lid else None

            # Seller type — seller dict has no explicit type field on Cars.com;
            # private sellers have no customerId or a distinct seller zip pattern.
            # Cars.com encodes this via stockType: "Used" = dealer, no reliable
            # private flag in this payload. Default dealer; override if text says private.
            seller_type = "dealer"
            card_text = card.get_text().lower()
            if "private seller" in card_text or "private party" in card_text:
                seller_type = "private"

            results.append({
                "year": year,
                "make": make,
                "model": model,
                "trim": trim,
                "mileage": mileage,
                "price": price,
                "vin": vin,
                "url": url,
                "image_url": image_url,
                "seller_type": seller_type,
            })
    except Exception as e:
        log.warning("Card extraction error: %s", e)
    return results


def _extract_total_count(html):
    """Return the total result count from the page, or None."""
    try:
        m = re.search(r'"totalCount"\s*:\s*(\d+)', html)
        if m:
            return int(m.group(1))
        soup = BeautifulSoup(html, "html.parser")
        count_el = soup.select_one("[class*='total-filter-count']") or \
                   soup.select_one("[class*='result-count']")
        if count_el:
            digits = re.sub(r"[^\d]", "", count_el.get_text())
            if digits:
                return int(digits)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# HTTP fetch (fast path)
# ---------------------------------------------------------------------------
def _fetch_requests(url):
    """Fetch via requests through proxy. Returns HTML or None if blocked/failed."""
    try:
        r = _SESSION.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        if _is_blocked(r.text):
            log.info("requests: blocked at %s (len=%d)", url, len(r.text))
            return None
        if not _looks_valid(r.text):
            log.info("requests: no listing data at %s (len=%d)", url, len(r.text))
            return None
        return r.text
    except requests.exceptions.ProxyError as e:
        log.warning("Proxy error on requests fetch: %s — will not retry direct", e)
        _disable_proxy()
        return None
    except Exception as e:
        log.debug("requests error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Playwright fetch (fallback)
# ---------------------------------------------------------------------------
_PW_AVAILABLE = None


def _playwright_available():
    global _PW_AVAILABLE
    if _PW_AVAILABLE is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa
            _PW_AVAILABLE = True
        except ImportError:
            _PW_AVAILABLE = False
    return _PW_AVAILABLE


def _fetch_playwright(url, headless=True):
    """Fetch a page with Playwright headless. Returns HTML or None."""
    if not _playwright_available():
        log.debug("Playwright not installed")
        return None
    from playwright.sync_api import sync_playwright
    kwargs = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    proxy = _pw_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**kwargs)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/Los_Angeles",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = ctx.new_page()
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(page)
            except ImportError:
                pass
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for selector in ("[data-listing-id]", ".vehicle-card", ".listings-page"):
                try:
                    page.wait_for_selector(selector, timeout=8000)
                    break
                except Exception:
                    continue
            time.sleep(1.5)
            html = page.content()
            browser.close()
        if _is_blocked(html) or not _looks_valid(html):
            log.info("Playwright: no valid content at %s", url)
            return None
        return html
    except Exception as e:
        log.warning("Playwright error: %s", e)
        return None


# ---------------------------------------------------------------------------
# curl_cffi — Chrome TLS impersonation (primary strategy, mirrors autotrader)
# ---------------------------------------------------------------------------
_CFFI_AVAILABLE = None
_CFFI_IMPERSONATE = "chrome123"


def _curl_cffi_available():
    global _CFFI_AVAILABLE
    if _CFFI_AVAILABLE is None:
        try:
            from curl_cffi import requests as _  # noqa
            _CFFI_AVAILABLE = True
        except ImportError:
            _CFFI_AVAILABLE = False
    return _CFFI_AVAILABLE


def _fetch_curl_cffi(url):
    """
    Fetch via curl_cffi with Chrome TLS impersonation + proxy.
    Bypasses Cloudflare bot detection. Always routes through DataImpulse proxy.
    Returns HTML string or None.
    """
    if not _curl_cffi_available():
        return None
    if not _PROXY_URL or not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        log.warning("curl_cffi: proxy not available — skipping")
        return None
    from curl_cffi import requests as cr
    proxies = {"http": _PROXY_URL, "https": _PROXY_URL}
    try:
        r = cr.get(
            url,
            impersonate=_CFFI_IMPERSONATE,
            timeout=30,
            proxies=proxies,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if _is_blocked(r.text):
            log.info("curl_cffi: block page at %s (len=%d)", url, len(r.text))
            return None
        if not _looks_valid(r.text):
            log.info("curl_cffi: no listing data at %s (len=%d)", url, len(r.text))
            return None
        return r.text
    except Exception as e:
        log.debug("curl_cffi error: %s", e)
        return None


def _fetch_page(url):
    """
    Fetch URL trying strategies in order:
      1. curl_cffi (Chrome TLS fingerprint — bypasses Cloudflare bot detection)
      2. requests (fast, may be fingerprint-blocked)
      3. headless Playwright (full browser fallback)
    """
    log.info("Fetching: %s", url)

    # Strategy 1: curl_cffi (Chrome TLS — same trick that works on AutoTrader)
    if _curl_cffi_available():
        html = _fetch_curl_cffi(url)
        if html:
            log.info("  curl_cffi succeeded (len=%d)", len(html))
            return html
        log.info("  curl_cffi blocked/failed — trying requests")

    # Strategy 2: requests
    html = _fetch_requests(url)
    if html:
        log.info("  requests succeeded (len=%d)", len(html))
        return html

    # Strategy 3: headless Playwright
    log.info("  requests blocked/failed — trying Playwright")
    html = _fetch_playwright(url, headless=True)
    if html:
        log.info("  Playwright succeeded (len=%d)", len(html))
        return html

    log.warning("  All fetch strategies failed for %s", url)
    return None


# ---------------------------------------------------------------------------
# Parse one page of HTML into listings
# ---------------------------------------------------------------------------
def _parse_page(html):
    """
    Extract listings from one Cars.com search HTML page.
    Reads data-vehicle-details JSON attribute on <fuse-card> elements directly.
    This is the authoritative data source — no JSON-LD or CSS selector guessing.
    """
    cards = _extract_card_listings(html)
    if cards:
        log.info("  card path: %d items (from data-vehicle-details)", len(cards))
        return cards
    log.info("  No listings extracted from page")
    return []


# ---------------------------------------------------------------------------
# Bootstrap state helpers
# ---------------------------------------------------------------------------
def _load_state():
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------
def scrape_carscom():
    """
    Scrape Cars.com for used Porsche listings.
    Always routes through DataImpulse proxy — never exposes bare Mac Mini IP.
    If proxy is unavailable, returns [] immediately (skips this cycle cleanly).
    """
    if not _PROXY_URL or not _PROXY_CFG.get("enabled"):
        log.warning("cars.com: proxy not configured — skipping scrape")
        return []

    state = _load_state()
    bootstrapped = state.get("bootstrapped", False)

    if bootstrapped:
        max_pages = 1
        log.info("cars.com: incremental run (1 page)")
    else:
        max_pages = 10
        log.info("cars.com: bootstrap run (up to %d pages)", max_pages)

    all_listings = []
    seen_keys = set()
    filtered_out = 0

    for page_num in range(1, max_pages + 1):
        # Abort if proxy died mid-session — don't expose bare IP
        if _PROXY_DEAD:
            log.warning("cars.com: proxy died mid-scrape — stopping (no naked-IP fallback)")
            break

        url = "{}&page_size={}&page={}".format(_SEARCH_BASE, _PAGE_SIZE, page_num)

        html = _fetch_page(url)
        if not html:
            log.info("cars.com: fetch failed on page %d — stopping", page_num)
            break

        raw = _parse_page(html)
        if not raw:
            log.info("cars.com: 0 listings on page %d — end of results", page_num)
            break

        new_this_page = 0
        for car in raw:
            # Dedup key: prefer VIN, then URL, then year|model|price
            key = car.get("vin") or car.get("url") or ""
            if not key:
                key = "{}|{}|{}".format(
                    car.get("year"), car.get("model"), car.get("price")
                )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Must have a per-listing URL (not just the search page)
            url_val = car.get("url") or ""
            if "/vehicledetail/" not in url_val:
                continue

            if not _is_valid_listing(car):
                filtered_out += 1
                continue

            all_listings.append(car)
            new_this_page += 1

        log.info("cars.com page %d: %d new listings (running total: %d)",
                 page_num, new_this_page, len(all_listings))

        if new_this_page == 0:
            break

        time.sleep(2.0)   # polite delay between pages

    if not bootstrapped and all_listings:
        _save_state({"bootstrapped": True})
        log.info("cars.com: bootstrap complete — state written to %s", _STATE_FILE)

    log.info("cars.com scrape complete: %d listings (%d filtered out)",
             len(all_listings), filtered_out)
    return all_listings


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Debug: show raw cards before filtering
    import sys
    if "--debug" in sys.argv:
        from curl_cffi import requests as cr
        url = "{}&page_size={}&page=1".format(_SEARCH_BASE, _PAGE_SIZE)
        r = cr.get(url, impersonate=_CFFI_IMPERSONATE, timeout=30, allow_redirects=True)

        # Show raw card HTML for first unique listing
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-listing-id]")
        seen_ids = set()
        print("Unique listing IDs: {}  (total elements: {})".format(
            len({c.get('data-listing-id') for c in cards}), len(cards)))
        print()
        for card in cards:
            lid = card.get("data-listing-id","")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            print("=== CARD HTML (listing_id={}) ===".format(lid))
            print(card.prettify()[:3000])
            print()
            if len(seen_ids) >= 2:
                break

        # Also check JSON-LD
        jld = _extract_json_ld_listings(r.text)
        print("JSON-LD items found: {}".format(len(jld)))
        if jld:
            print("First JSON-LD item keys:", list(jld[0].keys())[:10])

        sys.exit(0)

    results = scrape_carscom()
    print("\nTotal listings: {}".format(len(results)))

    if results:
        print("\nFirst 5 results:")
        for i, car in enumerate(results[:5]):
            url_preview = (car.get("url") or "")[:70]
            print("  {}. {} {} {} | {} | {}".format(
                i + 1,
                car.get("year"),
                car.get("model"),
                car.get("trim") or "(no trim)",
                car.get("seller_type") or "unknown",
                url_preview,
            ))

        # Count image coverage
        with_images = sum(1 for c in results if c.get("image_url"))
        print("\nImage coverage: {}/{} ({:.0f}%)".format(
            with_images, len(results),
            100 * with_images / len(results) if results else 0,
        ))

        print("\nFirst 3 results (full detail):")
        for i, car in enumerate(results[:3]):
            print("\n--- Listing {} ---".format(i + 1))
            for k, v in car.items():
                print("  {}: {}".format(k, v))

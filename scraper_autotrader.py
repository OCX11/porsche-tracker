"""
Standalone AutoTrader scraper for Porsche listings.

Strategy (in order of preference):
  1. requests → www.autotrader.com desktop search page (fastest; may be blocked)
  2. headless Playwright + stealth → same URL (fallback)
  4. AutoTrader REST API → /rest/lsc/listing (JSON, no HTML parsing needed)

The desktop search page embeds all listing data in a __NEXT_DATA__ JSON blob.
The REST API is a direct JSON endpoint requiring no page rendering.

Mirrors proxy/stealth patterns from scraper.py.
"""
import re
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEALER_NAME = "AutoTrader"

_SEARCH_BASE = (
    "https://m.autotrader.com/cars-for-sale/used-cars/porsche/porsche/"
    "?sellerTypes=p%2Cd"  # p=private, d=dealer — both; numRecords added dynamically
)
_BASE_URL = "https://www.autotrader.com"
_REST_BASE = "https://www.autotrader.com/rest/lsc/listing"

_STATE_FILE = Path.home() / "porsche-tracker" / "data" / "autotrader_state.json"

# ---------------------------------------------------------------------------
# Import filter from scraper.py
# ---------------------------------------------------------------------------
YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not increase until Jan 1 2027

try:
    from scraper import _is_valid_listing
except Exception:
    def _is_valid_listing(car):
        year = car.get("year")
        if year and not (YEAR_MIN <= int(year) <= YEAR_MAX):
            return False
        return True

# ---------------------------------------------------------------------------
# Proxy config (mirrors scraper.py; searches up to git repo root)
# ---------------------------------------------------------------------------
_PROXY_CFG = {}
_PROXY_URL = ""
_PROXY_DEAD = False

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
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


def _city_proxy_url(city):
    """
    Return a DataImpulse proxy URL with city targeting (semicolon syntax).
    City-targeted IPs have far better Akamai reputation than country-level __cr.us.
    Chicago IPs are empirically the most reliable for AutoTrader.
    """
    if not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        return None
    username = _PROXY_CFG.get("username", "")
    if username and "dataimpulse" in _PROXY_CFG.get("host", "").lower():
        city_user = "{user}__cr.us;city.{city}".format(user=username, city=city)
        return (
            "{proto}://{user}:{pwd}@{host}:{port}".format(
                proto=_PROXY_CFG.get("protocol", "http"),
                user=city_user,
                pwd=_PROXY_CFG.get("password", ""),
                host=_PROXY_CFG.get("host", ""),
                port=_PROXY_CFG.get("port", ""),
            )
        )
    return _PROXY_URL or None


def _disable_proxy():
    """Log proxy failure — but do NOT fall back to direct. AutoTrader requires the proxy."""
    global _PROXY_DEAD
    if not _PROXY_DEAD:
        _PROXY_DEAD = True
        log.warning("Proxy unavailable — AutoTrader scrape will be skipped this cycle (no naked-IP fallback)")


def _pw_proxy(city="chicago"):
    """Return Playwright proxy dict with city targeting if proxy is alive, else None."""
    if not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        return None
    username = _PROXY_CFG.get("username", "")
    if username and "dataimpulse" in _PROXY_CFG.get("host", "").lower():
        city_user = "{user}__cr.us;city.{city}".format(user=username, city=city)
    else:
        city_user = username
    return {
        "server": "{proto}://{host}:{port}".format(
            proto=_PROXY_CFG.get("protocol", "http"),
            host=_PROXY_CFG.get("host", ""),
            port=_PROXY_CFG.get("port", ""),
        ),
        "username": city_user,
        "password": _PROXY_CFG.get("password", ""),
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


def _is_listing_url(url):
    """True for individual AutoTrader listing pages (both URL formats)."""
    return bool(url and (
        "/cars-for-sale/listing/" in url
        or "/cars-for-sale/vehicle/" in url
    ))


def _is_blocked(html):
    """Return True if the response is an Akamai block page."""
    return bool(html) and (
        "akamai-block" in html
        or ("page unavailable" in html.lower() and len(html) < 20000)
    )


def _is_sports_car(car):
    """
    Return True if the listing is a 911 or Cayman variant (including 718 Cayman).
    Checks model and trim fields (case-insensitive).
    """
    haystack = " ".join([
        str(car.get("model") or ""),
        str(car.get("trim") or ""),
    ]).lower()
    return "911" in haystack or "cayman" in haystack


# ---------------------------------------------------------------------------
# Data extraction from __NEXT_DATA__
# ---------------------------------------------------------------------------
def _extract_next_data(html):
    """Return the __NEXT_DATA__ dict from the page HTML, or None."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception as e:
        log.warning("__NEXT_DATA__ parse error: %s", e)
        return None


def _parse_inventory_item(listing_id, item, owners=None):
    """
    Parse one inventory item from __eggsState.inventory into our listing dict.

    Key fields:
      year, make, model, trim  → strings/ints
      vin                      → str
      mileage                  → {label, value} — value is "12,345"
      pricingDetail            → {salePrice, incentive, ...}
      images.sources           → [{src, alt, width, height}]
      vdpBaseUrl               → relative URL like /cars-for-sale/vehicle/ID?...
      ownerName                → dealer name (absent for private sellers)
      listingType              → e.g. "USED", "PRIVATE", "PRIVATE_PARTY"
      ownerId                  → numeric dealer ID (null for private sellers)
      ownerType/sellerType     → direct type field if present

    owners: optional dict from __eggsState.owners keyed by ownerId string,
            each entry may have a 'type' field ('DEALER', 'PRIVATE', etc.)
    """
    if not isinstance(item, dict):
        return None

    year = item.get("year")
    make_obj = item.get("make", {})
    make = _clean(make_obj.get("name") if isinstance(make_obj, dict) else make_obj)
    model_obj = item.get("model", {})
    model = _clean(model_obj.get("name") if isinstance(model_obj, dict) else model_obj)
    trim_obj = item.get("trim", {})
    trim = _clean(trim_obj.get("name") if isinstance(trim_obj, dict) else trim_obj)

    vin = _clean(item.get("vin"))

    mileage_obj = item.get("mileage", {})
    if isinstance(mileage_obj, dict):
        mileage = _int(mileage_obj.get("value"))
    else:
        mileage = _int(mileage_obj)

    price_obj = item.get("pricingDetail", {})
    price = None
    if isinstance(price_obj, dict):
        price = _int(price_obj.get("salePrice") or price_obj.get("incentive"))

    # Build a clean canonical listing URL (strip query params)
    vdp = item.get("vdpBaseUrl", "")
    if vdp:
        url = _BASE_URL + vdp.split("?")[0]
    else:
        url = _BASE_URL + f"/cars-for-sale/vehicle/{listing_id}"

    # First real https:// image from images.sources
    image_url = None
    images_obj = item.get("images", {})
    if isinstance(images_obj, dict):
        sources = images_obj.get("sources", [])
        if sources and isinstance(sources[0], dict):
            src = sources[0].get("src", "")
            if src.startswith("https://"):
                image_url = src

    location = _clean(item.get("ownerName"))

    # Determine seller type — check several signals in priority order.
    #
    # NOTE: listingType='USED' is ambiguous (both dealers and private sellers
    # sell used cars), so we do NOT use it to infer "dealer".
    # The most reliable signal is ownerId: dealers have a numeric dealer ID;
    # private sellers have ownerId=null.
    _PRIVATE_VALS = {"PRIVATE", "PRIVATE_PARTY", "P", "PRIVATE_SELLER"}
    _DEALER_VALS = {"DEALER", "DEALER_CPO", "CPO", "D"}

    # 1. Direct type fields on the item
    raw_owner = str(
        item.get("ownerType") or item.get("sellerType") or ""
    ).upper()
    if raw_owner in _PRIVATE_VALS or item.get("privateSeller"):
        seller_type = "private"
    elif raw_owner in _DEALER_VALS:
        seller_type = "dealer"
    else:
        # 2. listingType: only trust explicit private/dealer values
        listing_type = str(item.get("listingType") or "").upper()
        if listing_type in _PRIVATE_VALS:
            seller_type = "private"
        elif listing_type in _DEALER_VALS:
            seller_type = "dealer"
        else:
            # 3. Cross-reference ownerId against __eggsState.owners map
            owner_id = item.get("ownerId")
            owner_entry = None
            if owners and owner_id is not None:
                owner_entry = owners.get(str(owner_id)) or owners.get(owner_id)
            if owner_entry and isinstance(owner_entry, dict):
                ot = str(owner_entry.get("type") or owner_entry.get("ownerType") or "").upper()
                if ot in _PRIVATE_VALS:
                    seller_type = "private"
                elif ot in _DEALER_VALS:
                    seller_type = "dealer"
                else:
                    # owner entry exists → dealer
                    seller_type = "dealer"
            else:
                # 4. ownerId None → private; ownerId present → dealer
                seller_type = "private" if owner_id is None else "dealer"

    return {
        "year": _int(year),
        "make": make or "Porsche",
        "model": model,
        "trim": trim,
        "mileage": mileage,
        "price": price,
        "vin": vin,
        "url": url,
        "image_url": image_url,
        "location": location,
        "seller_type": seller_type,
    }


def _find_inventory_recursive(obj, depth=0):
    """
    Recursively search obj for a dict keyed 'inventory' whose values look like
    vehicle listings (contain 'year' or 'make').
    Returns (inventory_dict, owners_dict) or (None, {}).
    """
    if depth > 6 or not isinstance(obj, dict):
        return None, {}
    inv = obj.get("inventory")
    if isinstance(inv, dict) and inv:
        first_val = next(iter(inv.values()), None)
        if isinstance(first_val, dict) and ("year" in first_val or "make" in first_val):
            return inv, obj.get("owners") or {}
    for v in obj.values():
        result, owners = _find_inventory_recursive(v, depth + 1)
        if result is not None:
            return result, owners
    return None, {}


def _extract_listings_from_html(html):
    """Extract inventory items from an AutoTrader search page (__NEXT_DATA__ JSON)."""
    data = _extract_next_data(html)
    if not data:
        log.warning("No __NEXT_DATA__ found in page")
        return []

    inventory = None
    owners = {}

    # Path a: props.pageProps.__eggsState.inventory  (mobile + some desktop builds)
    try:
        eggs = data["props"]["pageProps"]["__eggsState"]
        if isinstance(eggs, dict) and eggs.get("inventory"):
            inventory = eggs["inventory"]
            owners = eggs.get("owners") or {}
            log.info("Inventory path: __eggsState.inventory (%d items)", len(inventory))
    except (KeyError, TypeError):
        pass

    # Path b: props.pageProps.initialState.inventory
    if not inventory:
        try:
            init = data["props"]["pageProps"]["initialState"]
            if isinstance(init, dict) and init.get("inventory"):
                inventory = init["inventory"]
                owners = init.get("owners") or {}
                log.info("Inventory path: initialState.inventory (%d items)", len(inventory))
        except (KeyError, TypeError):
            pass

    # Path c: recursive search anywhere in the tree
    if not inventory:
        inventory, owners = _find_inventory_recursive(data)
        if inventory:
            log.info("Inventory path: recursive search (%d items)", len(inventory))

    if not inventory:
        log.info("inventory is empty in __NEXT_DATA__")
        return []

    listings = []
    for listing_id, item in inventory.items():
        car = _parse_inventory_item(listing_id, item, owners=owners)
        if car:
            listings.append(car)

    log.info("Extracted %d raw listings from __NEXT_DATA__", len(listings))
    return listings


# ---------------------------------------------------------------------------
# curl_cffi — Safari TLS impersonation (bypasses Akamai TLS fingerprinting)
# ---------------------------------------------------------------------------
_CFFI_AVAILABLE = None

# Safari profiles bypass Akamai — Chrome profiles (chrome110-124) are consistently blocked.
# Try safari17_0 first; safari15_3 as fallback.
_CFFI_PROFILES = ("safari17_0", "safari15_3")

# Chicago DataImpulse IPs have the best Akamai reputation.
# Try chicago first, then other US cities as fallback.
_PROXY_CITIES = ("chicago", "phoenix", "dallas", "houston")


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
    Fetch a page using curl_cffi with Safari TLS impersonation + city-targeted proxy.

    Akamai blocks Chrome TLS fingerprints and country-level (__cr.us) proxy IPs are
    ~50% blocked. City-targeted IPs (especially chicago) have much better reputation.
    Retries across safari profiles and cities until one succeeds.
    Returns HTML string or None.
    """
    if not _curl_cffi_available():
        return None
    if not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        log.warning("curl_cffi: proxy not available — skipping")
        return None
    from curl_cffi import requests as cr
    for city in _PROXY_CITIES:
        proxy_url = _city_proxy_url(city)
        if not proxy_url:
            continue
        proxies = {"http": proxy_url, "https": proxy_url}
        for profile in _CFFI_PROFILES:
            try:
                r = cr.get(url, impersonate=profile, timeout=25,
                           proxies=proxies, allow_redirects=True)
                if _is_blocked(r.text) or len(r.text) < 10000:
                    log.debug("curl_cffi %s/%s: block page (len=%d)", city, profile, len(r.text))
                    continue
                if "__NEXT_DATA__" not in r.text:
                    log.debug("curl_cffi %s/%s: no __NEXT_DATA__ (len=%d)", city, profile, len(r.text))
                    continue
                log.info("curl_cffi succeeded: city=%s profile=%s len=%d", city, profile, len(r.text))
                return r.text
            except Exception as e:
                log.debug("curl_cffi %s/%s error: %s", city, profile, e)
    log.info("curl_cffi: all city/profile combos blocked for %s", url)
    return None


# ---------------------------------------------------------------------------
# REST API fallback (returns JSON directly — no HTML parsing needed)
# ---------------------------------------------------------------------------
def _parse_rest_listing(item):
    """Parse one listing dict from the AutoTrader REST API response."""
    if not isinstance(item, dict):
        return None

    listing_id = str(item.get("id") or "")
    year = item.get("year")
    make = _clean(item.get("make"))
    model = _clean(item.get("model"))
    trim = _clean(item.get("trim"))
    vin = _clean(item.get("vin"))
    mileage = _int(item.get("mileage"))
    price = _int(item.get("derivedPrice") or item.get("price"))

    listing_url = item.get("listingUrl") or ""
    if listing_url and not listing_url.startswith("http"):
        url = _BASE_URL + listing_url
    elif listing_url:
        url = listing_url
    else:
        url = _BASE_URL + f"/cars-for-sale/vehicle/{listing_id}"

    # Image: REST API returns images as list of dicts with 'src'
    image_url = None
    images = item.get("images") or []
    if images and isinstance(images[0], dict):
        src = images[0].get("src", "")
        if src.startswith("https://"):
            image_url = src

    location = _clean(item.get("ownerName"))

    # ownerId/dealerId present → dealer; absent → private
    owner_id = item.get("ownerId") or item.get("dealerId")
    seller_type = "private" if owner_id is None else "dealer"

    return {
        "year": _int(year),
        "make": make or "Porsche",
        "model": model,
        "trim": trim,
        "mileage": mileage,
        "price": price,
        "vin": vin,
        "url": url,
        "image_url": image_url,
        "location": location,
        "seller_type": seller_type,
    }


def _fetch_rest_api(num_records, first_record):
    """
    Fetch listings from AutoTrader's REST listing API.
    Returns a list of parsed car dicts, or [] on failure.

    Uses CORS headers appropriate for an XHR/fetch call, not a page navigation.
    """
    url = (
        f"{_REST_BASE}?makeCode=PORSCHE"
        f"&numRecords={num_records}&firstRecord={first_record}"
        "&sellerTypes=p,d"
    )
    log.info("  Trying REST API: %s", url)
    # AJAX headers — sec-fetch-dest/mode differ from a page navigation
    ajax_headers = {
        "Accept": "application/json, text/plain, */*",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Referer": "https://www.autotrader.com/cars-for-sale/used-cars/porsche/porsche/",
    }
    data = None
    # Try curl_cffi first (Safari TLS + city proxy bypasses Akamai)
    if _curl_cffi_available():
        from curl_cffi import requests as cr
        for _city in _PROXY_CITIES:
            _cffi_proxy = _city_proxy_url(_city)
            if not _cffi_proxy:
                continue
            _cffi_proxies = {"http": _cffi_proxy, "https": _cffi_proxy}
            for _profile in _CFFI_PROFILES:
                try:
                    r = cr.get(url, impersonate=_profile, timeout=25,
                               headers=ajax_headers, proxies=_cffi_proxies, allow_redirects=True)
                    ct = r.headers.get("content-type", "")
                    if "text/html" in ct or _is_blocked(r.text):
                        log.debug("  REST API (curl_cffi) %s/%s: block page", _city, _profile)
                        continue
                    data = r.json()
                    break
                except Exception as e:
                    log.debug("  REST API curl_cffi %s/%s error: %s", _city, _profile, e)
            if data is not None:
                break

    # Fall back to requests
    if data is None:
        try:
            r = _SESSION.get(url, headers=ajax_headers, timeout=25,
                             allow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "text/html" in ct or _is_blocked(r.text):
                log.info("  REST API: block page (len=%d)", len(r.text))
                return []
            data = r.json()
        except Exception as e:
            log.warning("  REST API fetch failed: %s", e)
            return []

    if data is None:
        return []

    listings_raw = data.get("listings") or []
    if not listings_raw:
        log.info("  REST API: empty listings array (keys: %s)", list(data.keys())[:8])
        return []

    cars = []
    for item in listings_raw:
        car = _parse_rest_listing(item)
        if car:
            cars.append(car)

    log.info("  REST API: %d raw listings", len(cars))
    return cars


# ---------------------------------------------------------------------------
# HTTP fetch (fast path — requests)
# ---------------------------------------------------------------------------
def _fetch_requests(url):
    """Try fetching via requests through the proxy. Returns HTML or None if blocked/failed."""
    try:
        r = _SESSION.get(url, timeout=25, allow_redirects=True)
        r.raise_for_status()
        if _is_blocked(r.text):
            log.info("requests: bot-block page at %s", url)
            return None
        if "__NEXT_DATA__" not in r.text:
            log.info("requests: no __NEXT_DATA__ at %s (len=%d)", url, len(r.text))
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
# Playwright fetch (bypass path)
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


_FINGERPRINT_SCRIPT = """
// 1. Remove navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. Realistic plugins list
const _plugins = [
    {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
];
const pluginArray = Object.create(PluginArray.prototype);
_plugins.forEach(function(p, i) {
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperty(plugin, 'name', {get: () => p.name});
    Object.defineProperty(plugin, 'filename', {get: () => p.filename});
    Object.defineProperty(plugin, 'description', {get: () => p.description});
    Object.defineProperty(plugin, 'length', {get: () => 0});
    pluginArray[i] = plugin;
});
Object.defineProperty(pluginArray, 'length', {get: () => _plugins.length});
Object.defineProperty(navigator, 'plugins', {get: () => pluginArray});

// 3. Languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// 4. permissions.query — return granted for notifications
const _origQuery = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
if (navigator.permissions) {
    navigator.permissions.query = function(params) {
        if (params && params.name === 'notifications') {
            return Promise.resolve({state: 'granted', onchange: null});
        }
        return _origQuery ? _origQuery(params) : Promise.resolve({state: 'prompt', onchange: null});
    };
}

// 5. Canvas fingerprint noise — XOR last byte by 1
(function() {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
        const result = _toDataURL.call(this, type, quality);
        // Flip last base64 char to add imperceptible noise
        if (result && result.length > 4) {
            const arr = result.split('');
            const idx = arr.length - 2;
            const code = arr[idx].charCodeAt(0);
            arr[idx] = String.fromCharCode(code ^ 1);
            return arr.join('');
        }
        return result;
    };
    const _toBlob = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {
        return _toBlob.call(this, callback, type, quality);
    };
})();

// 6. WebGL renderer spoof
(function() {
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
        return _getParam.call(this, param);
    };
    try {
        const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Intel Inc.';
            if (param === 37446) return 'Intel Iris OpenGL Engine';
            return _getParam2.call(this, param);
        };
    } catch(e) {}
})();

// 7. chrome runtime object
if (!window.chrome) {
    window.chrome = {runtime: {}};
} else if (!window.chrome.runtime) {
    window.chrome.runtime = {};
}

// 8. Screen dimensions
Object.defineProperty(screen, 'width', {get: () => 1920});
Object.defineProperty(screen, 'height', {get: () => 1080});
Object.defineProperty(window, 'outerWidth', {get: () => 1280});
Object.defineProperty(window, 'outerHeight', {get: () => 900});

// 9. Remove HeadlessChrome from userAgent
(function() {
    const ua = navigator.userAgent.replace('HeadlessChrome', 'Chrome');
    Object.defineProperty(navigator, 'userAgent', {get: () => ua});
})();
"""

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--no-zygote",
]


def _fetch_playwright(url, headless=True):
    """
    Fetch a page with Playwright + comprehensive Akamai fingerprint spoofing.
    Injects JS overrides via add_init_script() before any page navigation,
    so Akamai's bot checks see spoofed values for webdriver, plugins, canvas,
    WebGL, chrome runtime, and screen dimensions.
    headless=False uses a real Chrome window for additional bypass capability.
    Returns HTML or None.
    """
    if not _playwright_available():
        log.debug("Playwright not installed")
        return None

    from playwright.sync_api import sync_playwright

    kwargs = {
        "headless": headless,
        "args": _LAUNCH_ARGS,
    }
    proxy = _pw_proxy()
    if proxy:
        kwargs["proxy"] = proxy

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**kwargs)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=_USER_AGENT,
                locale="en-US",
                timezone_id="America/Los_Angeles",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = ctx.new_page()

            # Inject fingerprint spoofs BEFORE any navigation (runs before page JS)
            page.add_init_script(_FINGERPRINT_SCRIPT)

            # Belt-and-suspenders: also apply playwright-stealth if available
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(page)
            except ImportError:
                pass

            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for listing data to appear
            for selector in (
                '[class*="inventory"]',
                '[data-cmp="itemCard"]',
                'script#__NEXT_DATA__',
            ):
                try:
                    page.wait_for_selector(selector, timeout=8000)
                    break
                except Exception:
                    continue

            time.sleep(1.5)
            html = page.content()

            if _is_blocked(html) or "__NEXT_DATA__" not in html:
                log.info(
                    "Playwright (headless=%s): blocked/no-data (len=%d) first 500 chars: %s",
                    headless, len(html), html[:500],
                )
                browser.close()
                return None

            browser.close()

        return html

    except Exception as e:
        log.warning("Playwright error (headless=%s): %s", headless, e)
        return None


# ---------------------------------------------------------------------------
# Page fetcher — tries all strategies in order
# ---------------------------------------------------------------------------
def _fetch_page(url):
    """
    Fetch a URL trying each strategy in order until one succeeds.

    Order:
      1. curl_cffi with Chrome TLS impersonation (bypasses Akamai TLS fingerprinting)
      2. requests (fast, may be TLS-fingerprint-blocked)
      3. headed Playwright (full browser, bypasses bot detection on same IP)
      4. headless Playwright + stealth (fallback)
    """
    log.info("Fetching: %s", url)

    # Strategy 1: curl_cffi (Chrome TLS fingerprint — bypasses Akamai)
    if _curl_cffi_available():
        html = _fetch_curl_cffi(url)
        if html:
            log.info("  ✓ curl_cffi succeeded (len=%d)", len(html))
            return html
        log.info("  curl_cffi blocked/failed — trying requests")

    # Strategy 2: requests
    html = _fetch_requests(url)
    if html:
        log.info("  ✓ requests succeeded (len=%d)", len(html))
        return html

    # Strategy 3: headless Playwright + stealth
    # Note: headed (visible) Playwright removed — opens Chrome window on screen
    log.info("  requests blocked/failed — trying headless Playwright")
    html = _fetch_playwright(url, headless=True)
    if html:
        log.info("  ✓ headless Playwright succeeded (len=%d)", len(html))
        return html

    log.warning("  All fetch strategies failed for %s", url)
    return None


# ---------------------------------------------------------------------------
# Bootstrap state helpers
# ---------------------------------------------------------------------------
def _load_state():
    """Load bootstrap state from disk; return {} if missing or unreadable."""
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    """Persist bootstrap state to disk (creates parent dirs if needed)."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------
def scrape_autotrader(max_pages=None):
    """
    Scrape AutoTrader for used Porsche listings (dealers + private sellers).
    Always runs through DataImpulse proxy — never falls back to direct IP.
    If proxy is unavailable, returns [] immediately (skips this cycle).

    max_pages: maximum pages to fetch (overrides internal bootstrap logic when provided).
      None (default) — 1 page after bootstrap, 10 pages on first run.
      1              — 1 page only (fast-cycle mode).
      3              — up to 3 pages (deep-cycle mode).
    """
    # Gate: refuse to run without proxy — naked Mac Mini IP gets blocked by Akamai
    if not _PROXY_URL or not _PROXY_CFG.get("enabled"):
        log.warning("AutoTrader: proxy not configured — skipping scrape")
        return []

    state = _load_state()
    bootstrapped = state.get("bootstrapped", False)

    if max_pages is not None:
        num_records = 25
        effective_max_pages = max_pages
        log.info("AutoTrader: run (max_pages=%d, %d records/page)", effective_max_pages, num_records)
    elif bootstrapped:
        num_records = 25
        effective_max_pages = 1
        log.info("AutoTrader: incremental run (1 page, %d records)", num_records)
    else:
        num_records = 100
        effective_max_pages = 10
        log.info("AutoTrader: bootstrap run (up to %d pages, %d records each)",
                 effective_max_pages, num_records)
    max_pages = effective_max_pages

    all_listings = []
    seen_keys = set()
    filtered_out = 0

    for page in range(max_pages):
        # Abort if proxy died mid-session — don't expose bare IP
        if _PROXY_DEAD:
            log.warning("AutoTrader: proxy died mid-scrape — stopping (no naked-IP fallback)")
            break

        first_record = page * num_records
        url = _SEARCH_BASE + f"&numRecords={num_records}&firstRecord={first_record}"

        html = _fetch_page(url)
        if html:
            raw = _extract_listings_from_html(html)
        else:
            # All HTML strategies failed — try REST API
            log.info("AutoTrader: HTML fetch failed on page %d — trying REST API", page + 1)
            raw = _fetch_rest_api(num_records, first_record)

        # Retry once on page 1 zero-results: proxy likely rotated to a blocked IP.
        # A 3-second pause forces a new IP assignment from the pool.
        if not raw and page == 0:
            log.info("AutoTrader: 0 listings on page 1 — retrying in 3s with fresh proxy IP")
            time.sleep(3)
            html = _fetch_page(url)
            if html:
                raw = _extract_listings_from_html(html)
            else:
                raw = _fetch_rest_api(num_records, first_record)
            if not raw:
                log.warning("AutoTrader: retry also returned 0 listings — giving up this cycle")
                break

        if not raw:
            log.info("AutoTrader: 0 listings on page %d — end of results", page + 1)
            break

        new_this_page = 0
        for car in raw:
            key = car.get("vin") or car.get("url") or ""
            if not key:
                key = f"{car.get('year')}|{car.get('model')}|{car.get('price')}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if not _is_listing_url(car.get("url")):
                continue

            if not _is_sports_car(car):
                filtered_out += 1
                continue

            if _is_valid_listing(car):
                all_listings.append(car)
                new_this_page += 1

        log.info("AutoTrader page %d: %d new listings (running total: %d)",
                 page + 1, new_this_page, len(all_listings))

        if new_this_page == 0:
            break

        time.sleep(2.0)  # be polite between pages

    if not bootstrapped and all_listings:
        _save_state({"bootstrapped": True})
        log.info("AutoTrader: bootstrap complete — state file written to %s", _STATE_FILE)

    log.info("AutoTrader scrape complete: %d listings (%d filtered out)",
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

    results = scrape_autotrader()
    print(f"\nTotal listings: {len(results)}")

    if results:
        print("\nFirst 5 results:")
        for i, car in enumerate(results[:5]):
            url_preview = (car.get("url") or "")[:70]
            print(f"  {i+1}. {car.get('year')} {car.get('model')} "
                  f"{car.get('trim') or '(no trim)'} "
                  f"| {car.get('seller_type') or 'unknown'} "
                  f"| {url_preview}")

        print("\nFirst 3 results (full detail):")
        for i, car in enumerate(results[:3]):
            print(f"\n--- Listing {i+1} ---")
            for k, v in car.items():
                print(f"  {k}: {v}")

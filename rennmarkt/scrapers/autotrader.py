"""
AutoTrader scraper — fetch via Decodo Web Scraping API.

Decodo handles Akamai bypass transparently. We POST the AutoTrader URL to
scraper-api.decodo.com/v2/scrape and get back the full page HTML.
All existing __NEXT_DATA__ parsing logic is unchanged.

Cost: ~$0.50/1K requests (standard tier). At 3 deep scrapes/hr + incremental
cadence = ~150 req/day = ~$2.25/month.
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
# Decodo Web Scraping API config
# ---------------------------------------------------------------------------
_DECODO_ENDPOINT = "https://scraper-api.decodo.com/v2/scrape"
_DECODO_CFG_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "decodo_config.json"

_DECODO_TOKEN = None

def _load_decodo_token():

    global _DECODO_TOKEN
    import base64
    try:
        with open(_DECODO_CFG_FILE) as f:
            cfg = json.load(f)
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        _DECODO_TOKEN = base64.b64encode(f"{username}:{password}".encode()).decode()
        log.info("Decodo: token loaded for user %s", username)
    except Exception as e:
        log.warning("Decodo config not found at %s: %s", _DECODO_CFG_FILE, e)
        _DECODO_TOKEN = None

_load_decodo_token()


def _increment_decodo_counter():
    """Increment local Decodo request counter for health monitoring."""
    from datetime import date as _date
    import json as _json
    try:
        usage_path = Path(__file__).resolve().parent.parent.parent / "data" / "decodo_usage.json"
        this_month = _date.today().strftime("%Y-%m")
        try:
            with open(usage_path) as f:
                usage = _json.load(f)
            if usage.get("month") != this_month:
                usage = {"month": this_month, "count": 0}
        except Exception:
            usage = {"month": this_month, "count": 0}
        usage["count"] = usage.get("count", 0) + 1
        with open(usage_path, "w") as f:
            _json.dump(usage, f)
    except Exception:
        pass  # Never break scraping over a counter

# Keep minimal session for non-AutoTrader requests (VDP fetches etc.)
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})

# Proxy not used — Decodo Web Scraping API handles all AutoTrader fetches
_PROXY_DEAD = True

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

    # First real https:// image — images may be a dict{sources:[...]} or a list
    image_url = None
    images_obj = item.get("images")
    if isinstance(images_obj, dict):
        sources = images_obj.get("sources") or []
        for _s in sources:
            if isinstance(_s, dict):
                _src = _s.get("src") or ""
                if _src.startswith("https://"):
                    image_url = _src
                    break
            elif isinstance(_s, str) and _s.startswith("https://"):
                image_url = _s
                break
    elif isinstance(images_obj, list):
        for _s in images_obj:
            if isinstance(_s, dict):
                _src = _s.get("src") or ""
                if _src.startswith("https://"):
                    image_url = _src
                    break
            elif isinstance(_s, str) and _s.startswith("https://"):
                image_url = _s
                break
    # Fallback: top-level photo field (some API shapes)
    if not image_url:
        for _field in ("primaryPhotoUrl", "heroPhotoUrl", "thumbnailPhoto", "photoUrl"):
            _src = item.get(_field) or ""
            if isinstance(_src, str) and _src.startswith("https://"):
                image_url = _src
                break

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
# Decodo Web Scraping API fetch — replaces proxy+curl_cffi
# ---------------------------------------------------------------------------
def _fetch_decodo(url, use_js=False):
    """
    Fetch a URL via Decodo scraping API. Handles Akamai bypass transparently.
    use_js=False: standard tier (~$0.50/1K) — sufficient for AutoTrader __NEXT_DATA__
    use_js=True:  JS rendering (~$0.75/1K) — fallback if standard returns empty
    """
    if not _DECODO_TOKEN:
        log.warning("Decodo: no token — skipping fetch")
        return None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {_DECODO_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "target": "universal",
        "url": url,
        "headless": "html",
        "locale": "en-us",
        "device_type": "desktop",
    }
    if use_js:
        payload["headless"] = "html"
        payload["javascript"] = True
    try:
        r = requests.post(_DECODO_ENDPOINT, headers=headers, json=payload, timeout=90)
        if r.status_code != 200:
            log.warning("Decodo: HTTP %d — %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        html = data.get("results", [{}])[0].get("content", "")
        if not html or len(html) < 10000:
            log.warning("Decodo: empty/small response (len=%d)", len(html))
            return None
        if "__NEXT_DATA__" not in html:
            log.warning("Decodo: no __NEXT_DATA__ in response (len=%d)", len(html))
            if not use_js:
                log.info("Decodo: retrying with JS rendering")
                return _fetch_decodo(url, use_js=True)
            return None
        log.info("Decodo: success len=%d", len(html))
        _increment_decodo_counter()
        return html
    except Exception as e:
        log.error("Decodo fetch error: %s", e)
        return None


# Keep _fetch_curl_cffi as a no-op alias so VDP enrich still compiles
def _fetch_curl_cffi(url):
    """Replaced by Decodo — kept for VDP enrich compatibility."""
    return _fetch_decodo(url)


# ---------------------------------------------------------------------------
# REST API fallback (returns JSON directly — no HTML parsing needed)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# VDP detail extraction — folds the old enrich_listing_detail.enrich_autotrader
# logic into the first-pass scrape. Spec fields live in __NEXT_DATA__ JSON
# embedded in the VDP HTML; we regex them out rather than walking the full tree.
#
# Cost control: only fetch VDP for listings that don't already have detail data
# in the DB. In steady state ~1 fetch per cycle (the one new listing on page 1).
# Cold start / bootstrap will fetch VDPs for all listings — expensive but a
# one-time cost.
# ---------------------------------------------------------------------------

_AT_BODY_CODE_MAP = {
    "CONVERT":   "Cabriolet",
    "COUPE":     "Coupe",
    "TARGA":     "Targa",
    "ROADSTER":  "Roadster",
    "SPEEDSTER": "Speedster",
}


def _drivetrain_with_trim_override(api_drv, trim, year):
    """Trim wins over API for tracked Porsche models. Mirrors the helper in
    scraper_ebay._drivetrain_with_trim_override and enrich_listing_detail."""
    if trim:
        t = trim.lower()
        if re.search(r"\bgt[234]\b", t):
            return "RWD"
        if re.search(r"\bturbo\s+s\b", t):
            return "AWD"
        if re.search(r"\bcarrera\s*4\b|\btarga\s*4\b|\b4s\b", t):
            return "AWD"
        if re.search(r"\bturbo\b", t) and year and int(year) >= 1995:
            return "AWD"
        if re.search(r"\bturbo\b", t) and year and int(year) <= 1994:
            return "RWD"
    return api_drv


def _vdp_extract_specs(html):
    """Pull color/engine/drivetrain/body_style from a VDP HTML page.
    AT's structured `transmission` field is usually empty {} so we don't try
    to extract it — leave None and let other paths fill it (e.g. NHTSA cache).
    """
    if not html:
        return {}
    color = None
    m = re.search(r'"exteriorColor"\s*:\s*"([^"]+)"', html)
    if m:
        color = m.group(1).strip() or None

    engine = None
    m = re.search(r'"engineDescription"\s*:\s*\{[^}]*?"value"\s*:\s*"([^"]+)"', html)
    if m:
        engine = m.group(1).strip()[:60] or None

    # Prefer driveType.name (already 'RWD'/'AWD'); fall back to .value
    drv = None
    m = re.search(r'"driveType"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', html)
    if not m:
        m = re.search(r'"driveType"\s*:\s*\{[^}]*?"value"\s*:\s*"([^"]+)"', html)
    if m:
        drv_raw = m.group(1).strip()
        if drv_raw.upper() in ("RWD", "AWD", "FWD", "4WD"):
            drv = drv_raw.upper().replace("4WD", "AWD")
        elif "rear" in drv_raw.lower() or "2 wheel" in drv_raw.lower():
            drv = "RWD"
        elif "all" in drv_raw.lower() or "four" in drv_raw.lower() or "4 wheel" in drv_raw.lower():
            drv = "AWD"

    body = None
    m = re.search(r'"bodyStyleCodes"\s*:\s*\[\s*"([^"]+)"', html)
    if m:
        body = _AT_BODY_CODE_MAP.get(m.group(1).upper())

    return {"color": color, "engine": engine, "drivetrain": drv, "body_style": body}


def _enrich_with_vdp(listings):
    """For each listing missing detail fields in the DB, fetch its VDP
    (via the same _fetch_curl_cffi proxy path as the search) and merge
    color/engine/drivetrain/body_style. In-place mutation; safe on empty input.
    """
    if not listings:
        return
    try:
        import db as _db
    except Exception:
        log.warning("AutoTrader VDP enrich: db module unavailable — skipping")
        return

    urls = [l.get("url") for l in listings if l.get("url")]
    if not urls:
        return

    # Find which URLs already have any detail data — skip those.
    already_enriched = set()
    try:
        with _db.get_conn() as conn:
            placeholders = ",".join(["?"] * len(urls))
            rows = conn.execute(
                f"""SELECT listing_url FROM listings
                    WHERE listing_url IN ({placeholders})
                      AND (transmission IS NOT NULL OR color IS NOT NULL OR engine IS NOT NULL)""",
                urls,
            ).fetchall()
            already_enriched = {r[0] for r in rows}
    except Exception as e:
        log.debug("AutoTrader VDP enrich: DB skip-check failed: %s", e)

    to_fetch = [l for l in listings if l.get("url") and l["url"] not in already_enriched]
    if not to_fetch:
        return

    log.info("AutoTrader VDP enrich: %d/%d listings need detail fetch",
             len(to_fetch), len(listings))

    fetched = 0
    for car in to_fetch:
        # Same proxy path as the search endpoint — no double-proxy, just one
        # extra request per new listing.
        html = _fetch_curl_cffi(car["url"])
        if not html:
            continue
        spec = _vdp_extract_specs(html)
        if spec.get("color"):
            car["color"] = spec["color"]
        if spec.get("engine"):
            car["engine"] = spec["engine"]
        if spec.get("body_style"):
            car["body_style"] = spec["body_style"]
        # Drivetrain: trim override wins
        api_drv = spec.get("drivetrain")
        drv = _drivetrain_with_trim_override(api_drv, car.get("trim"), car.get("year"))
        if drv:
            car["drivetrain"] = drv
        fetched += 1
        time.sleep(0.5)

    log.info("AutoTrader VDP enrich: %d fetched, %d skipped (already enriched)",
             fetched, len(listings) - len(to_fetch))


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

    # Image: REST API returns images as list of dicts or strings
    image_url = None
    images = item.get("images") or []
    for _img in images:
        if isinstance(_img, dict):
            _src = _img.get("src") or _img.get("url") or ""
            if isinstance(_src, str) and _src.startswith("https://"):
                image_url = _src
                break
        elif isinstance(_img, str) and _img.startswith("https://"):
            image_url = _img
            break
    # Fallback: top-level photo field (some API response shapes)
    if not image_url:
        for _field in ("primaryPhotoUrl", "heroPhotoUrl", "thumbnailPhoto", "photoUrl"):
            _src = item.get(_field) or ""
            if isinstance(_src, str) and _src.startswith("https://"):
                image_url = _src
                break

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


def _fetch_playwright(url, headless=True):
    """Removed — Decodo API replaced all fetch strategies."""
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
    log.info("Fetching via Decodo: %s", url)
    html = _fetch_decodo(url)
    if html:
        log.info("  ✓ Decodo succeeded (len=%d)", len(html))
        return html
    log.warning("  Decodo failed for %s", url)
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
    Uses Decodo Web Scraping API — no proxy required (Akamai bypass handled by Decodo).

    max_pages: maximum pages to fetch (overrides internal bootstrap logic when provided).
      None (default) — 1 page after bootstrap, 10 pages on first run.
      1              — 1 page only (fast-cycle mode).
      3              — up to 3 pages (deep-cycle mode).
    """
    # Gate: refuse to run without Decodo token
    if not _DECODO_TOKEN:
        log.warning("AutoTrader: Decodo token not configured — skipping scrape")
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

    # Fold detail-field enrichment into the first-pass scrape (replaces the
    # separate enrich_listing_detail.enrich_autotrader path). Only fetches
    # VDPs for listings whose detail fields are not yet in the DB.
    # VDP enrichment disabled — saves Decodo credits. Core data from __NEXT_DATA__ is sufficient.

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

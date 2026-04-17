"""
Standalone eBay Motors scraper for Porsche listings.

Uses the eBay Browse API (OAuth 2.0 Client Credentials) — clean JSON,
no HTML parsing, no DOM dependency. Returns both Buy It Now (RETAIL)
and auction listings.

Cache-based pattern (fixes mark_sold() destroying inventory):
  Full sweep: up to MAX_PAGES_FULL pages every CACHE_TTL_MINUTES (default 60 min).
  Incremental: page 0 only (newest listings) — merged into cache each other cycle.
  Every call returns the full cached inventory so mark_sold() never kills active listings.

Proxy: loaded from data/proxy_config.json — optional for eBay API (it's an
official REST API, not scraping). If proxy load fails, continues without it.
If proxy IS configured and loaded, it is used for all requests.
"""
import base64
import json
import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEALER_NAME = "eBay Motors"

_STATE_FILE = Path.home() / "porsche-tracker" / "data" / "ebay_state.json"
_CFG_FILE   = Path.home() / "porsche-tracker" / "data" / "ebay_api_config.json"
_CACHE_FILE = Path.home() / "porsche-tracker" / "data" / "ebay_cache.json"

# Full inventory refresh once per hour; incremental (page 0 only) every other cycle.
# This prevents mark_sold() from killing listings that weren't in the latest 20 results.
_CACHE_TTL_MINUTES = 60
_MAX_PAGES_FULL    = 50   # 50 × 20 = 1000 slots — covers the full eBay Porsche inventory

_OAUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE      = "https://api.ebay.com/oauth/api_scope"

# In-memory token cache — never written to disk
_token_cache = {"token": None, "expires_at": 0}

# ---------------------------------------------------------------------------
# Import filter from scraper.py
# ---------------------------------------------------------------------------
# NOTE: scraper.py imports scraper_ebay.py, creating a circular import.
# The try/except below catches the ImportError and falls back to return True,
# meaning the imported _is_valid_listing is effectively a no-op here.
# Use _local_valid() below for filtering inside scrape_ebay().
# scraper.py's run_all() will apply the real _is_valid_listing() afterwards.
try:
    from scraper import _is_valid_listing
except Exception:
    def _is_valid_listing(car):
        return True

# Allowed base-model substrings — mirrors scraper.py's _ALLOWED_MODELS.
# Used by _local_valid() to avoid circular-import dependency.
_ALLOWED_MODEL_TOKENS = frozenset({"911", "cayman", "boxster", "718"})


YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not increase until Jan 1 2027

def _local_valid(car):
    """Lightweight validity check that works without scraper.py's _is_valid_listing().
    Filters non-target Porsche models, year range, and extreme mileage.
    """
    model = (car.get("model") or "").lower()
    if not model:
        return False
    if not any(g in model for g in _ALLOWED_MODEL_TOKENS):
        return False
    year = car.get("year")
    if year and not (YEAR_MIN <= int(year) <= YEAR_MAX):
        return False
    mileage = car.get("mileage")
    if mileage is not None and mileage > 100_000:
        return False
    return True

# ---------------------------------------------------------------------------
# Proxy config — optional for eBay API
# ---------------------------------------------------------------------------
_PROXY_CFG = {}
_PROXY_URL = ""


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
                log.info("eBay: proxy loaded: %s:%s", cfg.get("host"), cfg.get("port"))
                return
        except Exception:
            pass
        p = p.parent
    log.info("eBay: proxy not configured — continuing without proxy (OK for official API)")


_load_proxy()


def _get_proxies():
    """Return requests-compatible proxies dict, or None if no proxy configured."""
    if _PROXY_URL:
        return {"http": _PROXY_URL, "https": _PROXY_URL}
    return None


# ---------------------------------------------------------------------------
# OAuth — Client Credentials flow
# ---------------------------------------------------------------------------
def _load_api_config():
    """Load eBay API credentials from data/ebay_api_config.json."""
    try:
        with open(_CFG_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error("eBay: failed to load API config from %s: %s", _CFG_FILE, e)
        return {}


def _get_token(app_id, cert_id):
    """
    Fetch OAuth token using Client Credentials flow.
    Caches in _token_cache (in-memory only). Returns token string or None.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    credentials = base64.b64encode(
        "{}:{}".format(app_id, cert_id).encode()
    ).decode()

    headers = {
        "Authorization": "Basic {}".format(credentials),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = "grant_type=client_credentials&scope={}".format(_SCOPE)

    proxies = _get_proxies()
    try:
        r = requests.post(
            _OAUTH_URL,
            headers=headers,
            data=data,
            timeout=20,
            proxies=proxies,
        )
        if r.status_code != 200:
            log.error("eBay OAuth failed: HTTP %d — %s", r.status_code, r.text[:500])
            return None
        body = r.json()
        token = body.get("access_token")
        expires_in = body.get("expires_in", 7200)
        if not token:
            log.error("eBay OAuth: no access_token in response — keys: %s", list(body.keys()))
            return None
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in
        log.info("eBay: OAuth token obtained (expires in %ds)", expires_in)
        return token
    except Exception as e:
        log.error("eBay OAuth request error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------
def _extract_year(title):
    """Extract 4-digit year (1900–2099) from title string."""
    if not title:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    return int(m.group(1)) if m else None


# Base models only — must contain one of scraper.py's _ALLOWED_MODELS
# ("911", "cayman", "boxster", "718") to pass _is_valid_listing().
# Longer/more-specific tokens first so "718 Cayman" wins over bare "718".
_MODEL_TOKENS = [
    "718 Cayman", "718 Boxster", "718",
    "Cayman", "Boxster", "911",
]

# Variants that imply a base model when no explicit model name appears in the title.
# e.g. "2019 Porsche GT3 RS" (no "911") → infer "911".
_VARIANT_TO_MODEL = [
    (frozenset({"gt3 rs", "gt3", "gt2 rs", "gt2", "turbo s", "turbo",
                "speedster", "sport classic", "targa"}), "911"),
    (frozenset({"gt4 rs", "gt4"}), "Cayman"),
    (frozenset({"spyder"}), "Boxster"),
]

# Titles that contain these strings are non-target Porsches — reject early
# so they don't get misclassified via variant inference (e.g. Cayenne Turbo → "911").
_TITLE_BLOCKED = frozenset({"cayenne", "macan", "panamera", "taycan"})


def _extract_model(title):
    """Return base model (911/Cayman/Boxster/718/718 Cayman/718 Boxster) from title.

    Checks base model tokens first, then falls back to variant inference.
    Returns None for blocked models (Cayenne/Macan/etc.) and unrecognised titles.
    """
    if not title:
        return None
    title_lower = title.lower()

    # Reject non-target Porsches before any inference
    if any(b in title_lower for b in _TITLE_BLOCKED):
        return None

    for token in _MODEL_TOKENS:
        if token.lower() in title_lower:
            return token

    # Infer base model from GT/variant keywords when the base name is absent
    for variants, base in _VARIANT_TO_MODEL:
        if any(v in title_lower for v in variants):
            return base

    return None


def _extract_trim(title):
    """
    Extract trim: everything after the model token in the title, cleaned up.
    Returns None if no model token found or nothing follows it.
    """
    if not title:
        return None
    title_lower = title.lower()
    for token in _MODEL_TOKENS:
        idx = title_lower.find(token.lower())
        if idx != -1:
            after = title[idx + len(token):].strip()
            # Strip leading punctuation/separators
            after = re.sub(r"^[\s\-–—|:,]+", "", after)
            # Truncate at common separators that start a new clause
            after = re.split(r"\s*[\|–—]\s*", after)[0].strip()
            # Clean extra whitespace
            after = re.sub(r"\s+", " ", after).strip()
            return after or None
    return None


def _extract_mileage(aspects):
    """
    Extract mileage from localizedAspects list.
    Each element is {"name": "...", "value": "..."}.
    Returns int or None.
    """
    if not aspects or not isinstance(aspects, list):
        return None
    for aspect in aspects:
        if not isinstance(aspect, dict):
            continue
        if aspect.get("name", "").lower() == "mileage":
            val = aspect.get("value", "")
            # Strip commas, "mi.", "miles", etc.
            digits = re.sub(r"[^\d]", "", str(val))
            return int(digits) if digits else None
    return None


def _extract_vin(aspects):
    """
    Extract VIN from localizedAspects list.
    Returns string or None.
    """
    if not aspects or not isinstance(aspects, list):
        return None
    for aspect in aspects:
        if not isinstance(aspect, dict):
            continue
        if aspect.get("name", "").lower() == "vin":
            val = str(aspect.get("value", "")).strip()
            return val if val else None
    return None


def _is_private_seller(item):
    """
    Heuristic: feedbackScore under 50 is almost certainly a private individual.
    Defaults to False (assume dealer) if signal is absent.
    """
    feedback_score = item.get("seller", {}).get("feedbackScore", 999)
    try:
        return int(feedback_score) < 50
    except (TypeError, ValueError):
        return False


def _upscale_image(url):
    """
    eBay search API returns s-l225 (225px) thumbnails.
    Swap to s-l1600 for full-size images (same CDN path).
    """
    if not url:
        return None
    return re.sub(r"/s-l\d+\.", "/s-l1600.", url)


def _parse_item(item):
    """
    Parse one eBay itemSummaries entry into our listing dict.
    Returns dict or None if a fatal field is missing.
    """
    title = item.get("title", "")
    aspects = item.get("localizedAspects")

    buying_options = item.get("buyingOptions") or []
    if "FIXED_PRICE" in buying_options:
        source_category = "RETAIL"
    elif "AUCTION" in buying_options:
        source_category = "AUCTION"
    else:
        source_category = "RETAIL"

    price_obj = item.get("price") or {}
    price_val = price_obj.get("value")
    try:
        price = int(float(price_val)) if price_val is not None else None
    except (TypeError, ValueError):
        price = None

    url = item.get("itemWebUrl")
    if not url:
        return None

    return {
        "year":            _extract_year(title),
        "make":            "Porsche",
        "model":           _extract_model(title),
        "trim":            _extract_trim(title),
        "price":           price,
        "mileage":         _extract_mileage(aspects),
        "vin":             _extract_vin(aspects),
        "url":             url,
        "image_url":       _upscale_image((item.get("image") or {}).get("imageUrl")),
        "seller_type":     "private" if _is_private_seller(item) else "dealer",
        "source_category": source_category,
    }


# ---------------------------------------------------------------------------
# API search
# ---------------------------------------------------------------------------
def _search_page(token, page):
    """
    Fetch one page (20 listings) from eBay Browse API.
    page=0 → offset=0, page=1 → offset=20, etc.
    Returns (items_list, total_count) or ([], 0) on failure.
    """
    params = {
        "q": "porsche",
        "category_ids": "6001",
        # itemLocationCountry:US — US listings only (geographic filter)
        "filter": "conditionIds:{3000|4000|6000},price:[25000..],priceCurrency:USD,itemLocationCountry:US",
        # aspect_filter: restrict to Make=Porsche server-side (eliminates Mercedes etc.)
        "aspect_filter": "categoryAspects:Make{Porsche}",
        "sort": "newlyListed",
        "limit": "20",
        "offset": str(page * 20),
    }
    headers = {
        "Authorization": "Bearer {}".format(token),
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }
    proxies = _get_proxies()
    try:
        r = requests.get(
            _SEARCH_URL,
            params=params,
            headers=headers,
            timeout=30,
            proxies=proxies,
        )
        if r.status_code != 200:
            log.error("eBay Browse API: HTTP %d on page %d — %s",
                      r.status_code, page, r.text[:500])
            return [], 0
        body = r.json()
        if "itemSummaries" not in body:
            log.error("eBay Browse API: 'itemSummaries' missing from response — keys: %s",
                      list(body.keys()))
            log.debug("eBay raw response (first 2000 chars): %s", str(body)[:2000])
            return [], 0
        items = body["itemSummaries"]
        total = int(body.get("total", 0))
        return items, total
    except Exception as e:
        log.error("eBay Browse API request error on page %d: %s", page, e)
        return [], 0


# ---------------------------------------------------------------------------
# Full-inventory cache — prevents mark_sold() from killing active listings
# ---------------------------------------------------------------------------
def _load_cache():
    """Return {"listings": [...], "ts": <epoch float>} or empty defaults."""
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"listings": [], "ts": 0.0}


def _save_cache(listings):
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w") as f:
        json.dump({"listings": listings, "ts": time.time()}, f)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------
# Seller usernames to always sweep specifically (owner's own listings etc.)
_WATCH_SELLERS = ["holtmotorsports"]

def _search_seller(token, seller_username):
    """Fetch all active Porsche listings for a specific eBay seller username.
    Uses seller_id filter on the Browse API. Returns parsed listing dicts.
    """
    params = {
        "q": "porsche",
        "category_ids": "6001",
        "filter": "sellers:{%s},conditionIds:{3000|4000|6000},priceCurrency:USD" % seller_username,
        "sort": "newlyListed",
        "limit": "50",
    }
    headers = {
        "Authorization": "Bearer {}".format(token),
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }
    proxies = _get_proxies()
    try:
        r = requests.get(_SEARCH_URL, params=params, headers=headers, timeout=30, proxies=proxies)
        if r.status_code != 200:
            log.warning("eBay seller search (%s): HTTP %d — %s", seller_username, r.status_code, r.text[:200])
            return []
        data = r.json()
        items = data.get("itemSummaries") or []
        log.info("eBay seller '%s': %d listings returned", seller_username, len(items))
        results = []
        for item in items:
            try:
                car = _parse_item(item)
                if car and _local_valid(car):
                    results.append(car)
            except Exception as e:
                log.warning("eBay seller parse error: %s", e)
        return results
    except Exception as e:
        log.warning("eBay seller search (%s) failed: %s", seller_username, e)
        return []


def _fetch_pages(token, max_pages):
    """
    Fetch up to max_pages from the eBay Browse API.
    Returns a list of parsed+validated listing dicts.
    Stops early once all API results have been retrieved.
    """
    all_listings = []
    seen_keys = set()
    filtered_out = 0

    for page in range(max_pages):
        items, total = _search_page(token, page)

        if not items:
            log.info("eBay: 0 items on page %d — end of results", page)
            break

        new_this_page = 0
        for item in items:
            try:
                car = _parse_item(item)
                if car is None:
                    continue

                key = car.get("vin") or car.get("url") or "{}|{}|{}".format(
                    car.get("year"), car.get("model"), car.get("price")
                )
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)

                if not _local_valid(car):
                    filtered_out += 1
                    continue

                all_listings.append(car)
                new_this_page += 1

            except Exception as e:
                log.warning("eBay: skipping bad listing (%s): %s",
                            item.get("itemId", "?"), e)

        log.info("eBay page %d: %d valid listings (API total: %d, running: %d)",
                 page, new_this_page, total, len(all_listings))

        # Stop once we've consumed all available results
        if (page + 1) * 20 >= total:
            break

        if page < max_pages - 1:
            time.sleep(0.5)

    log.info("eBay fetch complete: %d listings (%d filtered out)", len(all_listings), filtered_out)

    # Always sweep watch-list sellers — Browse API misses them in generic search
    seller_seen = {l.get("url") for l in all_listings if l.get("url")}
    for seller in _WATCH_SELLERS:
        seller_listings = _search_seller(token, seller)
        added = 0
        for sl in seller_listings:
            if sl.get("url") and sl["url"] not in seller_seen:
                all_listings.append(sl)
                seller_seen.add(sl["url"])
                added += 1
        log.info("eBay seller sweep '%s': %d new listings merged", seller, added)

    return all_listings


def scrape_ebay():
    """
    Scrape eBay Motors for Porsche listings via the Browse API.

    Cache strategy (solves mark_sold() inventory collapse):
      - Every cycle returns the FULL cached inventory so mark_sold() never
        falsely kills listings that weren't in the latest 20 API results.
      - Full sweep (all pages) runs once per hour to refresh the cache.
      - Incremental run (page 0 only) runs every other cycle and merges new
        listings into the cache.

    Proxy is used if configured, but not required (official API).
    """
    cfg = _load_api_config()
    app_id = cfg.get("app_id")
    cert_id = cfg.get("cert_id")
    if not app_id or not cert_id:
        log.error("eBay: missing app_id or cert_id in %s — skipping", _CFG_FILE)
        return []

    token = _get_token(app_id, cert_id)
    if not token:
        log.error("eBay: could not obtain OAuth token — skipping scrape")
        return []

    cache = _load_cache()
    cache_age_min = (time.time() - cache.get("ts", 0.0)) / 60.0
    cached_listings = cache.get("listings") or []

    if cached_listings and cache_age_min < _CACHE_TTL_MINUTES:
        # --- Incremental update ---
        log.info("eBay: incremental update (cache %.0f min old, %d cached listings)",
                 cache_age_min, len(cached_listings))

        items, total = _search_page(token, 0)
        log.info("eBay page 0: fetched %d items (API total: %d)", len(items), total)

        new_count = 0
        # Merge new/updated listings into cache by URL
        cached_by_url = {l["url"]: l for l in cached_listings if l.get("url")}
        for item in items:
            try:
                car = _parse_item(item)
                if car and car.get("url") and _local_valid(car):
                    if car["url"] not in cached_by_url:
                        new_count += 1
                    cached_by_url[car["url"]] = car
            except Exception as e:
                log.warning("eBay: parse error (%s): %s", item.get("itemId", "?"), e)

        # Also check watch-list sellers on every incremental run
        for seller in _WATCH_SELLERS:
            seller_listings = _search_seller(token, seller)
            s_added = 0
            for sl in seller_listings:
                if sl.get("url") and sl["url"] not in cached_by_url:
                    cached_by_url[sl["url"]] = sl
                    s_added += 1
                    new_count += 1
            if s_added:
                log.info("eBay seller sweep '%s': %d new listings added", seller, s_added)

        merged = list(cached_by_url.values())
        _save_cache(merged)
        log.info("eBay scrape complete: %d listings (%d new this cycle)", len(merged), new_count)
        return merged

    else:
        # --- Full sweep ---
        log.info("eBay: full sweep (cache %.0f min old — refreshing all pages)", cache_age_min)
        listings = _fetch_pages(token, _MAX_PAGES_FULL)

        if listings:
            _save_cache(listings)
            log.info("eBay: cache updated (%d listings)", len(listings))
        elif cached_listings:
            # API returned nothing — preserve cache rather than wiping inventory
            log.warning("eBay: full sweep returned 0 listings — preserving existing cache (%d)",
                        len(cached_listings))
            return cached_listings

        return listings


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    results = scrape_ebay()
    print("\nTotal listings: {}".format(len(results)))

    if not results:
        print("No listings returned — check logs above for API response details.")
    else:
        retail = sum(1 for c in results if c.get("source_category") == "RETAIL")
        auction = sum(1 for c in results if c.get("source_category") == "AUCTION")
        with_images = sum(1 for c in results if c.get("image_url"))
        with_vin = sum(1 for c in results if c.get("vin"))
        with_mileage = sum(1 for c in results if c.get("mileage"))

        print("  RETAIL (Buy It Now): {}".format(retail))
        print("  AUCTION:             {}".format(auction))
        print("  With image_url:      {}".format(with_images))
        print("  With VIN:            {}".format(with_vin))
        print("  With mileage:        {}".format(with_mileage))

        print("\nFirst 5 results (summary):")
        for i, car in enumerate(results[:5]):
            print("  {}. {} {} {} | ${} | {} mi | {} | {}".format(
                i + 1,
                car.get("year") or "?",
                car.get("model") or "?",
                car.get("trim") or "(no trim)",
                "{:,}".format(car["price"]) if car.get("price") else "?",
                "{:,}".format(car["mileage"]) if car.get("mileage") else "?",
                car.get("source_category", "?"),
                car.get("seller_type", "?"),
            ))

        print("\nFirst 5 results (full detail):")
        for i, car in enumerate(results[:5]):
            print("\n--- Listing {} ---".format(i + 1))
            for k, v in car.items():
                print("  {}: {}".format(k, v))

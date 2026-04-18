"""
scraper_dupont.py — DuPont Registry scraper for Porsche listings.

Uses the DuPont Registry internal API (api.dupontregistry.com).
No proxy needed — direct curl_cffi with Chrome impersonation works fine.

API endpoint: POST https://api.dupontregistry.com/api/v1/en_US/car/list
Filter: {"carBrand": [14]}  — Porsche brand ID
Pagination: 27 items per page, sorted newest first.
"""
import logging
import re
import time
from datetime import datetime, date

log = logging.getLogger(__name__)

DEALER_NAME = "DuPont Registry"
YEAR_MIN    = 1984
YEAR_MAX    = 2024  # HARD RULE: do not change — owner decision required

_API_BASE    = "https://api.dupontregistry.com/api/v1/en_US"
_LIST_URL    = f"{_API_BASE}/car/list"
_LISTING_URL = "https://www.dupontregistry.com/autos/{alias}/{id}"
_PORSCHE_BRAND_ID = 14
_PAGE_SIZE   = 27
_MAX_PAGES   = 62   # 1,656 listings / 27 = ~62 pages

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.dupontregistry.com",
    "Referer": "https://www.dupontregistry.com/autos/results/porsche",
}

# ---------------------------------------------------------------------------
# Target model tokens — same as other scrapers
# ---------------------------------------------------------------------------
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718", "spyder"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid(car):
    """Filter to target Porsche models and year range."""
    year = car.get("year")
    if not year:
        return False
    try:
        year = int(year)
    except (TypeError, ValueError):
        return False
    if not (YEAR_MIN <= year <= YEAR_MAX):
        return False
    model = (car.get("model") or "").lower()
    trim_check = (car.get("trim") or "").lower()
    if not any(tok in model for tok in _ALLOWED_MODELS) and not any(tok in trim_check for tok in _ALLOWED_MODELS):
        return False
    return True


def _parse_transmission(raw):
    if not raw:
        return None
    r = raw.lower()
    if "manual" in r:
        return "Manual"
    if "automatic" in r or "auto" in r:
        return "Automatic"
    return raw.strip()


def _parse_drivetrain(raw):
    if not raw:
        return None
    r = raw.lower()
    if "rear" in r or "rwd" in r:
        return "RWD"
    if "all" in r or "awd" in r or "4wd" in r:
        return "AWD"
    if "front" in r or "fwd" in r:
        return "FWD"
    return raw.strip()


def _listing_url(car_id, model_alias):
    """Build public listing URL from car ID and model alias."""
    alias = (model_alias or "porsche").lower().replace(" ", "-")
    return f"https://www.dupontregistry.com/autos/{alias}/{car_id}"


def _parse_car(item):
    """Parse a single API car object into our standard listing dict."""
    car_id  = item.get("id")
    year    = item.get("year")
    mileage = item.get("mileage")
    vin     = item.get("vin") or None
    trans   = _parse_transmission(item.get("transmission"))
    drive   = _parse_drivetrain(item.get("driveTrain"))

    # Price — 0 means "Call for Price"
    price = item.get("price")
    if price is not None and price == 0:
        price = None

    # Model variant — DuPont stores e.g. "Carrera 4S", "GT3 RS", "Boxster S"
    # in carModel.name. This becomes our trim. We infer base model from it.
    car_model   = item.get("carModel") or {}
    raw_variant = (car_model.get("name") or "").strip()
    model_alias = car_model.get("alias") or ""

    rv_lower = raw_variant.lower()
    if "boxster" in rv_lower:
        model_name = "Boxster"
    elif "cayman" in rv_lower:
        model_name = "Cayman"
    elif "718" in rv_lower:
        model_name = "718"
    elif "spyder" in rv_lower:
        model_name = "Boxster"
    elif any(x in rv_lower for x in ("panamera", "macan", "cayenne", "taycan")):
        model_name = raw_variant  # filtered out by _is_valid
    else:
        model_name = "911"

    # Strip model name prefix from trim if present (e.g. "911 Carrera 4S" -> "Carrera 4S")
    trim_raw = raw_variant or (item.get("trim") or "").strip()
    for prefix in ("911 ", "Boxster ", "Cayman ", "718 ", "Porsche "):
        if trim_raw.lower().startswith(prefix.lower()):
            trim_raw = trim_raw[len(prefix):].strip()
            break
    trim = trim_raw or None

    # Image
    photos    = item.get("photos") or []
    image_url = None
    for ph in photos:
        img = ph.get("image") or {}
        image_url = img.get("width_916") or img.get("width_720") or img.get("original")
        if image_url:
            break

    # Location
    city     = item.get("city") or ""
    state    = item.get("state") or ""
    location = ", ".join(p for p in [city, state] if p) or None

    # Listing URL
    url = _listing_url(car_id, model_alias)

    # Seller type
    is_private  = item.get("isPvtSeller", False)
    seller_type = "private" if is_private else "dealer"

    return {
        "year":          year,
        "make":          "Porsche",
        "model":         model_name,
        "trim":          trim,
        "mileage":       mileage,
        "price":         price,
        "vin":           vin,
        "url":           url,
        "image_url":     image_url,
        "location":      location,
        "transmission":  trans,
        "drivetrain":    drive,
        "seller_type":   seller_type,
        "source_category": "RETAIL",
    }


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_page(session, page):
    """Fetch one page of Porsche listings. Returns (items, total_count)."""
    payload = {
        "filter": {"carBrand": [_PORSCHE_BRAND_ID]},
        "pagination": {"currentPage": page, "itemsLimit": _PAGE_SIZE},
        "order": {"newest": True},
    }
    try:
        r = session.post(_LIST_URL, json=payload, headers=_HEADERS, timeout=30)
        if r.status_code != 200:
            log.warning("DuPont: HTTP %d on page %d", r.status_code, page)
            return [], 0
        data = r.json()
        if data.get("error"):
            log.warning("DuPont: API error on page %d: %s", page, data.get("error"))
            return [], 0
        pagination = data.get("pagination") or {}
        total      = pagination.get("totalItemCount") or 0
        items      = data.get("response") or []
        return items, total
    except Exception as e:
        log.warning("DuPont: fetch error page %d: %s", page, e)
        return [], 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_dupont():
    """
    Scrape DuPont Registry for active Porsche listings.
    Returns list of standard listing dicts.
    """
    try:
        import curl_cffi.requests as cffi
    except ImportError:
        log.error("DuPont: curl_cffi not installed — skipping")
        return []

    log.info("Scraping DuPont Registry...")
    session = cffi.Session()

    all_listings = []
    seen_ids     = set()
    filtered_out = 0

    page = 1
    while page <= _MAX_PAGES:
        items, total = _fetch_page(session, page)

        if not items:
            log.info("DuPont: no items on page %d — stopping", page)
            break

        if page == 1:
            # Recalculate max pages from actual total
            actual_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
            log.info("DuPont: %d total Porsche listings across %d pages", total, actual_pages)

        new_this_page = 0
        for item in items:
            car_id = item.get("id")
            seen_ids.add(car_id)  # track all fetched regardless of validity

            try:
                car = _parse_car(item)
            except Exception as e:
                log.warning("DuPont: parse error on item %s: %s", car_id, e)
                continue

            if not _is_valid(car):
                filtered_out += 1
                continue

            log.info("DuPont: %s %s %s | $%s | %s",
                     car.get("year"), car.get("model"),
                     car.get("trim") or "", car.get("price") or "?",
                     car.get("url", "")[-40:])
            all_listings.append(car)
            new_this_page += 1

        # Stop if we got a short page (last page)
        if len(items) < _PAGE_SIZE:
            break

        page += 1
        time.sleep(0.4)  # polite delay

    log.info("DuPont: %d valid Porsche listings (%d filtered out)",
             len(all_listings), filtered_out)
    return all_listings


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    results = scrape_dupont()
    print(f"\nTotal: {len(results)} listings")
    for c in results[:5]:
        print(f"  {c['year']} {c['model']} {c.get('trim','')} | ${c['price']} | {c.get('mileage','?')} mi | {c['url'][-50:]}")


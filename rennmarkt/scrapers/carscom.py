"""
rennmarkt/scrapers/carscom.py — Cars.com scraper using Scrapling.

No proxy needed — Scrapling's StealthyFetcher bypasses Cloudflare directly.
Parses data-vehicle-details JSON attribute on <fuse-card>/<[data-vehicle-details]> elements.

Strategy:
  1. Scrapling StealthyFetcher (primary — bypasses Cloudflare, no proxy)
  2. curl_cffi direct (fallback — already worked before for some pages)

Per-model slugs avoid Macan/Cayenne/Panamera/Taycan noise.
Incremental: stops paginating when all VINs on a page are already in DB.
"""
import re
import json
import logging
import time
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME = "cars.com"

_MODEL_SLUGS = [
    "porsche-911",
    "porsche-boxster",
    "porsche-cayman",
    "porsche-718_boxster",
    "porsche-718_cayman",
]
_SEARCH_TEMPLATE = (
    "https://www.cars.com/shopping/results/"
    "?makes[]=porsche&models[]={slug}"
    "&stock_type=used&maximum_distance=all"
    "&sort=listed_at_desc&page_size=20&page={page}"
)
_PAGE_SIZE = 20
_BASE_URL = "https://www.cars.com"
_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "carscom_state.json"

YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _int(s):
    if s is None:
        return None
    s = re.sub(r"[^\d]", "", str(s))
    return int(s) if s else None


def _clean(s):
    if not s:
        return None
    return re.sub(r"\s+", " ", str(s)).strip() or None


def _is_blocked(html):
    if not html or len(html) < 50_000:
        lower = (html or "").lower()
        return "just a moment" in lower or "access denied" in lower
    return False


def _looks_valid(html):
    return bool(html) and (
        "data-vehicle-details" in html
        or "data-listing-id" in html
        or "fuse-card" in html
    )


def _drivetrain_with_trim_override(api_drv, trim, year):
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
    if api_drv:
        r = api_drv.lower()
        if "rear" in r or "rwd" in r or "2wd" in r:
            return "RWD"
        if "all" in r or "awd" in r or "4wd" in r or "four" in r:
            return "AWD"
    return None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _fetch_scrapling(url):
    """Primary fetch via Scrapling StealthyFetcher — no proxy needed."""
    try:
        from scrapling import StealthyFetcher
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            timeout=60000,
            wait_selector="[data-vehicle-details]",
            wait_selector_state="attached",
            network_idle=False,
        )
        html = page.html_content
        if _looks_valid(html):
            log.info("Scrapling: success (len=%d)", len(html))
            return html
        log.debug("Scrapling: no listing data (len=%d)", len(html))
        return None
    except Exception as e:
        log.debug("Scrapling fetch error: %s", e)
        return None


def _fetch_curl_cffi(url):
    """Fallback: curl_cffi direct (no proxy)."""
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome131", timeout=25, allow_redirects=True)
        if _looks_valid(r.text) and not _is_blocked(r.text):
            log.info("curl_cffi: direct success (len=%d)", len(r.text))
            return r.text
        return None
    except Exception as e:
        log.debug("curl_cffi error: %s", e)
        return None


def _fetch_page(url):
    html = _fetch_scrapling(url)
    if html:
        return html
    log.info("Scrapling failed, trying curl_cffi direct: %s", url)
    return _fetch_curl_cffi(url)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------
def _parse_page(html):
    """Extract listings from data-vehicle-details JSON attributes."""
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
            raw_json = card.get("data-vehicle-details", "")
            try:
                data = json.loads(raw_json)
            except Exception:
                continue
            year    = _int(data.get("year"))
            make    = _clean(data.get("make")) or "Porsche"
            model   = _clean(data.get("model"))
            trim    = _clean(data.get("trim"))
            vin     = _clean(data.get("vin"))
            price   = _int(data.get("price"))
            mileage = _int(data.get("mileage"))
            thumb   = _clean(data.get("primaryThumbnail") or "")
            if thumb and "/in/v2/" in thumb and "/large/in/v2/" not in thumb:
                image_url = thumb.replace("/in/v2/", "/large/in/v2/")
            else:
                image_url = thumb or None
            lid = listing_id or data.get("listingId", "")
            url = f"{_BASE_URL}/vehicledetail/{lid}/" if lid else None
            results.append({
                "year": year, "make": make, "model": model, "trim": trim,
                "mileage": mileage, "price": price, "vin": vin,
                "url": url, "image_url": image_url, "seller_type": "dealer",
            })
    except Exception as e:
        log.warning("parse error: %s", e)
    return results


# ---------------------------------------------------------------------------
# VDP detail enrichment (transmission, color, engine, drivetrain)
# ---------------------------------------------------------------------------
_CC_SUFFIX_RULES = [
    ("transmission", " transmission"),
    ("drivetrain",   " drivetrain"),
    ("color",        " exterior color"),
    ("engine",       " engine"),
]


def _vdp_extract_specs(html):
    raw = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for li in soup.select("[data-qa=basics-entry]"):
            text = li.get_text(" ", strip=True)
            low = text.lower()
            for field, suffix in _CC_SUFFIX_RULES:
                if low.endswith(suffix):
                    raw[field] = text[:-len(suffix)].strip()
                    break
    except Exception as e:
        log.debug("VDP parse error: %s", e)
    return raw


def _enrich_with_vdp(listings):
    if not listings:
        return
    # Find DB path
    db_path = None
    p = Path(__file__).resolve().parent
    for _ in range(6):
        cand = p / "data" / "inventory.db"
        if cand.exists():
            db_path = cand
            break
        p = p.parent
    already_enriched = set()
    if db_path:
        try:
            conn = sqlite3.connect(str(db_path))
            urls = [l.get("url") for l in listings if l.get("url")]
            placeholders = ",".join(["?"] * len(urls))
            rows = conn.execute(
                f"SELECT listing_url FROM listings WHERE listing_url IN ({placeholders})"
                f" AND (transmission IS NOT NULL OR color IS NOT NULL OR engine IS NOT NULL)",
                urls
            ).fetchall()
            already_enriched = {r[0] for r in rows}
            conn.close()
        except Exception as e:
            log.debug("VDP DB check failed: %s", e)

    to_fetch = [l for l in listings if l.get("url") and l["url"] not in already_enriched]
    if not to_fetch:
        return
    log.info("cars.com VDP enrich: %d/%d need detail fetch", len(to_fetch), len(listings))
    fetched = 0
    for car in to_fetch:
        html = _fetch_curl_cffi(car["url"]) or _fetch_scrapling(car["url"])
        if not html:
            continue
        spec = _vdp_extract_specs(html)
        if spec.get("transmission"):
            car["transmission"] = spec["transmission"]
        if spec.get("color"):
            car["color"] = spec["color"]
        if spec.get("engine"):
            car["engine"] = spec["engine"]
        drv = _drivetrain_with_trim_override(spec.get("drivetrain"), car.get("trim"), car.get("year"))
        if drv:
            car["drivetrain"] = drv
        fetched += 1
        time.sleep(0.5)
    log.info("cars.com VDP enrich: %d fetched", fetched)


# ---------------------------------------------------------------------------
# State + known VINs
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


def _get_known_vins():
    p = Path(__file__).resolve().parent
    for _ in range(6):
        cand = p / "data" / "inventory.db"
        if cand.exists():
            try:
                conn = sqlite3.connect(str(cand))
                rows = conn.execute(
                    "SELECT vin FROM listings WHERE dealer='cars.com' AND vin IS NOT NULL"
                ).fetchall()
                conn.close()
                return set(r[0] for r in rows)
            except Exception:
                return set()
        p = p.parent
    return set()


# ---------------------------------------------------------------------------
# Filter (mirrors shared/scraper_utils._is_valid_listing without import)
# ---------------------------------------------------------------------------
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718", "930", "964", "993",
                              "996", "997", "991", "992", "gt3", "gt4", "turbo"})
_BLOCKED_MODELS = frozenset({"cayenne", "macan", "panamera", "taycan", "918"})


def _is_valid(car):
    year  = car.get("year")
    model = (car.get("model") or "").lower().strip()
    make  = (car.get("make") or "").lower().strip()
    if make and make != "porsche":
        return False
    if year and not (YEAR_MIN <= year <= YEAR_MAX):
        return False
    if not model:
        return False
    if any(b in model for b in _BLOCKED_MODELS):
        return False
    if not any(g in model for g in _ALLOWED_MODELS):
        return False
    mileage = car.get("mileage")
    if mileage is not None and mileage > 100_000:
        return False
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def scrape_carscom(max_pages=None):
    """
    Scrape Cars.com for used Porsche listings via Scrapling (no proxy required).

    max_pages: pages per model slug.
      None  — 15 pages on bootstrap, 3 pages thereafter.
      1     — page 1 only (fast cycle).
      3     — up to 3 pages (deep cycle).
    """
    state = _load_state()
    bootstrapped = state.get("bootstrapped", False)
    known_vins = _get_known_vins() if bootstrapped else set()

    if max_pages is not None:
        effective_max = max_pages
    elif not bootstrapped:
        effective_max = 15
    else:
        effective_max = 3

    log.info("cars.com: %s run (max_pages=%d/slug, known_vins=%d)",
             "bootstrap" if not bootstrapped else "incremental",
             effective_max, len(known_vins))

    all_listings = []
    seen_keys = set()
    filtered_out = 0

    for slug in _MODEL_SLUGS:
        model_name = slug.replace("porsche-", "").replace("_", " ")
        log.info("cars.com: slug=%s", slug)

        for page_num in range(1, effective_max + 1):
            url = _SEARCH_TEMPLATE.format(slug=slug, page=page_num)
            html = _fetch_page(url)
            if not html:
                log.info("cars.com: fetch failed %s p%d — next slug", model_name, page_num)
                break

            raw = _parse_page(html)
            if not raw:
                log.info("cars.com: empty %s p%d — done", model_name, page_num)
                break

            new_this_page = 0
            all_known = True
            for car in raw:
                vin = car.get("vin")
                key = vin or car.get("url") or f"{car.get('year')}|{car.get('model')}|{car.get('price')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                url_val = car.get("url") or ""
                if "/vehicledetail/" not in url_val:
                    continue
                if vin and vin not in known_vins:
                    all_known = False
                if not _is_valid(car):
                    filtered_out += 1
                    continue
                all_listings.append(car)
                new_this_page += 1

            log.info("cars.com %s p%d: %d new (total: %d)", model_name, page_num, new_this_page, len(all_listings))

            if bootstrapped and all_known:
                log.info("cars.com %s: frontier reached at p%d", model_name, page_num)
                break
            if new_this_page == 0:
                break
            time.sleep(1.5)

    if not bootstrapped and all_listings:
        _save_state({"bootstrapped": True})
        log.info("cars.com: bootstrap complete")

    try:
        _enrich_with_vdp(all_listings)
    except Exception as e:
        log.warning("cars.com VDP enrich error: %s", e)

    log.info("cars.com scrape complete: %d listings (%d filtered out)", len(all_listings), filtered_out)
    return all_listings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    results = scrape_carscom(max_pages=1)
    print(f"\nTotal: {len(results)}")
    for c in results[:5]:
        print(f"  {c['year']} {c['model']} {c.get('trim','')} | ${c['price']} | {c.get('mileage','?')} mi")

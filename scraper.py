"""
Porsche competitor inventory scraper — 11 dealers + BaT + PCA Mart.
Returns {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
All output is filtered: Porsche only, 1986-2027, 911/Cayman/Boxster models only.
"""
import re
import json
import time
import logging
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Listing filter — Porsche 911/Cayman/Boxster, 1984–2025 only
# (1984 is the first 930 Turbo year we track; pre-1984 are filtered at display)
# ---------------------------------------------------------------------------
YEAR_MIN = 1984
YEAR_MAX = 2024
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718"})
_BLOCKED_MODELS  = frozenset({"cayenne", "macan", "panamera", "taycan", "918"})
_JUNK_KEYWORDS   = frozenset({
    "parts", "engine", "wheels", "brochure",
    "poster", "emblem", "badge", "memorabilia",
})
# Single-word keywords that are only junk when they appear in model/trim — NOT in
# free-form auction titles where "manual" = transmission, "key" = included keys,
# "book" = service books. Applied to model+trim only (not title field).
_JUNK_KEYWORDS_STRICT = frozenset({"manual", "key", "book"})

# ASCII-but-foreign listing phrases — catches eBay Spain/Germany/Italy listings
# that slip past the non-ASCII filter (e.g. "Anuncio nuevo 2019 Porsche 911").
_FOREIGN_PHRASES = frozenset({
    # Spanish eBay UI labels and listing phrases
    "anuncio nuevo", "se vende", "en venta", "ocasion", "oportunidad",
    "nuevo anuncio", "vendido", "precio negociable", "destacado",
    # German
    "zu verkaufen", "gebraucht", "verkaufe", "neufahrzeug",
    # French
    "a vendre", "occasion", "vendu",
    # Italian
    "in vendita", "usato",
})

PRICE_MIN =    25_000   # listings below this are parts/beaters — skip
PRICE_MAX = 1_000_000   # sanity cap
MILEAGE_MAX = 100_000   # over 100k miles filtered out


def _is_valid_listing(car: dict) -> bool:
    # Non-ASCII in title, make, or model means a foreign-language listing — skip.
    # Trim is excluded: legitimate descriptions can contain en-dashes, accented names, etc.
    for field in ("title", "make", "model"):
        val = car.get(field) or ""
        if any(ord(c) > 127 for c in val):
            return False

    # ASCII-but-foreign phrase filter — checked across title AND model because
    # the HTML scraper may store the full title string in the model field when
    # _parse_ymmt fails to find a leading year.
    check_text = " ".join(filter(None, [
        (car.get("title") or "").lower(),
        (car.get("model") or "").lower(),
    ]))
    if any(phrase in check_text for phrase in _FOREIGN_PHRASES):
        return False

    make  = (car.get("make") or "").lower().strip()
    model = (car.get("model") or "").lower().strip()
    year  = car.get("year")

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

    # Junk keyword filter:
    # - broad keywords applied to model+trim+title (unambiguously non-car items)
    # - strict keywords (manual/key/book) applied only to model+trim to avoid
    #   false-positives on BaT/auction titles ("Manual Transmission", "1-Key", "Service Books")
    combined_text = " ".join(filter(None, [
        model,
        (car.get("trim") or "").lower(),
        (car.get("title") or "").lower(),
    ]))
    if any(kw in combined_text for kw in _JUNK_KEYWORDS):
        return False
    model_trim = " ".join(filter(None, [model, (car.get("trim") or "").lower()]))
    if any(kw in model_trim for kw in _JUNK_KEYWORDS_STRICT):
        return False

    # Mileage cap (only when present)
    mileage = car.get("mileage")
    if mileage is not None and mileage > MILEAGE_MAX:
        return False

    return True


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
})


# ---------------------------------------------------------------------------
# Proxy configuration (Webshare rotating residential)
# ---------------------------------------------------------------------------
_PROXY_CFG: dict = {}
_PROXY_URL: str = ""
_PROXY_DEAD: bool = False


def _disable_proxy_session():
    """Called on first ProxyError — clears proxy from SESSION for the rest of this run."""
    global _PROXY_DEAD
    if not _PROXY_DEAD:
        _PROXY_DEAD = True
        SESSION.proxies.clear()
        log.warning("Proxy unavailable (402/connection error) — falling back to direct for this run")


def _load_proxy():
    global _PROXY_CFG, _PROXY_URL
    try:
        cfg_path = Path(__file__).parent / "data" / "proxy_config.json"
        with open(cfg_path) as f:
            _PROXY_CFG = json.load(f)
        if _PROXY_CFG.get("enabled") and _PROXY_CFG.get("proxy_url"):
            _PROXY_URL = _PROXY_CFG["proxy_url"]
            SESSION.proxies.update({"http": _PROXY_URL, "https": _PROXY_URL})
            log.info("Proxy enabled: %s:%s", _PROXY_CFG.get("host"), _PROXY_CFG.get("port"))
            try:
                ip_resp = SESSION.get("https://api.ipify.org?format=json", timeout=8)
                exit_ip = ip_resp.json().get("ip", "?")
                log.info("Proxy exit IP: %s", exit_ip)
            except requests.exceptions.ProxyError:
                _disable_proxy_session()
            except Exception:
                pass
    except Exception as e:
        log.debug("No proxy config loaded: %s", e)


_load_proxy()


def _pw_proxy():
    """Return Playwright proxy dict if proxy is configured and alive, else None."""
    if not _PROXY_URL or not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        return None
    return {
        "server": f"{_PROXY_CFG['protocol']}://{_PROXY_CFG['host']}:{_PROXY_CFG['port']}",
        "username": _PROXY_CFG["username"],
        "password": _PROXY_CFG["password"],
    }


def _pw_launch(p):
    """Launch a Playwright Chromium browser with proxy if configured."""
    kwargs = {"headless": True}
    proxy = _pw_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    return p.chromium.launch(**kwargs)


def _stealth_page(parent):
    """Create a new page from a browser or context and apply playwright-stealth."""
    pg = parent.new_page()
    try:
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(pg)
    except ImportError:
        pass
    return pg


def get(url, referer=None, timeout=30, **kw) -> Optional[BeautifulSoup]:
    headers = {}
    if referer:
        headers["Referer"] = referer
    try:
        r = SESSION.get(url, headers=headers, timeout=timeout,
                        allow_redirects=True, **kw)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except requests.exceptions.ProxyError:
        _disable_proxy_session()
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True, **kw)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning("GET %s → %s", url, e)
            return None
    except Exception as e:
        log.warning("GET %s → %s", url, e)
        return None


def get_json(url, **kw):
    kw.setdefault("timeout", 25)
    try:
        r = SESSION.get(url, **kw)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ProxyError:
        _disable_proxy_session()
        try:
            r = SESSION.get(url, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("JSON GET %s → %s", url, e)
            return None
    except Exception as e:
        log.warning("JSON GET %s → %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Playwright helper (optional)
# ---------------------------------------------------------------------------
_PLAYWRIGHT_AVAILABLE = None


def _playwright_available():
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


def _get_rendered(url, wait_selector=None, timeout=20000,
                  wait_until="networkidle") -> Optional[BeautifulSoup]:
    if not _playwright_available():
        log.debug("Playwright not installed; skipping JS page: %s", url)
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _pw_launch(p)
            page = _stealth_page(browser)
            page.goto(url, wait_until=wait_until, timeout=timeout)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:
                    pass
            html = page.content()
            browser.close()
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        log.warning("Playwright error %s: %s", url, e)
        return None


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


_PORSCHE_MODEL_PREFIXES = {
    "911", "912", "914", "914-6", "916", "917", "918", "919",
    "924", "928", "930", "944", "959", "962", "968",
    "boxster", "cayman", "cayenne", "panamera", "macan", "taycan",
    "carrera", "targa", "turbo", "spyder", "speedster",
}


def _parse_ymmt(title: str):
    if not title:
        return None, None, title, None
    title = re.sub(r"\s*[-–—]\s*SOLD\s*$", "", title.strip(), flags=re.I).strip()
    title = re.sub(r"\s*\(#[^)]+\)", "", title).strip()

    m = re.match(r"^(\d{4})\s+(.+)$", title)
    if not m:
        return None, None, title, None

    year = int(m.group(1))
    if year < 1900 or year > 2030:
        return None, None, title, None

    rest = m.group(2).strip()
    parts = rest.split()
    if not parts:
        return year, None, rest, None

    if parts[0].lower() in _PORSCHE_MODEL_PREFIXES:
        return year, "Porsche", parts[0], " ".join(parts[1:]) or None

    make = parts[0]
    if len(parts) == 1:
        return year, make, None, None
    model = parts[1]
    trim = " ".join(parts[2:]) or None
    return year, make, model, trim


def _extract_jsonld(soup) -> list:
    cars = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if isinstance(t, list):
                    t = " ".join(t)
                if any(x in t for x in ("Car", "Vehicle", "Product")):
                    cars.append(item)
        except Exception:
            pass
    return cars


def _parse_jsonld_car(item, base_url="") -> dict:
    name = _clean(item.get("name", ""))
    year, make, model = None, None, name

    if "vehicleModelDate" in item:
        year = _int(item["vehicleModelDate"])

    brand = item.get("brand", {})
    if isinstance(brand, dict):
        make = _clean(brand.get("name"))
    elif isinstance(brand, str):
        make = brand

    if name:
        m = re.match(r"^(\d{4})\s+(\S+)\s+(.+)$", name)
        if m:
            year = year or int(m.group(1))
            make = make or m.group(2)
            model = m.group(3)

    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _int(offers.get("price"))

    url = _clean(offers.get("url") or item.get("url") or "")
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)

    mileage_obj = item.get("mileageFromOdometer", {})
    mileage = _int(mileage_obj.get("value") if isinstance(mileage_obj, dict) else mileage_obj)
    vin = _clean(item.get("vehicleIdentificationNumber"))

    return dict(year=year, make=make, model=model,
                trim=_clean(item.get("vehicleConfiguration") or item.get("trim")),
                mileage=mileage, price=price, vin=vin, url=url)


def _parse_card_generic(card, base_url: str) -> Optional[dict]:
    text = card.get_text(" ", strip=True)
    if not re.search(r"\b(19|20)\d{2}\b", text):
        return None

    title_el = card.select_one(
        "h1, h2, h3, h4, .title, .name, .vehicle-title, "
        "[class*='title'], [class*='name'], [class*='heading']"
    )
    title = _clean(title_el.get_text()) if title_el else ""
    year, make, model, trim = _parse_ymmt(title or text[:80])

    price_el = card.select_one(
        ".price, [class*='price'], [data-price], [class*='amount'], "
        ".woocommerce-Price-amount, .sherman_price"
    )
    price = None
    if price_el:
        price = _int(price_el.get("data-price") or price_el.get_text())
    if not price:
        pm = re.search(r"\$\s*([\d,]+)", text)
        if pm:
            price = _int(pm.group(1))

    miles_el = card.select_one("[class*='mile'], [class*='odometer'], [data-miles]")
    mileage = None
    if miles_el:
        mileage = _int(miles_el.get("data-miles") or miles_el.get_text())
    if not mileage:
        mm = re.search(r"([\d,]+)\s*(?:mi|miles|mile)\b", text, re.I)
        if mm:
            mileage = _int(mm.group(1))

    vin_el = card.select_one("[data-vin], [class*='vin']")
    vin = None
    if vin_el:
        vin = _clean(vin_el.get("data-vin") or vin_el.get_text())
    if not vin:
        vm = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", text)
        if vm:
            vin = vm.group(1)

    link = card.select_one("a[href]")
    url = urljoin(base_url, link.get("href", "")) if link else ""

    img = card.select_one("img[src]")
    image_url = img.get("src", "").split("?")[0] if img else None

    if not year:
        return None

    return dict(year=year, make=make, model=model, trim=trim,
                mileage=mileage, price=price, vin=vin, url=url, image_url=image_url)


def _extract_year_links(soup, base_url: str) -> list:
    """Extract all <a> tags whose text begins with a 4-digit year.
    Truncates title to first line/sentence to avoid pulling full descriptions."""
    cars = []
    seen = set()
    for a in soup.select("a[href]"):
        raw = a.get_text(" ", strip=True)
        if not raw or not re.match(r"^\d{4}\s", raw):
            continue
        # Take only first line and cap at 120 chars to avoid description blobs
        title = raw.split("\n")[0].strip()
        # Also cut at sentence boundaries that appear after year+make+model
        title = re.split(r"(?<=\w)\.\s+[A-Z]", title)[0].strip()
        title = title[:120].strip()
        title = _clean(title)
        if not title:
            continue
        year, make, model, trim = _parse_ymmt(title)
        if not year:
            continue
        # Extract mileage from raw text if present
        mm = re.search(r"([\d,]+)\s*(?:mi|miles|mile)\b", raw, re.I)
        mileage = _int(mm.group(1)) if mm else None
        href = urljoin(base_url, a.get("href", ""))
        key = f"{year}{make}{model}{href}"
        if key not in seen:
            seen.add(key)
            cars.append(dict(year=year, make=make, model=model, trim=trim,
                             mileage=mileage, price=None, vin=None, url=href))
    return cars


def _dedupe(cars: list) -> list:
    seen = set()
    out = []
    for c in cars:
        key = (c.get("vin") or
               f"{c.get('year')}|{c.get('make')}|{c.get('model')}|{c.get('url','')}")
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def _scrape_generic(base: str, paths: list, paginate=True, timeout=30) -> list:
    """Try multiple URL paths; JSON-LD → cards → year-links; optionally paginate."""
    cars = []
    seen = set()
    working_path = None

    for path in paths:
        url = base.rstrip("/") + ("" if not path else "/" + path.lstrip("/"))
        soup = get(url, timeout=timeout)
        if not soup:
            continue

        for ld in _extract_jsonld(soup):
            c = _parse_jsonld_car(ld, base)
            if c.get("year"):
                key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('vin','')}"
                if key not in seen:
                    seen.add(key)
                    cars.append(c)

        if not cars:
            for card in soup.select(
                ".vehicle-card, .vehicle-item, .inventory-item, .car-item, "
                "[class*='vehicle-listing'], [class*='inventory-listing'], "
                "article.listing, .car-block, [class*='car-block'], "
                ".inventory-card, [class*='inv-card']"
            ):
                c = _parse_card_generic(card, base)
                if c:
                    key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                    if key not in seen:
                        seen.add(key)
                        cars.append(c)

        if not cars:
            for c in _extract_year_links(soup, base):
                key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                if key not in seen:
                    seen.add(key)
                    cars.append(c)

        if cars:
            working_path = path
            break
        time.sleep(0.4)

    if cars and working_path and paginate:
        pbase = base.rstrip("/") + ("" if not working_path else "/" + working_path.lstrip("/"))
        for page in range(2, 25):
            sep = "&" if "?" in pbase else "?"
            soup = get(f"{pbase}{sep}page={page}", timeout=timeout)
            if not soup:
                break
            found = False
            for ld in _extract_jsonld(soup):
                c = _parse_jsonld_car(ld, base)
                if c.get("year"):
                    key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('vin','')}"
                    if key not in seen:
                        seen.add(key)
                        cars.append(c)
                        found = True
            if not found:
                for card in soup.select(
                    ".vehicle-card,.vehicle-item,.inventory-item,.car-item,"
                    ".car-block,[class*='car-block'],.inventory-card"
                ):
                    c = _parse_card_generic(card, base)
                    if c:
                        key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                        if key not in seen:
                            seen.add(key)
                            cars.append(c)
                            found = True
            if not found:
                for c in _extract_year_links(soup, base):
                    key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                    if key not in seen:
                        seen.add(key)
                        cars.append(c)
                        found = True
            if not found:
                break
            time.sleep(0.4)

    return cars


def _scrape_woocommerce(base: str, path="/inventory/") -> list:
    """WordPress + WooCommerce inventory pages."""
    cars = []
    seen = set()
    for page in range(1, 25):
        url = (f"{base.rstrip('/')}{path}" if page == 1
               else f"{base.rstrip('/')}{path}page/{page}/")
        soup = get(url)
        if not soup:
            break

        products = soup.select(".product, .type-product, li.product")
        if not products:
            if page == 1:
                # Try without trailing slash pagination
                products = soup.select("[class*='product']")
            if not products:
                break

        found = False
        for prod in products:
            title_el = prod.select_one(
                ".woocommerce-loop-product__title, h2, h3, .product-title"
            )
            title = _clean(title_el.get_text()) if title_el else ""
            year, make, model, trim = _parse_ymmt(title)
            if not year:
                continue

            price_el = prod.select_one(".woocommerce-Price-amount, .price, bdi")
            price = _int(price_el.get_text()) if price_el else None

            miles_el = prod.select_one("[class*='mile'], [class*='odometer']")
            mileage = _int(miles_el.get_text()) if miles_el else None

            link = prod.select_one("a.woocommerce-LoopProduct-link, a[href]")
            href = urljoin(base, link.get("href", "")) if link else ""

            vin_el = prod.select_one("[data-vin]")
            vin = _clean(vin_el.get("data-vin")) if vin_el else None

            key = f"{year}{make}{model}{href}"
            if key not in seen:
                seen.add(key)
                cars.append(dict(year=year, make=make, model=model, trim=trim,
                                 mileage=mileage, price=price, vin=vin, url=href))
                found = True

        if not found:
            break

        if not soup.find("a", class_=re.compile(r"next")):
            break
        time.sleep(0.5)

    return cars


def _scrape_webflow(base: str, path="/inventory") -> list:
    """Webflow server-rendered inventory pages."""
    cars = []
    seen = set()
    for page in range(1, 15):
        url = (f"{base.rstrip('/')}{path}" if page == 1
               else f"{base.rstrip('/')}{path}?page={page}")
        soup = get(url)
        if not soup:
            break

        found = False
        for card in soup.select(
            ".w-dyn-item, article, "
            "[class*='inventory-item'], [class*='car-item'], [class*='vehicle-item']"
        ):
            c = _parse_card_generic(card, base)
            if c:
                key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                if key not in seen:
                    seen.add(key)
                    cars.append(c)
                    found = True

        if not found:
            for c in _extract_year_links(soup, base):
                key = f"{c['year']}{c.get('make')}{c.get('model')}{c.get('url','')}"
                if key not in seen:
                    seen.add(key)
                    cars.append(c)
                    found = True

        if not found and page > 1:
            break

        has_next = bool(
            soup.find("a", string=re.compile(r"next|›|»", re.I)) or
            soup.find("a", class_=re.compile(r"next", re.I))
        )
        if not has_next and page > 1:
            break
        time.sleep(0.5)

    return cars


def _extract_aanwordpress_js(soup, base_url: str) -> list:
    """Extract vehicle data from All Auto Network / aanWordpress JS-rendered pages.
    Vehicles are stored in a JS array and rendered client-side via printVehicle().
    Look for patterns like: var vehicles=[{...}] or window.vehicles=[{...}]
    """
    cars = []
    for tag in soup.find_all("script"):
        txt = tag.string or ""
        for pattern in [
            r'var\s+vehicles\s*=\s*(\[.+?\])\s*;',
            r'var\s+inventory\s*=\s*(\[.+?\])\s*;',
            r'window\.vehicles\s*=\s*(\[.+?\])',
            r'"vehicles"\s*:\s*(\[.+?\])',
            r'vehicles\s*=\s*(\[.+?\])\s*[,;]',
        ]:
            m = re.search(pattern, txt, re.S)
            if m:
                try:
                    items = json.loads(m.group(1))
                    for item in items:
                        if item.get("sold") or item.get("pending_sale"):
                            continue
                        year = _int(item.get("year"))
                        make = _clean(item.get("make"))
                        model = _clean(item.get("model"))
                        trim = _clean(item.get("trim") or item.get("sub_model"))
                        mileage = _int(item.get("mileage"))
                        price = _int(item.get("lower_price") or item.get("price"))
                        vin = _clean(item.get("vin") or item.get("stockno"))
                        href = item.get("url_link") or item.get("url") or ""
                        full_url = urljoin(base_url, href) if href else ""
                        if year:
                            cars.append(dict(year=year, make=make, model=model, trim=trim,
                                             mileage=mileage, price=price, vin=vin, url=full_url))
                    if cars:
                        return cars
                except Exception:
                    pass
    return cars


def _scrape_aanwordpress(base: str, path: str) -> list:
    """All Auto Network / aanWordpress platform (Ryan Friedman, Motorcars of the Main Line)."""
    url = base.rstrip("/") + "/" + path.lstrip("/")
    soup = get(url)
    if not soup:
        return []

    # Try JS vehicle array extraction
    cars = _extract_aanwordpress_js(soup, base)
    if cars:
        return _dedupe(cars)

    # Fallback: Playwright for full JS render
    if _playwright_available():
        soup = _get_rendered(url, wait_selector=".inventory-card, .car-block")
        if soup:
            cars = _extract_aanwordpress_js(soup, base)
            if not cars:
                for card in soup.select(".inventory-card, .car-block, [class*='vehicle']"):
                    c = _parse_card_generic(card, base)
                    if c:
                        cars.append(c)
            if cars:
                return _dedupe(cars)

    # Generic fallback
    return _scrape_generic(base, [path, "/inventory", "/vehicles", ""])


# ---------------------------------------------------------------------------
# Site-specific scrapers — 11 dealers
# ---------------------------------------------------------------------------

def scrape_holtmotorsports():
    """holtmotorsports.com — structure unknown (timed out on initial probe)."""
    base = "https://www.holtmotorsports.com"
    # Use longer timeout since site was slow
    cars = _scrape_generic(base, ["/inventory", "/vehicles", "/used", "/cars", ""], timeout=45)
    if not cars and _playwright_available():
        soup = _get_rendered(f"{base}/inventory")
        if soup:
            for card in soup.select("[class*='vehicle'], [class*='listing'], article"):
                c = _parse_card_generic(card, base)
                if c:
                    cars.append(c)
            if not cars:
                cars = _extract_year_links(soup, base)
    return _dedupe(cars)


def scrape_ryanfriedmanmotorcars():
    """ryanfriedmanmotorcars.com — aanWordpress: inv_ids=[] in static HTML (AJAX-loaded).
    Must use Playwright to get JS-rendered vehicle cards."""
    base = "https://www.ryanfriedmanmotorcars.com"
    url = f"{base}/inventory/"

    if _playwright_available():
        soup = _get_rendered(url, wait_selector="h2, .inventory-card, .car-block",
                             timeout=60000, wait_until="domcontentloaded")
        if soup:
            cars = _extract_aanwordpress_js(soup, base)
            if not cars:
                for card in soup.select(
                    ".inventory-card, .car-block, [class*='vehicle'], "
                    "[class*='inventory'], article, .item"
                ):
                    c = _parse_card_generic(card, base)
                    if c:
                        cars.append(c)
            if not cars:
                cars = _extract_year_links(soup, base)
            if cars:
                return _dedupe(cars)

    # Static fallback (inv_ids will be empty but try anyway)
    return _dedupe(_scrape_generic(base, ["/inventory/", "/inventory", "/vehicles", ""]))


def scrape_velocitypcars():
    """velocitypcars.com — WordPress + WooCommerce at /inventory/."""
    base = "https://velocitypcars.com"
    cars = _scrape_woocommerce(base, "/inventory/")
    if not cars:
        cars = _scrape_generic(base, ["/inventory/", "/inventory", "/vehicles", ""])
    return _dedupe(cars)


def scrape_roadscholars():
    """roadscholars.com — Webflow at /inventory (server-rendered).
    Listings: <a href="/car-inventory/..."> with year/make/model in headings."""
    base = "https://www.roadscholars.com"
    cars = _scrape_webflow(base, "/inventory")
    if not cars:
        cars = _scrape_generic(base, ["/inventory", "/vehicles", "/cars", ""])
    return _dedupe(cars)


def scrape_gaudinclassic():
    """gaudinclassic.com — likely 403; attempt with Referer, fall back gracefully."""
    base = "https://www.gaudinclassic.com"
    cars = []
    for path in ["/inventory", "/collection", "/vehicles", "/cars", ""]:
        url = base + path
        soup = get(url, referer="https://www.google.com/")
        if not soup:
            continue
        for ld in _extract_jsonld(soup):
            c = _parse_jsonld_car(ld, base)
            if c.get("year"):
                cars.append(c)
        if not cars:
            for card in soup.select("[class*='vehicle'], [class*='listing'], article"):
                c = _parse_card_generic(card, base)
                if c:
                    cars.append(c)
        if not cars:
            cars = _extract_year_links(soup, base)
        if cars:
            break
        time.sleep(0.4)
    return _dedupe(cars)


def scrape_udriveautomobiles():
    """udriveautomobiles.co — AutoManager-hosted inventory at /custom-12?make=Porsche.

    Page is server-rendered; no JS required. Each car is a div with data-* attributes:
      data-displayyear / data-displaymake / data-displaymodel / data-displaytrim
      data-displaymileage  — numeric string (no commas)
      data-displayprice    — always "$" (call for price); price stored as None
      data-displayphoto    — 120 px thumbnail; full-size img[src] used instead
      data-displaytitle    — full title string "YYYY Make Model Trim description..."
    data-displaymodel is "Other" for 718 Boxster / 718 Cayman — model extracted
    from title in that case.  VIN extracted via 17-char regex from card text.
    URL: absolute href from the detail-page link inside each card.
    Pagination: ?page=N&make=Porsche (stop when a page returns 0 cards).
    """
    BASE = "https://www.udriveautomobiles.co"

    # Known model keywords to detect when data-displaymodel == "Other"
    _MODEL_KW = [
        ("718 Boxster", "boxster"),
        ("718 Cayman", "cayman"),
        ("Boxster",     "boxster"),
        ("Cayman",      "cayman"),
        ("911",         "911"),
    ]

    def _resolve_model(raw, title):
        """Return a model string even when the platform returns "Other"."""
        if raw and raw.lower() not in ("other", ""):
            return raw
        t = title.lower()
        for display, kw in _MODEL_KW:
            if kw in t:
                return display
        return raw  # give up — filter will drop non-matching models

    def _parse_page(soup):
        cars = []
        for card in soup.select("[data-id]"):
            title      = _clean(card.get("data-displaytitle") or "")
            raw_year   = card.get("data-displayyear", "")
            raw_make   = _clean(card.get("data-displaymake") or "Porsche")
            raw_model  = _clean(card.get("data-displaymodel") or "")
            raw_trim   = _clean(card.get("data-displaytrim") or "")
            raw_miles  = card.get("data-displaymileage", "")

            year = _int(raw_year)
            if not year:
                continue

            make  = raw_make or "Porsche"
            model = _resolve_model(raw_model, title)
            trim  = raw_trim or None

            mileage = _int(raw_miles)
            # Price is always "Call for Price" — no numeric value available
            price = None

            # VIN — 17-char alphanumeric in card text
            text = card.get_text(" ", strip=True)
            vm = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", text)
            vin = vm.group(1) if vm else None

            # Detail page URL
            link = card.select_one("a[href*='vehicle-details']")
            url  = link.get("href", "") if link else ""
            if url and not url.startswith("http"):
                url = BASE + url

            # Image — prefer large img[src] over the thumbnail in data-displayphoto
            img = card.select_one("img[src]")
            image_url = img.get("src", "").split("?")[0] if img else None
            if not image_url:
                image_url = card.get("data-displayphoto") or None

            cars.append(dict(
                year=year, make=make, model=model, trim=trim,
                mileage=mileage, price=price, vin=vin,
                url=url, image_url=image_url,
            ))
        return cars

    cars = []
    seen = set()

    for page in range(1, 20):
        url = (f"{BASE}/custom-12?make=Porsche" if page == 1
               else f"{BASE}/custom-12?page={page}&make=Porsche")
        soup = get(url)
        if not soup:
            break
        page_cars = _parse_page(soup)
        if not page_cars:
            break
        for c in page_cars:
            key = c.get("vin") or f"{c['year']}|{c.get('model')}|{c.get('url','')}"
            if key not in seen:
                seen.add(key)
                cars.append(c)
        time.sleep(0.4)

    if not cars:
        log.warning("udriveautomobiles.co: no listings found")

    return _dedupe(cars)


def scrape_motorcarsofthemainline():
    """motorcarsofthemainline.com — custom PHP CMS at /all-inventory/?make=Porsche.

    Page structure (server-rendered, no heavy JS required):
      Cards:   div.vehicle
      Title:   .name (may contain <br>; text = "YYYY Porsche Model Trim")
      Mileage: second div.mileage child span pair ("Miles: 44,832")
      Price:   .price > a text ("Price: $299,950")
      VIN:     .shortDescp table row where first td == "Vin:"
      Stock:   same table, "Stock:" row — used as fallback unique key
      URL:     onclick="getDetailed('/slug/')" on any <a> in the card
      Image:   .frame .inner img[src]

    Links use javascript:; href with onclick handlers — URL is constructed
    from the getDetailed() argument.
    """
    BASE = "https://www.motorcarsofthemainline.com"
    URL  = f"{BASE}/all-inventory/?make=Porsche"

    def _parse_cards(soup):
        cars = []
        seen = set()
        for card in soup.select("div.vehicle"):
            # --- URL from onclick ---
            onclick_url = None
            for a in card.select("a[onclick]"):
                m = re.search(r"getDetailed\(['\"]([^'\"]+)['\"]", a.get("onclick", ""))
                if m:
                    onclick_url = BASE + m.group(1)
                    break
            if not onclick_url:
                # No getDetailed link on this card — skip
                continue

            # --- Title / YMMT ---
            name_el = card.select_one(".name")
            if name_el:
                # Join text nodes separated by <br> into a single line
                title = " ".join(name_el.get_text(" ", strip=True).split())
            else:
                title = ""
            year, make, model, trim = _parse_ymmt(title)
            if not year:
                continue
            if not make:
                make = "Porsche"

            # --- Mileage ---
            # Two .mileage divs: first = Trans:, second = Miles:
            mileage = None
            for md in card.select(".mileage"):
                txt = md.get_text(" ", strip=True)
                if re.search(r"\bMiles?\b", txt, re.I):
                    mileage = _int(re.sub(r"[^\d,]", "", txt))
                    break

            # --- Price ---
            price = None
            price_el = card.select_one(".price a, .price")
            if price_el:
                price = _int(price_el.get_text())

            # --- VIN and Stock from shortDescp table ---
            vin = None
            stock = None
            for tr in card.select(".shortDescp table tr"):
                tds = tr.select("td")
                if len(tds) >= 2:
                    label = tds[0].get_text(strip=True).lower().rstrip(":")
                    value = _clean(tds[1].get_text(strip=True))
                    if label == "vin":
                        vin = value
                    elif label == "stock":
                        stock = value

            # Validate VIN format (17-char alphanumeric, no I/O/Q)
            if vin and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin, re.I):
                vin = None

            # --- Image ---
            image_url = None
            img = card.select_one(".frame img[src], .frame .inner img[src]")
            if img:
                src = img.get("src") or img.get("data-src") or ""
                image_url = src.split("?")[0] or None

            key = vin or stock or f"{year}|{model}|{onclick_url}"
            if key in seen:
                continue
            seen.add(key)

            cars.append(dict(
                year=year, make=make, model=model, trim=trim,
                mileage=mileage, price=price, vin=vin,
                url=onclick_url, image_url=image_url,
            ))
        return cars

    # 1. Try static GET (page is server-rendered)
    soup = get(URL, referer="https://www.google.com/")
    if soup:
        cars = _parse_cards(soup)
        if cars:
            log.info("motorcarsofthemainline: %d cards via static GET", len(cars))
            return _dedupe(cars)

    # 2. Playwright fallback
    if _playwright_available():
        soup = _get_rendered(URL, wait_selector="div.vehicle",
                             timeout=45000, wait_until="domcontentloaded")
        if soup:
            cars = _parse_cards(soup)
            if cars:
                log.info("motorcarsofthemainline: %d cards via Playwright", len(cars))
                return _dedupe(cars)

    log.warning("motorcarsofthemainline: no listings found")
    return []


def scrape_grandprimotors():
    """grandprimotors.com — Dealer.com/CDK platform, DOM scraping.

    Load the used-inventory page with Playwright, wait for vehicle cards to
    render, then extract listings from the DOM. The API endpoint previously
    used now returns HTML (not JSON), so DOM parsing is the primary strategy.

    Primary: vehicle cards with data-vin attribute (Dealer.com standard markup).
    Fallback: all <a> links pointing to /VehicleDetails/ pages.
    """
    if not _playwright_available():
        log.warning("Grand Prix Motors scraper requires Playwright")
        return []

    BASE = "https://www.grandprimotors.com"
    INVENTORY_URL = f"{BASE}/used-inventory/index.htm"

    def _parse_cards(soup):
        """Extract vehicles from Dealer.com card markup."""
        cars = []
        # Primary: cards carry data-vin and structured data attributes
        cards = soup.select("[data-vin]")
        for card in cards:
            vin  = _clean(card.get("data-vin"))
            year = _int(card.get("data-year"))
            make = _clean(card.get("data-make")) or "Porsche"
            model = _clean(card.get("data-model"))

            # Title link → URL and trim
            a = card.select_one("h2 a, h3 a, .title a, [class*='title'] a, a[href*='VehicleDetails']")
            url = (BASE + a["href"]) if a and a.get("href", "").startswith("/") else (
                a["href"] if a else INVENTORY_URL)
            title_text = _clean(a.get_text()) if a else ""

            # Derive trim by stripping "YEAR MAKE MODEL" prefix from title
            trim = None
            if title_text and year and model:
                prefix = f"{year} {make} {model}".strip()
                if title_text.lower().startswith(prefix.lower()):
                    trim = title_text[len(prefix):].strip() or None
                else:
                    trim = title_text

            # Price: data-price attribute, or first price-like element
            price = _int(card.get("data-price"))
            if not price:
                p_el = card.select_one(
                    "[class*='price']:not([class*='msrp']):not([class*='strike'])"
                )
                if p_el:
                    price = _int(p_el.get_text())

            # Mileage
            mileage = None
            for sel in ("[class*='miles']", "[class*='mileage']", "[class*='odometer']"):
                el = card.select_one(sel)
                if el:
                    mileage = _int(el.get_text())
                    if mileage:
                        break

            if not year:
                continue
            cars.append(dict(year=year, make=make, model=model, trim=trim,
                             mileage=mileage, price=price, vin=vin, url=url))
        return cars

    def _parse_links(soup):
        """Fallback: extract year/make/model/trim from VehicleDetails links."""
        cars = []
        seen_urls = set()
        for a in soup.select("a[href*='VehicleDetails'], a[href*='/used/']"):
            href = a.get("href", "")
            url = (BASE + href) if href.startswith("/") else href
            if url in seen_urls:
                continue
            seen_urls.add(url)
            text = _clean(a.get_text()) or ""
            year, make, model, trim = _parse_ymmt(text)
            if not year:
                # Try extracting year from URL pattern like /2019-Porsche-911-...
                m = re.search(r"/(\d{4})-(\w+)-(\w+)", href)
                if m:
                    year = _int(m.group(1))
                    make = _clean(m.group(2)) or "Porsche"
                    model = _clean(m.group(3))
            if year:
                cars.append(dict(year=year, make=make or "Porsche", model=model,
                                 trim=trim, mileage=None, price=None,
                                 vin=None, url=url))
        return cars

    cars = []
    try:
        from playwright.sync_api import sync_playwright

        def _fetch_page_html(idle_timeout=12000):
            with sync_playwright() as p:
                browser = _pw_launch(p)
                pg = _stealth_page(browser)
                # domcontentloaded fires before images/fonts; networkidle then
                # catches the secondary XHRs that populate real card data.
                pg.goto(INVENTORY_URL, wait_until="domcontentloaded", timeout=45000)
                found = False
                for selector in ("[data-vin]", ".vehicle-card", "[class*='vehicle-item']",
                                 "a[href*='VehicleDetails']"):
                    try:
                        pg.wait_for_selector(selector, timeout=8000)
                        log.debug("Grand Prix: found cards via selector '%s'", selector)
                        found = True
                        break
                    except Exception:
                        continue
                # Dealer.com CDK renders skeleton [data-vin] shells immediately
                # and fills real inventory via a second XHR. Without waiting for
                # networkidle we capture empty skeletons and parse 0 results.
                if found:
                    try:
                        pg.wait_for_load_state("networkidle", timeout=idle_timeout)
                    except Exception:
                        pass  # proceed with whatever loaded
                html = pg.content()
                browser.close()
            return html

        html = _fetch_page_html()
        soup = BeautifulSoup(html, "lxml")
        cars = _parse_cards(soup)
        if cars:
            log.debug("Grand Prix Motors: %d cards via [data-vin] DOM parse", len(cars))
        else:
            log.debug("Grand Prix Motors: no data-vin cards, trying link fallback")
            cars = _parse_links(soup)

        # Retry once with a longer idle window — slow inventory XHRs can exceed
        # the default 12 s on the first attempt.
        if not cars:
            log.debug("Grand Prix Motors: 0 results on first attempt, retrying with longer wait...")
            html = _fetch_page_html(idle_timeout=20000)
            soup = BeautifulSoup(html, "lxml")
            cars = _parse_cards(soup) or _parse_links(soup)

    except Exception as e:
        log.warning("Grand Prix Motors scraper error: %s", e)

    if not cars:
        log.warning("Grand Prix Motors: 0 results after retry — possible load failure or site change")
    log.info("Grand Prix Motors: %d raw vehicles fetched", len(cars))
    return _dedupe(cars)


# ---------------------------------------------------------------------------
# External marketplace scrapers
# ---------------------------------------------------------------------------

def scrape_bat():
    """bringatrailer.com/porsche/ — active auctions, Playwright required.
    Cards: div.listing-card; title in h3>a; bid in span.bid-formatted."""
    if not _playwright_available():
        log.warning("BaT scraper requires Playwright")
        return []
    cars = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # BaT doesn't block headless — try without proxy first (faster, avoids
            # proxy latency/timeouts); fall back to proxy only if first attempt fails.
            proxy = _pw_proxy()
            browser = p.chromium.launch(headless=True)
            pg = _stealth_page(browser)
            try:
                pg.goto("https://bringatrailer.com/porsche/",
                        wait_until="domcontentloaded", timeout=35000)
                pg.wait_for_selector("div.listing-card", timeout=10000)
            except Exception:
                browser.close()
                if proxy:
                    log.debug("BaT: no-proxy attempt failed, retrying via proxy")
                    browser = p.chromium.launch(headless=True, proxy=proxy)
                    pg = _stealth_page(browser)
                    pg.goto("https://bringatrailer.com/porsche/",
                            wait_until="domcontentloaded", timeout=45000)
                    try:
                        pg.wait_for_selector("div.listing-card", timeout=10000)
                    except Exception:
                        pass
                else:
                    raise
            html = pg.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.listing-card"):
            a = card.select_one("h3 > a") or card.select_one("h2 > a") or card.select_one("a[href]")
            if not a:
                continue
            title = _clean(a.get_text()) or ""
            url = a.get("href", "")
            if url and not url.startswith("http"):
                url = "https://bringatrailer.com" + url

            # Parse mileage from "34k-Mile" or "34,123-Mile" prefix
            mileage = None
            mm = re.search(r"([\d,]+)(k)?-Mile", title, re.I)
            if mm:
                val = int(mm.group(1).replace(",", ""))
                mileage = val * 1000 if mm.group(2) else val

            # Strip mileage prefix before parsing YMMT
            clean_title = re.sub(r"[\d,]+k?-Mile\s+", "", title, flags=re.I).strip()
            year, make, model, trim = _parse_ymmt(clean_title)

            bid_el = card.select_one("span.bid-formatted, [class*='bid-amount'], [class*='current-bid']")
            price = _int(bid_el.get_text()) if bid_el else None

            img = card.select_one("div.thumbnail img, .listing-thumbnail img, img[src]")
            image_url = None
            if img:
                image_url = (img.get("src") or img.get("data-src") or "").split("?")[0] or None

            if not year:
                continue
            c = dict(year=year, make=make or "Porsche", model=model, trim=trim,
                     mileage=mileage, price=price, vin=None, url=url, image_url=image_url)
            if _is_valid_listing(c):
                cars.append(c)
    except Exception as e:
        log.warning("BaT scraper error: %s", e)
    return _dedupe(cars)


def scrape_pcamart():
    """mart.pca.org — ColdFusion platform, Playwright required.
    POST /search/ returns column-oriented JSON. Paginate through all pages."""
    if not _playwright_available():
        log.warning("PCA Mart scraper requires Playwright")
        return []

    BASE = "https://mart.pca.org"
    FORM_TEMPLATE = (
        "zipGeo=&searchInput=&yearRange=1950;2026&startYear=&endYear="
        "&priceRange=0;500000&minPrice=&maxPrice=&region=0&zipCode="
        "&fahrvergnugen=&sortOrder=DESC&sortBy=lastUpdated&perPage=20"
        "&startPageNumber={page}"
    )
    cars = []

    def _parse_cf_page(data):
        if not data:
            return []
        cols = data.get("COLUMNS", [])
        rows_d = data.get("DATA", {})
        if not cols or not rows_d:
            return []
        n = len(next(iter(rows_d.values()), []))
        out = []
        for i in range(n):
            row = {col: rows_d.get(col, [None] * n)[i] for col in cols}
            if row.get("ADTYPEID") != 1:
                continue
            year = _int(row.get("YEAR"))
            make = _clean(row.get("MAKE")) or "Porsche"
            # Model/trim come from TITLE ("2023 911 Turbo S Cabriolet"); MODEL field is absent
            title = _clean(row.get("TITLE")) or ""
            title_parts = title.split()
            # Skip leading year token if it matches the YEAR field
            if title_parts and re.match(r"^\d{4}$", title_parts[0]):
                title_parts = title_parts[1:]
            model = title_parts[0] if title_parts else None
            trim = " ".join(title_parts[1:]) or None
            mileage = _int(row.get("MILEAGE"))
            price = _int(row.get("VEHICLEPRICE") or row.get("PRICE") or row.get("ASKINGPRICE"))
            adnum = row.get("ADNUMBER")
            url = f"{BASE}/ads/{adnum}" if adnum else ""
            img_name = _clean(row.get("MAINIMAGENAME"))
            remote_img = f"{BASE}/includes/images/ads/{img_name}.jpg" if img_name else None
            # PCA Mart hotlink-protects images — cache locally
            image_url = _cache_image(remote_img, referer=BASE) if remote_img else None
            if not year:
                continue
            c = dict(year=year, make=make, model=model, trim=trim,
                     mileage=mileage, price=price, vin=None, url=url, image_url=image_url)
            if _is_valid_listing(c):
                out.append(c)
        return out

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _pw_launch(p)
            pg = _stealth_page(browser)

            captured = {}

            def on_response(resp):
                if "/search/" in resp.url and resp.status == 200:
                    try:
                        captured["page1"] = resp.json()
                    except Exception:
                        pass

            pg.on("response", on_response)
            pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
            # Give the /search/ XHR up to 8 extra seconds to complete after DOM loads
            try:
                pg.wait_for_response(lambda r: "/search/" in r.url and r.status == 200,
                                     timeout=8000)
            except Exception:
                pass  # timeout is fine — fallback handles it below

            # Fallback: if interception missed the /search/ response (networkidle
            # fired before the XHR completed), POST directly via evaluate() which
            # inherits the session cookies established by the page load.
            first = captured.get("page1")
            if not first:
                log.debug("PCA Mart: interception missed page1, using evaluate fallback")
                try:
                    form_p1 = FORM_TEMPLATE.format(page=1).replace("'", "\\'")
                    first = pg.evaluate(f"""
                        fetch('/search/', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: '{form_p1}'
                        }}).then(r => r.json())
                    """)
                except Exception as e:
                    log.warning("PCA Mart fallback evaluate failed: %s", e)

            # ColdFusion gates /search/ behind a valid CF session cookie. When the
            # POST fires immediately after domcontentloaded the cookie isn't always
            # accepted yet, so the server returns TOTALRECORDS=0 despite valid JSON.
            # Retry the POST up to 2 more times with a delay to let the session settle.
            for _retry in range(2):
                _total = int((first.get("DATA") or {}).get("TOTALRECORDS", [0])[0] or 0) if first else 0
                if _total > 0:
                    break
                log.debug("PCA Mart: page1 returned 0 records, retrying in 3s (attempt %d/2)...",
                          _retry + 1)
                time.sleep(3)
                try:
                    form_p1 = FORM_TEMPLATE.format(page=1).replace("'", "\\'")
                    first = pg.evaluate(f"""
                        fetch('/search/', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: '{form_p1}'
                        }}).then(r => r.json())
                    """)
                except Exception as e:
                    log.warning("PCA Mart page1 retry %d failed: %s", _retry + 1, e)
                    first = None

            if first:
                cars.extend(_parse_cf_page(first))
                # TOTALRECORDS lives inside DATA dict, not at the top level
                total = (first.get("DATA") or {}).get("TOTALRECORDS", [0])[0] or 0
                pages = max(1, (int(total) + 19) // 20) if total else 1

                for pg_num in range(2, min(pages + 1, 60)):
                    try:
                        form = FORM_TEMPLATE.format(page=pg_num).replace("'", "\\'")
                        result = pg.evaluate(f"""
                            fetch('/search/', {{
                                method: 'POST',
                                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                                body: '{form}'
                            }}).then(r => r.json())
                        """)
                        batch = _parse_cf_page(result)
                        if not batch:
                            break
                        cars.extend(batch)
                        time.sleep(0.3)
                    except Exception as e:
                        log.warning("PCA Mart page %d error: %s", pg_num, e)
                        break

            # Download images via the existing page context (shares session cookies).
            # PCA Mart hotlink-protects images — external requests get redirected to homepage.
            import hashlib as _hl
            _IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached_count = 0
            for car in cars:
                img = car.get("image_url")
                if not img or not img.startswith("http") or "/static/img_cache/" in img:
                    continue
                try:
                    ext = img.rsplit(".", 1)[-1].split("?")[0].lower()
                    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                        ext = "jpg"
                    fname = _hl.md5(img.encode()).hexdigest() + "." + ext
                    fpath = _IMG_CACHE_DIR / fname
                    if not fpath.exists():
                        # pg.context.request shares the same cookies as the scraped page
                        resp = pg.context.request.get(
                            img,
                            headers={"Referer": "https://mart.pca.org/"},
                        )
                        body = resp.body()
                        log.debug("PCA img %s → %d bytes status=%s", fname, len(body), resp.status)
                        if resp.ok and len(body) > 5000:
                            fpath.write_bytes(body)
                    if fpath.exists():
                        car["image_url"] = f"/static/img_cache/{fname}"
                        cached_count += 1
                except Exception as _ie:
                    log.warning("PCA image cache error %s: %s", img, _ie)
            log.info("PCA Mart: cached %d/%d images to img_cache", cached_count, len(cars))

            browser.close()
    except Exception as e:
        log.warning("PCA Mart scraper error: %s", e)

    if not cars:
        log.warning("PCA Mart: 0 results after retries — session/API failure likely")
    return _dedupe(cars)


def scrape_pcarmarket():
    """pcarmarket.com — Porsche-only marketplace, Playwright required.
    Listings are at /marketplace; cards are <a href="/auction/..."> links.
    Title format: 'MarketPlace: YEAR Porsche CHASSIS-OR-MODEL TRIM— Active None'"""
    if not _playwright_available():
        log.warning("pcarmarket scraper requires Playwright")
        return []

    BASE = "https://www.pcarmarket.com"

    # Map Porsche chassis codes to canonical model names used by _is_valid_listing
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
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _pw_launch(p)
            pg = _stealth_page(browser)
            pg.goto(f"{BASE}/marketplace",
                    wait_until="domcontentloaded", timeout=45000)
            try:
                pg.wait_for_selector("a[href*='/auction/']", timeout=15000)
            except Exception:
                pass
            # Extra pause for Vue.js SPA hydration
            pg.wait_for_timeout(3000)
            html = pg.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        seen = set()
        for a in soup.select("a[href*='/auction/']"):
            href = a.get("href", "")
            url = href if href.startswith("http") else f"{BASE}{href}"
            text = a.get_text(" ", strip=True)
            # Skip "SAVE LISTING ..." duplicate entries
            if text.upper().startswith("SAVE LISTING"):
                continue
            # Strip "MarketPlace: " prefix and "— Active/Sold/Pending ..." suffix
            text = re.sub(r"^MarketPlace:\s*", "", text, flags=re.I).strip()
            text = re.sub(r"\s*[—–-]\s*(Active|Sold|Pending).*$", "", text, flags=re.I).strip()
            if not text:
                continue
            # If title doesn't start with a year, seek the year further in
            # e.g. "993-Style 1991 Porsche 964 ..." or "9k-Mile 2018 Mercedes ..."
            if not re.match(r"^\d{4}\s", text):
                m = re.search(r"\b(\d{4})\s", text)
                if m:
                    text = text[m.start():]
                else:
                    continue

            year, make, model, trim = _parse_ymmt(text)
            if not year:
                continue

            # Normalize chassis codes to canonical model names
            if model:
                canonical = _CHASSIS.get(model) or _CHASSIS.get(model.lower())
                if canonical:
                    model = canonical

            key = url or f"{year}{model}{trim}"
            if key in seen:
                continue
            seen.add(key)

            c = dict(year=year, make=make or "Porsche", model=model, trim=trim,
                     mileage=None, price=None, vin=None, url=url, image_url=None)
            if _is_valid_listing(c):
                cars.append(c)
    except Exception as e:
        log.warning("pcarmarket scraper error: %s", e)

    return _dedupe(cars)


def scrape_classic():
    """
    classic.com active Porsche listings via Playwright.
    NOTE: Cloudflare protection may block headless browsers. If listings
    come back empty, a residential proxy or cookie export is needed.
    """
    if not _playwright_available():
        log.warning("classic.com scraper requires Playwright")
        return []

    BASE = "https://www.classic.com"
    cars = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _pw_launch(p)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            pg = _stealth_page(ctx)
            pg.goto(
                f"{BASE}/search/?make=Porsche&status=active",
                wait_until="domcontentloaded", timeout=40000,
            )
            try:
                pg.wait_for_selector(
                    "article, [class*='listing'], [class*='car-card'], [class*='vehicle']",
                    timeout=15000,
                )
            except Exception:
                pass
            html = pg.content()
            browser.close()

        if "just a moment" in html.lower() or 'id="challenge' in html:
            log.warning(
                "classic.com: Cloudflare challenge — results empty. "
                "Add a residential proxy or export browser cookies to unblock."
            )
            return []

        soup = BeautifulSoup(html, "lxml")
        for card in soup.select(
            "article, [class*='listing-card'], [class*='vehicle-card'], [class*='car-card']"
        ):
            c = _parse_card_generic(card, BASE)
            if c and _is_valid_listing(c):
                cars.append(c)

        log.info("classic.com: %d valid listings", len(cars))
    except Exception as e:
        log.warning("classic.com scraper error: %s", e)

    return _dedupe(cars)


# ---------------------------------------------------------------------------
# Retail marketplace scrapers — cars.com, AutoTrader, eBay Motors
# ---------------------------------------------------------------------------

def scrape_carscom():
    """cars.com — paginated search results at /shopping/results/?makes[]=porsche.

    Cards: div[class*="vehicle-card"][data-listing-id]
    Title: h2.title a  (format: "YEAR MAKE MODEL TRIM")
    Price: .primary-price
    Mileage: .mileage span or element text
    URL:   https://www.cars.com/vehicledetail/{listing_id}/
    VIN:   data-vin attribute on card, or regex in card text
    Image: first img with non-placeholder src inside the card
    Pagination: ?page=N; stop when page returns no new cards.
    Capped at 10 pages (~200 listings) — nationwide inventory is large.
    Requires Playwright since static requests from some IPs are blocked.
    """
    BASE     = "https://www.cars.com"
    URL_TMPL = (f"{BASE}/shopping/results/?makes[]=porsche"
                "&page_size=20&page={p}&stock_type=all")

    def _parse_cards(soup):
        out = []
        for card in soup.select("[class*='vehicle-card'][data-listing-id]"):
            listing_id = card.get("data-listing-id", "")
            if not listing_id:
                continue

            title_el = card.select_one("h2.title a, h2 a, .title a, h2")
            title    = _clean(title_el.get_text()) if title_el else ""
            year, make, model, trim = _parse_ymmt(title)
            if not year:
                continue

            price_el = card.select_one(
                ".primary-price, [class*='primary-price'], "
                "[class*='price-section'] [class*='price']"
            )
            price = _int(price_el.get_text()) if price_el else None

            miles_el = card.select_one(".mileage, [class*='mileage']")
            mileage  = None
            if miles_el:
                # .mileage may contain nested spans; prefer inner span text
                inner = miles_el.select_one("span")
                mileage = _int((inner or miles_el).get_text())

            vin = _clean(card.get("data-vin") or "")
            if not vin:
                vm = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", card.get_text())
                vin = vm.group(1) if vm else None

            # Prefer the large image; avoid tracking/placeholder URLs
            img_url = None
            for img in card.select("img[src]"):
                src = img.get("src", "")
                if src and "placeholder" not in src.lower() and src.startswith("http"):
                    img_url = src.split("?")[0]
                    break

            out.append(dict(
                year=year, make=make, model=model, trim=trim,
                mileage=mileage, price=price, vin=vin,
                url=f"{BASE}/vehicledetail/{listing_id}/",
                image_url=img_url,
            ))
        return out

    cars = []
    seen = set()

    for p in range(1, 11):   # cap at 10 pages = ~200 listings
        soup = None

        # Try static GET first (works when not IP-blocked)
        soup = get(URL_TMPL.format(p=p))

        # Playwright fallback
        if not soup or not soup.select("[class*='vehicle-card'][data-listing-id]"):
            if _playwright_available():
                soup = _get_rendered(
                    URL_TMPL.format(p=p),
                    wait_selector="[class*='vehicle-card'][data-listing-id]",
                    timeout=30000,
                )
            if not soup:
                break

        page_cars = _parse_cards(soup)
        if not page_cars:
            break

        new = 0
        for c in page_cars:
            key = c.get("vin") or c["url"]
            if key not in seen:
                seen.add(key)
                cars.append(c)
                new += 1
        if new == 0:
            break
        time.sleep(1.2)

    log.info("cars.com: %d listings across %d pages", len(cars), p)
    return _dedupe(cars)


def scrape_autotrader():
    """autotrader.com — Next.js SPA; listings in __NEXT_DATA__ JSON.

    Primary strategy: static GET → parse window.__NEXT_DATA__ script tag.
      Listing data path varies by deployment but is hunted recursively.
      Fields extracted: id, year, make, model, trim, mileage, price,
      vin, listingUrl, heroImageUrl (or photos[0].url).
    Fallback: Playwright with realistic headers + same JSON extraction.
    Warning logged if site returns a bot-block page (< 10 KB response).
    Capped at 10 pages (~250 listings) — nationwide inventory is large.
    """
    BASE = "https://www.autotrader.com"

    def _hunt_listings(obj, depth=0):
        """Recursively find the first list whose items look like vehicle listings."""
        if depth > 8:
            return []
        if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
            first = obj[0]
            # Must have at least year + (make or model)
            if ("year" in first or "modelYear" in first) and (
                "make" in first or "model" in first or "listingUrl" in first
            ):
                return obj
        if isinstance(obj, dict):
            # Prefer keys that sound like listing collections
            for priority in ("listings", "vehicles", "vehicleListings",
                             "searchResults", "inventory", "results", "items"):
                if priority in obj:
                    found = _hunt_listings(obj[priority], depth + 1)
                    if found:
                        return found
            for v in obj.values():
                found = _hunt_listings(v, depth + 1)
                if found:
                    return found
        return []

    def _parse_listing(item):
        year  = _int(item.get("year") or item.get("modelYear"))
        make  = _clean(item.get("make") or item.get("makeName") or "Porsche")
        model = _clean(item.get("model") or item.get("modelName"))
        trim  = _clean(item.get("trim") or item.get("trimName"))
        mileage = _int(item.get("mileage") or item.get("odometer"))
        price   = _int(
            item.get("derivedPrice") or item.get("price") or
            item.get("listingPrice") or item.get("askingPrice")
        )
        vin = _clean(item.get("vin") or item.get("vehicleIdentificationNumber"))
        url = _clean(item.get("listingUrl") or item.get("detailPageUrl") or "")
        if url and not url.startswith("http"):
            url = BASE + url

        # Image — try several possible key names
        img = None
        for key in ("heroImageUrl", "primaryPhotoUrl", "imageUrl", "thumbnail"):
            if item.get(key):
                img = _clean(item[key])
                break
        if not img:
            photos = item.get("photos") or item.get("images") or []
            if photos and isinstance(photos[0], dict):
                img = _clean(
                    photos[0].get("url") or photos[0].get("src") or
                    photos[0].get("href") or ""
                )

        if not year:
            return None
        return dict(year=year, make=make, model=model, trim=trim,
                    mileage=mileage, price=price, vin=vin,
                    url=url, image_url=img)

    def _extract_from_html(html):
        soup = BeautifulSoup(html, "lxml")
        # __NEXT_DATA__ is the primary data vector
        nd = soup.find("script", id="__NEXT_DATA__")
        if not nd or not nd.string:
            return []
        try:
            data     = json.loads(nd.string)
            listings = _hunt_listings(data)
            out = []
            for item in listings:
                c = _parse_listing(item)
                if c:
                    out.append(c)
            return out
        except Exception as e:
            log.debug("autotrader __NEXT_DATA__ parse error: %s", e)
            return []

    _BLOCKED_SIGNALS = (
        "page unavailable", "captcha", "access denied",
        "robot", "are you a human", "challenge",
    )

    def _is_blocked(html):
        sample = html[:8000].lower()
        return len(html) < 20000 or any(s in sample for s in _BLOCKED_SIGNALS)

    cars = []
    seen = set()

    for p in range(1, 11):
        url = (f"{BASE}/cars-for-sale/porsche" if p == 1
               else f"{BASE}/cars-for-sale/porsche?firstRecord={(p-1)*25}")

        # 1. Static GET
        try:
            r = SESSION.get(url, timeout=30)
            html = r.text if r.ok else ""
        except Exception:
            html = ""

        # 2. Playwright fallback if blocked or empty
        if _is_blocked(html) and _playwright_available():
            if p == 1:
                log.debug("autotrader: static GET blocked, trying Playwright")
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = _pw_launch(pw)
                    ctx = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/123.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                        locale="en-US",
                    )
                    pg = _stealth_page(ctx)
                    pg.goto(url, wait_until="domcontentloaded", timeout=35000)
                    html = pg.content()
                    browser.close()
            except Exception as e:
                log.debug("autotrader Playwright error: %s", e)
                html = ""

        if _is_blocked(html):
            if p == 1:
                log.warning(
                    "autotrader: blocked (bot detection active). "
                    "Results will be empty — run from a residential IP."
                )
            break

        page_cars = _extract_from_html(html)
        if not page_cars:
            break

        new = 0
        for c in page_cars:
            key = c.get("vin") or c.get("url") or f"{c['year']}{c.get('model')}{p}"
            if key not in seen:
                seen.add(key)
                cars.append(c)
                new += 1
        if new == 0:
            break
        time.sleep(1.2)

    log.info("autotrader: %d listings", len(cars))
    return _dedupe(cars)


def scrape_ebay():
    """eBay Motors — Porsche listings via Browse API (primary) or HTML scraper (fallback).

    API: GET /buy/browse/v1/item_summary/search
         q=porsche, category_ids=6001, limit=200
    Auth: OAuth Client Credentials (app_id + cert_id from ebay_api_config.json).
    Falls back to HTML scraper if API credentials are missing or the call fails.
    """
    import base64
    from pathlib import Path as _Path

    CFG_PATH = _Path(__file__).parent / "data" / "ebay_api_config.json"

    def _load_cfg():
        try:
            with open(CFG_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    # Use a proxy-free session for the eBay REST API — no IP evasion needed for
    # a credentialed API, and the proxy can interfere with JSON responses.
    _api_sess = requests.Session()

    def _get_token(cfg):
        """Exchange app_id + cert_id for an OAuth app token."""
        creds = base64.b64encode(
            f"{cfg['app_id']}:{cfg['cert_id']}".encode()
        ).decode()
        r = _api_sess.post(
            f"{cfg.get('base_url', 'https://api.ebay.com')}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def _api_search(token, base_url, offset=0):
        r = _api_sess.get(
            f"{base_url}/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "q": "porsche",
                "category_ids": "6001",
                "limit": "200",
                "offset": str(offset),
                "fieldgroups": "EXTENDED",
                # US listings only, max price $300K (vehicle category is inherently used)
                "filter": "itemLocationCountry:US,price:[0..300000],priceCurrency:USD",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _parse_api_items(items):
        out = []
        for item in items:
            title = _clean(item.get("title") or "")
            # Skip foreign-language listings before any parsing
            if any(ord(c) > 127 for c in title):
                continue
            if any(phrase in title.lower() for phrase in _FOREIGN_PHRASES):
                continue
            year, make, model_parsed, trim = _parse_ymmt(title)

            # itemSpecifics supplements parsed fields
            specs = {}
            for s in (item.get("localizedAspects") or item.get("itemSpecifics") or []):
                k = (s.get("name") or s.get("localizedName") or "").lower()
                v = s.get("value") or (s.get("values") or [None])[0] or ""
                specs[k] = _clean(str(v))

            if not year:
                year = _int(specs.get("year") or specs.get("model year"))
            if not year:
                continue
            if not make:
                make = _clean(specs.get("make")) or "Porsche"

            # Use model from title parse; fall back to specs model field
            model = model_parsed or _clean(specs.get("model"))

            # Mileage from specs
            mileage = None
            for k in ("mileage", "miles", "odometer reading"):
                if specs.get(k):
                    mileage = _int(specs[k])
                    break

            vin = _clean(specs.get("vin") or specs.get("vehicle identification number"))

            price_obj = item.get("price") or {}
            price = _int(price_obj.get("value"))

            img = (item.get("image") or {}).get("imageUrl")
            url = item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or ""
            # Strip tracking params from URL
            if url and "?" in url:
                url = url.split("?")[0]

            out.append(dict(
                year=year, make=make, model=model, trim=trim,
                mileage=mileage, price=price, vin=vin,
                url=url, image_url=img,
                title=title,
            ))
        return out

    def _scrape_html():
        """Original HTML scraper as fallback."""
        BASE = "https://www.ebay.com"
        URL_TMPL = (f"{BASE}/sch/i.html?_nkw=porsche&_sacat=6001"
                    "&LH_BIN=1&_sop=10&LH_ItemCondition=3000"
                    "&_udhi=300000&LH_PrefLoc=1&_pgn={{p}}")
        _EBAY_JUNK = re.compile(
            r"^\s*(?:New\s+Listing|SPONSORED|Sponsored)\s*"
            r"|(?:\s*Opens\s+in\s+a\s+new\s+(?:window|tab).*$)",
            re.I | re.S,
        )

        def _parse_page(soup):
            page_out = []
            for card in soup.select("li.s-card"):
                text = card.get_text(" ", strip=True)
                if "Year:" not in text:
                    continue
                listing_id = card.get("data-listingid", "")
                url = f"{BASE}/itm/{listing_id}" if listing_id else ""
                title_el = card.select_one(".s-card__title")
                raw_title = title_el.get_text(" ", strip=True) if title_el else ""
                clean_title = _EBAY_JUNK.sub("", raw_title).strip()
                clean_title = re.sub(r"\s{2,}", " ", clean_title).strip()
                # Skip foreign-language titles
                if any(ord(c) > 127 for c in clean_title):
                    continue
                if any(phrase in clean_title.lower() for phrase in _FOREIGN_PHRASES):
                    continue
                yr, mk, mdl_parsed, tr = _parse_ymmt(clean_title)
                if not yr:
                    ym = re.search(r"Year:\s*(\d{4})", text)
                    yr = _int(ym.group(1)) if ym else None
                if not yr:
                    continue
                price = None
                for row in card.select(".s-card__attribute-row"):
                    t = row.get_text(strip=True)
                    if t.startswith("$"):
                        price = _int(t)
                        break
                sec = card.select_one(".su-card-container__attributes__secondary")
                sec_text = sec.get_text(" ", strip=True) if sec else text
                mm = re.search(r"Miles?:\s*([\d,]+)", sec_text, re.I)
                mileage = _int(mm.group(1)) if mm else None
                img = card.select_one("img.s-card__image[src]")
                image_url = img.get("src", "").split("?")[0] if img else None
                page_out.append(dict(year=yr, make=mk or "Porsche", model=mdl_parsed, trim=tr,
                                     mileage=mileage, price=price, vin=None,
                                     url=url, image_url=image_url, title=clean_title))
            return page_out

        html_cars = []
        seen_html = set()
        for p in range(1, 11):
            soup = get(URL_TMPL.format(p=p))
            if not soup:
                break
            page_cars = _parse_page(soup)
            if not page_cars:
                break
            new = 0
            for c in page_cars:
                key = c["url"] or f"{c['year']}|{c.get('model')}|p{p}"
                if key not in seen_html:
                    seen_html.add(key)
                    html_cars.append(c)
                    new += 1
            if new == 0:
                break
            time.sleep(0.8)
        log.info("ebay HTML fallback: %d listings", len(html_cars))
        return html_cars

    # --- Main: try API first ---
    cfg = _load_cfg()
    cars = []
    if cfg.get("app_id") and cfg.get("cert_id"):
        try:
            token = _get_token(cfg)
            base_url = cfg.get("base_url", "https://api.ebay.com")
            # eBay Browse API returns up to 200 per call; paginate to ~600
            all_items = []
            for offset in range(0, 600, 200):
                data = _api_search(token, base_url, offset=offset)
                items = data.get("itemSummaries") or []
                if not items:
                    break
                all_items.extend(items)
                total = int(data.get("total") or 0)
                if offset + 200 >= total:
                    break
                time.sleep(0.3)
            log.info("ebay API: %d raw items fetched", len(all_items))
            cars = _parse_api_items(all_items)
        except Exception as e:
            log.warning("ebay API failed (%s), falling back to HTML scraper", e)
            cars = _scrape_html()
    else:
        log.info("ebay: no API credentials, using HTML scraper")
        cars = _scrape_html()

    log.info("ebay: %d listings", len(cars))
    return _dedupe(cars)


# ---------------------------------------------------------------------------
# Apify-based scrapers (AutoTrader + Cars.com)
# ---------------------------------------------------------------------------
def _load_apify_token() -> str:
    cfg_path = Path(__file__).parent / "data" / "apify_config.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    # Support both key names
    token = cfg.get("api_token") or cfg.get("APIFY_API_TOKEN") or ""
    if not token:
        raise RuntimeError("Apify API token not found in apify_config.json")
    return token


def _apify_run_and_fetch(actor_id: str, run_input: dict) -> list:
    """
    Trigger an Apify actor run, poll until complete, return dataset items.
    actor_id: e.g. 'epctex~autotrader-scraper'
    """
    token = _load_apify_token()
    base = "https://api.apify.com/v2"

    # Trigger run
    actor_slug = actor_id.replace("/", "~")
    trigger_url = f"{base}/acts/{actor_slug}/runs?token={token}"
    log.info("Apify: triggering actor %s", actor_id)
    r = requests.post(trigger_url, json=run_input, timeout=30)
    if r.status_code not in (200, 201):
        log.warning("Apify trigger failed: %s %s", r.status_code, r.text[:200])
        return []

    run_data = r.json().get("data", {})
    run_id = run_data.get("id", "")
    log.info("Apify: run started id=%s", run_id)

    # Poll for completion
    poll_url = f"{base}/acts/{actor_slug}/runs/last?token={token}"
    deadline = time.time() + 600  # 10-minute timeout
    status = ""
    while time.time() < deadline:
        time.sleep(10)
        try:
            pr = requests.get(poll_url, timeout=20)
            pr.raise_for_status()
            status = pr.json().get("data", {}).get("status", "")
            log.info("Apify: run status = %s", status)
        except Exception as e:
            log.warning("Apify poll error: %s", e)
            continue
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        log.warning("Apify run did not succeed: status=%s", status)
        return []

    # Paginate dataset
    items = []
    offset = 0
    limit = 100
    dataset_url = f"{base}/acts/{actor_slug}/runs/last/dataset/items?token={token}"
    while True:
        try:
            dr = requests.get(dataset_url, params={"limit": limit, "offset": offset}, timeout=30)
            dr.raise_for_status()
            page = dr.json()
        except Exception as e:
            log.warning("Apify dataset fetch error: %s", e)
            break
        if not page:
            break
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    log.info("Apify: %d items fetched from dataset", len(items))
    return items


def scrape_autotrader_apify() -> list:
    """AutoTrader Porsche listings via Apify actor epctex/autotrader-scraper."""
    run_input = {
        "startUrls": [
            "https://www.autotrader.com/cars-for-sale/used-cars/porsche"
            "?searchRadius=0&isNewSearch=true&showAccelerateBanner=false"
            "&sortBy=relevance&numRecords=100"
        ],
        "maxItems": 100,
        "proxy": {"useApifyProxy": True},
        "endPage": 5,
    }
    try:
        items = _apify_run_and_fetch("epctex~autotrader-scraper", run_input)
    except Exception as e:
        log.warning("scrape_autotrader_apify error: %s", e)
        return []

    cars = []
    for item in items:
        # Log a sample item on first iteration for field mapping diagnosis
        if not cars and items:
            log.debug("AutoTrader sample item keys: %s", list(item.keys()))
            log.info("AutoTrader sample item: %s", {k: item.get(k) for k in list(item.keys())[:15]})

        # Make filter — only Porsche (brand field)
        brand = (item.get("brand") or "").strip()
        if brand and brand.lower() != "porsche":
            continue

        year = _int(item.get("year"))
        if not year:
            continue

        price = _int(item.get("price"))
        if not price or price <= 0:
            continue

        # model = model line (e.g. "911", "Cayenne"); trim parsed from title after year+model
        model = _clean(item.get("model")) or None
        title = _clean(item.get("title") or "")
        # title format: "Used 2020 Porsche 911 Carrera S" — extract everything after "model"
        trim = None
        if model and title:
            idx = title.find(model)
            if idx != -1:
                remainder = title[idx + len(model):].strip()
                trim = _clean(remainder) or None

        # mileage is a string like "18,993" — strip commas
        mileage_raw = str(item.get("mileage") or "").replace(",", "")
        mileage = _int(mileage_raw)

        days_on_site = _int(item.get("daysOnSite"))

        url = _clean(item.get("url") or "")
        vin = _clean(item.get("vin"))

        cars.append(dict(
            year=year,
            make="Porsche",
            model=model,
            trim=trim,
            mileage=mileage,
            price=price,
            vin=vin,
            url=url,
            image_url=None,
            source_category="RETAIL",
            days_on_site=days_on_site,
        ))
    log.info("scrape_autotrader_apify: %d Porsche listings", len(cars))
    return cars


def scrape_carsdotcom_apify() -> list:
    """Cars.com Porsche listings via Apify actor jgleesti/cars-com-scraper."""
    run_input = {
        "startUrls": [{"url": (
            "https://www.cars.com/shopping/results/"
            "?makes[]=porsche&stock_type=used&maximum_distance=all&zip=10001"
        )}],
        "maxRequestsPerCrawl": 100,
        "getDetails": False,
    }
    try:
        items = _apify_run_and_fetch("jgleesti~cars-com-scraper", run_input)
    except Exception as e:
        log.warning("scrape_carsdotcom_apify error: %s", e)
        return []

    cars = []
    for item in items:
        if not cars and items:
            log.info("Cars.com sample item: %s", {k: item.get(k) for k in list(item.keys())[:15]})

        make_raw = (item.get("make") or item.get("Make") or "").strip()
        if make_raw and make_raw.lower() != "porsche":
            continue

        year_raw = item.get("year") or item.get("Year") or item.get("modelYear")
        year = _int(year_raw)
        if not year:
            continue

        model_raw = item.get("model") or item.get("Model") or ""
        trim_raw = item.get("trim") or item.get("Trim") or item.get("trimName") or ""

        model = _clean(model_raw) or _clean(trim_raw) or None

        price_raw = (item.get("price") or item.get("Price") or
                     item.get("listPrice") or 0)
        price = _int(price_raw)
        if not price or price <= 0:
            continue

        mileage_raw = item.get("mileage") or item.get("miles") or item.get("Mileage")
        mileage = _int(mileage_raw)

        vin = _clean(item.get("vin") or item.get("VIN") or item.get("Vin"))
        url = _clean(item.get("url") or item.get("link") or item.get("vdpUrl") or "")
        trim = _clean(trim_raw) or None

        cars.append(dict(
            year=year,
            make="Porsche",
            model=model,
            trim=trim,
            mileage=mileage,
            price=price,
            vin=vin,
            url=url,
            image_url=None,
            source_category="RETAIL",
        ))
    log.info("scrape_carsdotcom_apify: %d Porsche listings", len(cars))
    return cars


# ---------------------------------------------------------------------------
# Image cache — download hotlink-protected images to static/img_cache/
# ---------------------------------------------------------------------------
_IMG_CACHE_DIR = Path(__file__).parent / "static" / "img_cache"


def _cache_image(remote_url: str, referer: str = "") -> str:
    """Download an image to static/img_cache/ and return its local web path.
    Used for sources with hotlink protection (e.g. PCA Mart).
    Returns the original URL unchanged on failure."""
    if not remote_url:
        return remote_url
    try:
        _IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Use a stable filename based on the URL
        import hashlib
        ext = remote_url.rsplit(".", 1)[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        fname = hashlib.md5(remote_url.encode()).hexdigest() + "." + ext
        fpath = _IMG_CACHE_DIR / fname
        if fpath.exists():
            return f"/static/img_cache/{fname}"
        headers = {"Referer": referer} if referer else {}
        r = requests.get(remote_url, headers=headers, timeout=15, proxies={})
        if r.status_code == 200 and len(r.content) > 1000:
            fpath.write_bytes(r.content)
            return f"/static/img_cache/{fname}"
    except Exception as e:
        log.debug("Image cache miss %s: %s", remote_url, e)
    return remote_url


# ---------------------------------------------------------------------------
# Rennlist Classifieds scraper
# ---------------------------------------------------------------------------
_RENNLIST_BASE  = "https://rennlist.com/forums/market/vehicles"
_RENNLIST_MAKES = ("porsche", "911", "boxster", "cayman", "718")


def _rennlist_chrome_cookies() -> list:
    """Load Chrome cookies for rennlist.com via browser-cookie3.
    Returns a list of Playwright-compatible cookie dicts."""
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="rennlist.com")
        pw_cookies = []
        for c in cj:
            cookie = {
                "name": c.name,
                "value": c.value,
                "domain": c.domain if c.domain.startswith(".") else f".{c.domain}",
                "path": c.path or "/",
                "secure": bool(c.secure),
            }
            if c.expires:
                cookie["expires"] = float(c.expires)
            pw_cookies.append(cookie)
        log.info("Rennlist: loaded %d Chrome cookies", len(pw_cookies))
        return pw_cookies
    except Exception as e:
        log.warning("Rennlist: could not load Chrome cookies: %s", e)
        return []


def _rennlist_fetch_page(page: int):
    """Fetch one Rennlist marketplace page using requests + Chrome cookies.
    Uses proxies={} to force direct connection (bypasses system/configured proxy).
    Rennlist requires a logged-in session and blocks all proxy exit IPs.
    Returns a BeautifulSoup or None."""
    url = f"{_RENNLIST_BASE}?page={page}" if page > 1 else _RENNLIST_BASE
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="rennlist.com")
        # proxies={} forces requests to ignore ALL proxy settings (system + configured)
        r = requests.get(
            url,
            cookies=cj,
            proxies={},   # empty dict = no proxy, bypasses system proxy too
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://rennlist.com/forums/market/",
            },
            timeout=30,
            allow_redirects=True,
        )
        r.raise_for_status()
        log.info("Rennlist: page %d → HTTP %d (direct, %d bytes)",
                 page, r.status_code, len(r.content))
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning("Rennlist fetch error page %d: %s", page, e)
        return None


def scrape_rennlist() -> list:
    """Rennlist Classifieds — parses JSON-LD <script> blocks per listing.

    Each listing on https://rennlist.com/forums/market/vehicles contains a
    <script type="application/ld+json"> block with a Car schema object that
    includes: VIN, model, year, transmission, price, seller location, individual
    listing URL (https://rennlist.com/forums/market/{id}), and image URL.

    Pagination: ?page=N  (12-13 listings/page, 35 pages total; stop on 0 Car blocks).
    Note: ?make=Porsche causes a PHP server error — we fetch all makes and filter in Python.
    Uses Playwright + Chrome session cookies (plain GET returns 403).
    """
    cars = []
    seen_vins: set = set()
    seen_urls: set = set()
    page = 1
    max_pages = 40  # 35 pages + safety margin

    while page <= max_pages:
        soup = _rennlist_fetch_page(page)
        if soup is None:
            log.warning("Rennlist: failed to fetch page %d", page)
            break

        # Build a url→date map from "Started: MMM DD, YYYY" <small> tags.
        # Each listing container has a <small> with the original post date.
        _date_re = re.compile(r"Started:\s*(\w+ \d+, \d{4})")
        _date_by_url: dict = {}
        _seen_date_urls: set = set()
        for small in soup.find_all("small"):
            dm = _date_re.search(small.get_text())
            if not dm:
                continue
            # Walk up to find the nearest Car JSON-LD in the same container
            el = small
            for _ in range(8):
                el = el.parent
                if el is None:
                    break
                ld = el.find("script", type="application/ld+json")
                if ld:
                    try:
                        d = json.loads(ld.string or "")
                        if d.get("@type") == "Car":
                            lu = d.get("url")
                            if lu and lu not in _seen_date_urls:
                                _seen_date_urls.add(lu)
                                _date_by_url[lu] = dm.group(1)
                    except Exception:
                        pass
                    break

        # Each listing carries its own <script type="application/ld+json"> Car block.
        ld_scripts = soup.find_all("script", type="application/ld+json")
        page_cars = []

        for tag in ld_scripts:
            try:
                raw = tag.string or ""
                if not raw.strip():
                    continue
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = item.get("@type", "")
                    if isinstance(t, list):
                        t = " ".join(t)
                    if "Car" not in t and "Vehicle" not in t:
                        continue

                    # ── Extract fields ────────────────────────────────────
                    vin   = _clean(item.get("vehicleIdentificationNumber"))
                    model = _clean(item.get("model")) or "911"
                    year  = _int(item.get("modelDate") or item.get("vehicleModelDate"))
                    trans = _clean(item.get("vehicleTransmission"))
                    image = _clean(item.get("image"))
                    if isinstance(image, list):
                        image = image[0] if image else None
                    # Upgrade thumbnail from 160x120 to 800x600
                    if image and "160x120" in image:
                        image = image.replace("160x120", "800x600")
                    listing_url = _clean(item.get("url"))

                    # Price from offers
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = _int(offers.get("price")) if isinstance(offers, dict) else None

                    # Location from seller address
                    location = ""
                    if isinstance(offers, dict):
                        seller = offers.get("seller") or {}
                        addr   = seller.get("address") or {}
                        if isinstance(addr, dict):
                            city  = addr.get("addressLocality", "")
                            state = addr.get("addressRegion", "")
                            location = ", ".join(filter(None, [city, state]))

                    # ── Dedup ─────────────────────────────────────────────
                    if vin and vin in seen_vins:
                        continue
                    if listing_url and listing_url in seen_urls:
                        continue
                    if vin:
                        seen_vins.add(vin)
                    if listing_url:
                        seen_urls.add(listing_url)

                    # ── Filter: Porsche only ──────────────────────────────
                    # Use brand.name as the most reliable Porsche signal.
                    # Fall back to model/name keyword matching.
                    name_lower  = (item.get("name") or "").lower()
                    model_lower = model.lower()
                    brand_obj   = item.get("brand") or {}
                    brand_name  = (brand_obj.get("name") if isinstance(brand_obj, dict) else str(brand_obj)).lower()
                    is_porsche = (
                        "porsche" in brand_name
                        or any(m in model_lower for m in _RENNLIST_MAKES)
                        or "porsche" in name_lower
                    )
                    if not is_porsche:
                        continue

                    # Skip non-sports Porsche models (SUVs, sedans, EVs)
                    _blocked = ("cayenne", "macan", "panamera", "taycan", "918")
                    if any(b in model_lower or b in name_lower for b in _blocked):
                        continue

                    # Normalise model to our canonical names
                    if "boxster" in model_lower or "boxster" in name_lower:
                        model = "Boxster"
                    elif "cayman" in model_lower or "cayman" in name_lower:
                        model = "Cayman"
                    elif "718" in model_lower or "718" in name_lower:
                        model = "718"
                    else:
                        model = "911"  # GT3, GT4, Turbo, Targa etc. are all 911-family

                    # Trim: parse from listing name (e.g. "2019 991.2 TTS CPO till oct 27...")
                    # Strip year + model token, keep remainder as trim
                    name_str = item.get("name") or ""
                    trim = None
                    trim_m = re.sub(
                        r"^\s*\d{4}\s+", "", name_str
                    )  # strip leading year
                    for tok in ("911", "Boxster", "Cayman", "718", "Porsche"):
                        trim_m = re.sub(
                            rf"(?i)^\s*{re.escape(tok)}\s*", "", trim_m
                        ).strip()
                    trim = _clean(trim_m) or None
                    # Truncate long trims (listing subjects can be chatty)
                    if trim and len(trim) > 60:
                        trim = trim[:57].rstrip() + "…"

                    # Look up the original listing date from the HTML
                    posted_date = _date_by_url.get(listing_url)
                    # Parse to ISO format for DB (e.g. "Mar 21, 2026" → "2026-03-21")
                    date_first_seen = None
                    if posted_date:
                        try:
                            import datetime
                            date_first_seen = datetime.datetime.strptime(
                                posted_date, "%b %d, %Y"
                            ).date().isoformat()
                        except Exception:
                            pass

                    page_cars.append(dict(
                        year=year,
                        make="Porsche",
                        model=model,
                        trim=trim,
                        mileage=None,
                        price=price,
                        vin=vin,
                        url=listing_url or url,
                        image_url=image,
                        location=location,
                        transmission=trans,
                        source_category="RETAIL",
                        date_first_seen=date_first_seen,
                    ))
            except Exception as exc:
                log.debug("Rennlist JSON-LD parse error (page %d): %s", page, exc)
                continue

        # Count total Car blocks on page to detect end of pagination
        total_car_blocks = sum(
            1 for tag in ld_scripts
            if tag.string and '"@type":"Car"' in tag.string
        )
        if total_car_blocks == 0:
            log.info("Rennlist: page %d has no Car listings — end of results", page)
            break
        if not page_cars:
            log.info("Rennlist: page %d had %d listings, 0 Porsche — continuing",
                     page, total_car_blocks)

        log.info("Rennlist: page %d → %d listings", page, len(page_cars))
        cars.extend(page_cars)
        page += 1
        time.sleep(1.0)   # be polite

    log.info("scrape_rennlist: %d total Porsche listings", len(cars))
    return cars


# ---------------------------------------------------------------------------
# Master dealer registry
# ---------------------------------------------------------------------------
DEALERS = [
    {"name": "Holt Motorsports",            "scrape": scrape_holtmotorsports},
    {"name": "Ryan Friedman Motor Cars",    "scrape": scrape_ryanfriedmanmotorcars},
    {"name": "Velocity Porsche",            "scrape": scrape_velocitypcars},
    {"name": "Road Scholars",               "scrape": scrape_roadscholars},
    {"name": "Gaudin Classic",              "scrape": scrape_gaudinclassic},
    {"name": "UDrive Automobiles",          "scrape": scrape_udriveautomobiles},
    {"name": "Motorcars of the Main Line",  "scrape": scrape_motorcarsofthemainline},
    {"name": "Grand Prix Motors",           "scrape": scrape_grandprimotors},
    # External auction/marketplace
    {"name": "Rennlist",                    "scrape": scrape_rennlist},
    {"name": "Bring a Trailer",             "scrape": scrape_bat},
    {"name": "PCA Mart",                    "scrape": scrape_pcamart},
    {"name": "pcarmarket",                  "scrape": scrape_pcarmarket},
    {"name": "eBay Motors",                 "scrape": scrape_ebay},
    # Apify-based scrapers
    {"name": "AutoTrader",                  "scrape": scrape_autotrader_apify},
    {"name": "Cars.com",                    "scrape": scrape_carsdotcom_apify},
    # DISABLED — classic.com: third-party API requires approval (contact insight@classic.com).
    #   Cloudflare Turnstile also blocks headless scrapers. Re-enable via API once key obtained.
    # {"name": "classic.com", "scrape": scrape_classic},
    #
    # DISABLED — cars.com HTML scraper: IP-level TCP block from this network.
    # {"name": "cars.com", "scrape": scrape_carscom},
    #
    # DISABLED — AutoTrader HTML scraper: IP-level block.
    # {"name": "AutoTrader", "scrape": scrape_autotrader},
]


def run_all(dealers=None) -> dict:
    from db import classify_tier  # imported here to avoid circular imports at module load
    results = {}
    targets = dealers or DEALERS
    for d in targets:
        name = d["name"]
        log.info("Scraping %s…", name)
        try:
            raw = d["scrape"]()
            cars = [c for c in raw if _is_valid_listing(c)]
            for car in cars:
                car["tier"] = classify_tier(car.get("model"), car.get("trim"), car.get("year"))
            filtered = len(raw) - len(cars)
            if filtered:
                log.info("  → %d listings (%d filtered out)", len(cars), filtered)
            else:
                log.info("  → %d listings", len(cars))
            results[name] = cars
        except Exception as e:
            log.error("  ✗ %s: %s", name, e)
            log.debug(traceback.format_exc())
            results[name] = []
        time.sleep(0.5)
    return results

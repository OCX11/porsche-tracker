"""
shared/scraper_utils.py — Common scraper utilities shared by RennMarkt and RennAuktion.

Extracted from scraper.py: filter constants, HTTP session, proxy loading,
Playwright helpers, parse helpers, and dedup.
"""
import re
import json
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Listing filter constants
# ---------------------------------------------------------------------------
YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not increase until Jan 1 2027 — owner decision required
_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718",
                              "930", "964", "993", "996", "997", "991", "992",
                              "gt3", "gt4", "turbo"})
_BLOCKED_MODELS  = frozenset({"cayenne", "macan", "panamera", "taycan", "918"})
_JUNK_KEYWORDS   = frozenset({
    "parts", "engine", "wheels", "brochure",
    "poster", "emblem", "badge", "memorabilia",
})
_JUNK_KEYWORDS_STRICT = frozenset({"key", "book"})

_FOREIGN_PHRASES = frozenset({
    "anuncio nuevo", "se vende", "en venta", "ocasion", "oportunidad",
    "nuevo anuncio", "vendido", "precio negociable", "destacado",
    "zu verkaufen", "gebraucht", "verkaufe", "neufahrzeug",
    "a vendre", "occasion", "vendu",
    "in vendita", "usato",
})

PRICE_MIN   =   25_000
PRICE_MAX   = 1_000_000
MILEAGE_MAX =  100_000


def _is_valid_listing(car: dict) -> bool:
    for field in ("title", "make", "model"):
        val = car.get(field) or ""
        if any(ord(c) > 127 for c in val):
            return False

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

    trim_lower = (car.get("trim") or "").lower()
    if model == "911" and ("1.8 targa" in trim_lower or
                           ("914" in trim_lower and "914-6" not in trim_lower)):
        return False

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
# Proxy configuration
# ---------------------------------------------------------------------------
_PROXY_CFG: dict = {}
_PROXY_URL: str = ""
_PROXY_DEAD: bool = False


def _disable_proxy_session():
    global _PROXY_DEAD
    if not _PROXY_DEAD:
        _PROXY_DEAD = True
        SESSION.proxies.clear()
        log.warning("Proxy unavailable — falling back to direct for this run")


def _load_proxy():
    global _PROXY_CFG, _PROXY_URL
    try:
        # Walk up from this file's location to find data/proxy_config.json
        p = Path(__file__).resolve().parent
        for _ in range(6):
            cand = p / "data" / "proxy_config.json"
            if cand.exists():
                with open(cand) as f:
                    cfg = json.load(f)
                if cfg.get("enabled") and cfg.get("proxy_url"):
                    _PROXY_CFG = cfg
                    _PROXY_URL = cfg["proxy_url"]
                    SESSION.proxies.update({"http": _PROXY_URL, "https": _PROXY_URL})
                    log.info("Proxy enabled: %s:%s", cfg.get("host"), cfg.get("port"))
                    try:
                        ip_resp = SESSION.get("https://api.ipify.org?format=json", timeout=8)
                        exit_ip = ip_resp.json().get("ip", "?")
                        log.info("Proxy exit IP: %s", exit_ip)
                    except requests.exceptions.ProxyError:
                        _disable_proxy_session()
                    except Exception:
                        pass
                    return
                break
            p = p.parent
    except Exception as e:
        log.debug("No proxy config loaded: %s", e)


_load_proxy()


def _pw_proxy():
    if not _PROXY_URL or not _PROXY_CFG.get("enabled") or _PROXY_DEAD:
        return None
    return {
        "server": f"{_PROXY_CFG['protocol']}://{_PROXY_CFG['host']}:{_PROXY_CFG['port']}",
        "username": _PROXY_CFG["username"],
        "password": _PROXY_CFG["password"],
    }


def _pw_launch(p):
    kwargs = {"headless": True}
    proxy = _pw_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    return p.chromium.launch(**kwargs)


def _stealth_page(parent):
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
# Playwright helper
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

    _PREFIX_PATS = (
        re.compile(r"^[\d,]+k?-(?:Mile|Kilometer)[,\s]+", re.I),
        re.compile(r"^\d+-Years?-\S+\s+", re.I),
        re.compile(r"^RoW\s+"),
        re.compile(r"^(?:Modified|Supercharged|Turbocharged|Widebody|\w+-Built|\w+-Owner)\b[,\s]+", re.I),
    )
    for _ in range(5):
        prev = title
        for pat in _PREFIX_PATS:
            title = pat.sub("", title).strip()
        if title == prev:
            break
    if not re.match(r"^\d{4}\s", title):
        title = re.sub(r"^(?:\S+\s+){1,3}(?=\d{4}\s)", "", title).strip()

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
    cars = []
    seen = set()
    for a in soup.select("a[href]"):
        raw = a.get_text(" ", strip=True)
        if not raw or not re.match(r"^\d{4}\s", raw):
            continue
        title = raw.split("\n")[0].strip()
        title = re.split(r"(?<=\w)\.\s+[A-Z]", title)[0].strip()
        title = title[:120].strip()
        title = _clean(title)
        if not title:
            continue
        year, make, model, trim = _parse_ymmt(title)
        if not year:
            continue
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

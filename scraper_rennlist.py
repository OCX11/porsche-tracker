"""
Standalone Rennlist Classifieds scraper for Porsche listings.

Fetches page 1 of the Rennlist vehicle classifieds (pre-filtered: USA only,
for-sale, active, vehicles, newest first) using Playwright headless Chromium
+ DataImpulse proxy.  Parses .shelf-item elements with BeautifulSoup — logic
ported directly from distill_poller.py's rennlist HTML branch.

Returns a list of {year, make, model, trim, mileage, price, vin, url, image_url} dicts.
No pagination (page 1 is sufficient for polling).
No state file — fetch and return every run.
"""
import json
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEALER_NAME = "Rennlist"

_SEARCH_URL = (
    "https://rennlist.com/forums/market/vehicles"
    "?countryid=5&sortby=dateline_desc"
    "&intent%5B2%5D=2&status%5B0%5D=0&type%5B0%5D=1"
    "&filterstates%5Bvehicle_sellertype%5D=0"
    "&filterstates%5Bvehicle_types%5D=1"
    "&filterstates%5Bvehicle_statuses%5D=1"
    "&filterstates%5Bvehicle_condition%5D=0"
    "&filterstates%5Bvehicle_price%5D=0"
    "&filterstates%5Bvehicle_mileage%5D=0"
    "&filterstates%5Bvehicle_location%5D=0"
    "&filterstates%5Bvehicle_color%5D=0"
    "&filterstates%5Bvehicle_vehicletype%5D=0"
    "&filterstates%5Bvehicle_engine%5D=0"
    "&filterstates%5Bvehicle_transmission%5D=0"
)

# ---------------------------------------------------------------------------
# Proxy config (mirrors scraper_autotrader.py pattern)
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
                log.info("Proxy enabled: %s:%s (from %s)",
                         cfg.get("host"), cfg.get("port"), cand)
                return
        except Exception:
            pass
        p = p.parent


_load_proxy()


def _pw_proxy():
    """Return Playwright proxy dict if proxy is configured, else None."""
    if not _PROXY_URL or not _PROXY_CFG.get("enabled"):
        return None
    return {
        "server": f"{_PROXY_CFG['protocol']}://{_PROXY_CFG['host']}:{_PROXY_CFG['port']}",
        "username": _PROXY_CFG["username"],
        "password": _PROXY_CFG["password"],
    }


# ---------------------------------------------------------------------------
# Parse helpers (ported from distill_poller.py)
# ---------------------------------------------------------------------------
_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-2]\d)\b")
_PRICE_RE = re.compile(r"\$(\d[\d,]+)|\b(\d{1,3}(?:,\d{3})+)\b")

_MODEL_TOKENS = [
    "911", "GT3", "GT2", "GT4", "Turbo S", "Turbo", "Carrera", "Targa",
    "Cayman", "Boxster", "Speedster", "Spyder", "Sport Classic",
    "930", "964", "993", "996", "997", "991", "992", "718",
]

_PORSCHE_KW_RE = re.compile(
    r"\bPorsche\b|" + "|".join(rf"\b{re.escape(t)}\b" for t in _MODEL_TOKENS),
    re.I,
)


def _int(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _parse_title(title: str) -> dict:
    """Extract year/model/trim from a listing title line (mirrors distill_poller._parse_title)."""
    result = {"year": None, "make": "Porsche", "model": None, "trim": None}
    if not title:
        return result

    m = _YEAR_RE.search(title)
    if m:
        result["year"] = int(m.group(1))

    clean = re.sub(r"(?i)^(used|new|cpo|certified|pre-owned)\s+", "", title).strip()

    for tok in sorted(_MODEL_TOKENS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(tok)}\b", clean, re.I):
            if tok.isdigit() or len(tok) <= 4:
                model = tok
            elif tok.lower() in ("cayman", "boxster"):
                model = tok.capitalize()
            else:
                model = "911"

            result["model"] = model

            after = re.split(rf"\b{re.escape(tok)}\b", clean, maxsplit=1, flags=re.I)[-1]
            trim = re.sub(r"^\s*[-–—,]\s*", "", after).strip()
            trim = re.split(r"[—–|\$]|\d{1,3}(,\d{3})+\s*mi", trim)[0].strip()
            trim = re.sub(r"\s+\d{4,}(?:\s*mi(?:les?)?)?\s*$", "", trim, flags=re.I).strip()
            if trim:
                result["trim"] = trim
            break

    return result


def _extract_price(text: str):
    """Find first plausible listing price in free text (≥ $10k)."""
    if not text:
        return None
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        s = re.sub(r"[^\d]", "", str(raw))
        if not s:
            continue
        v = int(s)
        if 10_000 <= v < 5_000_000:
            return v
    return None


def _extract_mileage(text: str):
    """Extract mileage from free text (mirrors distill_poller._extract_mileage)."""
    if not text:
        return None

    m = re.search(r"less\s+than\s+(\d+)\s*k\s*(?:mi(?:les?)?)?", text, re.I)
    if m:
        v = int(m.group(1)) * 1000
        return v if 0 <= v < 500_000 else None

    m = re.search(r"\b(\d+)\s*k\s*(?:mi(?:les?)?)?(?=[\s,.\n]|$)", text, re.I)
    if m:
        v = int(m.group(1)) * 1000
        return v if 0 <= v < 500_000 else None

    m = re.search(r"([\d,]+)\s*mi(?:les?)?\.?(?!\s*(?:per|away)\b)", text, re.I)
    if m:
        s = re.sub(r"[^\d]", "", m.group(1))
        if s:
            v = int(s)
            return v if 0 <= v < 500_000 else None

    return None


def _best_title_line(block: str) -> str:
    """Return the most informative title line (mirrors distill_poller._best_title_line)."""
    for line in block.split("\n"):
        line = line.strip()
        if _YEAR_RE.search(line) and _PORSCHE_KW_RE.search(line):
            return line
    for line in block.split("\n"):
        line = line.strip()
        if _YEAR_RE.search(line):
            return line
    return block[:200]


# ---------------------------------------------------------------------------
# Playwright fetch
# ---------------------------------------------------------------------------
def _fetch_page(url: str):
    """
    Fetch URL with Playwright headless Chromium + DataImpulse proxy.
    Applies playwright-stealth if available.
    Returns HTML string or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed — cannot scrape Rennlist")
        return None

    proxy = _pw_proxy()
    launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
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

            try:
                page.wait_for_selector(".shelf-item", timeout=10000)
            except Exception:
                pass

            html = page.content()
            browser.close()

        return html
    except Exception as e:
        log.warning("Playwright fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# HTML parsing (ported from distill_poller.py rennlist HTML branch)
# ---------------------------------------------------------------------------
def _parse_html(html: str) -> list:
    """
    Parse Rennlist .shelf-item elements — ported directly from
    distill_poller.py's _split_blocks() rennlist HTML branch.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for item in soup.select(".shelf-item"):
        link = item.select_one("a[href*='/forums/market/']")
        img  = item.select_one("img[src*='ibsrv.net']")
        text_content = item.get_text(separator="\n", strip=True)

        if not _YEAR_RE.search(text_content):
            continue

        # URL
        url = None
        if link and link.get("href"):
            href = link["href"]
            if not href.startswith("http"):
                href = "https://rennlist.com" + href
            url = href

        # Image
        image_url = img["src"] if img else None

        # Title / year / model / trim
        title = _best_title_line(text_content)
        parsed = _parse_title(title)

        if not parsed["year"]:
            m = _YEAR_RE.search(text_content)
            if m:
                parsed["year"] = int(m.group(1))

        price = _extract_price(text_content)
        mileage = _extract_mileage(text_content)

        listings.append({
            "year":      parsed["year"],
            "make":      "Porsche",
            "model":     parsed["model"],
            "trim":      parsed["trim"],
            "mileage":   mileage,
            "price":     price,
            "vin":       None,
            "url":       url,
            "image_url": image_url,
        })

    log.info("Rennlist: parsed %d listings from HTML", len(listings))
    return listings


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------
def scrape_rennlist() -> list:
    """
    Scrape page 1 of Rennlist vehicle classifieds (USA only, for-sale, active,
    newest first) via Playwright + DataImpulse proxy.

    Returns list of {year, make, model, trim, mileage, price, vin, url, image_url}.
    No state file — fetch and return every run.
    """
    log.info("Rennlist: fetching page 1")
    html = _fetch_page(_SEARCH_URL)
    if not html:
        log.warning("Rennlist: failed to fetch page — returning []")
        return []

    listings = _parse_html(html)
    log.info("Rennlist scrape complete: %d listings", len(listings))
    return listings


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    results = scrape_rennlist()
    print(f"\nTotal listings: {len(results)}")
    for i, car in enumerate(results[:5]):
        print(f"  {i+1}. {car.get('year')} {car.get('model')} {car.get('trim') or ''} "
              f"| ${car.get('price') or '?'} | {(car.get('url') or '')[:70]}")

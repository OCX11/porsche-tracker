"""
Sold-comp scraper for Porsche market analysis.

Sources:
  - Bring a Trailer (bringatrailer.com) — completed auctions
  - PCA Mart (mart.pca.org) — sold/expired ads (ADSTATUSID != 1)

Writes records to sold_comps table via db.upsert_sold_comp().
Filter: Porsche only, 1986–2024, 911/Cayman/Boxster models only.
"""
import os
import re
import json
import time
import logging
from datetime import datetime

from bs4 import BeautifulSoup

import core.db as db
from shared.scraper_utils import (
    SESSION, _int, _clean, _parse_ymmt, _is_valid_listing,
    _playwright_available, YEAR_MIN, YEAR_MAX,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BaT sold comps — /auctions/results/ paginated
# ---------------------------------------------------------------------------

def _bat_parse_result_card(card, base="https://bringatrailer.com"):
    """Extract sold comp data from a BaT results-page listing card."""
    a = card.select_one("h3 > a") or card.select_one("h2 > a") or card.select_one("a[href]")
    if not a:
        return None
    title = _clean(a.get_text()) or ""
    url = a.get("href", "")
    if url and not url.startswith("http"):
        url = base + url

    # Mileage from "34k-Mile" prefix
    mileage = None
    mm = re.search(r"([\d,]+)(k)?-Mile", title, re.I)
    if mm:
        val = int(mm.group(1).replace(",", ""))
        mileage = val * 1000 if mm.group(2) else val

    clean_title = re.sub(r"[\d,]+k?-Mile\s+", "", title, flags=re.I).strip()
    year, make, model, trim = _parse_ymmt(clean_title)

    # Sold price
    price_el = (card.select_one(".bid-result, .sold-price, span.bid-formatted, "
                                "[class*='result'], [class*='sold']"))
    sold_price = _int(price_el.get_text()) if price_el else None

    # Sold date — look for a date string near "Sold" text
    sold_date = None
    date_el = card.select_one("[class*='date'], time, .listing-available-until")
    if date_el:
        dt_text = date_el.get("datetime") or date_el.get_text()
        dm = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", dt_text or "")
        if dm:
            sold_date = dm.group(1)

    img = card.select_one("div.thumbnail img, img[src]")
    image_url = None
    if img:
        image_url = (img.get("src") or img.get("data-src") or "").split("?")[0] or None

    if not year:
        return None

    return dict(
        year=year, make=make or "Porsche", model=model, trim=trim,
        mileage=mileage, sold_price=sold_price, sold_date=sold_date,
        listing_url=url, image_url=image_url, title=title,
    )


def scrape_bat_sold(max_pages=50):
    """Scrape BaT recent completed auctions using JSON API with nonce auth.
    
    Fetches newest pages first (page 1 = most recent auctions).
    Stops early when hitting URLs already in sold_comps.
    Designed for daily incremental use — typically only needs 1-3 pages.
    """
    import requests as _req

    API_URL = "https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter"
    BAT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer":    "https://bringatrailer.com/porsche/",
        "Accept":     "application/json",
    }
    comps = []

    # Load known URLs for early-stop — normalize by stripping trailing slash
    try:
        _conn = db.get_conn()
        known_urls = set(
            r[0].rstrip('/') for r in _conn.execute(
                "SELECT listing_url FROM sold_comps WHERE listing_url IS NOT NULL"
            ).fetchall()
        )
        _conn.close()
    except Exception:
        known_urls = set()

    # Fetch nonce from BaT Porsche page
    nonce = None
    try:
        session = _req.Session()
        r = session.get("https://bringatrailer.com/porsche/", headers=BAT_HEADERS, timeout=20)
        m = re.search(r'"restNonce"\s*:\s*"([^"]+)"', r.text)
        if m:
            nonce = m.group(1)
            log.info("BaT comp scraper: got nonce %s", nonce[:8])
    except Exception as e:
        log.warning("BaT comp scraper: nonce fetch failed: %s", e)

    try:
        for page_num in range(1, max_pages + 1):
            params = [
                ("base_filter[keyword_s]", "Porsche"),
                ("base_filter[items_type]", "make"),
                ("page", page_num),
                ("per-page", 36),
                ("get_items", 1),
                ("get_stats", 0),
            ]
            headers = dict(BAT_HEADERS)
            if nonce:
                headers["X-WP-Nonce"] = nonce

            try:
                resp = session.get(API_URL, params=params, headers=headers, timeout=20)
                if resp.status_code != 200:
                    log.warning("BaT comp API page %d: status %d", page_num, resp.status_code)
                    break
                data = resp.json()
            except Exception as e:
                log.warning("BaT comp API page %d error: %s", page_num, e)
                break

            items = data.get("items") or []
            if not items:
                log.info("BaT comp API: no items on page %d — done", page_num)
                break

            # Filter to sold items only (have sold_text)
            sold_items = [i for i in items if i.get("sold_text")]
            if not sold_items:
                time.sleep(0.5)
                continue

            found = 0
            known_on_page = 0
            for item in sold_items:
                url = item.get("url") or ""
                url_norm = url.rstrip('/')
                if url_norm and url_norm in known_urls:
                    known_on_page += 1
                    continue  # skip but don't stop — page may have newer items too

                title = _clean(item.get("title") or "")
                # Extract mileage BEFORE stripping the prefix
                mileage = None
                mm = re.search(r'([\d,]+)(k)?-Mile', title, re.I)
                if mm:
                    v = int(mm.group(1).replace(',', ''))
                    mileage = v * 1000 if mm.group(2) else v
                # Strip leading mileage prefix for year/model parsing
                title = re.sub(r'^[\d,]+k?-Mile\s+', '', title, flags=re.I)
                year, make, model, trim = _parse_ymmt(title)
                if not year or year < 1950:  # comp scraper allows all Porsche eras
                    continue

                sold_text = item.get("sold_text") or ""
                # Distinguish actual sales from reserve-not-met (high bid only)
                # "Sold for USD $63,333" = actual sale
                # "Bid to USD $48,000"   = reserve not met, high bid only
                is_rnm = sold_text.strip().lower().startswith("bid to")
                sold_price = None
                pm = re.search(r'\$([\d,]+)', sold_text)
                if pm:
                    amount = _int(pm.group(1).replace(',', ''))
                    if is_rnm:
                        # Store high bid separately; sold_price stays NULL
                        pass
                    else:
                        sold_price = amount
                # Fallback to current_bid only for actual sales
                if not sold_price and not is_rnm:
                    sold_price = _int(str(item.get("current_bid") or ""))

                # Extract date from sold_text
                sold_date = None
                dm = re.search(r'on\s+(\d{1,2}/\d{1,2}/\d{4})', sold_text)
                if dm:
                    try:
                        from datetime import datetime as _dt
                        sold_date = _dt.strptime(dm.group(1), '%m/%d/%Y').strftime('%Y-%m-%d')
                    except Exception:
                        pass

                image_url = item.get("thumbnail_url") or None

                comp = dict(year=year, make=make or "Porsche", model=model,
                            trim=trim, mileage=mileage, sold_price=sold_price,
                            sold_date=sold_date or None, listing_url=url,
                            source="BaT", transmission=None, engine=None,
                            color=None, vin=None, image_url=image_url)

                # Simple validity for sold comps: need year and a Porsche model
                if not year or not model:
                    continue
                # Skip excluded models (SUVs, sedans, EVs — not relevant to FMV scoring)
                EXCLUDED_MODELS = {'Cayenne', 'Panamera', 'Macan', 'Taycan'}
                if model in EXCLUDED_MODELS:
                    continue
                # Skip non-car items (parts, go-karts, tool kits)
                skip_keywords = ['go-kart', 'parts', 'tool kit', 'engine', 'wheels', 'collection']
                if any(kw in title.lower() for kw in skip_keywords):
                    continue

                comps.append(comp)
                found += 1

            log.info("BaT comp API page %d: %d new comps (%d already known)", page_num, found, known_on_page)

            # Stop when page is entirely known comps — we've caught up
            if known_on_page == len(sold_items) and len(sold_items) > 0:
                log.info("BaT comp API: page %d fully known — stopping", page_num)
                break

            time.sleep(0.5)

    except Exception as e:
        log.warning("BaT sold scraper error: %s", e)

    return comps


# ---------------------------------------------------------------------------
# PCA Mart sold/expired comps
# ---------------------------------------------------------------------------

def scrape_pcamart_sold(max_pages=60):
    """Scrape PCA Mart for sold/expired vehicle ads using Playwright."""
    if not _playwright_available():
        log.warning("PCA Mart sold comps require Playwright")
        return []

    BASE = "https://mart.pca.org"
    # Include expired/sold ads by adding adStatus parameter
    FORM_TEMPLATE = (
        "zipGeo=&searchInput=&yearRange=1950;2026&startYear=&endYear="
        "&priceRange=0;500000&minPrice=&maxPrice=&region=0&zipCode="
        "&fahrvergnugen=&adStatus=sold&sortOrder=DESC&sortBy=lastUpdated&perPage=20"
        "&startPageNumber={page}"
    )
    comps = []

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
            model = _clean(row.get("MODEL"))
            trim = _clean(row.get("TRIM") or row.get("SUBMODEL"))
            mileage = _int(row.get("MILEAGE"))
            sold_price = _int(row.get("PRICE") or row.get("ASKINGPRICE"))
            adnum = row.get("ADNUMBER")
            url = f"{BASE}/ads/{adnum}" if adnum else ""
            img_name = _clean(row.get("MAINIMAGENAME"))
            image_url = f"{BASE}/includes/images/ads/{img_name}.jpg" if img_name else None
            # Sold date from UPDATEDATE or ENDDATE
            sold_date = _clean(row.get("ENDDATE") or row.get("UPDATEDATE"))
            title = _clean(row.get("TITLE") or row.get("ADTITLE"))
            if not year:
                continue
            c = dict(year=year, make=make, model=model, trim=trim,
                     mileage=mileage, sold_price=sold_price, sold_date=sold_date,
                     listing_url=url, image_url=image_url, title=title)
            if _is_valid_listing(c):
                out.append(c)
        return out

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            pg = browser.new_page()

            captured = {}

            def on_response(resp):
                if "/search/" in resp.url and resp.status == 200:
                    try:
                        captured["page1"] = resp.json()
                    except Exception:
                        pass

            # Load with sold status
            pg.on("response", on_response)
            # POST directly since the default page load uses active ads
            pg.goto(f"{BASE}/", wait_until="networkidle", timeout=30000)

            # Trigger a sold-ads search via JS
            try:
                form0 = FORM_TEMPLATE.format(page=1).replace("'", "\\'")
                first = pg.evaluate(f"""
                    fetch('/search/', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: '{form0}'
                    }}).then(r => r.json())
                """)
            except Exception:
                first = captured.get("page1")

            if first:
                comps.extend(_parse_cf_page(first))
                total = (first.get("TOTALCOUNT") or first.get("totalCount") or
                         first.get("RECORDCOUNT") or 0)
                pages = max(1, (int(total) + 19) // 20) if total else 1

                for pg_num in range(2, min(pages + 1, max_pages + 1)):
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
                        comps.extend(batch)
                        time.sleep(0.3)
                    except Exception as e:
                        log.warning("PCA Mart sold page %d error: %s", pg_num, e)
                        break

            browser.close()
    except Exception as e:
        log.warning("PCA Mart sold scraper error: %s", e)

    return comps


# ---------------------------------------------------------------------------
# Classic.com sold comps
# ---------------------------------------------------------------------------

def scrape_classic_sold():
    """
    classic.com sold/completed Porsche listings via Playwright.
    URL: classic.com/search/?make=Porsche&status=sold
    NOTE: Cloudflare may block headless browsers — returns empty if challenged.
    """
    if not _playwright_available():
        log.warning("classic.com sold scraper requires Playwright")
        return []

    BASE = "https://www.classic.com"
    comps = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            pg = ctx.new_page()
            pg.goto(
                f"{BASE}/search/?make=Porsche&status=sold",
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
                "classic.com sold: Cloudflare challenge — no comps retrieved. "
                "Add a residential proxy or export browser cookies to unblock."
            )
            return []

        from bs4 import BeautifulSoup as _BS
        soup = _BS(html, "lxml")

        for card in soup.select(
            "article, [class*='listing-card'], [class*='vehicle-card'], [class*='car-card']"
        ):
            a = card.select_one("a[href]")
            if not a:
                continue
            url = a.get("href", "")
            if url and not url.startswith("http"):
                url = BASE + url

            title_el = card.select_one("h2, h3, [class*='title'], [class*='name']")
            raw_title = _clean(title_el.get_text()) if title_el else _clean(a.get_text())
            if not raw_title or "porsche" not in raw_title.lower():
                continue

            year, make, model, trim = _parse_ymmt(raw_title)
            if not year:
                continue

            # Mileage
            mileage = None
            for el in card.select("[class*='mile'], [class*='odometer'], [class*='mileage']"):
                mileage = _int(el.get_text())
                if mileage:
                    break

            # Sold price — look for price element
            sold_price = None
            for el in card.select("[class*='price'], [class*='sold'], [class*='bid']"):
                p = _int(el.get_text())
                if p and 1000 < p < 5_000_000:
                    sold_price = p
                    break

            # Date
            sold_date = None
            for el in card.select("time, [class*='date'], [class*='sold-date']"):
                dt = el.get("datetime") or el.get_text()
                import re as _re
                dm = _re.search(r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})", dt or "")
                if dm:
                    sold_date = dm.group(1)
                    break

            img = card.select_one("img[src]")
            image_url = (img.get("src") or "").split("?")[0] if img else None

            c = dict(year=year, make=make or "Porsche", model=model, trim=trim,
                     mileage=mileage, sold_price=sold_price)
            if not _is_valid_listing(c):
                continue

            comps.append(dict(
                source="classic.com",
                year=year, make=make or "Porsche", model=model, trim=trim,
                mileage=mileage, sold_price=sold_price, sold_date=sold_date,
                listing_url=url, image_url=image_url, title=raw_title,
            ))

        log.info("classic.com: %d sold comps found", len(comps))
    except Exception as e:
        log.warning("classic.com sold scraper error: %s", e)

    return comps


# ---------------------------------------------------------------------------
# Hagerty Valuation Tool scraper
# ---------------------------------------------------------------------------

# Representative models per generation to scrape from Hagerty.
# trim_kw must appear in the Hagerty URL path for that trim.
HAGERTY_TARGETS = [
    # 964 (1989–1994)
    {"gen": "964",       "year": 1990, "model_slug": "911",         "model": "911",     "trim": "Carrera 2",  "trim_kw": "carrera_2"},
    {"gen": "964",       "year": 1994, "model_slug": "911",         "model": "911",     "trim": "Carrera",    "trim_kw": "carrera"},
    # 993 (1995–1998)
    {"gen": "993",       "year": 1995, "model_slug": "911",         "model": "911",     "trim": "Carrera",    "trim_kw": "carrera"},
    {"gen": "993",       "year": 1996, "model_slug": "911",         "model": "911",     "trim": "Turbo",      "trim_kw": "turbo"},
    {"gen": "993",       "year": 1995, "model_slug": "911",         "model": "911",     "trim": "Carrera RS", "trim_kw": "carrera_rs"},
    # 996 (1999–2004)
    {"gen": "996",       "year": 2001, "model_slug": "911",         "model": "911",     "trim": "Carrera",    "trim_kw": "carrera"},
    {"gen": "996",       "year": 2001, "model_slug": "911",         "model": "911",     "trim": "Turbo",      "trim_kw": "turbo"},
    {"gen": "996",       "year": 2004, "model_slug": "911",         "model": "911",     "trim": "GT3",        "trim_kw": "gt3"},
    # 997 (2005–2012)
    {"gen": "997",       "year": 2007, "model_slug": "911",         "model": "911",     "trim": "Carrera S",  "trim_kw": "carrera_s"},
    {"gen": "997",       "year": 2007, "model_slug": "911",         "model": "911",     "trim": "Turbo",      "trim_kw": "turbo"},
    {"gen": "997",       "year": 2007, "model_slug": "911",         "model": "911",     "trim": "GT3",        "trim_kw": "gt3"},
    {"gen": "997",       "year": 2011, "model_slug": "911",         "model": "911",     "trim": "GT3 RS",     "trim_kw": "gt3_rs"},
    # 991 (2012–2019)
    {"gen": "991",       "year": 2014, "model_slug": "911",         "model": "911",     "trim": "Carrera S",  "trim_kw": "carrera_s"},
    {"gen": "991",       "year": 2015, "model_slug": "911",         "model": "911",     "trim": "GT3",        "trim_kw": "gt3"},
    {"gen": "991",       "year": 2014, "model_slug": "911",         "model": "911",     "trim": "Turbo S",    "trim_kw": "turbo_s"},
    # 992 (2019+)
    {"gen": "992",       "year": 2020, "model_slug": "911",         "model": "911",     "trim": "Carrera S",  "trim_kw": "carrera_s"},
    {"gen": "992",       "year": 2022, "model_slug": "911",         "model": "911",     "trim": "GT3",        "trim_kw": "gt3"},
    {"gen": "992",       "year": 2021, "model_slug": "911",         "model": "911",     "trim": "Turbo S",    "trim_kw": "turbo_s"},
    # Cayman GT4
    {"gen": "Cayman GT4","year": 2016, "model_slug": "cayman",      "model": "Cayman",  "trim": "GT4",        "trim_kw": "gt4"},
    {"gen": "Cayman GT4","year": 2021, "model_slug": "718",         "model": "Cayman",  "trim": "GT4",        "trim_kw": "gt4"},
    # Boxster Spyder
    {"gen": "Boxster Spyder","year": 2016, "model_slug": "boxster", "model": "Boxster", "trim": "Spyder",     "trim_kw": "spyder"},
    {"gen": "Boxster Spyder","year": 2020, "model_slug": "718",     "model": "Boxster", "trim": "Spyder",     "trim_kw": "spyder"},
]


def _hagerty_extract_prices(soup):
    """
    Extract (good_price, excellent_price) from a Hagerty vehicle detail page.
    Good condition (#3) is publicly available.
    Excellent (#2) requires a free Hagerty account session cookie.
    Returns (int|None, int|None).
    """
    good_price = None
    excellent_price = None
    for chunk in soup.select("[class*='conditionChunkWrapper']"):
        label_el = chunk.select_one("[class*='conditionLabelShort']")
        value_el = chunk.select_one("[class*='conditionValue']")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).lower()
        if value_el:
            price = _int(value_el.get_text(strip=True))
            if price and price > 0:
                if "good" in label:
                    good_price = price
                elif "excellent" in label:
                    excellent_price = price
    return good_price, excellent_price


def scrape_hagerty_valuations():
    """
    Scrape Hagerty Valuation Tool for Good and Excellent condition prices
    for the models defined in HAGERTY_TARGETS.

    Publicly available (no auth): #3 Good condition.
    #2 Excellent requires a free Hagerty account. Set the HAGERTY_SESSION_TOKEN
    environment variable to the value of the 'next-auth.session-token' cookie
    from a logged-in Hagerty browser session to unlock Excellent prices.

    Returns list of dicts:
        {year, model, trim, gen, condition_good, condition_excellent, url}
    """
    if not _playwright_available():
        log.warning("Hagerty scraper requires Playwright")
        return []

    session_token = os.environ.get("HAGERTY_SESSION_TOKEN", "")
    BASE = "https://www.hagerty.com/valuation-tools"
    results = []

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            if session_token:
                ctx.add_cookies([{
                    "name": "next-auth.session-token",
                    "value": session_token,
                    "domain": "www.hagerty.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                }])
                log.info("Hagerty: using session token — Excellent prices may be available")
            else:
                log.info("Hagerty: no HAGERTY_SESSION_TOKEN — only Good condition will be collected")

            pg = ctx.new_page()

            for t in HAGERTY_TARGETS:
                year       = t["year"]
                slug       = t["model_slug"]
                trim_kw    = t["trim_kw"]
                trim_label = t["trim"]
                model      = t["model"]
                gen        = t["gen"]

                try:
                    # Step 1: load year-level page and find trim links
                    year_url = f"{BASE}/porsche/{slug}/{year}"
                    pg.goto(year_url, wait_until="domcontentloaded", timeout=25000)
                    try:
                        pg.wait_for_selector("a[href*='/valuation-tools/porsche/']", timeout=8000)
                    except Exception:
                        pass

                    year_html = pg.content()
                    year_soup = BeautifulSoup(year_html, "lxml")

                    # Collect all trim links for this year
                    all_links = [
                        a.get("href", "")
                        for a in year_soup.select("a[href*='/valuation-tools/porsche/']")
                        if f"/{year}/" in a.get("href", "")
                    ]

                    # Prefer links matching trim_kw; fall back to first available
                    matching = [l for l in all_links if trim_kw in l.lower()]
                    if not matching:
                        # Looser: just first link for this year
                        matching = all_links[:1]
                    if not matching:
                        log.warning("Hagerty: no link found for %s/%s/%s (kw=%s)", slug, year, trim_label, trim_kw)
                        continue

                    # Step 2: load the first matching vehicle page
                    trim_url = matching[0]
                    if not trim_url.startswith("http"):
                        trim_url = "https://www.hagerty.com" + trim_url

                    pg.goto(trim_url, wait_until="networkidle", timeout=30000)
                    try:
                        pg.wait_for_selector("[class*='conditionChunkWrapper']", timeout=12000)
                    except Exception:
                        pass

                    detail_soup = BeautifulSoup(pg.content(), "lxml")
                    good_price, excellent_price = _hagerty_extract_prices(detail_soup)

                    if good_price:
                        log.info(
                            "Hagerty %s %d %s %s — Good: $%s  Excellent: %s",
                            gen, year, model, trim_label,
                            f"{good_price:,}",
                            f"${excellent_price:,}" if excellent_price else "locked (no session)",
                        )
                        results.append(dict(
                            year=year, model=model, trim=trim_label, gen=gen,
                            condition_good=good_price,
                            condition_excellent=excellent_price,
                            url=trim_url,
                        ))
                    else:
                        log.warning("Hagerty: no Good price at %s", trim_url)

                    time.sleep(1.2)

                except Exception as e:
                    log.warning("Hagerty error %s %d %s: %s", slug, year, trim_kw, e)

            browser.close()

    except Exception as e:
        log.error("Hagerty scraper failed: %s", e)

    log.info("Hagerty: %d valuations collected", len(results))
    return results


def run_hagerty_scrape():
    """Scrape Hagerty valuations and persist to DB. Run monthly."""
    db.init_db()
    conn = db.get_conn()
    valuations = scrape_hagerty_valuations()
    saved = 0
    for v in valuations:
        db.upsert_hagerty_valuation(
            conn,
            year=v["year"], model=v["model"], trim=v["trim"],
            generation=v["gen"],
            condition_good=v["condition_good"],
            condition_excellent=v["condition_excellent"],
            url=v["url"],
        )
        saved += 1
    conn.commit()
    conn.close()
    log.info("Hagerty scrape complete — %d valuations saved", saved)
    return saved



# ---------------------------------------------------------------------------
# Cars & Bids sold comps — daily incremental
# ---------------------------------------------------------------------------

def scrape_cnb_sold(days_back=14):
    """
    Scrape recently-ended Cars & Bids Porsche auctions and return sold comps.

    Strategy:
      1. Fetch sitemap to get all Porsche auction URLs
      2. Filter to ones not already in DB
      3. Scrape each via Playwright (v2/autos/auctions/{id} API)
      4. Keep only ended auctions with a sale_amount

    days_back: also used as a cap — skip auctions whose sitemap lastmod
    is older than this many days (sitemap is updated daily, so this
    keeps daily runs fast with only a handful of new pages).
    For the initial backfill use backfill_cnb_comps.py instead.
    """
    if not _playwright_available():
        log.warning("C&B comp scraper requires Playwright")
        return []

    from curl_cffi import requests as cr

    # 1. Fetch sitemap and get Porsche URLs + lastmod dates
    SITEMAP = "https://carsandbids.com/cab-sitemap/auctions.xml"
    try:
        r = cr.get(SITEMAP, impersonate="safari17_0", timeout=20)
        if r.status_code != 200:
            log.warning("C&B sitemap fetch failed: %d", r.status_code)
            return []
    except Exception as e:
        log.warning("C&B sitemap error: %s", e)
        return []

    # Parse URLs with their lastmod dates
    entries = re.findall(
        r"<url>.*?<loc>(https://carsandbids\.com/auctions/[^<]+)</loc>.*?(?:<lastmod>([^<]+)</lastmod>).*?</url>",
        r.text, re.DOTALL
    )
    if not entries:
        # Fallback: just get all URLs without dates
        urls_only = re.findall(r"<loc>(https://carsandbids\.com/auctions/[^<]+)</loc>", r.text)
        entries = [(u, None) for u in urls_only]

    # Filter to Porsche, exclude SUVs/sedans
    _EXCL = ("cayenne", "macan", "panamera", "taycan", "918-spyder")
    porsche_entries = [
        (url, lm) for url, lm in entries
        if "porsche" in url.lower() and not any(x in url.lower() for x in _EXCL)
    ]
    log.info("C&B sitemap: %d Porsche target URLs", len(porsche_entries))

    # 2. Filter to recently-modified entries (daily incremental)
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    recent = []
    for url, lm in porsche_entries:
        if lm:
            try:
                lm_dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
                if lm_dt >= cutoff:
                    recent.append(url)
            except Exception:
                recent.append(url)  # include if can't parse date
        else:
            recent.append(url)

    log.info("C&B: %d URLs modified in last %d days", len(recent), days_back)

    # 3. Filter out URLs already in DB
    try:
        _conn = db.get_conn()
        known = {r[0].rstrip("/") for r in _conn.execute(
            "SELECT listing_url FROM sold_comps WHERE source='Cars and Bids' AND listing_url IS NOT NULL"
        ).fetchall()}
        _conn.close()
    except Exception:
        known = set()

    new_urls = [u for u in recent if u.rstrip("/") not in known]
    log.info("C&B: %d new URLs to scrape", len(new_urls))

    if not new_urls:
        return []

    # 4. Scrape each via Playwright
    from playwright.sync_api import sync_playwright
    comps = []

    def _parse_title(title):
        m = re.match(r"(\d{4})\s+Porsche\s+(.*)", title, re.I)
        if not m:
            return None, None, None
        year = int(m.group(1))
        rest = m.group(2).strip()
        parts = rest.split(None, 1)
        return year, parts[0] if parts else rest, parts[1] if len(parts) > 1 else None

    _EXCL_MODELS = frozenset({"cayenne", "macan", "panamera", "taycan"})
    _TARGET_MODELS = frozenset({"911", "cayman", "boxster", "718", "930", "964",
                                "993", "996", "997", "991", "992", "gt3", "gt4"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        for url in new_urls:
            try:
                auction_info = {}

                def on_resp(resp):
                    rurl = resp.url
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct and "v2/autos/auctions/" in rurl:
                        try:
                            body = resp.json()
                            if isinstance(body, dict) and "stats" in body and "listing" in body:
                                auction_info["data"] = body
                        except Exception:
                            pass

                page.on("response", on_resp)
                page.goto(url, wait_until="networkidle", timeout=30000)
                time.sleep(0.8)
                page.remove_listener("response", on_resp)

                if not auction_info.get("data"):
                    continue

                data    = auction_info["data"]
                stats   = data.get("stats", {})
                listing = data.get("listing", {})
                sale_price = stats.get("sale_amount")
                if not sale_price:
                    continue

                title = listing.get("title", "")
                year, model, trim = _parse_title(title)
                if not year or not model:
                    continue

                model_low = model.lower()
                if any(x in model_low for x in _EXCL_MODELS):
                    continue
                if not any(x in model_low for x in _TARGET_MODELS):
                    continue
                if not (YEAR_MIN <= year <= YEAR_MAX):
                    continue

                mi_raw = listing.get("mileage")
                mileage = None
                if mi_raw:
                    m2 = re.search(r"([\d,]+)", str(mi_raw))
                    mileage = int(m2.group(1).replace(",", "")) if m2 else None

                tx_raw = listing.get("transmission")
                transmission = "Manual" if tx_raw == 2 else ("PDK" if tx_raw == 1 else None)

                sold_date = None
                ended_raw = stats.get("auction_end") or data.get("auction_end")
                if ended_raw:
                    try:
                        dt = datetime.fromisoformat(ended_raw.replace("Z", "+00:00"))
                        sold_date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        sold_date = str(ended_raw)[:10]

                img = listing.get("main_photo", {})
                image_url = None
                if isinstance(img, dict) and img.get("base_url") and img.get("path"):
                    image_url = "https://{}/{}".format(img["base_url"], img["path"])

                comps.append(dict(
                    year=year, make="Porsche", model=model, trim=trim,
                    mileage=mileage, transmission=transmission,
                    sold_price=sale_price, sold_date=sold_date,
                    listing_url=url, image_url=image_url, title=title,
                ))
                log.info("C&B comp: %d %s %s $%s", year, model, trim or "", sale_price)

            except Exception as e:
                log.debug("C&B comp error %s: %s", url[-40:], e)

            time.sleep(0.5)

        browser.close()

    log.info("C&B comp scrape: %d new sold comps", len(comps))
    return comps

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_comp_scrape():
    """Run all sold-comp scrapers and persist to DB."""
    db.init_db()
    conn = db.get_conn()

    total = 0

    log.info("Scraping BaT sold comps…")
    try:
        bat_comps = scrape_bat_sold(max_pages=50)
        log.info("  BaT: %d qualifying sold comps", len(bat_comps))
        for c in bat_comps:
            db.upsert_sold_comp(
                conn, source="BaT",
                year=c["year"], make=c.get("make"), model=c.get("model"),
                trim=c.get("trim"), mileage=c.get("mileage"),
                sold_price=c.get("sold_price"), sold_date=c.get("sold_date"),
                listing_url=c.get("listing_url"), image_url=c.get("image_url"),
                title=c.get("title"),
            )
        conn.commit()
        total += len(bat_comps)
    except Exception as e:
        log.error("BaT comp scrape failed: %s", e)

    log.info("Scraping PCA Mart sold comps…")
    try:
        pca_comps = scrape_pcamart_sold(max_pages=60)
        log.info("  PCA Mart: %d qualifying sold comps", len(pca_comps))
        for c in pca_comps:
            db.upsert_sold_comp(
                conn, source="PCA Mart",
                year=c["year"], make=c.get("make"), model=c.get("model"),
                trim=c.get("trim"), mileage=c.get("mileage"),
                sold_price=c.get("sold_price"), sold_date=c.get("sold_date"),
                listing_url=c.get("listing_url"), image_url=c.get("image_url"),
                title=c.get("title"),
            )
        conn.commit()
        total += len(pca_comps)
    except Exception as e:
        log.error("PCA Mart comp scrape failed: %s", e)

    log.info("Scraping Cars & Bids sold comps (incremental, last 14 days)…")
    try:
        cnb_comps = scrape_cnb_sold(days_back=14)
        log.info("  C&B: %d new sold comps", len(cnb_comps))
        for c in cnb_comps:
            db.upsert_sold_comp(
                conn, source="Cars and Bids",
                year=c["year"], make=c.get("make"), model=c.get("model"),
                trim=c.get("trim"), mileage=c.get("mileage"),
                sold_price=c.get("sold_price"), sold_date=c.get("sold_date"),
                listing_url=c.get("listing_url"), image_url=c.get("image_url"),
                title=c.get("title"), transmission=c.get("transmission"),
            )
        conn.commit()
        total += len(cnb_comps)
    except Exception as e:
        log.error("C&B comp scrape failed: %s", e)

    log.info("Scraping Classic.com sold comps…")
    try:
        classic_comps = scrape_classic_sold()
        log.info("  Classic.com: %d qualifying sold comps", len(classic_comps))
        for c in classic_comps:
            db.upsert_sold_comp(
                conn, source="classic.com",
                year=c["year"], make=c.get("make"), model=c.get("model"),
                trim=c.get("trim"), mileage=c.get("mileage"),
                sold_price=c.get("sold_price"), sold_date=c.get("sold_date"),
                listing_url=c.get("listing_url"), image_url=c.get("image_url"),
                title=c.get("title"),
            )
        conn.commit()
        total += len(classic_comps)
    except Exception as e:
        log.error("Classic.com comp scrape failed: %s", e)

    # Auto-expiry: BaT comps older than 24 months removed (6000+ available, older ones add noise).
    # C&B, PCA Mart, pcarmarket kept indefinitely — small catalogs, every comp is valuable.
    try:
        cur = conn.execute(
            "DELETE FROM sold_comps WHERE source='Bring a Trailer' AND sold_date < date('now', '-24 months') AND sold_date IS NOT NULL"
        )
        expired = cur.rowcount
        conn.commit()
        if expired:
            log.info("Comp expiry: removed %d BaT comps older than 24 months", expired)
    except Exception as e:
        log.warning("Comp expiry failed: %s", e)

    conn.close()
    log.info("Comp scrape complete — %d total records written", total)
    return total


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    run_comp_scrape()

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

import db
from scraper import (
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
    """Scrape BaT completed auction results using Playwright."""
    if not _playwright_available():
        log.warning("BaT sold comps require Playwright")
        return []

    BASE = "https://bringatrailer.com"
    comps = []

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            pg = browser.new_page()

            for page_num in range(1, max_pages + 1):
                url = (f"{BASE}/auctions/results/?s=porsche&page={page_num}"
                       if page_num > 1
                       else f"{BASE}/auctions/results/?s=porsche")
                pg.goto(url, wait_until="networkidle", timeout=35000)
                try:
                    pg.wait_for_selector("div.listing-card", timeout=10000)
                except Exception:
                    pass

                html = pg.content()
                soup = BeautifulSoup(html, "lxml")
                cards = soup.select("div.listing-card, li.listing-item")

                if not cards:
                    log.info("BaT sold: no cards on page %d — stopping", page_num)
                    break

                found = 0
                for card in cards:
                    comp = _bat_parse_result_card(card, BASE)
                    if comp and _is_valid_listing(comp):
                        comps.append(comp)
                        found += 1

                log.info("BaT sold page %d: %d qualifying comps", page_num, found)
                time.sleep(1.0)

                # Stop if we've gone past YEAR_MIN (results are roughly chronological)
                years = [c["year"] for c in comps if c.get("year")]
                if years and min(years) < YEAR_MIN:
                    log.info("BaT sold: reached pre-%d listings, stopping", YEAR_MIN)
                    break

            browser.close()
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

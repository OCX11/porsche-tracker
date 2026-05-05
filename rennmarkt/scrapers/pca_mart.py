"""
rennmarkt/scrapers/pca_mart.py — PCA Mart scraper.
Extracted from scraper.py. Playwright required.
mart.pca.org — ColdFusion platform, POST /search/ returns column-oriented JSON.
"""
import re
import json
import time
import logging
import hashlib as _hl
from datetime import datetime as _dt
from pathlib import Path

from shared.scraper_utils import (
    _playwright_available, _pw_launch, _stealth_page,
    _int, _clean, _is_valid_listing, _dedupe,
)

log = logging.getLogger(__name__)

DEALER_NAME = "PCA Mart"

# Walk up from this file to find docs/img_cache/ and data/
def _find_project_dir():
    p = Path(__file__).resolve().parent
    for _ in range(6):
        if (p / "data").exists() and (p / "docs").exists():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent.parent  # fallback: project root

_PROJECT_DIR  = _find_project_dir()
_IMG_CACHE_DIR = _PROJECT_DIR / "docs" / "img_cache"


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
            title = _clean(row.get("TITLE")) or ""
            title_parts = title.split()
            if title_parts and re.match(r"^\d{4}$", title_parts[0]):
                title_parts = title_parts[1:]
            model = title_parts[0] if title_parts else None
            trim = " ".join(title_parts[1:]) or None
            mileage = _int(row.get("MILEAGE"))
            price = _int(row.get("VEHICLEPRICE") or row.get("PRICE") or row.get("ASKINGPRICE"))
            adnum = row.get("ADNUMBER")
            url = f"{BASE}/ads/{adnum}" if adnum else ""
            img_name = _clean(row.get("MAINIMAGENAME"))
            image_url = f"{BASE}/includes/images/martAdImages/{adnum}/{img_name}.jpg" if (img_name and adnum) else None
            last_updated = _clean(row.get("LASTUPDATED")) or ""
            date_fs = None
            if last_updated:
                try:
                    import datetime as _datetime
                    lu_clean = last_updated.replace(",", "").strip()
                    dt = _datetime.datetime.strptime(lu_clean, "%B %d %Y %H:%M:%S")
                    date_fs = dt.strftime("%Y-%m-%d")
                except Exception:
                    try:
                        date_fs = last_updated[:10] if len(last_updated) >= 10 else None
                    except Exception:
                        pass
            if not year:
                continue
            c = dict(year=year, make=make, model=model, trim=trim,
                     mileage=mileage, price=price, vin=None, listing_url=url, image_url=image_url,
                     date_first_seen=date_fs)
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

            # Login via pca.org to get an authenticated session for image downloads.
            _pca_cfg_path = _PROJECT_DIR / "data" / "pca_config.json"
            try:
                _cfg = json.loads(_pca_cfg_path.read_text())
                pg.goto("https://www.pca.org/login/mart/ads",
                        wait_until="domcontentloaded", timeout=30000)
                pg.get_by_role("textbox", name="Enter email").fill(_cfg["username"])
                pg.get_by_role("textbox", name="Password").fill(_cfg["password"])
                pg.get_by_role("button", name="Login").click()
                pg.wait_for_load_state("networkidle", timeout=20000)
                log.debug("PCA Mart: logged in, now at %s", pg.url)
            except Exception as _le:
                log.debug("PCA Mart: login step failed (%s), continuing unauthenticated", _le)

            pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
            try:
                pg.wait_for_response(lambda r: "/search/" in r.url and r.status == 200,
                                     timeout=8000)
            except Exception:
                pass

            first = captured.get("page1")
            if not first:
                log.debug("PCA Mart: interception missed page1, using evaluate fallback")
                try:
                    first = pg.evaluate(
                        """(body) => fetch('/search/', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                            body: body
                        }).then(r => r.json())""",
                        FORM_TEMPLATE.format(page=1)
                    )
                except Exception as e:
                    log.warning("PCA Mart fallback evaluate failed: %s", e)

            def _pca_fetch(page_num):
                body = FORM_TEMPLATE.format(page=page_num)
                return pg.evaluate(
                    """(body) => fetch('/search/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: body
                    }).then(r => r.json())""",
                    body
                )

            for _retry in range(3):
                _total = int((first.get("DATA") or {}).get("TOTALRECORDS", [0])[0] or 0) if first else 0
                if _total > 0:
                    break
                log.debug("PCA Mart: page1 returned 0 records, retrying in 3s (attempt %d/3)...",
                          _retry + 1)
                time.sleep(3)
                try:
                    first = _pca_fetch(1)
                except Exception as e:
                    log.warning("PCA Mart page1 retry %d failed: %s", _retry + 1, e)
                    first = None

            if first:
                cars.extend(_parse_cf_page(first))
                total = (first.get("DATA") or {}).get("TOTALRECORDS", [0])[0] or 0
                pages = max(1, (int(total) + 19) // 20) if total else 1
                log.info("PCA Mart: %d total records across %d pages", total, pages)

                for pg_num in range(2, min(pages + 1, 80)):
                    try:
                        result = _pca_fetch(pg_num)
                        batch = _parse_cf_page(result)
                        if not batch and pg_num > 5:
                            pass
                        cars.extend(batch)
                        time.sleep(0.2)
                    except Exception as e:
                        log.warning("PCA Mart page %d error: %s", pg_num, e)
                        break

            # Download images via the authenticated page context.
            _IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached_count = 0
            for car in cars:
                img = car.get("image_url")
                if not img or not img.startswith("http") or "/img_cache/" in img:
                    continue
                try:
                    ext = img.rsplit(".", 1)[-1].split("?")[0].lower()
                    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                        ext = "jpg"
                    fname = _hl.md5(img.encode()).hexdigest() + "." + ext
                    fpath = _IMG_CACHE_DIR / fname
                    if not fpath.exists():
                        resp = pg.context.request.get(
                            img,
                            headers={"Referer": BASE + "/"},
                            timeout=20000,
                        )
                        body = resp.body()
                        ct = resp.headers.get("content-type", "")
                        if resp.ok and "image/" in ct and len(body) > 5000:
                            fpath.write_bytes(body)
                            log.debug("PCA img cached %s (%d bytes)", fname, len(body))
                    if fpath.exists():
                        car["image_url_cdn"] = img
                        car["image_url"] = f"/img_cache/{fname}"
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

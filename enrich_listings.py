#!/usr/bin/env python3
"""
enrich_listings.py — Fill in missing price and mileage for active listings.

For each active listing where price IS NULL or mileage IS NULL, visit the
listing URL and attempt to extract the missing fields from the detail page.
Updates the DB in-place. Capped at 200 enrichments per run.

Run after main.py in run_daily.sh:
    python3 enrich_listings.py >> logs/cron.log 2>&1
"""
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "logs" / "enrich.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

import db
import scraper as sc

CAP = 200  # max enrichments per run


def _extract_price(soup):
    """Try several common price patterns from a detail page."""
    # Structured data / meta tags first (fastest, most reliable)
    for meta in soup.select('meta[property="product:price:amount"], '
                            'meta[itemprop="price"], '
                            'meta[name="twitter:data1"]'):
        v = sc._int(meta.get("content") or meta.get("value") or "")
        if v:
            return v

    # Common CSS patterns across dealer platforms
    for sel in (
        "[class*='price']:not([class*='msrp']):not([class*='strike']):not([class*='was'])",
        "[class*='asking']",
        "[class*='internet-price']",
        "[class*='sale-price']",
        "span[data-price]",
        ".price",
    ):
        el = soup.select_one(sel)
        if el:
            v = sc._int(el.get_text())
            if v and v > 1000:
                return v

    # JSON-LD structured data
    import json
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            d = json.loads(script.string or "")
            if isinstance(d, list):
                d = d[0]
            offers = d.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0]
            v = sc._int(str(offers.get("price") or ""))
            if v:
                return v
        except Exception:
            pass

    # Regex fallback: find "$XX,XXX" pattern in page text
    text = soup.get_text(" ")
    m = re.search(r"\$\s*([\d,]{5,10})", text)
    if m:
        v = sc._int(m.group(1))
        if v and v > 1000:
            return v

    return None


def _extract_mileage(soup):
    """Try several common mileage patterns from a detail page."""
    # Common element selectors
    for sel in (
        "[class*='miles']",
        "[class*='mileage']",
        "[class*='odometer']",
        "[itemprop='mileageFromOdometer']",
        "[class*='specs'] li",
    ):
        for el in soup.select(sel):
            v = sc._int(el.get_text())
            if v and 100 < v < 500_000:
                return v

    # JSON-LD
    import json
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            d = json.loads(script.string or "")
            if isinstance(d, list):
                d = d[0]
            v = sc._int(str(
                d.get("mileageFromOdometer") or
                (d.get("mileageFromOdometer") or {}).get("value") or ""
            ))
            if v and v > 0:
                return v
        except Exception:
            pass

    # Regex: "XX,XXX miles" or "XXk miles"
    text = soup.get_text(" ")
    m = re.search(r"([\d,]+)\s*(?:k)?\s*(?:miles?|mi\.)", text, re.I)
    if m:
        raw = m.group(1).replace(",", "")
        multiplier = 1000 if "k" in m.group(0).lower() else 1
        v = sc._int(raw)
        if v:
            v *= multiplier
        if v and 100 < v < 500_000:
            return v

    return None


def run_enrichment():
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, listing_url, dealer, year, make, model, price, mileage
            FROM listings
            WHERE status = 'active'
              AND listing_url IS NOT NULL
              AND listing_url != ''
              AND (price IS NULL OR mileage IS NULL)
            ORDER BY date_last_seen DESC
            LIMIT ?
        """, (CAP,)).fetchall()

    if not rows:
        log.info("enrich: nothing to enrich")
        return

    log.info("enrich: %d listings need price/mileage enrichment (cap=%d)", len(rows), CAP)
    enriched = 0
    failed = 0

    for row in rows:
        url = row["listing_url"]
        need_price   = row["price"]   is None
        need_mileage = row["mileage"] is None

        soup = sc.get(url, timeout=20)
        if not soup:
            failed += 1
            log.debug("enrich: could not fetch %s", url)
            continue

        updates = {}
        if need_price:
            p = _extract_price(soup)
            if p:
                updates["price"] = p
        if need_mileage:
            m = _extract_mileage(soup)
            if m:
                updates["mileage"] = m

        if not updates:
            log.debug("enrich: no data found at %s", url)
            continue

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [row["id"]]
        with db.get_conn() as conn:
            conn.execute(f"UPDATE listings SET {set_clause} WHERE id = ?", vals)

        enriched += 1
        log.info(
            "enrich [%s %s %s %s] id=%d: %s",
            row["year"], row["make"], row["model"],
            row["dealer"], row["id"],
            ", ".join(f"{k}={v}" for k, v in updates.items()),
        )

    with db.get_conn() as conn:
        still_missing = conn.execute("""
            SELECT COUNT(*) FROM listings
            WHERE status = 'active'
              AND (price IS NULL OR price = 0 OR mileage IS NULL OR mileage = 0)
        """).fetchone()[0]

    log.info("enrich: done — %d enriched, %d fetch failures out of %d candidates | %d active listings still missing price or mileage",
             enriched, failed, len(rows), still_missing)


if __name__ == "__main__":
    db.init_db()
    run_enrichment()

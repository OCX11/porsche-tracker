"""
backfill_comps.py — Historical sold-comp backfill.

Sources:
  BaT (Bring a Trailer)  — Paginated Porsche closed auctions, 24 months of history.
                           API: /wp-json/bringatrailer/1.0/data/listings-filter
                           Returns 24 Porsche-only items/page with sold_text in JSON.
                           Progress saved per-page; safe to interrupt and resume.
  Cars & Bids            — BLOCKED: Cloudflare (403). Requires residential proxy.
  Classic.com            — BLOCKED: Cloudflare (403). Requires residential proxy.

Run: python backfill_comps.py
All output goes to stdout; progress is committed per page so a restart is safe.
"""
import json
import random
import re
import time
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import requests

# Add project dir to path
sys.path.insert(0, str(Path(__file__).parent))
import db
from scraper import _int, _clean, _parse_ymmt

# Plain session for BaT — no proxy, no special headers.
# BaT does not block standard requests; the shared SESSION in scraper.py
# routes through a paid proxy which is not needed (and currently erroring) here.
BAT_SESSION = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

CUTOFF_DATE    = "2024-03-01"   # 24 months back from 2026-03-27
PROGRESS_FILE  = Path(__file__).parent / "data" / "backfill_progress.json"


def _load_progress():
    """Return saved progress dict, or a clean slate if missing / corrupt."""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            log.warning("Progress file unreadable — starting fresh.")
    return {"sources": {}, "totals": {"inserted": 0}}


def _save_progress(progress: dict):
    """Atomically write progress JSON (tmp + rename avoids corrupt files)."""
    progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=2))
    tmp.replace(PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_bat_result_text(text):
    """
    Parse BaT .item-results text like:
      'Sold for USD $443,069on 3/19/26'
      'Bid to USD $325,000on 3/20/26'
    Returns (sold_price_int, sold_date_iso, reserve_met_bool).
    """
    text = (text or "").replace("\u00a0", " ").strip()
    sold = text.lower().startswith("sold for")
    # Price
    pm = re.search(r"\$\s*([\d,]+)", text)
    price = _int(pm.group(1)) if pm else None
    # Date: M/D/YY  or  M/D/YYYY
    dm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    sold_date = None
    if dm:
        m, d, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        if y < 100:
            y += 2000
        try:
            sold_date = date(y, m, d).isoformat()
        except ValueError:
            pass
    return price, sold_date, sold


def _parse_bat_title(raw_title):
    """
    Strip mileage prefix ('34k-Mile', '34,123-Mile') from BaT title,
    extract mileage, then parse YMMT.
    Returns (year, make, model, trim, mileage).
    """
    title = (raw_title or "").strip()
    mileage = None
    mm = re.search(r"([\d,]+)(k)?-Mile", title, re.I)
    if mm:
        val = int(mm.group(1).replace(",", ""))
        mileage = val * 1000 if mm.group(2) else val
    clean = re.sub(r"[\d,]+k?-Mile\s+", "", title, flags=re.I).strip()
    # Also strip leading descriptors before "YEAR Porsche"
    clean = re.sub(r"^[^1-2]*?((?:19|20)\d{2}\s)", r"\1", clean)
    year, make, model, trim = _parse_ymmt(clean)
    return year, make, model, trim, mileage


# ---------------------------------------------------------------------------
# Comp-specific validity filter
# ---------------------------------------------------------------------------

# Only collect comps for the three model lines we actually trade.
# 718 is the current-gen Cayman/Boxster so it's included.
# Everything else (944, 928, 914, 912, 356, Cayenne, Macan, etc.) is skipped.
_COMP_ALLOWED_MODELS = frozenset({"911", "cayman", "boxster", "718"})

# Regex-based junk title filter (catches parts, manuals, accessories)
_COMP_JUNK_RE = re.compile(
    r"""
    \b(?:
        wheels?|tires?|brakes?|seats?|
        manual|manuals|literature|
        transaxle|gearbox|
        carburet|injection|fuel\s+pump|airbox|
        go.kart|replica(?!\s+(?:of|by|from))|kit\s+car|
        emblem|badge|poster|sign|
        hardtop\s*$
    )\b
    """,
    re.VERBOSE | re.IGNORECASE
)


def _is_valid_comp(car: dict, require_price: bool = True) -> bool:
    """Validity check for historical sold comps.

    Keeps:  911, Cayman, Boxster, 718 — the three model lines we trade.
            All variants, all years, all mileages.
    Drops:  Everything else (944, 928, 914, 912, 356, SUVs, sedans, junk).

    require_price=False allows reserve-not-met records through as floor signals.
    """
    make  = (car.get("make") or "").lower().strip()
    model = (car.get("model") or "").lower().strip()

    if not car.get("year") or not model:
        return False

    if require_price:
        price = car.get("sold_price")
        if not price or price <= 0:
            return False

    if make and make != "porsche":
        return False

    # Must be one of our three model lines
    if not any(allowed in model for allowed in _COMP_ALLOWED_MODELS):
        return False

    # Junk filter on title
    title = (car.get("title") or "").lower()
    if _COMP_JUNK_RE.search(title):
        return False

    return True


# ---------------------------------------------------------------------------
# BaT scraper — paginated Porsche closed auction history
# ---------------------------------------------------------------------------

_BAT_API_URL = "https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter"
_BAT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _bat_fetch_nonce():
    """Fetch a fresh nonce from the BaT Porsche page. Returns nonce string or None."""
    try:
        r = BAT_SESSION.get(
            "https://bringatrailer.com/porsche/",
            headers=_BAT_HEADERS, timeout=20,
        )
        r.raise_for_status()
        m = re.search(r'"restNonce"\s*:\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
        log.warning("BaT: could not find restNonce in page")
    except Exception as e:
        log.error("BaT: nonce fetch failed: %s", e)
    return None


def _bat_fetch_page(page_num, nonce=None):
    """Fetch one page of BaT Porsche closed auctions via the JSON API.

    Uses the wp-json listings-filter endpoint which:
      - Returns Porsche-only results (base_filter[keyword_s]=Porsche, items_type=make)
      - Includes sold_text, title, url, thumbnail_url fields directly in JSON
      - Supports clean pagination via ?page=N
      - Returns 24 items/page across ~1,247 pages (29,922 total Porsche auctions)

    Returns a dict with 'items', 'pages_total', 'page_current' keys, or None on error.
    """
    params = [
        ("base_filter[keyword_s]", "Porsche"),
        ("base_filter[items_type]", "make"),
        ("page", page_num),
        ("per_page", 24),
        ("get_items", 1),
        ("get_stats", 0),
    ]
    headers = dict(_BAT_HEADERS)
    if nonce:
        headers["X-WP-Nonce"] = nonce

    # Retry up to 4 times with exponential backoff on 429 / transient errors
    for attempt in range(4):
        try:
            r = BAT_SESSION.get(_BAT_API_URL, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                wait = 30 * (2 ** attempt)   # 30s, 60s, 120s, 240s
                log.warning("BaT page %d: 429 rate-limited — waiting %ds (attempt %d/4)",
                            page_num, wait, attempt + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if "items" not in data:
                log.warning("BaT page %d: unexpected response keys: %s",
                            page_num, list(data.keys()))
                return None
            return data
        except Exception as e:
            if attempt < 3:
                wait = 15 * (2 ** attempt)
                log.warning("BaT page %d: error %s — retrying in %ds", page_num, e, wait)
                time.sleep(wait)
            else:
                log.error("BaT page %d fetch failed after 4 attempts: %s", page_num, e)
    return None


def _parse_transmission(text):
    """Infer transmission from BaT title text. Returns 'manual', 'automatic', or None."""
    t = (text or "").lower()
    if re.search(r"\bmanual\b|\b[456]-?speed\b|\bgated\b", t):
        return "manual"
    if re.search(r"\bpdk\b|\bautomatic\b|\btiptronic\b|\bdsg\b", t):
        return "automatic"
    return None


def scrape_bat_backfill(conn, progress, save_progress_fn):
    """
    Paginate through BaT's Porsche closed auctions via the JSON API back to CUTOFF_DATE.

    Endpoint: /wp-json/bringatrailer/1.0/data/listings-filter
    Params:   base_filter[keyword_s]=Porsche, base_filter[items_type]=make,
              page=N, per_page=24, get_items=1, get_stats=0

    Returns all Porsche-only results (29,922 total / 1,247 pages) with sold_text,
    title, url, and thumbnail_url fully populated in the JSON — no HTML parsing needed.

    Inserts directly into the DB and saves progress after each page so a crash
    mid-run resumes from the next unprocessed page.
    Returns total inserted count for this run.
    """
    bat_state = progress["sources"].get("BaT", {})
    start_page = bat_state.get("last_page_completed", 0) + 1
    total_inserted = bat_state.get("inserted", 0)
    total_seen = bat_state.get("total_seen", 0)

    # Fetch a fresh nonce once — stays valid for the whole run
    nonce = _bat_fetch_nonce()
    if nonce:
        log.info("BaT: acquired nonce %s", nonce)
    else:
        log.warning("BaT: no nonce — proceeding without (may still work)")

    if start_page > 1:
        log.info("BaT: resuming from page %d (%d already inserted)", start_page, total_inserted)
    else:
        log.info("BaT: starting paginated backfill via JSON API — cutoff %s", CUTOFF_DATE)

    page_num = start_page
    hit_cutoff = False

    while not hit_cutoff:
        page_data = _bat_fetch_page(page_num, nonce=nonce)
        if not page_data:
            log.warning("BaT: page %d fetch failed — stopping", page_num)
            break

        items = page_data.get("items", [])
        if not items:
            log.info("BaT: page %d — no items returned, end of results", page_num)
            break

        if page_num == start_page:
            log.info("BaT: %s total items across %s pages",
                     page_data.get("items_total", "?"), page_data.get("pages_total", "?"))

        page_comps = 0
        page_inserted = 0
        page_skipped_date = 0
        page_skipped_filter = 0

        for item in items:
            try:
                raw_title   = (item.get("title") or "").strip()
                listing_url = (item.get("url") or "").strip()
                result_text = (item.get("sold_text") or "").strip()
                image_url   = (item.get("thumbnail_url") or "").split("?")[0] or None

                if not raw_title:
                    continue

                sold_price, sold_date, reserve_met = _parse_bat_result_text(result_text)

                # BaT results are reverse-chronological — once we hit a date before
                # the cutoff, everything from here on is older too.
                if sold_date and sold_date < CUTOFF_DATE:
                    page_skipped_date += 1
                    hit_cutoff = True
                    continue

                year, make, model, trim, mileage = _parse_bat_title(raw_title)
                make = make or "Porsche"
                transmission = _parse_transmission(raw_title)

                display_title = (result_text + " — " + raw_title) if result_text else raw_title

                # Validate — allow through even without price (RNM records)
                c = dict(year=year, make=make, model=model, trim=trim,
                         mileage=mileage, sold_price=sold_price, title=display_title)
                if not _is_valid_comp(c, require_price=False):
                    page_skipped_filter += 1
                    continue

                page_comps += 1
                before = conn.total_changes

                if reserve_met and sold_price:
                    # Completed sale
                    db.upsert_sold_comp(
                        conn,
                        source="BaT",
                        year=year, make=make, model=model, trim=trim,
                        mileage=mileage, sold_price=sold_price, sold_date=sold_date,
                        listing_url=listing_url, image_url=image_url, title=display_title,
                        transmission=transmission,
                    )
                else:
                    # Reserve not met — store as floor price signal (sold_price=None)
                    db.upsert_sold_comp(
                        conn,
                        source="BaT",
                        year=year, make=make, model=model, trim=trim,
                        mileage=mileage, sold_price=None, sold_date=sold_date,
                        listing_url=listing_url, image_url=image_url, title=display_title,
                        transmission=transmission,
                    )
                    if sold_price:
                        db.insert_bat_reserve_not_met(
                            conn,
                            title=display_title, year=year, model=model,
                            high_bid=sold_price, auction_date=sold_date,
                            listing_url=listing_url,
                        )

                if conn.total_changes > before:
                    page_inserted += 1

            except Exception as e:
                log.warning("BaT page %d: skipping malformed item: %s", page_num, e)

        conn.commit()
        total_inserted += page_inserted
        total_seen += page_comps

        log.info(
            "BaT page %3d: %2d comps  %2d new  |  skipped: %d cutoff  %d filtered  |  running total: %d",
            page_num, page_comps, page_inserted,
            page_skipped_date, page_skipped_filter, total_inserted,
        )

        # Save per-page progress — crash between pages resumes from the next one
        done = hit_cutoff or not items
        progress["sources"]["BaT"] = {
            "completed": done,
            "last_page_completed": page_num,
            "inserted": total_inserted,
            "total_seen": total_seen,
        }
        if done:
            progress["sources"]["BaT"]["completed_at"] = datetime.now().isoformat(timespec="seconds")
        progress["totals"]["inserted"] = sum(
            s.get("inserted", 0) for s in progress["sources"].values()
        )
        save_progress_fn(progress)

        if hit_cutoff:
            log.info("BaT: cutoff %s reached at page %d — done", CUTOFF_DATE, page_num)
            break

        page_num += 1
        delay = random.uniform(4, 7)
        log.debug("BaT: sleeping %.1fs before page %d", delay, page_num)
        time.sleep(delay)

    return total_inserted


# ---------------------------------------------------------------------------
# C&B stub (Cloudflare-blocked)
# ---------------------------------------------------------------------------

def scrape_carsandbids_backfill():
    """
    Cars & Bids past auctions.
    BLOCKED: Cloudflare protection returns 403/challenge for both static
    and headless Playwright requests.
    Resolution: use a residential proxy or manual cookie export.
    """
    log.warning("Cars & Bids: BLOCKED by Cloudflare — skipping.")
    log.warning("  To unblock: add a rotating residential proxy to SESSION or export cookies.")
    return []


# ---------------------------------------------------------------------------
# Classic.com stub (Cloudflare-blocked)
# ---------------------------------------------------------------------------

def scrape_classic_backfill():
    """
    Classic.com sold listings.
    BLOCKED: Cloudflare protection on both /m/porsche/ and API endpoints.
    Resolution: use a residential proxy or manual cookie export.
    """
    log.warning("Classic.com: BLOCKED by Cloudflare — skipping.")
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_backfill():
    db.init_db()
    conn = db.get_conn()

    progress = _load_progress()
    if "started_at" not in progress:
        progress["started_at"] = datetime.now().isoformat(timespec="seconds")
    if "totals" not in progress:
        progress["totals"] = {"inserted": 0}

    totals = {}

    # --- BaT: paginated, handles its own insertion and per-page progress ---
    log.info("=" * 60)
    bat_state = progress["sources"].get("BaT", {})
    # Only skip if completed by the paginated version (has last_page_completed).
    # Old single-page runs set completed=True without that key — re-run those.
    if bat_state.get("completed") and "last_page_completed" in bat_state:
        log.info("BaT: already completed (through page %d, %d inserted) — skipping",
                 bat_state["last_page_completed"], bat_state["inserted"])
        totals["BaT"] = bat_state["inserted"]
    else:
        try:
            totals["BaT"] = scrape_bat_backfill(conn, progress, _save_progress)
        except Exception as e:
            log.error("BaT: unexpected error: %s", e)
            totals["BaT"] = progress["sources"].get("BaT", {}).get("inserted", 0)

    # --- Cloudflare-blocked stubs ---
    for source_name, fn in [
        ("Cars & Bids", scrape_carsandbids_backfill),
        ("Classic.com", scrape_classic_backfill),
    ]:
        if progress["sources"].get(source_name, {}).get("completed"):
            log.info("%-15s already completed (%d inserted) — skipping",
                     source_name, progress["sources"][source_name]["inserted"])
            totals[source_name] = progress["sources"][source_name]["inserted"]
            continue

        log.info("=" * 60)
        log.info("Source: %s", source_name)
        try:
            fn()
        except Exception as e:
            log.error("%s: unexpected error: %s", source_name, e)

        totals[source_name] = 0
        progress["sources"][source_name] = {
            "completed": True,
            "inserted": 0,
            "total_seen": 0,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        progress["totals"]["inserted"] = sum(totals.values())
        _save_progress(progress)

    conn.close()

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE")
    log.info("  %-20s  records", "Source")
    for src, cnt in totals.items():
        log.info("  %-20s  %d", src, cnt)
    log.info("  %-20s  %d", "TOTAL", sum(totals.values()))
    log.info("")
    log.info("Note: C&B and Classic.com remain Cloudflare-blocked.")
    log.info("comp_scraper.py will continue accumulating comps going forward.")
    return totals


if __name__ == "__main__":
    run_backfill()

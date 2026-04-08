"""
apify_backfill.py — Pull Porsche sold auction data from Apify actors
                    and insert into sold_comps.

Sources:
  BaT (parseforge/bringatrailer-auctions-scraper)
  Cars & Bids (ivanvs/cars-and-bids-scraper)

Run: python apify_backfill.py
"""
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import db

# ---------------------------------------------------------------------------
# Config / Auth
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "data" / "apify_config.json"
APIFY_BASE  = "https://api.apify.com/v2"
CUTOFF_DATE = (date.today() - timedelta(days=183)).isoformat()  # ~6 months

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _load_token() -> str:
    """
    Return APIFY_API_TOKEN from env var or data/apify_config.json.
    Writes a placeholder config file if one doesn't exist yet.
    """
    # 1. Environment variable takes priority
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if token:
        return token

    # 2. Config file
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            token = cfg.get("APIFY_API_TOKEN", "").strip()
            if token and token != "YOUR_APIFY_API_TOKEN_HERE":
                return token
        except Exception:
            pass

    # 3. Write placeholder so the user knows where to put it
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({"APIFY_API_TOKEN": "YOUR_APIFY_API_TOKEN_HERE"}, indent=2))
        log.warning("Created %s — fill in your Apify API token there or set APIFY_API_TOKEN env var.", CONFIG_PATH)

    raise RuntimeError(
        f"No APIFY_API_TOKEN found.\n"
        f"  Option A: export APIFY_API_TOKEN=apify_api_...\n"
        f"  Option B: edit {CONFIG_PATH} and replace the placeholder."
    )


# ---------------------------------------------------------------------------
# Apify REST helpers
# ---------------------------------------------------------------------------

def _run_actor(token: str, actor_id: str, actor_input: dict) -> dict:
    """Trigger an actor run. Returns the run object dict."""
    url = f"{APIFY_BASE}/acts/{actor_id}/runs?token={token}"
    resp = requests.post(url, json=actor_input, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]


def _poll_run(token: str, actor_id: str, poll_interval: int = 10) -> dict:
    """Poll the last run for actor_id until SUCCEEDED or FAILED. Returns run dict."""
    url = f"{APIFY_BASE}/acts/{actor_id}/runs/last?token={token}"
    while True:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        run = resp.json()["data"]
        status = run.get("status", "")
        log.info("  Actor %s status: %s", actor_id, status)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return run
        time.sleep(poll_interval)


def _fetch_dataset(token: str, dataset_id: str, limit: int = 1000) -> list:
    """Paginate through an Apify dataset and return all items."""
    items = []
    offset = 0
    while True:
        url = (
            f"{APIFY_BASE}/datasets/{dataset_id}/items"
            f"?token={token}&limit={limit}&offset={offset}"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return items


def _run_and_collect(token: str, actor_id: str, actor_input: dict) -> list:
    """Trigger actor, wait for completion, return all dataset items."""
    log.info("Triggering actor: %s", actor_id)
    run = _run_actor(token, actor_id, actor_input)
    run_id = run.get("id", "?")
    log.info("  Run started: %s — polling for completion…", run_id)
    run = _poll_run(token, actor_id)
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Actor {actor_id} run {run_id} ended with status: {run.get('status')}")
    dataset_id = run.get("defaultDatasetId")
    log.info("  Fetching results from dataset: %s", dataset_id)
    return _fetch_dataset(token, dataset_id)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_year_from_title(title: str):
    """Extract first 4-digit year (1900-2099) from title string."""
    m = re.search(r"\b(19\d{2}|20\d{2})\b", title or "")
    return int(m.group(1)) if m else None


def _parse_mileage_from_listing_details(details: list):
    """
    Parse mileage from BaT listingDetails array.
    Looks for: "34k Miles", "34,123 Miles", "34K Miles"
    """
    for item in (details or []):
        m = re.search(r"([\d,]+)\s*k\s*Miles", item, re.I)
        if m:
            return int(m.group(1).replace(",", "")) * 1000
        m = re.search(r"([\d,]+)\s+Miles", item, re.I)
        if m:
            val = int(m.group(1).replace(",", ""))
            if val < 2000:          # "34 Miles" — treat as-is, not *1000
                return val
            return val
    return None


def _parse_transmission_from_listing_details(details: list):
    """
    Parse transmission from BaT listingDetails array.
    Returns 'manual', 'automatic', or None.
    """
    for item in (details or []):
        t = item.lower()
        if re.search(r"six.speed manual|five.speed manual|four.speed manual|"
                     r"\d.speed manual|\bmanual\b|\bgated\b", t):
            return "manual"
        if re.search(r"\bpdk\b|\bautomatic\b|\btiptronic\b", t):
            return "automatic"
    return None


def _parse_vin_from_listing_details(details: list):
    """Parse VIN/chassis number from BaT listingDetails (after 'Chassis: ')."""
    for item in (details or []):
        m = re.search(r"Chassis:\s*([A-HJ-NPR-Z0-9]{5,17})", item, re.I)
        if m:
            return m.group(1).strip()
    return None


def _parse_date_from_iso(dt_str: str):
    """Return YYYY-MM-DD from an ISO datetime string."""
    if not dt_str:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
    return m.group(1) if m else None


# Month name → number, for C&B endTime parsing
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_cab_end_time(end_time: str):
    """
    Parse C&B endTime like "Ended March 25th at 7:47 PM UTC" → "2026-03-25".
    Year is inferred: if the parsed month/day would be in the future, use
    the previous year.
    """
    if not end_time:
        return None
    m = re.search(
        r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?",
        end_time, re.I
    )
    if not m:
        return None
    month_name = m.group(1).lower()
    day = int(m.group(2))
    month = _MONTH_MAP.get(month_name)
    if not month:
        return None
    today = date.today()
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    # If candidate is in the future, it must be from last year
    if candidate > today:
        year -= 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
    return candidate.isoformat()


def _strip_porsche_prefix(model: str) -> str:
    """Remove leading 'Porsche ' from model string."""
    return re.sub(r"(?i)^porsche\s+", "", (model or "").strip())


def _parse_model_from_title(title: str) -> str:
    """
    For C&B: extract model from title by dropping the leading year token.
    E.g. "2002 Porsche 911 Carrera 4S Coupe" → "911 Carrera 4S Coupe"
    """
    title = (title or "").strip()
    # Drop leading year
    cleaned = re.sub(r"^\s*(19|20)\d{2}\s+", "", title)
    # Drop "Porsche " prefix
    cleaned = re.sub(r"(?i)^porsche\s+", "", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# BaT source
# ---------------------------------------------------------------------------

BAT_ACTOR   = "parseforge~bringatrailer-auctions-scraper"
BAT_INPUT   = {
    "startUrl": "https://bringatrailer.com/auctions/results/?s=porsche",
    "maxItems": 100,
}


def _map_bat_item(item: dict):
    """
    Map a raw BaT API item to a sold_comp dict.
    Returns None if the record should be skipped.
    """
    # Only ended auctions where reserve was met
    if item.get("auctionStatus") != "ended":
        return None
    if not item.get("reserveMet", False):
        return None

    sale_price = item.get("auctionResultHighBid") or 0
    if not sale_price or sale_price <= 0:
        return None

    title = item.get("title", "")
    # Porsche filter (actor already filters but be defensive)
    make_raw = (item.get("make") or "").strip()
    if make_raw and make_raw.lower() != "porsche":
        if "porsche" not in title.lower():
            return None

    sold_date = _parse_date_from_iso(item.get("auctionEndDate"))
    if sold_date and sold_date < CUTOFF_DATE:
        return None

    year = _parse_year_from_title(title) or item.get("year") or None
    listing_details = item.get("listingDetails") or []
    model_raw = _strip_porsche_prefix(item.get("model") or "")

    return {
        "source":       "BaT",
        "sale_price":   sale_price,
        "sold_date":    sold_date,
        "title":        title,
        "year":         year,
        "make":         "Porsche",
        "model":        model_raw,
        "mileage":      _parse_mileage_from_listing_details(listing_details),
        "transmission": _parse_transmission_from_listing_details(listing_details),
        "vin":          _parse_vin_from_listing_details(listing_details),
        "listing_url":  item.get("auctionUrl"),
    }


def process_bat(token: str) -> dict:
    """Run BaT actor and return stats dict."""
    stats = {
        "pulled": 0, "porsche_only": 0, "inserted": 0,
        "skipped_duplicate": 0, "skipped_no_price": 0,
        "skipped_reserve_not_met": 0, "skipped_old": 0,
        "rnm_inserted": 0, "rnm_duplicate": 0,
    }
    raw_items = _run_and_collect(token, BAT_ACTOR, BAT_INPUT)
    stats["pulled"] = len(raw_items)
    log.info("BaT: %d raw items fetched", stats["pulled"])

    db.init_db()
    conn = db.get_conn()

    for item in raw_items:
        status = item.get("auctionStatus")
        reserve_met = item.get("reserveMet", False)
        high_bid = item.get("auctionResultHighBid") or 0
        title = item.get("title", "")
        listing_url = item.get("auctionUrl")
        auction_date = _parse_date_from_iso(item.get("auctionEndDate"))

        # Route reserve-not-met into bat_reserve_not_met table
        if status == "ended" and not reserve_met and high_bid > 0:
            stats["skipped_reserve_not_met"] += 1
            if auction_date and auction_date < CUTOFF_DATE:
                stats["skipped_old"] += 1
                continue
            # Porsche filter
            make_raw = (item.get("make") or "").strip()
            if make_raw and make_raw.lower() != "porsche":
                if "porsche" not in title.lower():
                    continue
            year = _parse_year_from_title(title)
            model = _strip_porsche_prefix(item.get("model") or "")
            before = conn.total_changes
            db.insert_bat_reserve_not_met(
                conn,
                title=title,
                year=year,
                model=model,
                high_bid=high_bid,
                auction_date=auction_date,
                listing_url=listing_url,
                bids=item.get("auctionResultBids"),
            )
            conn.commit()
            if conn.total_changes > before:
                stats["rnm_inserted"] += 1
            else:
                stats["rnm_duplicate"] += 1
            continue

        comp = _map_bat_item(item)
        if comp is None:
            if not high_bid:
                stats["skipped_no_price"] += 1
            if auction_date and auction_date < CUTOFF_DATE:
                stats["skipped_old"] += 1
            continue

        stats["porsche_only"] += 1

        # Duplicate check
        if listing_url:
            exists = conn.execute(
                "SELECT 1 FROM sold_comps WHERE listing_url=? LIMIT 1", (listing_url,)
            ).fetchone()
            if exists:
                stats["skipped_duplicate"] += 1
                continue

        before = conn.total_changes
        db.upsert_sold_comp(
            conn,
            source=comp["source"],
            year=comp["year"],
            make=comp["make"],
            model=comp["model"],
            trim=None,
            mileage=comp["mileage"],
            sold_price=comp["sale_price"],
            sold_date=comp["sold_date"],
            listing_url=comp["listing_url"],
            title=comp["title"],
            transmission=comp["transmission"],
            vin=comp["vin"],
        )
        conn.commit()
        if conn.total_changes > before:
            stats["inserted"] += 1

    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Cars & Bids source
# ---------------------------------------------------------------------------

CAB_ACTOR = "ivanvs~cars-and-bids-scraper"
CAB_INPUT = {
    "urls": [{"url": "https://carsandbids.com/past-auctions/?q=porsche&sold=true"}],
    "onlyLiveAuctions": False,
    "maxNumberOfResults": 100,
}


def _map_cab_item(item: dict):
    """
    Map a raw C&B API item to a sold_comp dict.
    Returns None if the record should be skipped.
    """
    title = item.get("title", "")

    # Porsche filter
    if "porsche" not in title.lower():
        return None

    offer = item.get("offer") or {}
    sale_price = offer.get("price") or 0
    if not sale_price or sale_price <= 0:
        return None

    sold_date = _parse_cab_end_time(item.get("endTime", ""))
    if sold_date and sold_date < CUTOFF_DATE:
        return None

    year = _parse_year_from_title(title)
    model = _parse_model_from_title(title)

    return {
        "source":      "Cars & Bids",
        "sale_price":  sale_price,
        "sold_date":   sold_date,
        "title":       title,
        "year":        year,
        "make":        "Porsche",
        "model":       model,
        "listing_url": item.get("url"),
    }


def process_cab(token: str) -> dict:
    """Run Cars & Bids actor and return stats dict."""
    stats = {
        "pulled": 0, "porsche_only": 0, "inserted": 0,
        "skipped_duplicate": 0, "skipped_no_price": 0,
        "skipped_reserve_not_met": 0, "skipped_old": 0,
    }
    raw_items = _run_and_collect(token, CAB_ACTOR, CAB_INPUT)
    stats["pulled"] = len(raw_items)
    log.info("Cars & Bids: %d raw items fetched", stats["pulled"])

    # Pre-filter: keep only Porsche listings by title
    porsche_items = [i for i in raw_items if "porsche" in (i.get("title") or "").lower()]
    skipped_non_porsche = len(raw_items) - len(porsche_items)
    if skipped_non_porsche:
        log.info("Cars & Bids: dropped %d non-Porsche records (title filter)", skipped_non_porsche)

    db.init_db()
    conn = db.get_conn()

    for item in porsche_items:
        offer = item.get("offer") or {}
        sale_price = offer.get("price") or 0

        comp = _map_cab_item(item)
        if comp is None:
            if not sale_price:
                stats["skipped_no_price"] += 1
            sold_date = _parse_cab_end_time(item.get("endTime", ""))
            if sold_date and sold_date < CUTOFF_DATE:
                stats["skipped_old"] += 1
            continue

        stats["porsche_only"] += 1

        # Duplicate check
        listing_url = comp["listing_url"]
        if listing_url:
            exists = conn.execute(
                "SELECT 1 FROM sold_comps WHERE listing_url=? LIMIT 1", (listing_url,)
            ).fetchone()
            if exists:
                stats["skipped_duplicate"] += 1
                continue

        before = conn.total_changes
        db.upsert_sold_comp(
            conn,
            source=comp["source"],
            year=comp["year"],
            make=comp["make"],
            model=comp["model"],
            trim=None,
            mileage=None,
            sold_price=comp["sale_price"],
            sold_date=comp["sold_date"],
            listing_url=comp["listing_url"],
            title=comp["title"],
        )
        conn.commit()
        if conn.total_changes > before:
            stats["inserted"] += 1

    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _log_stats(source: str, stats: dict):
    msg = ("%-16s  pulled=%-4d  porsche=%-4d  inserted=%-4d  "
           "dup=%-4d  no_price=%-4d  reserve_not_met=%-4d  old=%-4d")
    args = [source, stats["pulled"], stats["porsche_only"], stats["inserted"],
            stats["skipped_duplicate"], stats["skipped_no_price"],
            stats["skipped_reserve_not_met"], stats["skipped_old"]]
    if "rnm_inserted" in stats:
        msg += "  rnm_inserted=%-4d  rnm_dup=%-4d"
        args += [stats["rnm_inserted"], stats["rnm_duplicate"]]
    log.info(msg, *args)


def main():
    token = _load_token()
    log.info("Apify backfill starting — cutoff date: %s", CUTOFF_DATE)

    all_stats = {}

    log.info("=" * 60)
    log.info("Source: BaT")
    try:
        all_stats["BaT"] = process_bat(token)
    except Exception as e:
        log.error("BaT failed: %s", e)
        all_stats["BaT"] = {}

    log.info("=" * 60)
    log.info("Source: Cars & Bids")
    try:
        all_stats["Cars & Bids"] = process_cab(token)
    except Exception as e:
        log.error("Cars & Bids failed: %s", e)
        all_stats["Cars & Bids"] = {}

    log.info("=" * 60)
    log.info("SUMMARY")
    for source, stats in all_stats.items():
        if stats:
            _log_stats(source, stats)
        else:
            log.info("%-16s  (failed — no data)", source)

    total_inserted = sum(s.get("inserted", 0) for s in all_stats.values())
    log.info("Total inserted: %d", total_inserted)


if __name__ == "__main__":
    main()

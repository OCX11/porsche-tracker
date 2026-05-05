#!/usr/bin/env python3
"""
rennmarkt/main.py — RennMarkt entry point.

Scrapes 7 retail sources every 2 minutes and writes listings to the shared DB.
Sources: DuPont Registry, eBay Motors, cars.com, AutoTrader, PCA Mart, Rennlist, Built for Backroads.

Usage:
  python3 rennmarkt/main.py              # Full scrape + dashboard + alerts
  python3 rennmarkt/main.py --mode deep  # Deep scrape (3 pages for paginated sources)
  python3 rennmarkt/main.py --dashboard  # Regenerate dashboard only
"""
import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Add project root to sys.path so core/, shared/, rennmarkt/ all resolve
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "rennmarkt.log"

SCRAPE_LOG_DIR = PROJECT_ROOT / "data" / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Imports after logging ─────────────────────────────────────────────────────
import core.db as database
from rennmarkt.scrapers.dupont    import scrape_dupont
from rennmarkt.scrapers.ebay      import scrape_ebay
from rennmarkt.scrapers.carscom   import scrape_carscom
from rennmarkt.scrapers.autotrader import scrape_autotrader
from rennmarkt.scrapers.pca_mart  import scrape_pcamart
from rennmarkt.scrapers.rennlist  import scrape_rennlist
from rennmarkt.scrapers.bfb       import scrape_bfb
from rennmarkt import notify_push
import rennmarkt.build_dashboard as ndash

# Shared utilities
from shared.scraper_utils import _is_valid_listing
import core.fmv as fmv_engine
import core.vin_decoder as vin_decoder
import enrich_vin_trim
import enrich_from_archive
import promote_auction_comps
import health_monitor


DEALERS = [
    {"name": "DuPont Registry",     "scrape": scrape_dupont},
    {"name": "eBay Motors",         "scrape": scrape_ebay},
    {"name": "cars.com",            "scrape": scrape_carscom},
    {"name": "AutoTrader",          "scrape": scrape_autotrader},
    {"name": "PCA Mart",            "scrape": scrape_pcamart},
    {"name": "Rennlist",            "scrape": scrape_rennlist},
    {"name": "Built for Backroads", "scrape": scrape_bfb},
]

_PAGINATED_DEALERS = {"AutoTrader", "cars.com", "eBay Motors", "DuPont Registry"}


def _run_all(dealers, max_pages=None) -> dict:
    import time, traceback
    results = {}
    for d in dealers:
        name = d["name"]
        log.info("Scraping %s…", name)
        try:
            if max_pages is not None and name in _PAGINATED_DEALERS:
                raw = d["scrape"](max_pages=max_pages)
            else:
                raw = d["scrape"]()
            cars = [c for c in raw if _is_valid_listing(c)]
            for car in cars:
                car["tier"] = database.classify_tier(car.get("model"), car.get("trim"), car.get("year"))
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


def write_scrape_summary(results: dict, today: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = sum(len(v) for v in results.values())
    zero_sources = [name for name, cars in results.items() if not cars]

    lines = [f"=== RennMarkt scrape {timestamp} ==="]
    for name, cars in sorted(results.items()):
        count = len(cars)
        flag = "  [check logs]" if count == 0 else ""
        lines.append(f"  {name:<35} {count:>4}{flag}")
    lines.append(f"  {'---':<35}")
    zero_note = f"  ({len(zero_sources)} zero)" if zero_sources else ""
    lines.append(f"  {'TOTAL':<35} {total:>4}  ({len(results)} sources){zero_note}")
    lines.append("")

    summary = "\n".join(lines)
    SCRAPE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SCRAPE_LOG_DIR / f"scrape_{today}.log"
    with open(log_path, "a") as f:
        f.write(summary + "\n")
    print("\n" + summary)


def run_snapshot(dealer_results: dict, today: str):
    """Persist scraped data and update listings/snapshots."""
    with database.get_conn() as conn:
        new_total = updated_total = sold_total = 0
        new_ids = []

        for dealer_name, cars in dealer_results.items():
            if not cars:
                log.warning("  [%s] 0 cars scraped — skipping sold-marking", dealer_name)
                continue

            active_keys = set()
            for car in cars:
                vin = car.get("vin")
                listing_url = car.get("listing_url") or car.get("url")
                key = vin if vin else (
                    listing_url or
                    f"{car.get('year')}|{car.get('make')}|"
                    f"{car.get('model')}|{car.get('mileage')}|{car.get('price')}"
                )
                active_keys.add(key)

                try:
                    listing_id, is_new, price_changed = database.upsert_listing(
                        conn,
                        dealer=dealer_name,
                        year=car.get("year"),
                        make=car.get("make"),
                        model=car.get("model"),
                        trim=car.get("trim"),
                        mileage=car.get("mileage"),
                        price=car.get("price"),
                        vin=car.get("vin"),
                        url=car.get("listing_url") or car.get("url"),
                        today=today,
                        image_url=car.get("image_url"),
                        location=car.get("location"),
                        transmission=car.get("transmission"),
                        color=car.get("color"),
                        body_style=car.get("body_style"),
                        drivetrain=car.get("drivetrain"),
                        engine=car.get("engine"),
                        date_first_seen=car.get("date_first_seen"),
                        auction_ends_at=car.get("auction_ends_at"),
                        image_url_cdn=car.get("image_url_cdn"),
                    )
                    if is_new:
                        new_total += 1
                        new_ids.append(listing_id)
                    elif price_changed:
                        updated_total += 1
                except Exception as e:
                    log.error("DB upsert error [%s]: %s", dealer_name, e)

            database.save_snapshot(conn, today, dealer_name, cars)

            currently_active = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE dealer=? AND status='active'",
                (dealer_name,)
            ).fetchone()[0]
            min_threshold = max(5, int(currently_active * 0.5))
            if len(cars) < min_threshold:
                log.warning("  [%s] Only %d cars scraped vs %d active — skipping sold-marking",
                            dealer_name, len(cars), currently_active)
                continue

            before = currently_active
            database.mark_sold(conn, dealer_name, active_keys, today)

            after = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE dealer=? AND status='active'",
                (dealer_name,)
            ).fetchone()[0]
            sold_this_dealer = before - after
            if sold_this_dealer:
                log.info("  [%s] %d marked sold", dealer_name, sold_this_dealer)
                sold_total += sold_this_dealer

        log.info("Snapshot complete — new: %d  price changes: %d  sold: %d",
                 new_total, updated_total, sold_total)
        archived = database.archive_stale_listings(conn, days=90)
        if archived:
            log.info("Archived %d stale listings (90d rule)", archived)
        cleaned = database.cleanup_stale_retail_listings(conn, days=14)
        if cleaned:
            log.info("Staleness cleanup: marked %d retail listings sold (not seen 14d)", cleaned)

    return new_total, updated_total, sold_total, new_ids


def _build_retail_comps_json():
    """Build docs/retail_comps.json — comp dots keyed by listing ID.
    Fetched lazily by JS on hover-expand of retail listing cards."""
    import json as _json
    from pathlib import Path
    try:
        from core.fmv import get_fmv, normalize_trim
        comps_by_id = {}
        with database.get_conn() as conn:
            rows = conn.execute(
                """SELECT id, year, model, trim FROM listings
                   WHERE status='active' AND source_category='RETAIL'
                   AND fmv_confidence IN ('HIGH','MEDIUM')
                   ORDER BY id"""
            ).fetchall()
            fmv_cache = {}
            for lid, year, model, trim in rows:
                key = (model, year, normalize_trim(trim))
                if key not in fmv_cache:
                    fmv_cache[key] = get_fmv(conn, year=year, model=model, trim=trim)
                r = fmv_cache[key]
                dots = [
                    {
                        "p":  int(c.sold_price),
                        "d":  (getattr(c, "sold_date", None) or "")[:10],
                        "t":  (getattr(c, "trim", None) or "")[:24],
                        "mi": getattr(c, "mileage", None) or "",
                        "yr": getattr(c, "year", None) or "",
                    }
                    for c in getattr(r, "comps", [])
                    if getattr(c, "sold_price", None) and int(c.sold_price) > 0
                       and getattr(c, "sold_date", None)
                ][:60]
                if dots:
                    comps_by_id[str(lid)] = dots
        out = PROJECT_ROOT / "docs" / "retail_comps.json"
        out.write_text(_json.dumps(comps_by_id), encoding="utf-8")
        log.info("retail_comps.json: %d listings with comp dots", len(comps_by_id))
    except Exception as e:
        log.warning("retail_comps.json build failed: %s", e)


def main():
    parser = argparse.ArgumentParser(description="RennMarkt — retail listing tracker")
    parser.add_argument("--dashboard", action="store_true",
                        help="Regenerate dashboard only, no scraping")
    parser.add_argument("--mode", choices=["fast", "deep"], default="fast",
                        help="Scrape depth (fast=page 1, deep=3 pages)")
    args = parser.parse_args()

    database.init_db()
    today = date.today().isoformat()

    if args.dashboard:
        np = ndash.generate()
        print(f"Dashboard: file://{np}")
        return

    max_pages = 1 if args.mode == "fast" else 3
    log.info("RennMarkt scrape — mode: %s (max_pages=%d)", args.mode, max_pages)

    # Scrape
    results = _run_all(DEALERS, max_pages=max_pages)

    # Persist
    new_total, updated_total, sold_total, new_ids = run_snapshot(results, today)
    write_scrape_summary(results, today)

    # VIN trim enrichment
    try:
        with database.get_conn() as conn:
            stats = enrich_vin_trim.enrich_missing_trims(conn)
            if stats["enriched_local"] + stats["enriched_nhtsa"] > 0:
                log.info("VIN enrichment: %d trims filled", stats["enriched_local"] + stats["enriched_nhtsa"])
            stats2 = enrich_vin_trim.enrich_all_vins_with_trims(conn)
            if stats2["upgraded"] > 0:
                log.info("VIN enrichment: %d uninformative trims upgraded", stats2["upgraded"])
    except Exception as e:
        log.warning("VIN trim enrichment failed: %s", e)

    # Title keyword trim enrichment
    try:
        with database.get_conn() as conn:
            stats = enrich_vin_trim.enrich_title_keywords(conn)
            if stats["enriched"] > 0:
                log.info("Title keyword enrichment: %d trims detected", stats["enriched"])
    except Exception as e:
        log.warning("Title keyword enrichment failed: %s", e)

    # Archive-based mileage + VIN enrichment
    try:
        with database.get_conn() as conn:
            stats = enrich_from_archive.enrich_from_archives()
            if stats["mileage_filled"] + stats["vin_filled"] > 0:
                log.info("Archive enrichment: %d mileage, %d VIN filled",
                         stats["mileage_filled"], stats["vin_filled"])
    except Exception as e:
        log.warning("Archive enrichment failed: %s", e)

    # Promote auction comps
    try:
        with database.get_conn() as conn:
            stats = promote_auction_comps.promote_ended_auctions(conn)
            if stats["promoted"] > 0:
                log.info("Auction comps: %d new sold comps promoted", stats["promoted"])
    except Exception as e:
        log.warning("Auction comp promotion failed: %s", e)

    # FMV persist + retail_comps.json for hover graph
    try:
        with database.get_conn() as conn:
            n_fmv = fmv_engine.score_and_persist(conn)
            log.info("FMV persist: scored %d listings", n_fmv)
        _build_retail_comps_json()
    except Exception as e:
        log.warning("FMV persist failed: %s", e)

    # VIN decode
    try:
        vin_decoder.main(use_nhtsa=False)
    except Exception as e:
        log.warning("VIN decode failed: %s", e)

    # Build dashboard
    try:
        np = ndash.generate()
        log.info("Dashboard: file://%s", np)
        print(f"Dashboard: file://{np}")
    except Exception as e:
        log.warning("Dashboard generation failed: %s", e)

    # Search data
    try:
        import json as _json
        with database.get_conn() as _sc:
            _sc.row_factory = sqlite3.Row
            _rows = _sc.execute('''
                SELECT year, make, model, trim, price, mileage, dealer,
                       vin, listing_url, image_url, date_first_seen,
                       source_category, tier, color, transmission,
                       body_style, drive_type, generation,
                       fmv_value, fmv_confidence, fmv_comp_count,
                       fmv_low, fmv_high, fmv_pct
                FROM listings
                WHERE status='active'
                ORDER BY created_at DESC
            ''').fetchall()
        _search_path = PROJECT_ROOT / "docs" / "search_data.json"
        with open(_search_path, "w") as _sf:
            _json.dump([dict(r) for r in _rows], _sf, default=str)
        log.info("Search data: %d active listings → docs/search_data.json", len(_rows))
    except Exception as e:
        log.warning("Search data generation failed: %s", e)

    # Push alerts — new listings
    try:
        with database.get_conn() as conn:
            cutoff = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
            if new_ids:
                placeholders = ",".join("?" * len(new_ids))
                fresh_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM listings WHERE id IN ({placeholders}) AND created_at >= ?",
                    (*new_ids, cutoff)
                ).fetchall()]
                if len(fresh_ids) != len(new_ids):
                    log.info("Alert filter: %d new IDs, %d within 20min window",
                             len(new_ids), len(fresh_ids))
                notify_push.notify_new_listings(conn, fresh_ids)
                notify_push.notify_watchlist(conn, fresh_ids)
            else:
                notify_push.notify_new_listings(conn, new_ids)
                notify_push.notify_watchlist(conn, new_ids)
    except Exception as e:
        log.warning("Push new-listing alerts failed: %s", e)

    # Push alerts — DOM (days on market)
    try:
        with database.get_conn() as conn:
            notify_push.notify_dom_alert(conn)
    except Exception as e:
        log.warning("Push DOM alerts failed: %s", e)

    # Health monitor
    try:
        health_monitor.main()
    except Exception as e:
        log.warning("Health monitor failed: %s", e)


if __name__ == "__main__":
    main()

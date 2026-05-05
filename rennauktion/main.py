#!/usr/bin/env python3
"""
rennauktion/main.py — RennAuktion entry point.

Scrapes 3 auction sources every 5 minutes and writes listings to the shared DB.
Sources: Bring a Trailer, Cars & Bids, pcarmarket.

Usage:
  python3 rennauktion/main.py             # Full scrape + dashboard + alerts
  python3 rennauktion/main.py --dashboard # Regenerate auction dashboard only
  python3 rennauktion/main.py --comps     # Run daily sold-comp scrape only
"""
import argparse
import logging
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# Add project root to sys.path so core/, shared/, rennauktion/ all resolve
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "rennauktion.log"

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
from rennauktion.scrapers.bat        import scrape_bat, fetch_bat_sold_price
from rennauktion.scrapers.cnb        import scrape_cnb, fetch_cnb_sold_price
from rennauktion.scrapers.pcarmarket import scrape_pcarmarket
from rennauktion import notify_push
import rennauktion.build_dashboard as auc_dash
import rennauktion.comp_scraper as comp_scraper

from shared.scraper_utils import _is_valid_listing
import core.fmv as fmv_engine


DEALERS = [
    {"name": "Bring a Trailer", "scrape": scrape_bat},
    {"name": "Cars and Bids",   "scrape": scrape_cnb},
    {"name": "pcarmarket",      "scrape": scrape_pcarmarket},
]

_AUCTION_DEALERS = frozenset({"Bring a Trailer", "Cars and Bids", "pcarmarket"})


def _run_all(dealers) -> dict:
    import time
    results = {}
    for d in dealers:
        name = d["name"]
        log.info("Scraping %s…", name)
        try:
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

    lines = [f"=== RennAuktion scrape {timestamp} ==="]
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
    log_path = SCRAPE_LOG_DIR / f"scrape_auctions_{today}.log"
    with open(log_path, "a") as f:
        f.write(summary + "\n")
    print("\n" + summary)


def _capture_auction_result(conn, dealer_name, listing, today):
    """Fetch final hammer price for a just-sold auction and upsert to sold_comps."""
    url = listing.get("listing_url")
    if not url:
        return
    try:
        if dealer_name == "Bring a Trailer":
            final_price = fetch_bat_sold_price(url)
            source = "BaT"
        else:
            final_price = fetch_cnb_sold_price(url)
            source = "Cars and Bids"

        if final_price is None:
            return

        listing_id = listing.get("id")
        old_price  = listing.get("price")

        if old_price != final_price:
            conn.execute("UPDATE listings SET price=? WHERE id=?", (final_price, listing_id))
            log.info("[%s] Final hammer price updated $%s → $%s",
                     dealer_name,
                     f"{old_price:,}" if old_price else "?",
                     f"{final_price:,}")

        database.upsert_sold_comp(
            conn,
            source=source,
            year=listing.get("year"),
            make="Porsche",
            model=listing.get("model"),
            trim=listing.get("trim"),
            mileage=listing.get("mileage"),
            sold_price=final_price,
            sold_date=today,
            listing_url=url,
            image_url=listing.get("image_url"),
        )
        log.info("[%s] Sold comp upserted: %s %s $%s",
                 dealer_name, listing.get("year"), listing.get("model"),
                 f"{final_price:,}")
    except Exception as exc:
        log.warning("[%s] _capture_auction_result error: %s", dealer_name, exc)


def run_snapshot(dealer_results: dict, today: str):
    """Persist scraped auction data."""
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
            min_threshold = max(3, int(currently_active * 0.5))
            if len(cars) < min_threshold:
                log.warning("  [%s] Only %d cars scraped vs %d active — skipping sold-marking",
                            dealer_name, len(cars), currently_active)
                continue

            before = currently_active

            # BaT/C&B: record timestamp before mark_sold for final price capture
            _pre_sold_ts = None
            if dealer_name in ("Bring a Trailer", "Cars and Bids"):
                _pre_sold_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # Auction guard: protect listings whose auction hasn't ended yet
            future_keys = set(
                r[0] for r in conn.execute(
                    """SELECT COALESCE(vin, listing_url)
                       FROM listings
                       WHERE dealer=? AND status='active'
                       AND auction_ends_at > datetime('now')
                       AND (vin IS NOT NULL OR listing_url IS NOT NULL)""",
                    (dealer_name,)
                ).fetchall()
                if r[0]
            )
            if future_keys:
                active_keys |= future_keys
                log.info("[%s] Auction guard: protecting %d future-ending listings",
                         dealer_name, len(future_keys))

            database.mark_sold(conn, dealer_name, active_keys, today)

            # Capture final hammer price for newly sold auctions
            if _pre_sold_ts is not None:
                try:
                    newly_sold = conn.execute(
                        """SELECT id, listing_url, year, model, trim, mileage, price, image_url
                           FROM listings
                           WHERE dealer=? AND status='sold' AND archive_reason='sold'
                             AND archived_at >= ?""",
                        (dealer_name, _pre_sold_ts)
                    ).fetchall()
                    for sold_row in newly_sold:
                        _capture_auction_result(conn, dealer_name, dict(sold_row), today)
                except Exception as cap_exc:
                    log.warning("[%s] Auction result capture error: %s", dealer_name, cap_exc)

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

    return new_total, updated_total, sold_total, new_ids


def main():
    parser = argparse.ArgumentParser(description="RennAuktion — auction aggregator")
    parser.add_argument("--dashboard", action="store_true",
                        help="Regenerate auction dashboard only, no scraping")
    parser.add_argument("--comps", action="store_true",
                        help="Run sold-comp scraper only (BaT daily comps)")
    args = parser.parse_args()

    database.init_db()
    today = date.today().isoformat()

    if args.dashboard:
        auc_dash.generate()
        log.info("Auction dashboard regenerated")
        return

    if args.comps:
        comp_scraper.run_comp_scrape()
        return

    log.info("RennAuktion scrape starting…")

    # Scrape
    results = _run_all(DEALERS)

    # Persist
    new_total, updated_total, sold_total, new_ids = run_snapshot(results, today)
    write_scrape_summary(results, today)

    # FMV persist (for auction listings too)
    try:
        with database.get_conn() as conn:
            n_fmv = fmv_engine.score_and_persist(conn)
            log.info("FMV persist: scored %d listings", n_fmv)
    except Exception as e:
        log.warning("FMV persist failed: %s", e)

    # Rebuild auction dashboard
    try:
        auc_dash.generate()
        log.info("Auction dashboard regenerated")
    except Exception as e:
        log.warning("Auction dashboard generation failed: %s", e)

    # Daily sold comp scrape (once per day)
    try:
        _comp_stamp = PROJECT_ROOT / "data" / "last_comp_scrape.txt"
        _run_comps = True
        if _comp_stamp.exists():
            _last = _comp_stamp.read_text().strip()
            if _last == today:
                _run_comps = False
        if _run_comps:
            log.info("Running daily sold comp scrape...")
            comp_scraper.run_comp_scrape()
            _comp_stamp.write_text(today)
            log.info("Sold comp scrape complete")
    except Exception as e:
        log.warning("Sold comp scrape failed: %s", e)

    # Push alerts — new auction listings
    try:
        with database.get_conn() as conn:
            cutoff = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
            if new_ids:
                placeholders = ",".join("?" * len(new_ids))
                fresh_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM listings WHERE id IN ({placeholders}) AND created_at >= ?",
                    (*new_ids, cutoff)
                ).fetchall()]
                notify_push.notify_auction_ending(conn)
    except Exception as e:
        log.warning("Push auction alerts failed: %s", e)

    # Push alerts — ending soon
    try:
        with database.get_conn() as conn:
            notify_push.notify_auction_ending(conn)
    except Exception as e:
        log.warning("Push auction-ending alerts failed: %s", e)


if __name__ == "__main__":
    main()

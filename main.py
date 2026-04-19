#!/usr/bin/env python3
"""
Porsche Competitor Inventory Tracker — main entry point.

Usage:
  python main.py              # Run full scrape + snapshot + dashboard
  python main.py --test       # Quick test: scrape LBI Limited, Road Scholars, Velocity Porsche
  python main.py --dashboard  # Regenerate dashboard only
  python main.py --report     # Regenerate market analysis report only
  python main.py --comps      # Run sold-comp scrapers only (BaT + PCA Mart)
  python main.py --hagerty    # Scrape Hagerty Good/Excellent condition prices
  python main.py --dealers "LBI Limited,Road Scholars"  # Scrape specific dealers
"""
import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ---- logging setup ---------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "scraper.log"

SCRAPE_LOG_DIR = Path(__file__).parent / "data" / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ---- imports after logging setup -------------------------------------------
import db as database
import scraper as sc
import dashboard as dash
import new_dashboard as ndash
import auction_dashboard as auc_dash
# live_feed.py deleted — removed import
import comp_scraper
import report as rpt
import daily_report
import weekly_report
import monthly_report
import notify_imessage
import health_monitor


def write_scrape_summary(results: dict, today: str):
    """Append a per-source result summary to data/logs/scrape_YYYY-MM-DD.log
    and print the same text to console."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = sum(len(v) for v in results.values())
    zero_sources = [name for name, cars in results.items() if not cars]

    lines = [f"=== Scrape {timestamp} ==="]
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
        new_total = 0
        updated_total = 0
        sold_total = 0
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

            # Save raw snapshot
            database.save_snapshot(conn, today, dealer_name, cars)

            # Mark no-longer-seen as sold.
            # Safety guard: for sources where pagination may be partial, only mark
            # sold if we scraped at least as many as currently active (with 20% buffer).
            # This prevents partial scrapes from mass-flagging cars as sold.
            currently_active = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE dealer=? AND status='active'",
                (dealer_name,)
            ).fetchone()[0]
            min_threshold = max(5, int(currently_active * 0.5))
            if len(cars) < min_threshold:
                log.warning("  [%s] Only %d cars scraped vs %d active — skipping sold-marking (partial scrape guard)",
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

        log.info(
            "Snapshot complete — new: %d  price changes: %d  sold: %d",
            new_total, updated_total, sold_total
        )
        # Archive listings not seen in 90+ days
        archived = database.archive_stale_listings(conn, days=90)
        if archived:
            log.info("Archived %d stale listings (90d rule)", archived)
    return new_total, updated_total, sold_total, new_ids


def main():
    parser = argparse.ArgumentParser(description="Porsche Inventory Tracker")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: scrape cars.com, AutoTrader, classic.com (proxy test)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Regenerate dashboard only, no scraping")
    parser.add_argument("--live", action="store_true",
                        help="Regenerate live feed dashboard only, no scraping")
    parser.add_argument("--report", action="store_true",
                        help="Regenerate market analysis report only, no scraping")
    parser.add_argument("--comps", action="store_true",
                        help="Run sold-comp scrapers (BaT + PCA Mart) and exit")
    parser.add_argument("--daily", action="store_true",
                        help="Regenerate daily auction report only")
    parser.add_argument("--weekly", action="store_true",
                        help="Regenerate weekly market report only")
    parser.add_argument("--monthly", action="store_true",
                        help="Regenerate monthly market report only")
    parser.add_argument("--hagerty", action="store_true",
                        help="Scrape Hagerty valuations (run monthly)")
    parser.add_argument("--dealers", type=str, default="",
                        help="Comma-separated list of dealer names to scrape")
    args = parser.parse_args()

    # Init DB
    database.init_db()
    today = date.today().isoformat()

    if args.dashboard:
        path = dash.generate()
        np = ndash.generate()
        print(f"\nDashboard: file://{path}")
        print(f"New Dashboard: file://{np}")
        return

    if args.live:
        print("Live feed removed — see index.html")
        return

    if args.report:
        path = rpt.generate()
        print(f"\nMarket report: file://{path}")
        return

    if args.comps:
        comp_scraper.run_comp_scrape()
        return

    if args.hagerty:
        n = comp_scraper.run_hagerty_scrape()
        log.info("Hagerty: %d valuations saved", n)
        return

    if args.daily:
        p = daily_report.generate()
        print(f"\nDaily report: file://{p}")
        return

    if args.weekly:
        p = weekly_report.generate()
        print(f"\nWeekly report: file://{p}")
        return

    if args.monthly:
        p = monthly_report.generate()
        print(f"\nMonthly report: file://{p}")
        return

    # Select dealers to scrape
    if args.test:
        target_names = {"cars.com", "AutoTrader", "classic.com"}
        dealers = [d for d in sc.DEALERS if d["name"] in target_names]
        log.info("TEST MODE — scraping %d dealers: %s", len(dealers),
                 ", ".join(d["name"] for d in dealers))
    elif args.dealers:
        target_names = {n.strip() for n in args.dealers.split(",")}
        dealers = [d for d in sc.DEALERS if d["name"] in target_names]
        log.info("Scraping specified dealers: %s",
                 ", ".join(d["name"] for d in dealers))
    else:
        dealers = sc.DEALERS
        log.info("Scraping all %d dealers…", len(dealers))

    # Run scraper
    results = sc.run_all(dealers)

    # Persist
    new_total, updated_total, sold_total, new_ids = run_snapshot(results, today)

    # Per-source summary → console + data/logs/scrape_YYYY-MM-DD.log
    write_scrape_summary(results, today)

    # Regenerate dashboards
    path = dash.generate()
    log.info("Dashboard: file://%s", path)
    print(f"Dashboard: file://{path}")

    # Regenerate search data
    try:
        import json as _json
        with database.get_conn() as _sc:
            _sc.row_factory = sqlite3.Row
            _rows = _sc.execute('''SELECT year, make, model, trim, price, mileage, dealer,
                status, vin, listing_url, image_url, date_first_seen, created_at,
                source_category, tier, color, transmission FROM listings ORDER BY created_at DESC''').fetchall()
        _search_path = BASE_DIR / "docs" / "search_data.json"
        with open(_search_path, "w") as _sf:
            _json.dump([dict(r) for r in _rows], _sf, default=str)
        log.info("Search data: %d listings → docs/search_data.json", len(_rows))
    except Exception as e:
        log.warning("Search data generation failed: %s", e)

    try:
        np = ndash.generate()
        log.info("New Dashboard: file://%s", np)
        print(f"New Dashboard: file://{np}")
    except Exception as e:
        log.warning("New dashboard generation failed: %s", e)

    try:
        auc_dash.generate()
        log.info("Auction page generated")
    except Exception as e:
        log.warning("Auction dashboard generation failed: %s", e)

    # Regenerate reports
    for label, fn in [
        ("Market report", rpt.generate),
        ("Daily report",  daily_report.generate),
    ]:
        try:
            p = fn()
            log.info("%s: file://%s", label, p)
            print(f"{label}: file://{p}")
        except Exception as e:
            log.warning("%s failed: %s", label, e)

    # Run sold comp scraper once per day
    try:
        _comp_stamp = BASE_DIR / "data" / "last_comp_scrape.txt"
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


    # iMessage alerts — new listing ping (every new car, no FMV threshold)
    try:
        with database.get_conn() as conn:
            # Safety: only alert on listings created in the last 20 minutes.
            # This prevents bulk re-ingestion events (cache clears, bootstrap,
            # dedup changes) from firing hundreds of iMessage notifications.
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
            if new_ids:
                placeholders = ",".join("?" * len(new_ids))
                fresh_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM listings WHERE id IN ({placeholders}) AND created_at >= ?",
                    (*new_ids, cutoff)
                ).fetchall()]
                if len(fresh_ids) != len(new_ids):
                    log.info("Alert filter: %d new IDs, %d within 20min window — alerting only fresh",
                             len(new_ids), len(fresh_ids))
                notify_imessage.notify_new_listings(conn, fresh_ids)
            else:
                notify_imessage.notify_new_listings(conn, new_ids)
    except Exception as e:
        log.warning("iMessage new-listing alerts failed: %s", e)

    # iMessage deal alerts — TEMPORARILY DISABLED (re-enable when ready)
    # notify_imessage.main()

    # iMessage auction-ending alerts (3hr Tier1, 1hr Tier2)
    try:
        with database.get_conn() as conn:
            notify_imessage.notify_auction_ending(conn)
    except Exception as e:
        log.warning("iMessage auction-ending alerts failed: %s", e)

    # Health monitor
    try:
        health_monitor.main()
    except Exception as e:
        log.warning("Health monitor failed: %s", e)


if __name__ == "__main__":
    main()

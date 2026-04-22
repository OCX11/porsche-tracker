"""
promote_auction_comps.py — Auto-promote ended auction results into sold_comps.

When an auction listing transitions to status='sold', its final price becomes
a sold comp for FMV calculations. Reserve-not-met results (if tracked) get
added to bat_reserve_not_met with lower weight.

Run standalone: python3 promote_auction_comps.py
Or import: from promote_auction_comps import promote_ended_auctions
"""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

DB_PATH = Path(__file__).parent / "data" / "inventory.db"

# Auction sources whose final prices should become comps
AUCTION_DEALERS = {"Bring a Trailer", "Cars and Bids", "pcarmarket"}


def promote_ended_auctions(conn=None, dry_run=False):
    # type: (Optional[sqlite3.Connection], bool) -> dict
    """
    Find sold auction listings not yet in sold_comps, insert them.
    
    Returns dict with stats: {found, promoted, skipped, errors}
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close_conn = True

    # Find sold auction listings NOT already in sold_comps
    placeholders = ",".join("?" * len(AUCTION_DEALERS))
    rows = conn.execute(
        """SELECT l.id, l.dealer, l.year, l.model, l.trim, l.mileage, l.price,
                  l.listing_url, l.image_url, l.vin, l.source_category, l.tier,
                  l.auction_ends_at, l.date_last_seen
           FROM listings l
           WHERE l.status = 'sold'
             AND l.price IS NOT NULL AND l.price > 0
             AND l.dealer IN (%s)
             AND NOT EXISTS (
                 SELECT 1 FROM sold_comps sc WHERE sc.listing_url = l.listing_url
             )
           ORDER BY l.auction_ends_at DESC""" % placeholders,
        tuple(AUCTION_DEALERS),
    ).fetchall()

    stats = {"found": len(rows), "promoted": 0, "skipped": 0, "errors": 0}

    for row in rows:
        (lid, dealer, year, model, trim, mileage, price, url, image_url,
         vin, src_cat, tier, auction_ends_at, date_last_seen) = row

        # Use the EARLIEST of auction_ends_at, date_last_seen, and today.
        # When auctions end early (Buy It Now / reserve met), auction_ends_at still
        # holds the original scheduled end date (future). Using min() ensures
        # sold_date is never in the future.
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candidates = [today_str]
        if auction_ends_at:
            try:
                candidates.append(auction_ends_at[:10])
            except (TypeError, IndexError):
                pass
        if date_last_seen:
            try:
                candidates.append(date_last_seen[:10])
            except (TypeError, IndexError):
                pass
        sold_date = min(candidates)  # earliest date — never in the future

        # Safety net: skip if sold_date is still somehow in the future
        if sold_date > today_str:
            log.warning("  SKIP future-dated comp: %s %s sold_date=%s", year, model, sold_date)
            stats["skipped"] += 1
            continue

        # Build title
        title = "%s %s %s" % (year, model, trim or "")
        title = title.strip()

        try:
            if not dry_run:
                conn.execute(
                    """INSERT INTO sold_comps 
                       (source, year, make, model, trim, mileage, sold_price, sold_date,
                        listing_url, image_url, title, scraped_at, source_category, tier,
                        vin, generation)
                       VALUES (?, ?, 'Porsche', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (dealer, year, model, trim, mileage, price, sold_date,
                     url, image_url, title, datetime.now(timezone.utc).isoformat(),
                     src_cat or "AUCTION", tier, vin),
                )
            stats["promoted"] += 1
            log.info("  PROMOTED: %s %s %s $%s (%s, sold %s)",
                     year, model, trim or "", "{:,}".format(price), dealer[:20], sold_date)
        except Exception as e:
            stats["errors"] += 1
            log.warning("  ERROR promoting %s: %s", url, e)

    if not dry_run and stats["promoted"] > 0:
        conn.commit()
        log.info("Committed %d new sold comps", stats["promoted"])

    if close_conn:
        conn.close()

    return stats


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv

    log.info("=" * 60)
    log.info("Auction Result → Sold Comp Promotion")
    log.info("=" * 60)

    stats = promote_ended_auctions(dry_run=dry_run)
    log.info("\nResults: %d found, %d promoted, %d skipped, %d errors" % (
        stats["found"], stats["promoted"], stats["skipped"], stats["errors"]))
    
    if dry_run:
        log.info("(DRY RUN — no changes made)")

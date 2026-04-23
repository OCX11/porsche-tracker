"""
vin_tracker.py — VIN lifecycle event recorder for PTOX11.

Records every significant event per physical car (identified by VIN):
  listed        — first time VIN appears in our DB
  price_change  — asking price changed on an existing listing
  cross_source  — same VIN found on a different dealer/source
  sold          — listing archived (auction ended or listing removed)
  relisted      — VIN reappears after being archived

Called by upsert_listing() in db.py after each scrape cycle.
Events are queryable for the VIN timeline feature on listing cards.
"""
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


def record_event(
    conn,
    vin: str,
    listing_id: int,
    event_type: str,
    dealer: str,
    price: Optional[int] = None,
    mileage: Optional[int] = None,
    notes: Optional[str] = None,
    recorded_at: Optional[str] = None,
) -> int:
    """Insert one VIN lifecycle event. Returns the new row id."""
    if not vin or len(vin) != 17:
        return 0
    ts = recorded_at or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """INSERT INTO vin_history
               (vin, listing_id, event_type, dealer, price, mileage, recorded_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (vin, listing_id, event_type, dealer, price, mileage, ts, notes),
    )
    return cur.lastrowid


def get_timeline(conn, vin: str) -> list:
    """Return full event timeline for a VIN, oldest first."""
    if not vin:
        return []
    rows = conn.execute(
        """SELECT id, vin, listing_id, event_type, dealer, price, mileage,
                  recorded_at, notes
           FROM vin_history
           WHERE vin = ?
           ORDER BY recorded_at ASC""",
        (vin,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cross_source_vins(conn, limit: int = 50) -> list:
    """Return VINs that have appeared on 2+ distinct dealers — useful for UI."""
    rows = conn.execute(
        """SELECT vin,
                  COUNT(DISTINCT dealer) as dealer_count,
                  GROUP_CONCAT(DISTINCT dealer) as dealers,
                  MIN(recorded_at) as first_seen,
                  MAX(recorded_at) as last_seen
           FROM vin_history
           WHERE event_type IN ('listed', 'cross_source')
           GROUP BY vin
           HAVING COUNT(DISTINCT dealer) > 1
           ORDER BY dealer_count DESC, last_seen DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def backfill_from_listings(conn) -> int:
    """
    One-time backfill: insert 'listed' events for every listing in the DB
    that has a valid VIN and no existing vin_history entry.
    Safe to run multiple times (skips VINs already recorded).
    Returns count of events inserted.
    """
    already = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT vin FROM vin_history"
        ).fetchall()
    )

    rows = conn.execute(
        """SELECT id, vin, dealer, price, mileage, date_first_seen, status
           FROM listings
           WHERE vin IS NOT NULL AND length(vin) = 17
           ORDER BY date_first_seen ASC, id ASC"""
    ).fetchall()

    inserted = 0
    for row in rows:
        lid, vin, dealer, price, mileage, first_seen, status = row
        if vin in already:
            continue
        # Use date_first_seen as recorded_at for historical accuracy
        ts = (first_seen or "")[:10]
        if ts:
            ts = ts + "T00:00:00Z"
        else:
            ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        record_event(conn, vin, lid, "listed", dealer or "", price, mileage,
                     notes="backfill", recorded_at=ts)
        already.add(vin)
        inserted += 1

    conn.commit()
    log.info("VIN backfill: inserted %d 'listed' events", inserted)
    return inserted

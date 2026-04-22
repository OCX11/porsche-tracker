"""
enrich_from_archive.py — Extract mileage + VIN from archived listing HTML.

Reads the saved HTML files in archive/html/ and backfills mileage and VIN
for listings that are missing them. No internet needed.

Run: python3 enrich_from_archive.py [--dry-run]
"""
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Tuple

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "inventory.db"


# ── Per-source parsers ───────────────────────────────────────────────────────

def _parse_bat(html):
    # type: (str) -> Tuple[Optional[int], Optional[str]]
    """Parse mileage and VIN from a Bring a Trailer archived page."""
    mileage = None
    vin = None

    # Mileage: "Showing XX,XXX miles" pattern (most reliable)
    m = re.search(r'[Ss]howing\s+([\d,]+)\s*miles', html)
    if m:
        val = int(m.group(1).replace(",", ""))
        if val > 0:
            mileage = val
    else:
        # Fallback: "~XX,XXX miles" or "XX,XXX-Mile" — require 3+ digits
        # to avoid false positives from page elements
        m = re.search(r'~?([\d,]{3,})\s*(?:-[Mm]ile|[Mm]iles)', html)
        if m:
            val = int(m.group(1).replace(",", ""))
            if 100 < val < 500000:  # min 100 to avoid false positives
                mileage = val

    # VIN: Porsche VINs start with WP0 (or WP1 for Cayenne but we don't track those)
    vin_matches = re.findall(r'\bWP0[A-HJ-NPR-Z0-9]{14}\b', html)
    if vin_matches:
        vin = vin_matches[0]  # first match is usually the subject car

    return mileage, vin


def _parse_pcarmarket(html):
    # type: (str) -> Tuple[Optional[int], Optional[str]]
    """Parse mileage and VIN from a pcarmarket archived page."""
    mileage = None
    vin = None

    soup = BeautifulSoup(html, "lxml")

    # pcarmarket uses <dt>Mileage</dt><dd>6,320 mi</dd>
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        val = dd.get_text(strip=True)

        if label == "mileage" or label == "miles":
            digits = re.sub(r"[^\d]", "", val)
            if digits:
                mileage = int(digits)
        elif label == "vin":
            clean = val.strip()
            if len(clean) == 17:
                vin = clean

    # Fallback VIN from raw text
    if not vin:
        vin_matches = re.findall(r'\bWP0[A-HJ-NPR-Z0-9]{14}\b', html)
        if vin_matches:
            vin = vin_matches[0]

    return mileage, vin


def _parse_carsandbids(html):
    # type: (str) -> Tuple[Optional[int], Optional[str]]
    """Parse mileage and VIN from a Cars and Bids archived page."""
    mileage = None
    vin = None

    # C&B listing pages show mileage in various formats
    # Try structured data first
    soup = BeautifulSoup(html, "lxml")

    # Look for mileage in key-value pairs
    for el in soup.find_all(["dt", "th", "span", "div"]):
        text = el.get_text(strip=True).lower()
        if text in ("mileage", "miles", "odometer"):
            sibling = el.find_next_sibling()
            if sibling:
                val = sibling.get_text(strip=True)
                digits = re.sub(r"[^\d]", "", val)
                if digits and 0 < int(digits) < 500000:
                    mileage = int(digits)
                    break

    # Fallback: regex in full text
    if not mileage:
        m = re.search(r'~?([\d,]+)\s*(?:[Mm]iles|mi\b)', html)
        if m:
            val = int(m.group(1).replace(",", ""))
            if 0 < val < 500000:
                mileage = val

    # VIN
    vin_matches = re.findall(r'\bWP0[A-HJ-NPR-Z0-9]{14}\b', html)
    if vin_matches:
        vin = vin_matches[0]

    return mileage, vin


def _parse_generic(html):
    # type: (str) -> Tuple[Optional[int], Optional[str]]
    """Generic fallback parser for any source."""
    mileage = None
    vin = None

    # Mileage patterns
    for pattern in [
        r'[Ss]howing\s+([\d,]+)\s*miles',
        r'[Oo]dometer.*?([\d,]+)\s*mi',
        r'[Mm]ileage.*?([\d,]+)',
        r'~?([\d,]+)\s*(?:-[Mm]ile|[Mm]iles)',
    ]:
        m = re.search(pattern, html)
        if m:
            digits = m.group(1).replace(",", "")
            if digits:
                val = int(digits)
                if 100 < val < 500000:  # min 100 to avoid false positives
                    mileage = val
                    break

    # VIN: Porsche pattern
    vin_matches = re.findall(r'\bWP0[A-HJ-NPR-Z0-9]{14}\b', html)
    if vin_matches:
        vin = vin_matches[0]

    return mileage, vin


# Parser dispatch by dealer
_PARSERS = {
    "Bring a Trailer": _parse_bat,
    "pcarmarket": _parse_pcarmarket,
    "Cars and Bids": _parse_carsandbids,
}


# ── Main enrichment logic ────────────────────────────────────────────────────

def enrich_from_archives(dry_run=False):
    # type: (bool) -> dict
    """Read archived HTML files and backfill mileage + VIN where missing."""
    conn = sqlite3.connect(str(DB_PATH))

    # Find listings with archived HTML that are missing mileage or VIN
    rows = conn.execute(
        """SELECT id, dealer, html_path, year, model, trim, mileage, vin
           FROM listings
           WHERE html_path IS NOT NULL
             AND html_path != ''
             AND html_path != 'FAILED'
             AND ((mileage IS NULL OR mileage = 0) OR (vin IS NULL OR vin = ''))"""
    ).fetchall()

    stats = {
        "checked": len(rows),
        "mileage_filled": 0,
        "vin_filled": 0,
        "files_missing": 0,
        "parse_failed": 0,
    }

    batch = []
    for lid, dealer, html_path, year, model, trim, db_mi, db_vin in rows:
        full_path = os.path.join(str(BASE_DIR), html_path)
        if not os.path.exists(full_path):
            stats["files_missing"] += 1
            continue

        try:
            with open(full_path, "r", errors="ignore") as f:
                html = f.read()
        except Exception:
            stats["parse_failed"] += 1
            continue

        # Select parser
        parser = _PARSERS.get(dealer, _parse_generic)
        parsed_mi, parsed_vin = parser(html)

        updates = []
        params = []

        # Only fill if DB value is missing
        if parsed_mi and (not db_mi or db_mi == 0):
            updates.append("mileage = ?")
            params.append(parsed_mi)
            stats["mileage_filled"] += 1

        if parsed_vin and (not db_vin or db_vin == ""):
            updates.append("vin = ?")
            params.append(parsed_vin)
            stats["vin_filled"] += 1

        if updates:
            params.append(lid)
            batch.append(("UPDATE listings SET %s WHERE id = ?" % ", ".join(updates), params))
            log.info("  %s %s %-25s mi=%s vin=%s (%s)",
                     year, model, (trim or "")[:25],
                     parsed_mi or "-", (parsed_vin or "-")[:11],
                     dealer[:15])

    if not dry_run and batch:
        skipped = 0
        for sql, p in batch:
            try:
                conn.execute(sql, p)
            except Exception as e:
                if "UNIQUE" in str(e):
                    skipped += 1
                else:
                    log.warning("Update failed: %s", e)
                    skipped += 1
        conn.commit()
        log.info("Committed %d updates (%d skipped due to constraints)", len(batch) - skipped, skipped)
    elif dry_run:
        log.info("DRY RUN — would update %d listings", len(batch))

    conn.close()
    return stats


# ── Also enrich sold_comps ───────────────────────────────────────────────────

def enrich_comps_from_archives(dry_run=False):
    # type: (bool) -> dict
    """Same logic but for sold_comps table. BaT comps have listing_urls
    that may correspond to archived HTML files."""
    conn = sqlite3.connect(str(DB_PATH))

    # Find comps missing mileage or VIN that have listing_urls matching archive files
    rows = conn.execute(
        """SELECT sc.id, sc.source, sc.listing_url, sc.year, sc.model, sc.trim,
                  sc.mileage, sc.vin, l.html_path
           FROM sold_comps sc
           LEFT JOIN listings l ON sc.listing_url = l.listing_url
           WHERE l.html_path IS NOT NULL
             AND l.html_path != ''
             AND l.html_path != 'FAILED'
             AND ((sc.mileage IS NULL OR sc.mileage = 0) OR (sc.vin IS NULL OR sc.vin = ''))"""
    ).fetchall()

    stats = {"checked": len(rows), "mileage_filled": 0, "vin_filled": 0}
    batch = []

    for cid, source, url, year, model, trim, db_mi, db_vin, html_path in rows:
        full_path = os.path.join(str(BASE_DIR), html_path)
        if not os.path.exists(full_path):
            continue

        try:
            with open(full_path, "r", errors="ignore") as f:
                html = f.read()
        except Exception:
            continue

        parser = _PARSERS.get(source, _parse_generic)
        parsed_mi, parsed_vin = parser(html)

        updates = []
        params = []
        if parsed_mi and (not db_mi or db_mi == 0):
            updates.append("mileage = ?")
            params.append(parsed_mi)
            stats["mileage_filled"] += 1
        if parsed_vin and (not db_vin or db_vin == ""):
            updates.append("vin = ?")
            params.append(parsed_vin)
            stats["vin_filled"] += 1

        if updates:
            params.append(cid)
            batch.append(("UPDATE sold_comps SET %s WHERE id = ?" % ", ".join(updates), params))

    if not dry_run and batch:
        skipped = 0
        for sql, p in batch:
            try:
                conn.execute(sql, p)
            except Exception:
                skipped += 1
        conn.commit()
        log.info("Comps: committed %d updates (%d skipped)", len(batch) - skipped, skipped)

    conn.close()
    return stats


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    log.info("=" * 60)
    log.info("Archive HTML → Mileage + VIN Enrichment")
    log.info("=" * 60)

    log.info("\n--- Listings ---")
    s1 = enrich_from_archives(dry_run=dry_run)
    log.info("Checked: %d | Mileage filled: %d | VIN filled: %d | Files missing: %d" % (
        s1["checked"], s1["mileage_filled"], s1["vin_filled"], s1["files_missing"]))

    log.info("\n--- Sold Comps ---")
    s2 = enrich_comps_from_archives(dry_run=dry_run)
    log.info("Checked: %d | Mileage filled: %d | VIN filled: %d" % (
        s2["checked"], s2["mileage_filled"], s2["vin_filled"]))

    log.info("\nDone!")

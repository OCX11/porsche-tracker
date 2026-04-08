#!/usr/bin/env python3
"""
enrich_bat_vins.py — Backfill VIN, mileage, and transmission on sold_comps
                     by scraping each BaT individual listing page.

BaT requires every listing to have a VIN/chassis number, but the index API
(listings-filter) does not return it. The individual listing page has all three:
  - VIN:          .listing-essentials li  "Chassis: WP0CA299XSS342926"
  - Mileage:      .listing-essentials li  "Mileage: 34,000"
  - Transmission: .listing-essentials li  "Transmission: 6-Speed Manual"

One page fetch → three fields recovered. Processes any comp missing ANY of
those three fields so it stays useful on incremental Monday runs.

Progress saved to data/vin_enrich_progress.json — safe to Ctrl+C and resume.
Rate-limited to ~1 request per 5-8 seconds to avoid 429s.

Run:  python3 enrich_bat_vins.py
"""
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR    = Path(__file__).parent
PROGRESS_FILE = SCRIPT_DIR / "data" / "vin_enrich_progress.json"
LOG_DIR       = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "vin_enrich.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(SCRIPT_DIR))
import db

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {"done_ids": [], "updated": 0, "failed_ids": []}


def _save_progress(prog: dict):
    PROGRESS_FILE.parent.mkdir(exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    prog["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp.write_text(json.dumps(prog, indent=2))
    tmp.replace(PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Listing page scraper — VIN + mileage + transmission in one fetch
# ---------------------------------------------------------------------------

def _parse_mileage(text: str):
    """Parse mileage from strings like '34,000', '34k', '34,000 Miles'."""
    t = text.replace(",", "").strip()
    m = re.search(r"(\d+)\s*k\b", t, re.I)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"(\d+)", t)
    if m:
        val = int(m.group(1))
        # Sanity check: Porsches don't have 17-digit mileages; skip if it looks like a VIN digit
        if val < 1_000_000:
            return val
    return None


def _parse_transmission_from_essentials(text: str):
    """Normalise a transmission string from listing essentials.
    Handles both numeric ('6-Speed Manual') and word-form ('Five-Speed Manual Transaxle').
    """
    t = text.lower()
    if re.search(r"\bpdk\b|\bdoppelkupplung\b", t):
        return "automatic"
    if re.search(r"tiptronic|automatic", t):
        return "automatic"
    # Word-form speeds: five-speed, six-speed, seven-speed, etc.
    if re.search(r"\b(five|six|seven|eight)-speed\b", t):
        return "manual"
    if re.search(r"manual|[456789]-?\s*speed|gated|getrag|transaxle", t):
        return "manual"
    return None


def _parse_color(text: str):
    """Extract paint/color from li text like 'Repainted in Guards Red' or 'Black over Tan'."""
    t = text.strip()
    # Remove common prefixes
    t = re.sub(r'^(repainted\s+in|finished\s+in|painted\s+in|original\s+)\s*', '', t, flags=re.I)
    # Reject lines that are clearly not color (engine, drivetrain, etc.)
    if re.search(r'\b(liter|flat|engine|speed|drive|wheel|transaxle|differential|leather|seat)\b', t, re.I):
        return None
    return t.strip() or None


def _parse_engine(text: str):
    """Extract engine description from li text like '3.6-Liter M64 Flat-Six'."""
    if re.search(r'\b(liter|cc\b|flat-?(?:four|six)|boxer|turbo\b.*engine|naturally.aspirated)', text, re.I):
        return text.strip()
    return None


def _parse_drivetrain(text: str):
    """Extract drivetrain from li text like 'All-Wheel Drive', 'Rear-Wheel Drive'."""
    t = text.lower()
    if re.search(r'\ball.wheel.drive\b|awd\b', t):
        return "AWD"
    if re.search(r'\brear.wheel.drive\b|rwd\b', t):
        return "RWD"
    if re.search(r'\bfront.wheel.drive\b|fwd\b', t):
        return "FWD"
    return None


def _fetch_listing_fields(listing_url: str) -> dict:
    """
    Fetch a BaT listing page and extract all fields from the Listing Details block.
    Returns a dict with keys: vin, mileage, transmission, engine, drivetrain, color, options.
    Returns None on unrecoverable fetch error.
    """
    empty = {"vin": None, "mileage": None, "transmission": None,
             "engine": None, "drivetrain": None, "color": None, "options": None}

    for attempt in range(3):
        try:
            r = SESSION.get(listing_url, timeout=20)
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                log.warning("429 rate-limited — waiting %ds (attempt %d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                log.debug("404 (listing removed): %s", listing_url)
                return empty.copy()
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "lxml")
            result = empty.copy()
            leftover = []  # li items that don't match a known field → goes into options

            # Parse .essentials li items — BaT free-text format (not "Label: Value")
            # e.g. "Chassis: WP0AB096XKS450171", "51k Miles Shown", "Five-Speed Manual Transaxle"
            for li in soup.select(".essentials li, .listing-essentials li"):
                txt = li.get_text(" ", strip=True)
                if not txt:
                    continue
                lower = txt.lower()
                claimed = False

                # VIN: "Chassis: WP0..."
                if "chassis" in lower and not result["vin"]:
                    m = re.search(r'([A-HJ-NPR-Z0-9]{17})', txt)
                    if m:
                        result["vin"] = m.group(1)
                        claimed = True

                # Mileage: "51k Miles Shown" or "51,000 Miles"
                if not claimed and result["mileage"] is None and re.search(r'\bmiles?\b', lower):
                    result["mileage"] = _parse_mileage(txt)
                    claimed = True

                # Drivetrain: "All-Wheel Drive", "Rear-Wheel Drive" — must come
                # before transmission so a combined li like "6-Speed Rear-Wheel Drive"
                # is not fully consumed by the transmission check first.
                if not claimed and not result["drivetrain"]:
                    drv = _parse_drivetrain(txt)
                    if drv:
                        result["drivetrain"] = drv
                        claimed = True

                # Transmission: "Five-Speed Manual Transaxle", "PDK", "Tiptronic", etc.
                if not claimed and not result["transmission"] and re.search(
                    r'\b(speed|pdk|tiptronic|automatic|manual|transaxle|doppelkupplung)\b', lower
                ):
                    result["transmission"] = _parse_transmission_from_essentials(txt)
                    if result["transmission"]:
                        claimed = True

                # Engine: "3.6-Liter M64 Flat-Six"
                if not claimed and not result["engine"]:
                    eng = _parse_engine(txt)
                    if eng:
                        result["engine"] = eng
                        claimed = True

                # Color: "Repainted in Guards Red", "Black over Tan"
                if not claimed and not result["color"] and re.search(
                    r'\b(repaint|finish|paint|color|colour)\b', lower
                ):
                    col = _parse_color(txt)
                    if col:
                        result["color"] = col
                        claimed = True

                # Anything else goes into options (skip chassis line even if VIN parse failed)
                if not claimed and "chassis" not in lower:
                    leftover.append(txt)

            if leftover:
                import json
                result["options"] = json.dumps(leftover)

            # VIN fallback — WP0 regex on raw page text
            if not result["vin"]:
                m = re.search(r'\b(WP0[A-Z0-9]{14})\b', r.text)
                if m:
                    result["vin"] = m.group(1)
            if not result["vin"]:
                m = re.search(r'[Cc]hassis[^A-Z0-9]*([A-HJ-NPR-Z0-9]{17})\b', r.text)
                if m:
                    result["vin"] = m.group(1)

            return result

        except Exception as e:
            if attempt < 2:
                log.warning("fetch error %s — retry %d/3", e, attempt + 1)
                time.sleep(10 * (attempt + 1))
            else:
                log.error("fetch failed after 3 attempts: %s — %s", listing_url, e)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    db.init_db()
    conn = db.get_conn()

    # Target: 911/Cayman/Boxster/718 BaT comps missing any enrichable field
    rows = conn.execute("""
        SELECT id, listing_url, year, model, vin, mileage, transmission, engine, drivetrain, color, options
        FROM sold_comps
        WHERE source = 'BaT'
          AND listing_url IS NOT NULL
          AND listing_url != ''
          AND (
              LOWER(title) LIKE '%911%'
              OR LOWER(title) LIKE '%cayman%'
              OR LOWER(title) LIKE '%boxster%'
              OR LOWER(title) LIKE '%718%'
          )
          AND (
              vin IS NULL OR vin = ''
              OR mileage IS NULL
              OR transmission IS NULL
              OR engine IS NULL
              OR drivetrain IS NULL
          )
        ORDER BY id
    """).fetchall()

    total_missing = len(rows)
    log.info("Found %d BaT comps missing VIN / mileage / transmission", total_missing)

    if not total_missing:
        log.info("Nothing to do — all comps fully enriched.")
        conn.close()
        return

    prog      = _load_progress()
    done_ids  = set(prog.get("done_ids", []))
    failed_ids = set(prog.get("failed_ids", []))
    updated   = prog.get("updated", 0)

    # A row already in done_ids is only truly skippable if drivetrain is now populated.
    # Rows that were processed before the drivetrain parser existed will still have
    # drivetrain=NULL — re-include them so the new parser can fill the field.
    done_with_drivetrain = {r["id"] for r in rows if r["id"] in done_ids and r["drivetrain"]}
    pending = [r for r in rows if r["id"] not in done_with_drivetrain and r["id"] not in failed_ids]
    log.info("%d pending  |  already done: %d  |  failed: %d",
             len(pending), len(done_ids), len(failed_ids))

    vin_added   = 0
    miles_added = 0
    trans_added = 0

    for i, row in enumerate(pending, 1):
        comp_id     = row["id"]
        listing_url = row["listing_url"]
        label       = f"{row['year']} {row['model']} (id={comp_id})"

        fields = _fetch_listing_fields(listing_url)

        if fields is None:
            # Unrecoverable fetch error
            failed_ids.add(comp_id)
            continue

        # Build UPDATE only for fields that are missing and now found
        updates = {}
        if not row["vin"] and fields["vin"]:
            updates["vin"] = fields["vin"]
            vin_added += 1
        if row["mileage"] is None and fields["mileage"] is not None:
            updates["mileage"] = fields["mileage"]
            miles_added += 1
        if not row["transmission"] and fields["transmission"]:
            updates["transmission"] = fields["transmission"]
            trans_added += 1
        if not row["engine"] and fields.get("engine"):
            updates["engine"] = fields["engine"]
        if not row["drivetrain"] and fields.get("drivetrain"):
            updates["drivetrain"] = fields["drivetrain"]
        if not row["color"] and fields.get("color"):
            updates["color"] = fields["color"]
        if not row["options"] and fields.get("options"):
            updates["options"] = fields["options"]

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [comp_id]
            conn.execute(f"UPDATE sold_comps SET {set_clause} WHERE id=?", vals)
            conn.commit()
            updated += 1
            log.info("[%d/%d] %-42s  vin=%-18s  miles=%-7s  trans=%s",
                     i, len(pending), label,
                     updates.get("vin", "—"),
                     str(updates.get("mileage", "—")),
                     updates.get("transmission", "—"))
        else:
            log.debug("[%d/%d] %-42s  nothing new found", i, len(pending), label)

        done_ids.add(comp_id)

        # Save progress every 25 fetches
        if i % 25 == 0:
            prog["done_ids"]   = list(done_ids)
            prog["failed_ids"] = list(failed_ids)
            prog["updated"]    = updated
            _save_progress(prog)
            log.info("--- checkpoint: %d VINs  %d mileages  %d transmissions added so far ---",
                     vin_added, miles_added, trans_added)

        if i < len(pending):
            time.sleep(random.uniform(5, 8))

    # Final save
    prog["done_ids"]     = list(done_ids)
    prog["failed_ids"]   = list(failed_ids)
    prog["updated"]      = updated
    prog["completed_at"] = datetime.now().isoformat(timespec="seconds")
    _save_progress(prog)

    conn.close()
    log.info("Done — %d VINs  %d mileages  %d transmissions added  |  %d failed",
             vin_added, miles_added, trans_added, len(failed_ids))


if __name__ == "__main__":
    run()

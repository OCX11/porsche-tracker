"""
distill_watcher.py
------------------
File-system watcher that processes Distill webhook drop files.

Watches ~/porsche-tracker/distill_drops/ for new JSON files written by
distill_receiver.py. For each file it:

  1. Reads and parses the Distill payload
  2. Detects the source site from the URI
  3. Extracts/cleans: year, model, trim, price, mileage, location, url, condition
  4. Upserts the record into the main inventory.db (listings + price_history)
  5. Moves the processed file to distill_drops/processed/ so it's never re-run

Run permanently via launchd (see com.porschetracker.distill-watcher.plist).

──────────────────────────────────────────────────────────────────────────────
Supported Distill monitors
──────────────────────────────────────────────────────────────────────────────
  classic.com          → dealer="classic.com"           category=AUCTION
  cars.com             → dealer="cars.com"              category=RETAIL
  rennlist.com         → dealer="Rennlist"              category=RETAIL
  builtforbackroads.com→ dealer="Built for Backroads"   category=DEALER
  mart.pca.org         → dealer="PCA Mart"               category=RETAIL

──────────────────────────────────────────────────────────────────────────────
Distill payload shapes handled
──────────────────────────────────────────────────────────────────────────────
Shape A — single change object (most common for page monitors):
  {
    "uri": "https://classic.com/...",
    "content": "2019 Porsche 911 Carrera S — $124,500 — 8,200 mi",
    "changes": [ { "key": "price", "old": "129500", "new": "124500" } ]
  }

Shape B — structured data table (Distill data extraction):
  {
    "uri": "https://cars.com/...",
    "data": [
      { "title": "2019 Porsche 911 Carrera S", "price": "$124,500",
        "mileage": "8,200 mi", "location": "Philadelphia, PA",
        "url": "https://cars.com/vehicledetail/..." }
    ]
  }

Shape C — raw HTML diff (older Distill versions):
  { "uri": "...", "diff": "<ins>2019 Porsche 911...</ins>" }
"""

import json
import logging
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import sys
sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn, init_db, upsert_listing

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
DROP_DIR      = BASE_DIR / "distill_drops"
PROCESSED_DIR = DROP_DIR / "processed"
LOG_FILE      = BASE_DIR / "logs" / "distill_watcher.log"
POLL_INTERVAL = 2

DROP_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Source routing
# ─────────────────────────────────────────────────────────────────────────────

# Maps a URI hostname fragment → (dealer_name, source_category, skip)
# skip=True means we already have an active scraper for this source — log and
# archive without writing to the DB so we don't double-count.
_SOURCE_MAP = [
    ("classic.com",          "classic.com",            "AUCTION", False),
    ("cars.com",             "cars.com",               "RETAIL",  False),
    ("rennlist.com",         "Rennlist",               "RETAIL",  False),
    ("builtforbackroads.com","Built for Backroads",    "DEALER",  False),
    ("mart.pca.org",         "PCA Mart",               "RETAIL",  False),  # allow Distill to catch listings the main scraper misses
    ("pca.org",              "PCA Mart",               "RETAIL",  False),  # pca.org catch-all
]


def _resolve_source(uri: str) -> tuple:
    """
    Given a URI, return (dealer_name, source_category, skip).
    Falls back to (hostname, 'RETAIL', False) for unknown sources.
    """
    uri_lower = (uri or "").lower()
    for fragment, dealer, category, skip in _SOURCE_MAP:
        if fragment in uri_lower:
            return dealer, category, skip

    # Unknown source — extract hostname as dealer name, treat as RETAIL
    try:
        from urllib.parse import urlparse
        host = urlparse(uri).netloc.replace("www.", "")
        return host or "distill-unknown", "RETAIL", False
    except Exception:
        return "distill-unknown", "RETAIL", False


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_price(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if 5_000 < v < 5_000_000 else None
    s = re.sub(r"[^\d]", "", str(raw))
    return int(s) if s and 5_000 < int(s) < 5_000_000 else None


def _clean_mileage(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if 0 <= v < 500_000 else None
    s = re.sub(r"[^\d]", "", str(raw).split(" ")[0])
    return int(s) if s and 0 <= int(s) < 500_000 else None


_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-2]\d)\b")

# Price: $ prefix OR comma-formatted (e.g. 124,500).  Bare 4-digit numbers excluded.
_PRICE_RE = re.compile(r"\$(\d[\d,]+)|\b(\d{1,3}(?:,\d{3})+)\b")

# Mileage: digits followed by mi/miles/k mi
_MILE_RE = re.compile(r"([\d,]+)\s*(?:mi(?:les?)?|k\s*mi)", re.I)

_MODEL_TOKENS = [
    "911", "GT3", "GT2", "GT4", "Turbo S", "Turbo", "Carrera", "Targa",
    "Cayman", "Boxster", "Speedster", "Spyder", "Sport Classic",
    "930", "964", "993", "996", "997", "991", "992", "718",
]


def _parse_title(title: str) -> Dict:
    result = {"year": None, "make": "Porsche", "model": None, "trim": None}
    if not title:
        return result

    m = _YEAR_RE.search(title)
    if m:
        result["year"] = int(m.group(1))

    clean = re.sub(r"(?i)^(used|new|cpo|certified|pre-owned)\s+", "", title).strip()

    for tok in sorted(_MODEL_TOKENS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(tok)}\b", clean, re.I):
            result["model"] = tok if (tok.isdigit() or len(tok) <= 4) else "911"
            after = re.split(rf"\b{re.escape(tok)}\b", clean, maxsplit=1, flags=re.I)[-1]
            trim = re.sub(r"^\s*[-–—,]\s*", "", after).strip()
            # Split on em-dash, pipe, dollar sign, or comma-formatted mileage
            trim = re.split(r"—|–|\||\$|\d{1,3}(,\d{3})+\s*mi", trim)[0].strip()
            # Strip trailing bare numbers (prices / mileage that bled in without $ or comma)
            trim = re.sub(r"\s+\d{4,}(?:\s*mi(?:les?)?)?\s*$", "", trim, flags=re.I).strip()
            # Repeat once more to catch chained bleed e.g. "4S 149900 12400 mi"
            trim = re.sub(r"\s+\d{4,}(?:\s*mi(?:les?)?)?\s*$", "", trim, flags=re.I).strip()
            if trim:
                result["trim"] = trim
            break

    return result


def _extract_price_from_text(text: str) -> Optional[int]:
    """Find a price in free text. Requires $ prefix or comma formatting."""
    if not text:
        return None
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        candidate = _clean_price(raw)
        if candidate and candidate >= 10_000:
            return candidate
    return None


def _extract_mileage_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = _MILE_RE.search(text)
    return _clean_mileage(m.group(1)) if m else None


def _to_int(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _extract_listings_from_payload(payload: Dict, source_uri: str) -> List[Dict]:
    """
    Normalise any Distill payload shape into a list of listing dicts.
    source_uri is used as fallback URL when individual rows don't have one.
    """
    listings = []

    # ── Shape B: structured data table ───────────────────────────────────────
    data = payload.get("data") or payload.get("listings") or []
    if isinstance(data, list) and data:
        for row in data:
            if not isinstance(row, dict):
                continue
            parsed = _parse_title(
                row.get("title") or row.get("name") or row.get("heading") or ""
            )
            listings.append({
                "year":      parsed["year"] or _to_int(row.get("year")),
                "make":      "Porsche",
                "model":     parsed["model"] or row.get("model"),
                "trim":      parsed["trim"]  or row.get("trim"),
                "price":     _clean_price(row.get("price") or row.get("asking_price")),
                "mileage":   _clean_mileage(row.get("mileage") or row.get("miles")),
                "url":       row.get("url") or row.get("link") or source_uri,
                "location":  row.get("location") or row.get("city") or "",
                "condition": row.get("condition") or "",
                "vin":       row.get("vin") or None,
            })
        return listings

    # ── Shape A / C: single URI + content / diff ─────────────────────────────
    content  = payload.get("content") or payload.get("diff") or payload.get("text") or ""
    changes  = payload.get("changes") or []

    change_map = {}
    for ch in changes:
        if isinstance(ch, dict) and "key" in ch and "new" in ch:
            change_map[ch["key"].lower()] = ch["new"]

    parsed  = _parse_title(content or source_uri)
    price   = (
        _clean_price(change_map.get("price") or change_map.get("asking_price"))
        or _extract_price_from_text(content)
    )
    mileage = (
        _clean_mileage(change_map.get("mileage") or change_map.get("miles"))
        or _extract_mileage_from_text(content)
    )

    if parsed["year"] or price or source_uri:
        listings.append({
            "year":      parsed["year"],
            "make":      "Porsche",
            "model":     parsed["model"],
            "trim":      parsed["trim"],
            "price":     price,
            "mileage":   mileage,
            "url":       change_map.get("url") or source_uri,
            "location":  change_map.get("location") or "",
            "condition": change_map.get("condition") or "",
            "vin":       change_map.get("vin") or None,
        })

    return listings


# ─────────────────────────────────────────────────────────────────────────────
# Core processing
# ─────────────────────────────────────────────────────────────────────────────

def process_file(path: Path):
    log.info("Processing %s", path.name)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Cannot parse %s: %s", path.name, exc)
        _archive(path, error=True)
        return

    uri = payload.get("uri") or payload.get("url") or ""
    dealer, category, skip = _resolve_source(uri)

    # PCA Mart (and any other skip=True source) — active scraper owns it
    if skip:
        log.info("SKIP %s — active scraper owns '%s', not double-writing", path.name, dealer)
        _archive(path)
        return

    log.info("Source: %s (%s)  uri=%s", dealer, category, uri[:80])

    listings = _extract_listings_from_payload(payload, uri)
    if not listings:
        log.warning("No listings extracted from %s — check payload shape", path.name)
        log.debug("Payload preview: %s", json.dumps(payload, indent=2)[:600])
        _archive(path)
        return

    today    = date.today().isoformat()
    inserted = updated = skipped = 0

    try:
        with get_conn() as conn:
            for lst in listings:
                year    = lst["year"]
                model   = lst["model"] or "911"
                trim    = lst["trim"]  or ""
                price   = lst["price"]
                mileage = lst["mileage"]
                url     = lst["url"] or ""
                vin     = lst["vin"]

                if not year and not price and not url:
                    skipped += 1
                    continue

                listing_id, is_new, price_changed = upsert_listing(
                    conn,
                    dealer=dealer,
                    year=year,
                    make="Porsche",
                    model=model,
                    trim=trim,
                    mileage=mileage,
                    price=price,
                    vin=vin,
                    url=url,
                    today=today,
                )

                if is_new:
                    inserted += 1
                    log.info(
                        "NEW  [%s] %s %s %s %s  $%s  %s mi  id=%s",
                        dealer, year, "Porsche", model, trim,
                        "{:,}".format(price) if price else "?",
                        "{:,}".format(mileage) if mileage else "?",
                        listing_id,
                    )
                elif price_changed:
                    updated += 1
                    log.info(
                        "PRICE CHANGE  [%s] id=%s  %s %s → $%s",
                        dealer, listing_id, year, model,
                        "{:,}".format(price) if price else "?",
                    )

    except Exception as exc:
        log.exception("DB error processing %s: %s", path.name, exc)
        _archive(path, error=True)
        return

    log.info("Done %s → inserted=%d  updated=%d  skipped=%d", path.name, inserted, updated, skipped)
    _archive(path)


def _archive(path: Path, error: bool = False):
    dest_dir = PROCESSED_DIR / ("errors" if error else "")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest_dir / path.name))


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog handler
# ─────────────────────────────────────────────────────────────────────────────

class DropHandler(FileSystemEventHandler):

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() == ".json" and path.parent == DROP_DIR:
            time.sleep(0.5)
            process_file(path)

    def on_moved(self, event):
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() == ".json" and path.parent == DROP_DIR:
            time.sleep(0.5)
            process_file(path)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("Distill watcher starting — watching %s", DROP_DIR)
    log.info("Active source routes: %s", [s[1] for s in _SOURCE_MAP if not s[3]])
    log.info("Skipped (own scraper): %s", [s[1] for s in _SOURCE_MAP if s[3]])

    init_db()

    existing = sorted(DROP_DIR.glob("distill_*.json"))
    if existing:
        log.info("Catching up: %d unprocessed file(s) from previous run", len(existing))
        for f in existing:
            process_file(f)

    observer = Observer()
    observer.schedule(DropHandler(), str(DROP_DIR), recursive=False)
    observer.start()
    log.info("Watcher running.")

    try:
        while True:
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Shutting down…")
        observer.stop()

    observer.join()
    log.info("Watcher stopped.")


if __name__ == "__main__":
    main()

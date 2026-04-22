"""
enrich_vin_trim.py — Fill missing trim fields using VIN series codes and NHTSA API.

Tier 1: Local VIN decode (positions 4-6 encode Porsche series/trim family)
Tier 2: NHTSA vPIC API (free, rate-limited)

Run: python3 enrich_vin_trim.py
"""
import json
import logging
import time
import sqlite3
from typing import Optional
from pathlib import Path

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    urlopen = None

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

DB_PATH = Path(__file__).parent / "data" / "inventory.db"


# ── Tier 1: Local VIN Series Code → Trim Family ──────────────────────────────
# Porsche VIN positions 4-6 (0-indexed 3:6) encode the model series.
# This maps those codes to the most likely trim family.
# Source: cross-referenced from actual listing data in the DB.
#
# For 992/718 era (2020+), the codes are well-defined:
#   AA2 = base Carrera / base Cayman     AB2 = Carrera S/4S/GTS / Cayman S/GTS
#   AC2 = GT3 / GT4                      AD2 = Turbo / Turbo S / Cayman GTS(some)
#   AE2 = GT4 RS                         AF2 = GT3 RS / S/T
#   AG2 = Sport Classic                  BA2 = Targa 4 (base)
#   BB2 = Targa 4S / Targa 4 GTS        CA2 = Carrera Cab / base Boxster
#   CB2 = Carrera S Cab / Boxster S      CC2 = Spyder (718)
#   CD2 = Turbo Cab / Turbo S Cab / Boxster GTS
#   CE2 = Spyder RS
#
# For older eras, the codes are less granular. We map to the broadest
# useful trim family. Better to set "Carrera" than leave NULL.

_VIN_SERIES_TO_TRIM = {
    # ── 911 Coupe variants ──
    "AA2": {"911": "Carrera",       "Cayman": None,       "718 Cayman": None,
            "718": None,            "Boxster": None,      "718 Boxster": None},
    "AB2": {"911": "Carrera S",     "Cayman": "S",        "718 Cayman": "S",
            "718": "S",             "Boxster": "S",       "718 Boxster": "S"},
    "AC2": {"911": "GT3",           "Cayman": "GT4",      "718 Cayman": "GT4",
            "718": "GT4"},
    "AD2": {"911": "Turbo",         "Cayman": "GTS",      "718 Cayman": "GTS",
            "718": "GTS"},
    "AE2": {"911": "GT3",           "Cayman": "GT4 RS",   "718 Cayman": "GT4 RS",
            "718": "GT4 RS"},
    "AF2": {"911": "GT3 RS"},       # also S/T — can't distinguish from VIN alone
    "AG2": {"911": "Sport Classic"},

    # ── 911 Targa ──
    "BA2": {"911": "Targa 4"},
    "BB2": {"911": "Targa 4S"},

    # ── 911 Cabriolet / Boxster ──
    "CA2": {"911": "Carrera Cabriolet", "Boxster": None,  "718 Boxster": None,
            "718": None},
    "CB2": {"911": "Carrera S",     "Boxster": "S",       "718 Boxster": "S",
            "718": "S"},
    "CC2": {"911": "Speedster",     "Boxster": "Spyder",  "718 Boxster": "Spyder",
            "718": "Spyder"},
    "CD2": {"911": "Turbo",         "Boxster": "GTS",     "718 Boxster": "GTS",
            "718": "GTS"},
    "CE2": {"Boxster": "Spyder RS", "718 Boxster": "Spyder RS",
            "718": "Spyder RS"},

    # ── Pre-992 series codes (less granular) ──
    # 996/997/991 used similar but not identical codes.
    # For older cars, positions 4-6 often follow the same pattern.
    # These are safe mappings that won't produce wrong trims.
    "AA0": {"911": "Carrera"},      # 964/993 era base
    "AB0": {"911": "Carrera"},      # 964/993 era
    "JA0": {"911": "Turbo"},        # 930 Turbo
    "JB0": {"911": "Turbo"},        # 930 Turbo
}


def _vin_local_trim(vin, model):
    # type: (str, str) -> Optional[str]
    """Decode trim from VIN series code (positions 4-6)."""
    if not vin or len(vin) < 6:
        return None
    code = vin[3:6].upper()
    mapping = _VIN_SERIES_TO_TRIM.get(code)
    if not mapping:
        return None
    # Try exact model match, then normalized
    model_key = (model or "").strip()
    if model_key in mapping:
        return mapping[model_key]
    # Try lowercase matching
    for k, v in mapping.items():
        if k.lower() == model_key.lower():
            return v
    # Try partial matching (e.g. model="911" matches key="911")
    for k, v in mapping.items():
        if k in model_key or model_key in k:
            return v
    return None


# ── Tier 2: NHTSA vPIC API ──────────────────────────────────────────────────

_NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/%s?format=json"
_NHTSA_CACHE = {}  # in-memory cache to avoid re-hitting same VINs


def _vin_nhtsa_trim(vin):
    # type: (str) -> Optional[str]
    """Query NHTSA vPIC API for trim/series info."""
    if not vin or len(vin) != 17:
        return None
    if vin in _NHTSA_CACHE:
        return _NHTSA_CACHE[vin]
    if urlopen is None:
        return None

    try:
        url = _NHTSA_URL % vin
        req = Request(url, headers={"User-Agent": "PTOX11/1.0"})
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.debug("NHTSA API error for %s: %s", vin, e)
        _NHTSA_CACHE[vin] = None
        return None

    results = {r["Variable"]: r["Value"] for r in data.get("Results", []) if r.get("Value")}
    
    # NHTSA sometimes has "Series" which gives trim family
    series = results.get("Series", "")
    trim = results.get("Trim", "")
    
    decoded = None
    if trim:
        decoded = trim
    elif series:
        # Map NHTSA series names to our canonical trims
        s = series.lower()
        if "turbo s" in s:
            decoded = "Turbo S"
        elif "turbo" in s:
            decoded = "Turbo"
        elif "gt3 rs" in s:
            decoded = "GT3 RS"
        elif "gt3" in s:
            decoded = "GT3"
        elif "gt2" in s:
            decoded = "GT2 RS" if "rs" in s else "GT2"
        elif "gt4 rs" in s:
            decoded = "GT4 RS"
        elif "gt4" in s:
            decoded = "GT4"
        elif "carrera s" in s:
            decoded = "Carrera S"
        elif "carrera 4s" in s or "4s" in s:
            decoded = "Carrera 4S"
        elif "carrera gts" in s or "gts" in s:
            decoded = "Carrera GTS"
        elif "carrera 4" in s:
            decoded = "Carrera 4"
        elif "carrera" in s:
            decoded = "Carrera"
        elif "targa" in s:
            decoded = "Targa 4S" if "4s" in s else "Targa 4"
        elif "speedster" in s:
            decoded = "Speedster"
        elif "spyder" in s:
            decoded = "Spyder"
        else:
            decoded = series  # return raw if we can't map

    # Clean up NHTSA multi-trim responses like "Cayman / Cayman T"
    if decoded:
        # Take first option when NHTSA gives "X / Y" alternatives
        if ' / ' in decoded:
            decoded = decoded.split(' / ')[0].strip()
        # Remove model prefix (NHTSA sometimes returns "Cayman, Cayman Style Edition")
        if ', ' in decoded:
            decoded = decoded.split(', ')[0].strip()
        # Strip "Boxster 718" prefix
        if decoded.startswith('Boxster 718'):
            decoded = 'Boxster'
    _NHTSA_CACHE[vin] = decoded
    return decoded


# ── Main enrichment logic ────────────────────────────────────────────────────

def enrich_missing_trims(conn=None, dry_run=False):
    # type: (Optional[sqlite3.Connection], bool) -> dict
    """
    Find listings with VINs but missing trims, decode trims, update DB.
    
    Returns dict with stats: {total, enriched_local, enriched_nhtsa, failed}
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close_conn = True

    rows = conn.execute(
        """SELECT id, vin, year, model, trim, dealer
           FROM listings
           WHERE status='active'
             AND vin IS NOT NULL AND vin != ''
             AND (trim IS NULL OR trim = '')"""
    ).fetchall()

    stats = {"total": len(rows), "enriched_local": 0, "enriched_nhtsa": 0, "failed": 0}
    updates = []

    for lid, vin, year, model, trim, dealer in rows:
        # Tier 1: Local VIN decode
        decoded = _vin_local_trim(vin, model)
        source = "local"

        # Tier 2: NHTSA API (only if local fails)
        if decoded is None:
            decoded = _vin_nhtsa_trim(vin)
            source = "nhtsa"
            # Rate limit: 0.5s between NHTSA calls
            time.sleep(0.5)

        if decoded:
            updates.append((decoded, lid))
            if source == "local":
                stats["enriched_local"] += 1
            else:
                stats["enriched_nhtsa"] += 1
            log.info("  [%s] %s %s %s VIN=%s -> trim='%s' (%s)",
                     dealer[:15], year, model, vin[:11], vin[3:6], decoded, source)
        else:
            stats["failed"] += 1
            log.info("  [%s] %s %s %s VIN=%s -> FAILED (no decode)",
                     dealer[:15], year, model, vin[:11], vin[3:6])

    if not dry_run and updates:
        conn.executemany("UPDATE listings SET trim = ? WHERE id = ?", updates)
        conn.commit()
        log.info("Updated %d listings with enriched trims", len(updates))
    elif dry_run:
        log.info("DRY RUN — would update %d listings", len(updates))

    if close_conn:
        conn.close()

    return stats


# ── Also enrich listings that have trims but are poorly normalized ───────────

def enrich_all_vins_with_trims(conn=None, dry_run=False):
    # type: (Optional[sqlite3.Connection], bool) -> dict
    """
    Broader pass: for ALL listings with VINs, check if the VIN series code
    gives a more specific trim than what we have. Only upgrade, never downgrade.
    
    E.g. listing has trim="Base" but VIN says AC2 (GT3/GT4) -> upgrade to GT3.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close_conn = True

    rows = conn.execute(
        """SELECT id, vin, year, model, trim, dealer
           FROM listings
           WHERE status='active'
             AND vin IS NOT NULL AND LENGTH(vin) = 17"""
    ).fetchall()

    stats = {"checked": len(rows), "upgraded": 0}
    upgrades = []

    # Trims that are "uninformative" — VIN decode would be more useful
    uninformative = {None, "", "Base", "BASE", "base", "911", "718"}

    for lid, vin, year, model, trim, dealer in rows:
        if trim not in uninformative:
            continue
        decoded = _vin_local_trim(vin, model)
        if decoded and decoded != trim:
            upgrades.append((decoded, lid))
            stats["upgraded"] += 1
            log.info("  UPGRADE: %s %s '%s' -> '%s' (VIN %s)",
                     year, model, trim or "NULL", decoded, vin[3:6])

    if not dry_run and upgrades:
        conn.executemany("UPDATE listings SET trim = ? WHERE id = ?", upgrades)
        conn.commit()
        log.info("Upgraded %d listings with better trims from VIN", len(upgrades))

    if close_conn:
        conn.close()

    return stats


# ── Title-keyword enrichment ─────────────────────────────────────────────────
# Some scrapers store the full listing title in the trim field.
# Keywords in the title can identify specific trims that VIN decoding can't.

# Trims specific enough that no keyword pass is needed.
_SPECIFIC_TRIMS = frozenset({
    "GT3", "GT3 RS", "GT3 Touring", "GT3 Cup",
    "GT2", "GT2 RS",
    "GT4", "GT4 RS",
    "Spyder RS", "Spyder",
    "Turbo S", "Turbo S Exclusive",
    "Speedster", "Sport Classic", "Safari",
    "RS America", "America Roadster",
    "Dakar", "S/T",
    "Carrera GT", "918 Spyder",
    "Boxster 25 Years",
})


def _detect_trim_from_keywords(year, model, trim_text):
    # type: (Optional[int], str, Optional[str]) -> Optional[str]
    """
    Return a canonical trim detected from keywords in trim_text.
    Returns None if no confident detection.
    Requires model/year context for ambiguous cases (e.g. Weissach).
    """
    if not trim_text:
        return None
    text = trim_text.lower().strip()
    model_lower = (model or "").lower()
    is_911 = "911" in model_lower or model_lower == "911"
    is_718 = "718" in model_lower or model_lower in ("cayman", "boxster")

    # "exclusive series" → Turbo S Exclusive (991.2+ = 2017+)
    if "exclusive series" in text:
        if is_911 and year and year >= 2017:
            return "Turbo S Exclusive"

    # "weissach" as a standalone keyword (not prefixed by a specific trim)
    # Weissach is an optional package only on GT2 RS, GT3 RS, GT4 RS, Spyder RS.
    if "weissach" in text:
        if is_911:
            if "gt2" in text:
                return "GT2 RS"
            # Default to GT3 RS (more common Weissach car)
            return "GT3 RS"
        if is_718:
            if "spyder" in text:
                return "Spyder RS"
            return "GT4 RS"

    # "speedster" anywhere → Speedster
    if "speedster" in text:
        return "Speedster"

    # "safari" → Safari special build
    if "safari" in text:
        return "Safari"

    # "sport classic" → Sport Classic
    if "sport classic" in text:
        return "Sport Classic"

    # "gt3 touring" or "gt3" + "touring" → GT3 Touring
    if "gt3 touring" in text or ("gt3" in text and "touring" in text):
        return "GT3 Touring"

    # "rs america" → RS America (964-era)
    if "rs america" in text:
        return "RS America"

    return None


def enrich_title_keywords(conn=None, dry_run=False):
    # type: (Optional[sqlite3.Connection], bool) -> dict
    """
    Keyword pass: detect more specific trim from keywords in the trim field.
    Targets listings where the trim field contains a title/description rather
    than a canonical trim. Only upgrades — never downgrades.
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fmv import normalize_trim

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close_conn = True

    rows = conn.execute(
        """SELECT id, year, model, trim, dealer
           FROM listings
           WHERE status='active'"""
    ).fetchall()

    stats = {"checked": len(rows), "enriched": 0}
    updates = []

    for lid, year, model, trim, dealer in rows:
        # Skip if already a specific canonical trim
        norm = normalize_trim(trim)
        if norm in _SPECIFIC_TRIMS:
            continue

        detected = _detect_trim_from_keywords(year, model, trim)
        if detected and detected != norm:
            updates.append((detected, lid))
            stats["enriched"] += 1
            log.info("  [TITLE-KW] %s %s %s trim '%s' -> '%s'",
                     dealer[:15] if dealer else "?", year, model,
                     (trim or "NULL")[:40], detected)

    if not dry_run and updates:
        conn.executemany("UPDATE listings SET trim = ? WHERE id = ?", updates)
        conn.commit()
        log.info("Title keyword enrichment: updated %d listings", len(updates))
    elif dry_run:
        log.info("DRY RUN — would update %d listings", len(updates))

    if close_conn:
        conn.close()

    return stats


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv

    log.info("=" * 60)
    log.info("VIN Trim Enrichment")
    log.info("=" * 60)

    log.info("\n--- Pass 1: Fill missing trims ---")
    stats1 = enrich_missing_trims(dry_run=dry_run)
    log.info("Results: %d total, %d local, %d NHTSA, %d failed" % (
        stats1["total"], stats1["enriched_local"], stats1["enriched_nhtsa"], stats1["failed"]))

    log.info("\n--- Pass 2: Upgrade uninformative trims ---")
    stats2 = enrich_all_vins_with_trims(dry_run=dry_run)
    log.info("Results: %d checked, %d upgraded" % (stats2["checked"], stats2["upgraded"]))

    log.info("\n--- Pass 3: Title keyword enrichment ---")
    stats3 = enrich_title_keywords(dry_run=dry_run)
    log.info("Results: %d checked, %d enriched" % (stats3["checked"], stats3["enriched"]))

    log.info("\nDone!")

"""
vin_decoder.py — 3-layer Porsche VIN decoder
=============================================

Layer 1 (local, instant, no API):
  - VIN pos 4-5   → body style (Coupe/Cabriolet/Targa) + drivetrain (RWD/AWD)
  - VIN pos 10    → authoritative model year (resolves 997.1/997.2/991.1/991.2 ambiguity)
  - VIN pos 7+8+12 → Porsche internal model code (997, 991, 992, 982, etc.)
  - VIN pos 4-6   → generation bucket (existing logic, now driven by VIN year not listing year)

Layer 2 (NHTSA, cached in DB):
  - Series name when scraper trim is dirty ("GT3", "Carrera", "Turbo")
  - Called once per VIN, result cached in vin_nhtsa_cache table
  - Skipped for non-WP0 VINs and pre-1999 cars

Layer 3 (fallback):
  - If no VIN, fall back to listing year + existing get_generation() logic

Run standalone to decode all VINs in DB:
    python3 vin_decoder.py
"""

import sqlite3
import logging
import urllib.request
import json
import time
import re
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "inventory.db"

# ── VIN year character table ─────────────────────────────────────────────────
_VIN_YEAR_MAP = {}
_sequence = [
    ("A",1980),("B",1981),("C",1982),("D",1983),("E",1984),("F",1985),
    ("G",1986),("H",1987),("J",1988),("K",1989),("L",1990),("M",1991),
    ("N",1992),("P",1993),("R",1994),("S",1995),("T",1996),("V",1997),
    ("W",1998),("X",1999),("Y",2000),
    ("1",2001),("2",2002),("3",2003),("4",2004),("5",2005),("6",2006),
    ("7",2007),("8",2008),("9",2009),
    ("A",2010),("B",2011),("C",2012),("D",2013),("E",2014),("F",2015),
    ("G",2016),("H",2017),("J",2018),("K",2019),("L",2020),("M",2021),
    ("N",2022),("P",2023),("R",2024),("S",2025),("T",2026),("V",2027),
    ("W",2028),("X",2029),("Y",2030),
]
for _ch, _yr in _sequence:
    _VIN_YEAR_MAP.setdefault(_ch, []).append(_yr)


def vin_model_year(vin: str, db_year: Optional[int] = None) -> Optional[int]:
    """Decode model year from VIN position 10 (index 9). Authoritative."""
    if not vin or len(vin) < 10:
        return None
    ch = vin[9].upper()
    candidates = _VIN_YEAR_MAP.get(ch)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if db_year:
        return min(candidates, key=lambda y: abs(y - db_year))
    return candidates[-1]  # most recent default


# ── VIN position 4-5 → body style + drivetrain ──────────────────────────────
# North American WP0 VINs only (positions 4-6 = ZZZ on European/RoW VINs)
#
# Position 4 = car line:
#   A = 911 Carrera body (coupe/convertible RWD or AWD)
#   B = 911 Targa
#   C = Boxster/Cayman convertible/roadster
#   D = Turbo (separate from Carrera in some generations)
#   J = Cayenne (WP1 prefix — won't reach here)
#
# Position 5 = drivetrain/body variant:
#   A = Coupe, RWD (2WD Carrera, GT cars)
#   B = Coupe, AWD (Carrera 4, Turbo coupe)
#   C = Cabriolet, RWD (Carrera Cabriolet)
#   D = Turbo Cabriolet AWD / Convertible AWD
#   B (with pos4=B) = Targa AWD

_POS45_MAP = {
    # 911 body
    "AA": ("Coupe",      "RWD"),   # Carrera, GT3, GT2
    "AB": ("Coupe",      "AWD"),   # Carrera 4, Turbo Coupe
    "AC": ("Coupe",      "RWD"),   # GT cars (GT3/GT3RS/GT2RS) — same code as AA but series=AC2
    "AD": ("Coupe",      "AWD"),   # Turbo coupe (some gens)
    "CA": ("Cabriolet",  "RWD"),   # Carrera Cabriolet
    "CB": ("Cabriolet",  "AWD"),   # Carrera 4 Cabriolet
    "CC": ("Cabriolet",  "RWD"),   # Cayman/Boxster variants
    "CD": ("Cabriolet",  "AWD"),   # Turbo Cabriolet
    "BB": ("Targa",      "AWD"),   # Targa 4/4S
    "BA": ("Targa",      "RWD"),   # Targa (rare)
}


def decode_body_style(vin: str) -> tuple:
    """
    Returns (body_style, drive_type) from VIN positions 4-5.
    body_style: 'Coupe' | 'Cabriolet' | 'Targa' | None
    drive_type: 'RWD' | 'AWD' | None
    Only works for North American WP0 VINs (pos 4-6 != ZZZ).
    """
    if not vin or len(vin) < 6:
        return (None, None)
    if vin[3:6].upper() == "ZZZ":
        return (None, None)   # European/RoW VIN — no body code
    pos45 = vin[3:5].upper()
    result = _POS45_MAP.get(pos45)
    if result:
        return result
    return (None, None)


# ── VIN pos 7+8+12 → Porsche model code ─────────────────────────────────────
# For North American VINs: model code = "9" + pos8 + pos12
# For RoW (ZZZ) VINs:     model code = pos7 + pos8 + pos12
# This gives us: 997, 991, 992, 982 (718), 986, 987, 981, etc.

def decode_porsche_model_code(vin: str) -> Optional[str]:
    """
    Decode Porsche internal model code from VIN.
    Works reliably for RoW (ZZZ) VINs only.
    For North American VINs, use vin_model_year() + series for generation.
    Returns None for North American VINs (model code not decodable this way).
    """
    if not vin or len(vin) < 13:
        return None
    vin = vin.upper()
    is_row = vin[3:6] == "ZZZ"
    if not is_row:
        return None   # NA VIN — pos 7+8+12 method doesn't apply
    # RoW: pos 7, 8, 12 (index 6, 7, 11)
    return vin[6] + vin[7] + vin[11]


# ── Generation from VIN (authoritative) ─────────────────────────────────────
# Uses VIN year (pos 10) instead of listing year to resolve overlap ambiguity.
# The 2012 997.2/991.1 overlap is resolved by VIN year character:
#   'C' in second cycle = 2012 model year (both could be 997.2 or 991.1)
#   Use Porsche model code to disambiguate: model code 997 → 997.2, 991 → 991.1

def decode_generation_from_vin(vin: str, db_year: Optional[int] = None) -> Optional[str]:
    """
    Returns authoritative generation string using VIN positions.
    Falls back to None if VIN is insufficient.

    Generation strings match fmv.py get_generation() output:
    997_1, 997_2, 991_1, 991_2, 992, 996, 993, 964, etc.
    """
    if not vin or len(vin) < 10:
        return None

    vin = vin.upper().strip()

    # Only handle WP0 (Porsche sports cars) and pre-1981 classics
    if not vin.startswith("WP0"):
        if re.match(r"^9(11|12|14|16)", vin):
            return "Classic"
        return None

    if len(vin) < 17:
        return "Classic"

    # Get authoritative model year from VIN pos 10
    vin_year = vin_model_year(vin, db_year=db_year)
    if not vin_year:
        return None

    series = vin[3:6]  # pos 4-6

    # ── 911 Carrera / GT / Turbo family ─────────────────────────────────────
    if series in ("AA0","AB0","AC0","AA1","AB1","AC1","JA0","JB0","JC0"):
        if vin_year <= 1993: return "964"
        return "993"

    if series in ("AA2","AB2","AC2","AA3","AB3","AC3",
                  "AD2","AD3","CD2","CD3",
                  "CA2","CB2","BB2","BA2"):
        # CA2/CB2 = 911 Cabriolet (RWD/AWD) in modern era
        # BB2 = 911 Targa
        # NOTE: Boxster/Cayman also uses CA2/CB2 but in different year ranges
        # We resolve by year: post-1998 CA2/CB2 with a non-Boxster series context
        # are 911 Cabriolets. Actual Boxsters get CC2/CD2 or are identified
        # by the lower year range below in the Boxster block.

        if vin_year <= 1993: return "964"
        if vin_year <= 1998: return "993"
        if vin_year <= 2004: return "996"

        # 2005: could be last 996 Turbo — use series to distinguish
        if vin_year == 2005:
            if series in ("CD2","CD3","AD2","AD3"):
                # Turbo Cabriolet series codes — check if 996 or 997 Turbo
                # 996 Turbo ran through 2005 MY. Without model code for NA VINs,
                # use the fact that 997 Turbo also launched in 2007 for NA.
                # 2005 Turbo = almost certainly still 996 Turbo for NA market.
                return "996"
            return "997_1"

        if vin_year <= 2008: return "997_1"
        if vin_year == 2009: return "997_2"
        if vin_year <= 2011: return "997_2"

        # 2012: For NA-market WP0 VINs, production had fully transitioned
        # to the 991 by MY2012. All NA-sourced 2012 911s are 991.1.
        # (A tiny number of 997.2 carryovers may exist but they're European
        # imports with ZZZ VINs, which we don't handle here.)
        if vin_year == 2012: return "991_1"

        if vin_year <= 2016: return "991_1"
        if vin_year <= 2019: return "991_2"
        return "992"

    # ── Boxster / Cayman / 718 ───────────────────────────────────────────────
    # CC2/CD2 = 718-era Boxster/Cayman (2017+)
    # CA2/CB2 in pre-2005 range = 986/987 Boxster
    # These overlap with 911 CA2/CB2 so we handle via year ranges above
    # AE2/AE3 = 718 Cayman GT4/GT4 RS/Spyder RS (2020+)
    # CE2/CE3 = 718 Boxster Spyder RS / open variants
    if series in ("AE2","AE3","CE2","CE3"):
        return "718_cayman"

    if series in ("CC2","CC3","CB3","CD2","CD3"):
        if vin_year <= 2004: return "986"
        if vin_year <= 2011: return "987"
        if vin_year <= 2016: return "981"
        return "718_cayman"

    return None


# ── NHTSA lookup (Layer 2) ───────────────────────────────────────────────────
NHTSA_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
NHTSA_FIELDS = ("Model Year", "Series", "Body Class", "Drive Type", "Trim", "Make")


def _nhtsa_lookup(vin: str) -> dict:
    """Call NHTSA API. Returns dict of decoded fields. Empty dict on failure."""
    url = NHTSA_BASE.format(vin=vin)
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        results = data.get("Results", [{}])[0]
        return {
            k: results.get(k, "") for k in NHTSA_FIELDS
        }
    except Exception as e:
        log.debug("NHTSA lookup failed for %s: %s", vin, e)
        return {}


def get_nhtsa_cached(conn, vin: str) -> dict:
    """
    Get NHTSA data for VIN, using DB cache.
    Creates cache table if needed. Returns {} on miss or failure.
    """
    vin = vin.upper().strip()
    # Only look up WP0 VINs from 1999+
    if not vin.startswith("WP0") or len(vin) != 17:
        return {}

    # Ensure cache table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vin_nhtsa_cache (
            vin         TEXT PRIMARY KEY,
            model_year  TEXT,
            series      TEXT,
            body_class  TEXT,
            drive_type  TEXT,
            trim        TEXT,
            make        TEXT,
            fetched_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # Check cache
    row = conn.execute(
        "SELECT model_year, series, body_class, drive_type, trim, make FROM vin_nhtsa_cache WHERE vin=?",
        (vin,)
    ).fetchone()
    if row:
        return {
            "Model Year": row[0], "Series": row[1], "Body Class": row[2],
            "Drive Type": row[3], "Trim": row[4], "Make": row[5]
        }

    # Fetch from NHTSA
    data = _nhtsa_lookup(vin)
    if data:
        conn.execute("""
            INSERT OR REPLACE INTO vin_nhtsa_cache
            (vin, model_year, series, body_class, drive_type, trim, make)
            VALUES (?,?,?,?,?,?,?)
        """, (
            vin,
            data.get("Model Year",""), data.get("Series",""),
            data.get("Body Class",""), data.get("Drive Type",""),
            data.get("Trim",""), data.get("Make","")
        ))
        conn.commit()

    return data


# ── Body style normalization ─────────────────────────────────────────────────
def normalize_body_style(raw: str) -> Optional[str]:
    """Normalize body style string to Coupe/Cabriolet/Targa/None."""
    if not raw:
        return None
    r = raw.lower()
    if "targa" in r:
        return "Targa"
    if any(x in r for x in ("convertible", "cabriolet", "roadster", "spyder")):
        return "Cabriolet"
    if "coupe" in r or "hatchback" in r:
        return "Coupe"
    return None


# ── Full decode: all layers ──────────────────────────────────────────────────
def decode_vin_full(vin: str, db_year: Optional[int] = None, conn=None) -> dict:
    """
    Full 3-layer VIN decode. Returns dict with:
      vin_year:     int | None  — model year from VIN pos 10
      generation:   str | None  — '997_1', '991_2', '992', etc.
      body_style:   str | None  — 'Coupe' | 'Cabriolet' | 'Targa'
      drive_type:   str | None  — 'RWD' | 'AWD'
      model_code:   str | None  — '997', '991', '992', '982', etc.
      nhtsa_series: str | None  — 'GT3', 'Carrera', 'Turbo', etc.
      nhtsa_body:   str | None  — body class from NHTSA
      source:       str         — 'vin_local' | 'vin_nhtsa' | 'fallback'
    """
    if not vin or len(vin) < 10:
        return {"source": "fallback"}

    vin = vin.upper().strip()
    result = {}

    # Layer 1: local decode
    result["vin_year"]   = vin_model_year(vin, db_year)
    result["generation"] = decode_generation_from_vin(vin, db_year)
    body, drive          = decode_body_style(vin)
    result["body_style"] = body
    result["drive_type"] = drive
    result["model_code"] = decode_porsche_model_code(vin)
    result["source"]     = "vin_local"

    # Layer 2: NHTSA (only for 997.2+ US VINs, skip if body already known)
    if conn and vin.startswith("WP0") and len(vin) == 17:
        vin_yr = result.get("vin_year") or db_year or 0
        if vin_yr >= 2009:  # NHTSA is reliable from 997.2 onward
            nhtsa = get_nhtsa_cached(conn, vin)
            if nhtsa:
                result["nhtsa_series"] = nhtsa.get("Series") or None
                nb = normalize_body_style(nhtsa.get("Body Class",""))
                result["nhtsa_body"] = nb
                # Use NHTSA body if local failed
                if not result["body_style"] and nb:
                    result["body_style"] = nb
                    result["source"] = "vin_nhtsa"

    return result


# ── DB migration: add missing columns ───────────────────────────────────────
def _ensure_columns(conn):
    """Add vin_model_year, drive_type, generation columns to listings and sold_comps if missing."""
    for table, extra_cols in [
        ("listings",   ["drive_type", "vin_model_year", "generation"]),
        ("sold_comps", ["drive_type", "vin_model_year", "body_style"]),
    ]:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in extra_cols:
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                log.info("Added %s to %s", col, table)
    conn.commit()


# ── Main: decode all VINs in DB ──────────────────────────────────────────────
def main(use_nhtsa: bool = False):
    """
    Decode all VINs in listings and sold_comps.
    Set use_nhtsa=True to make NHTSA API calls (cached).
    Default False for fast local-only decode.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)

    # ── listings table ───────────────────────────────────────────────────────
    rows = conn.execute(
        "SELECT id, vin, year, model FROM listings WHERE vin IS NOT NULL AND vin != ''"
    ).fetchall()
    log.info("Decoding %d VINs in listings...", len(rows))

    updated = skipped = 0
    gen_counts = {}

    for row in rows:
        vin = (row["vin"] or "").strip().upper()
        if len(vin) < 10:
            skipped += 1
            continue

        decoded = decode_vin_full(vin, db_year=row["year"], conn=conn if use_nhtsa else None)

        gen  = decoded.get("generation")
        body = decoded.get("body_style")
        drv  = decoded.get("drive_type")
        vyr  = decoded.get("vin_year")

        if gen or body or drv:
            conn.execute("""
                UPDATE listings
                SET generation=COALESCE(?,generation),
                    body_style=COALESCE(?,body_style),
                    drive_type=COALESCE(?,drive_type),
                    vin_model_year=COALESCE(?,vin_model_year)
                WHERE id=?
            """, (gen, body, drv, vyr, row["id"]))
            updated += 1
            if gen:
                gen_counts[gen] = gen_counts.get(gen, 0) + 1
        else:
            skipped += 1

    conn.commit()
    log.info("listings: %d updated, %d skipped/unrecognized", updated, skipped)

    # ── sold_comps table ─────────────────────────────────────────────────────
    rows2 = conn.execute(
        "SELECT id, vin, year, model FROM sold_comps WHERE vin IS NOT NULL AND vin != ''"
    ).fetchall()
    log.info("Decoding %d VINs in sold_comps...", len(rows2))

    updated2 = skipped2 = 0
    for row in rows2:
        vin = (row["vin"] or "").strip().upper()
        if len(vin) < 10:
            skipped2 += 1
            continue

        decoded = decode_vin_full(vin, db_year=row["year"])  # no NHTSA for comps

        gen  = decoded.get("generation")
        body = decoded.get("body_style")
        drv  = decoded.get("drive_type")
        vyr  = decoded.get("vin_year")

        if gen or body or drv:
            conn.execute("""
                UPDATE sold_comps
                SET generation=COALESCE(?,generation),
                    body_style=COALESCE(?,body_style),
                    drive_type=COALESCE(?,drive_type),
                    vin_model_year=COALESCE(?,vin_model_year)
                WHERE id=?
            """, (gen, body, drv, vyr, row["id"]))
            updated2 += 1
        else:
            skipped2 += 1

    conn.commit()
    log.info("sold_comps: %d updated, %d skipped", updated2, skipped2)
    log.info("Generation breakdown (listings):")
    for gen, count in sorted(gen_counts.items(), key=lambda x: -x[1]):
        log.info("  %-12s %d", gen, count)

    conn.close()


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if "--run" in sys.argv:
        nhtsa = "--nhtsa" in sys.argv
        log.info("Running full VIN decode (NHTSA=%s)...", nhtsa)
        main(use_nhtsa=nhtsa)
    else:
        # Test decode on sample VINs
        test_vins = [
            ("WP0AA2A98AS706505",  2010, "Carrera"),
            ("WP0AB2A96AS720870",  2010, "Carrera S"),
            ("WP0CA2A98AS740405",  2010, "Carrera Cabriolet"),
            ("WP0CB2A98AS754463",  2010, "Carrera 4S Cabriolet"),
            ("WP0BB2A99AS733097",  2010, "Targa 4S"),
            ("WP0AC2A93AS783387",  2010, "GT3"),
            ("WP0CD2A90AS773082",  2010, "Turbo Cabriolet"),
            ("WP0AB29965S741731",  2005, "Carrera S (996 era)"),
            ("WP0AA29905S715208",  2005, "Carrera (997.1 era)"),
            ("WP0AB2A91CS730400",  2012, "Carrera Cabriolet 991.1"),
            ("WP0CB2A97HS730500",  2017, "Targa 4S 991.2"),
            ("WP0AB2A97MS730600",  2021, "Carrera Cabriolet 992"),
        ]

        print(f"\n{'VIN':<22} {'DB Yr':<7} {'VIN Yr':<8} {'Gen':<10} {'Body':<12} {'Drive':<6} {'Model':<6}  Expected")
        print("-" * 100)
        for vin, yr, label in test_vins:
            d = decode_vin_full(vin, db_year=yr)
            print(f"{vin:<22} {yr:<7} {str(d.get('vin_year','')):<8} "
                  f"{str(d.get('generation','')):<10} "
                  f"{str(d.get('body_style','')):<12} "
                  f"{str(d.get('drive_type','')):<6} "
                  f"{str(d.get('model_code','')):<6}  ({label})")

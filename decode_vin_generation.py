"""
decode_vin_generation.py
========================
Decodes Porsche VINs to add generation labels (993, 996, 997, 991, 992, etc.)
to the sold_comps table. Safe to re-run — skips already-decoded rows.

Generation is determined by VIN positions 4-6 (model series code) cross-
referenced against model year (position 10) where needed to disambiguate.

Run:
    python3 decode_vin_generation.py
"""

import sqlite3
import logging
import re
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "inventory.db"

# ---------------------------------------------------------------------------
# VIN year character → model year
# ---------------------------------------------------------------------------
# VIN position 10 year encoding cycles A-Y (skipping I, O, Q, U, Z) then 1-9,
# then repeats the letter cycle starting at 2010.  Because letters repeat, we
# use the db_year hint (from the title/listing year) to resolve ambiguity.
# Ordered list of (vin_char, earliest_year_for_this_char) pairs — first cycle
# covers 1980-2009, second cycle 2010-2039.
_VIN_YEAR_SEQUENCE = [
    # First cycle: 1980 – 2009
    ("A", 1980), ("B", 1981), ("C", 1982), ("D", 1983), ("E", 1984),
    ("F", 1985), ("G", 1986), ("H", 1987), ("J", 1988), ("K", 1989),
    ("L", 1990), ("M", 1991), ("N", 1992), ("P", 1993), ("R", 1994),
    ("S", 1995), ("T", 1996), ("V", 1997), ("W", 1998), ("X", 1999),
    ("Y", 2000),
    ("1", 2001), ("2", 2002), ("3", 2003), ("4", 2004), ("5", 2005),
    ("6", 2006), ("7", 2007), ("8", 2008), ("9", 2009),
    # Second cycle: 2010 – 2039 (letters repeat)
    ("A", 2010), ("B", 2011), ("C", 2012), ("D", 2013), ("E", 2014),
    ("F", 2015), ("G", 2016), ("H", 2017), ("J", 2018), ("K", 2019),
    ("L", 2020), ("M", 2021), ("N", 2022), ("P", 2023), ("R", 2024),
    ("S", 2025), ("T", 2026), ("V", 2027), ("W", 2028), ("X", 2029),
    ("Y", 2030),
]

# Build lookup: char → list of candidate years (in chronological order)
_VIN_YEAR_CANDIDATES: dict[str, list[int]] = {}
for _ch, _yr in _VIN_YEAR_SEQUENCE:
    _VIN_YEAR_CANDIDATES.setdefault(_ch, []).append(_yr)


def vin_model_year(vin: str, db_year: int | None = None) -> int | None:
    """Decode model year from VIN position 10 (index 9).

    Letters A-Y repeat every 30 years.  When a db_year hint is available
    (from the listing title or the sold_comps.year column) we pick the
    candidate closest to it.  Without a hint we return the most recent
    candidate (appropriate for post-2010 VINs which dominate the DB).
    """
    if not vin or len(vin) < 10:
        return None
    ch = vin[9].upper()
    candidates = _VIN_YEAR_CANDIDATES.get(ch)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Ambiguous letter — use db_year to pick the closest candidate
    if db_year:
        return min(candidates, key=lambda y: abs(y - db_year))
    # No hint — return most recent (safe default for modern inventory)
    return candidates[-1]


# ---------------------------------------------------------------------------
# Generation decoder
# Porsche VIN positions 4-6 (index 3:6) = model/series code
#
# 911 generations:
#   Pre-1974 (air-cooled, short VINs) — "Classic"
#   930  (1975–1989 Turbo)
#   964  (1989–1994) — AA0, AB0, AC0 family
#   993  (1994–1998) — AA0/AB0/AC0 but year 1994-1998
#   996  (1999–2004) — AA2, AB2, AC2 + year < 2005
#   997.1 (2005–2008) — AA2, AB2, AC2 + year 2005-2008
#   997.2 (2009–2012) — AA2, AB2, AC2 + year 2009-2012
#   991.1 (2012–2015) — AA2/AB2/AC2 + year 2012-2015
#   991.2 (2016–2019) — AA2/AB2/AC2 + year 2016-2019
#   992  (2019+)     — AA2/AB2/AC2 + year 2019+
#
# Boxster/Cayman:
#   986  Boxster (1997–2004) — CA2 + year ≤ 2004
#   987  Boxster/Cayman (2005–2012) — CA2/CB2 + year 2005-2012
#   981  Boxster/Cayman (2012–2016) — CA2/CB2 + year 2012-2016
#   718  Boxster/Cayman (2017+)     — CA2/CB2/CC2/CD2 + year 2017+
#
# Turbo (930): JA0, JB0 series
# GT cars: AC2 (GT3/GT3RS/GT2RS in 997/991/992 era)
# ---------------------------------------------------------------------------

def decode_generation(vin: str, db_year: int | None = None) -> str | None:
    """
    Returns a generation string like '992', '991.2', '991.1', '997.2', '997.1',
    '996', '993', '964', '718/982', '981', '987', '986', '930', or None if unknown.
    """
    if not vin or len(vin) < 10:
        return None

    vin = vin.upper().strip()

    # Must start with WP0 for modern Porsche
    if not vin.startswith("WP0") and not _is_pre_vin_porsche(vin):
        return None

    # Pre-1974 short VINs (classic air-cooled)
    if len(vin) < 17:
        return "Classic"

    series = vin[3:6]   # positions 4-6
    model_year = vin_model_year(vin, db_year=db_year) or db_year

    if model_year is None:
        return None

    # ---- 911 / Turbo / GT family ----
    if series in ("AA0", "AB0", "AC0", "AA1", "AB1", "AC1"):
        # 964 era: 1989-1994, 993 era: 1994-1998
        if model_year <= 1993:
            return "964"
        else:
            return "993"

    if series in ("JA0", "JB0", "JC0"):
        # 930 Turbo
        if model_year <= 1989:
            return "930"
        elif model_year <= 1993:
            return "964"  # 964 Turbo uses JB0
        else:
            return "993"  # 993 Turbo

    if series in ("AA2", "AB2", "AC2", "AA3", "AB3", "AC3"):
        # Guard: pre-1999 cars with AA2/AB2/AC2 series codes are 964/993-era
        # air-cooleds (Porsche used both series code families in that era).
        # The VIN year is the authoritative signal — never assign 996+ to a
        # car whose model year sits in the air-cooled window.
        if model_year <= 1993:
            return "964"
        elif model_year <= 1998:
            return "993"
        elif model_year <= 2004:
            return "996"
        elif model_year <= 2008:
            return "997.1"
        elif model_year <= 2012:
            return "997.2"
        elif model_year <= 2015:
            return "991.1"
        elif model_year <= 2019:
            return "991.2"
        else:
            return "992"

    # ---- Boxster / Cayman / 718 family ----
    if series in ("CA2", "CB2", "CC2", "CD2", "CA3", "CB3"):
        if model_year <= 2004:
            return "986"
        elif model_year <= 2011:
            return "987"
        elif model_year <= 2016:
            return "981"
        else:
            return "718/982"

    # ---- Panamera ----
    if series in ("AA8", "AB8", "AC8"):
        return "Panamera"

    # ---- Macan ----
    if series in ("BA4", "BA5"):
        return "Macan"

    # ---- Cayenne ----
    if series in ("EA1", "EA2", "EB1", "EB2"):
        return "Cayenne"

    # ---- Taycan ----
    if series in ("AA4", "AB4"):
        return "Taycan"

    return None


def _is_pre_vin_porsche(vin: str) -> bool:
    """Pre-1980 Porsches have non-WP0 VINs (e.g. 911XXXXXXX)."""
    return bool(re.match(r"^9(11|12|14|16)", vin))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Add generation column if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sold_comps)").fetchall()]
    if "generation" not in cols:
        conn.execute("ALTER TABLE sold_comps ADD COLUMN generation TEXT")
        conn.commit()
        log.info("Added 'generation' column to sold_comps")

    # Fetch all rows with a VIN (re-decode everything so it's idempotent)
    rows = conn.execute("""
        SELECT id, vin, year, title
        FROM sold_comps
        WHERE vin IS NOT NULL AND vin != ''
    """).fetchall()

    log.info("Decoding %d VINs...", len(rows))

    updated = 0
    unknown = 0
    gen_counts: dict[str, int] = {}

    for row in rows:
        gen = decode_generation(row["vin"], db_year=row["year"])
        if gen:
            conn.execute(
                "UPDATE sold_comps SET generation=? WHERE id=?",
                (gen, row["id"]),
            )
            gen_counts[gen] = gen_counts.get(gen, 0) + 1
            updated += 1
        else:
            unknown += 1
            log.debug("Unknown VIN pattern: %s | %s", row["vin"], row["title"])

    conn.commit()
    conn.close()

    log.info("Done — %d decoded, %d unknown", updated, unknown)
    log.info("Generation breakdown:")
    for gen, count in sorted(gen_counts.items(), key=lambda x: -x[1]):
        log.info("  %-12s %d", gen, count)


if __name__ == "__main__":
    main()

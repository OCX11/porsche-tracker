"""
fmv.py — Fair Market Value calculation engine for Porsche inventory tracker.

Usage:
    from fmv import get_fmv, FMVResult

    result = get_fmv(conn, year=2018, model="911", trim="GT3 Touring")
    print(result.weighted_median, result.confidence, result.comp_count)

FMV Hierarchy (from HANDOVER.md):
    BaT sold comps      weight 1.0  — real transactions, gold standard
    Classic.com sold    weight 1.0
    Cars & Bids sold    weight 1.0
    BaT reserve-not-met weight 0.5  — floor signal, not a true sale price
    Dealer asking       weight 0.3–0.7  (handled in report.py, not here)

Confidence levels:
    HIGH   — 4+ sold comps within 24 months for this segment
    MEDIUM — 2–3 sold comps, or 4+ but older than 12 months
    LOW    — 1 sold comp or only RNM data
    NONE   — no comparable data found
"""
import re
import math
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ── Generation mapping ────────────────────────────────────────────────────────
# Maps year ranges to Porsche generation codes so we don't compare
# a 996 GT3 price with a 991.2 GT3 price.

def get_generation(year: Optional[int], model: str, trim: str = "") -> str:
    """Return a generation bucket string for grouping comps."""
    if not year:
        return "unknown"

    model_lower = (model or "").lower()
    trim_lower  = (trim  or "").lower()

    if "911" in model_lower or model_lower == "911":
        if year <= 1963:  return "356_era"       # SWB 911 precursors
        if year <= 1973:  return "901_swb_lwb"   # 911/912/911S/911T/911E
        if year <= 1977:  return "g_series_early" # 2.7/Carrera RS era
        if year <= 1983:  return "g_series_sc"   # 911SC
        if year <= 1989:  return "g_series_32"   # 3.2 Carrera
        if year <= 1994:  return "964"
        if year <= 1998:  return "993"
        if year <= 2004:  return "996"
        if year <= 2013:  return "997"   # 997.2 ran through 2012 MY (titled 2013 in some markets)
        if year <= 2019:  return "991"
        return "992"

    if model_lower == "cayman":
        if year <= 2012:  return "987_cayman"
        if year <= 2016:  return "981_cayman"
        return "718_cayman"

    if model_lower == "boxster":
        if year <= 2004:  return "986_boxster"
        if year <= 2012:  return "987_boxster"
        if year <= 2016:  return "981_boxster"
        return "718_boxster"

    if model_lower in ("718", "718 cayman", "718 boxster", "718 spyder"):
        # 718-badged cars are all 2017+; use trim/model to split Cayman vs Boxster/Spyder.
        # Scrapers sometimes emit "718 Boxster" / "718 Cayman" / "718 Spyder" as the model.
        if model_lower in ("718 boxster", "718 spyder"):  return "718_boxster"
        if "spyder" in trim_lower:  return "718_boxster"
        if "boxster" in trim_lower: return "718_boxster"
        return "718_cayman"  # Cayman, GT4, GT4 RS, etc.

    if model_lower == "914":  return "914"
    if model_lower == "912":  return "912"
    if model_lower == "356":  return "356"
    if model_lower == "carrera gt" or "carrera gt" in model_lower:  return "carrera_gt"
    if model_lower == "918 spyder" or "918" in model_lower:  return "918_spyder"
    if model_lower == "944":  return "944"

    return model_lower or "unknown"


# ── Trim normalization ────────────────────────────────────────────────────────

# Maps messy/variant trim strings to a canonical form for grouping
_TRIM_ALIASES = {
    # ── Singer / restomod / coachbuilt — never comparable to stock ───────────
    # These are bespoke builds on 911 bodies. They sell for 5-20x stock prices
    # and must not pollute standard trim FMV pools. Map to a sentinel that will
    # score 0.0 against every real trim family.
    "dls by singer":                        "_restomod_singer",
    "carrera 4 coupe by singer":            "_restomod_singer",
    "carrera coupe by singer":              "_restomod_singer",
    "carrera targa by singer":              "_restomod_singer",
    "by singer":                            "_restomod_singer",
    "singer":                               "_restomod_singer",
    "by gunther werks":                     "_restomod_gw",
    "carrera by gunther werks":             "_restomod_gw",
    "by dp motorsport":                     "_restomod_dp",
    "slantnose conversion":                 "_restomod_slantnose",
    "slant nose conversion":                "_restomod_slantnose",
    "slant-nose conversion":                "_restomod_slantnose",
    "gemballa":                             "_restomod_gemballa",

    # ── Air-cooled body-style preservation ──────────────────────────────────
    # For G-series (3.2), 964, 993 — coupe/cabriolet/targa have 30-40% price
    # gaps. Preserve body style in the canonical trim so they don't get pooled.
    # BaT comp trims look like: "Carrera Coupe G50", "Carrera Cabriolet M491",
    # "Carrera Targa 5-Speed", "Turbo Cabriolet M505 Slant Nose", etc.
    "carrera coupe g50":                "Carrera Coupe",
    "carrera coupe 3.4l g50":           "Carrera Coupe",
    "carrera coupe 3.6l g50":           "Carrera Coupe",
    "carrera coupe":                    "Carrera Coupe",
    "carrera cabriolet":                "Carrera Cabriolet",
    "carrera cabriolet 6-speed":        "Carrera Cabriolet",
    "carrera cabriolet 5-speed":        "Carrera Cabriolet",
    "carrera coupe 5-speed":            "Carrera Coupe",
    "carrera cabriolet g50":            "Carrera Cabriolet",
    "carrera cabriolet m491":           "Carrera Cabriolet",
    "carrera targa g50":                "Carrera Targa",
    "carrera targa 5-speed":            "Carrera Targa",
    "turbo coupe 5-speed":              "Turbo Coupe",
    "turbo coupe":                      "Turbo Coupe",
    "turbo cabriolet m505 slant nose":  "Turbo Cabriolet",
    "turbo targa":                      "Turbo Targa",
    # 964-era body variants
    "carrera 2 coupe":                  "Carrera Coupe",
    "carrera 2 coupe 5-speed":          "Carrera Coupe",
    "carrera 2 cabriolet":              "Carrera Cabriolet",
    "carrera 2 cabriolet 5-speed":      "Carrera Cabriolet",
    "carrera 2 targa":                  "Carrera Targa",
    "carrera 4 coupe 5-speed":          "Carrera 4",
    "carrera 4 cabriolet 5-speed":      "Carrera 4",
    "carrera 2 speedster":              "Speedster",
    # 993-era body variants
    "carrera s coupe":                  "Carrera S",
    "carrera coupe 6-speed":            "Carrera Coupe",
    "targa 5-speed":                    "Carrera Targa",

    # ── Missing trim variants from scraper/comp audit ────────────────────────
    # High-frequency unmatched trims found in listings + sold_comps data.

    # Bare gearbox-only trims (comp data) — map to base of context model
    "5-speed":                          None,      # bare gearbox, no trim info
    "6-speed":                          None,      # bare gearbox, no trim info
    "7-speed":                          None,

    # "S" variants with body/gearbox suffixes
    "s 6-speed":                        "S",
    "s 5-speed":                        "S",
    "s 7-speed":                        "S",
    "s coupe":                          "S",
    "s cabriolet":                      "S",
    "s cabriolet 2d":                   "S",
    "s 2dr coupe":                      "S",
    "s 6-speed track car":              "S",
    "s track car":                      "S",
    "s 6-speed race car":               "S",
    "s roadster":                       "S",
    "s roadster 2d":                    "S",
    "s coupe 2d":                       "S",
    "s limited edition 6-speed":        "S",
    "s 550 anniversary edition 6-speed": "S",

    # Base model aliases
    "base":                             None,      # base model — no trim
    "base 2dr convertible":             None,
    "base (m5)":                        None,
    "2dr roadster":                     None,      # base Boxster body style
    "convertible":                      None,      # base Cabriolet
    "convertible 2d":                   None,
    "boxster roadster 2d":              None,      # base 718 Boxster

    # 911 Carrera short aliases
    "4s":                               "Carrera 4S",
    "4 gts":                            "Carrera 4 GTS",
    "carrera t 7-speed":                "Carrera T",
    "carrera t 6-speed":                "Carrera T",
    "991 carrera s":                    "Carrera S",
    "991 carrera":                      "Carrera",

    # 992 special trims
    "dakar":                            "Dakar",
    "st":                               "S/T",
    "s/t":                              "S/T",
    "s/t heritage design":              "S/T",

    # Body-style-first formats (eBay/AutoTrader)
    "2dr cabriolet turbo":              "Turbo",
    "2dr coupe turbo":                  "Turbo",
    "2dr cabriolet turbo s":            "Turbo S",
    "2dr coupe turbo s":                "Turbo S",
    "2dr coupe gt3":                    "GT3",
    "2dr coupe gt3 rs":                 "GT3 RS",
    "awd 2dr coupe":                    "Carrera 4",
    "turbo awd 2dr coupe":              "Turbo",

    # GT3RS without space (common scraper artifact)
    "gt3rs":                            "GT3 RS",
    "gt3 rs 4.0":                       "GT3 RS",
    "gt2rs":                            "GT2 RS",
    "gt4rs":                            "GT4 RS",

    # Bare "911" as trim (eBay artifact — model repeated in trim field)
    "911":                              None,

    # Boxster/Cayman special editions
    "rs 60 spyder":                     "Spyder",
    "rs 60 spyder 6-speed":             "Spyder",
    "boxster 25 years":                 "Boxster 25 Years",
    "25 years":                         "Boxster 25 Years",

    # 964-era special models
    "america roadster 5-speed":         "America Roadster",
    "america roadster":                 "America Roadster",
    "rs america":                       "RS America",
    "rs america rs america":            "RS America",  # eBay duplicate artifact

    # 993 special editions
    "40th anniversary 6-speed":         "Carrera",
    "40th anniversary edition 6-speed": "Carrera",
    "50th anniversary edition":         "Carrera",

    # Air-cooled era variants
    "carrera 2.7 mfi coupe":            "Carrera",
    "carrera 3.0 targa":                "Carrera Targa",
    "soft-window targa":                "Carrera Targa",

    # Weissach package variants
    "weissach coupe":                   "GT3 RS",  # Weissach = GT3 RS or GT2 RS option

    # Restomod/exotic identifiers (normalize so they can be excluded or weighted)
    "reimagined by singer":             "Singer",
    "918 spyder":                       "918 Spyder",
    "carrera gt":                       "Carrera GT",

    # Race/track cars
    "race car":                         None,      # exclude from FMV — not road cars
    "gt3 cup":                          "GT3 Cup",
    "limited edition 5-speed":          "S",

    # 718 model-in-trim artifacts
    "718 cayman":                       "Cayman",
    "718 cayman s":                     "S",
    "718 boxster":                      "Boxster",
    "718":                              None,      # bare 718, no trim info

        # GT3 family
    "gt3 rs weissach":          "GT3 RS",
    "gt3 rs tribute to carrera rs": "GT3 RS",
    "gt3 rs":                   "GT3 RS",
    "gt3 touring 6-speed":      "GT3 Touring",
    "gt3 touring":              "GT3 Touring",
    "gt3 6-speed":              "GT3",
    "gt3":                      "GT3",
    # GT2 family
    "gt2 rs weissach":          "GT2 RS",
    "gt2 rs clubsport":         "GT2 RS",
    "gt2 rs":                   "GT2 RS",
    "gt2":                      "GT2",
    # GT4 family
    "gt4 rs":                   "GT4 RS",
    "gt4 6-speed":              "GT4",
    "gt4":                      "GT4",
    # Turbo family
    "turbo s cabriolet 6-speed": "Turbo S",
    "turbo s cabriolet":        "Turbo S",
    "turbo s coupe":            "Turbo S",
    "turbo s":                  "Turbo S",
    "turbo coupe 6-speed":      "Turbo",
    "turbo cabriolet":          "Turbo",
    "turbo":                    "Turbo",
    # Carrera variants
    "carrera 4s 6-speed":       "Carrera 4S",
    "carrera 4s":               "Carrera 4S",
    "carrera 4 6-speed":        "Carrera 4",
    "carrera 4 5-speed":        "Carrera 4",
    "carrera 4":                "Carrera 4",
    "carrera s 6-speed":        "Carrera S",
    "carrera s":                "Carrera S",
    "carrera gts":              "Carrera GTS",
    "carrera rs":               "Carrera RS",
    "carrera targa":            "Carrera Targa",
    "carrera g50":              "Carrera",
    "carrera 6-speed":          "Carrera",
    "carrera 5-speed":          "Carrera",
    "carrera":                  "Carrera",
    # Special editions
    "sport classic":            "Sport Classic",
    "speedster":                "Speedster",
    "spyder 6-speed":           "Spyder",
    "spyder":                   "Spyder",
    "targa 4s":                 "Targa 4S",
    "targa":                    "Targa",
    # Air-cooled
    "sc 5-speed":               "SC",
    "sc":                       "SC",
    # Cayman/Boxster
    "r":                        "R",

    # ── 718 Cayman/Boxster — BaT stores trims with "Cayman"/"Boxster" prefix ──
    # Comps: "Cayman GT4 RS Weissach", "Cayman GT4 6-Speed", etc.
    # Listings from some scrapers: "GT4 RS", "GT4", "Cayman S", etc.
    # Normalize both to the same canonical so they match.
    "cayman gt4 rs weissach":       "GT4 RS",
    "cayman gt4 rs":                "GT4 RS",
    "cayman gt4 6-speed":           "GT4",
    "cayman gt4":                   "GT4",
    "cayman gts 4.0 6-speed":       "GTS",
    "cayman gts 4.0":               "GTS",
    "cayman gts 6-speed":           "GTS",
    "cayman gts":                   "GTS",
    "cayman s 6-speed":             "S",
    "cayman s":                     "S",
    "cayman 6-speed":               "Cayman",   # base 718 Cayman
    "cayman t 6-speed":             "T",
    "cayman t":                     "T",
    "boxster gts 4.0 6-speed":      "GTS",
    "boxster gts 4.0":              "GTS",
    "boxster gts 6-speed":          "GTS",
    "boxster gts":                  "GTS",
    "boxster s 6-speed":            "S",
    "boxster s":                    "S",
    "boxster 6-speed":              "Boxster",  # base 718 Boxster
    "25 years 6-speed":             "Boxster 25 Years",
    "spyder rs weissach":           "Spyder RS",
    "spyder rs":                    "Spyder RS",
    "718 spyder":                   "Spyder",

    # ── Body-style / transmission suffix variants ─────────────────────────────
    # BaT comps include body style ("Coupe", "Cabriolet") and gearbox in trim.
    # Dealer/scraper listings often omit these.  Both sides must normalize to the
    # same canonical so they can match in _trim_match_score.
    "carrera s coupe 7-speed":          "Carrera S",
    "carrera s coupe 6-speed":          "Carrera S",
    "carrera s cabriolet 7-speed":      "Carrera S",
    "carrera s cabriolet 6-speed":      "Carrera S",
    "carrera s cabriolet":              "Carrera S",
    "carrera 4s coupe 7-speed":         "Carrera 4S",
    "carrera 4s coupe 6-speed":         "Carrera 4S",
    "carrera 4s coupe":                 "Carrera 4S",
    "carrera 4s cabriolet 7-speed":     "Carrera 4S",
    "carrera 4s cabriolet 6-speed":     "Carrera 4S",
    "carrera 4s cabriolet":             "Carrera 4S",
    "carrera gts coupe 7-speed":        "Carrera GTS",
    "carrera gts coupe 6-speed":        "Carrera GTS",
    "carrera gts coupe":                "Carrera GTS",
    "carrera gts cabriolet 7-speed":    "Carrera GTS",
    "carrera gts cabriolet 6-speed":    "Carrera GTS",
    "carrera gts cabriolet":            "Carrera GTS",
    "carrera 4 coupe 6-speed":          "Carrera 4",
    "carrera 4 coupe":                  "Carrera 4",
    "carrera 4 cabriolet 6-speed":      "Carrera 4",
    "carrera 4 cabriolet":              "Carrera 4",
    "carrera t coupe 7-speed":          "Carrera T",
    "carrera t coupe 6-speed":          "Carrera T",
    "carrera t coupe":                  "Carrera T",
    "targa 4s 7-speed":                 "Targa 4S",
    "targa 4s 6-speed":                 "Targa 4S",
    "targa 4 gts 7-speed":              "Targa 4 GTS",
    "targa 4 gts":                      "Targa 4 GTS",
    "gt3 rs clubsport":                 "GT3 RS",
    "turbo s coupe exclusive series":   "Turbo S",
    "turbo s cabriolet exclusive series": "Turbo S",
    "turbo coupe 7-speed":              "Turbo",
    "turbo cabriolet 7-speed":          "Turbo",
    "turbo 2dr coupe":                  "Turbo",

    # ── Short / eBay body-style-only trims (718 Boxster / Cayman) ────────────
    # eBay and AutoTrader sometimes use only body style or partial names as trim.
    "gts 4.0 6-speed":                  "GTS",
    "gts 4.0":                          "GTS",
    "gts":                              "GTS",
    "spyder spyder":                    "Spyder",    # eBay duplicate artifact
    "roadster":                         "Boxster",   # base 718 Boxster
    "roadster 2d":                      "Boxster",
    "coupe":                            "Cayman",    # base 718 Cayman
    "coupe 2d":                         "Cayman",
    "boxster spyder":                   "Spyder",    # 987/981 Boxster Spyder

    # ── Carrera base body-style variants ─────────────────────────────────────
    # BaT comps for base 911 Carrera include body style in trim.
    "carrera coupe 7-speed":            "Carrera",
    "carrera s coupe 2d":               "Carrera S",

    # ── "2dr/4dr" body-style-first formats (AutoTrader/eBay/Cars.com) ────────
    # Some scrapers emit "2dr Cabriolet Carrera S" or "2dr Coupe Carrera" etc.
    "2dr cabriolet carrera s":          "Carrera S",
    "2dr coupe carrera s":              "Carrera S",
    "2dr cabriolet carrera":            "Carrera",
    "2dr coupe carrera":                "Carrera",

    # ── 911 prefix in trim field (eBay/PCA Mart sometimes includes model) ─────
    "911 turbo s":                      "Turbo S",
    "911 turbo":                        "Turbo",
    "911 gt3 rs":                       "GT3 RS",
    "911 gt3":                          "GT3",
    "911 gt2 rs":                       "GT2 RS",
    "911 gt2":                          "GT2",
    "911 carrera s":                    "Carrera S",
    "911 carrera gts":                  "Carrera GTS",
    "911 carrera 4s":                   "Carrera 4S",
    "911 carrera 4":                    "Carrera 4",
    "911 carrera":                      "Carrera",

    # ── G-Series / air-cooled era variant names ───────────────────────────────
    "carrera 2":                        "Carrera",
    "carrera 3.2":                      "Carrera",
    "carrera 3.2 cabriolet":            "Carrera",
    "cabriolet":                        "Carrera Cabriolet",  # bare Cabriolet = Carrera Cabriolet
    "coupe 5-speed":                    "Carrera Coupe",  # G-series base coupe

    # ── Keyword-prefixed descriptions (dealer marketing text in trim field) ───
    # Prefix matching handles longer strings that START with these keys.
    "new generation carrera":           "Carrera",
    "carrera s coupe pdk":              "Carrera S",
    "carrera s pdk":                    "Carrera S",
    "carrera gts pdk":                  "Carrera GTS",
    "carrera 4s pdk":                   "Carrera 4S",
}

# ── Prefix-matching support ───────────────────────────────────────────────────
# Sorted alias keys (longest first) for prefix matching.  This handles long
# eBay/scraper titles that start with a recognizable trim prefix, e.g.:
#   "GT4 RS Weissach Package, CCB, Front Axle Lift..." → "GT4 RS"
#   "Carrera GTS Coupe 6-Speed Carbon Fiber..."        → "Carrera GTS"
#   "Turbo S Exclusive Manufaktur Leather..."           → "Turbo S"
# We EXCLUDE the bare "carrera" key because it is too short and would
# incorrectly absorb "Carrera 2 Coupe 5-Speed" and similar variant trims
# that already receive accurate comp matching through their exact strings.
_TRIM_ALIAS_KEYS_BY_LEN = sorted((k for k, v in _TRIM_ALIASES.items() if v is not None), key=len, reverse=True)
_PREFIX_MATCH_EXCLUDED = frozenset({"carrera"})


def normalize_trim(trim: Optional[str]) -> Optional[str]:
    """Return canonical trim name for grouping. None if unknown.

    Returns None for trims explicitly mapped to None (e.g. 'Base', '5-Speed')
    meaning the trim carries no useful grouping info.
    """
    if not trim:
        return None
    key = trim.lower().strip()
    # Use sentinel to distinguish 'key not found' from 'key maps to None'
    _MISSING = object()
    exact = _TRIM_ALIASES.get(key, _MISSING)
    if exact is not _MISSING:
        return exact  # may be None for trims like 'Base', '5-Speed'
    # Prefix match: "GT4 RS Weissach Package, CCB..." → "GT4 RS"
    for alias_key in _TRIM_ALIAS_KEYS_BY_LEN:
        if alias_key in _PREFIX_MATCH_EXCLUDED:
            continue
        if key.startswith(alias_key + " "):
            return _TRIM_ALIASES[alias_key]
    return trim.strip()


# ── Comp matching ─────────────────────────────────────────────────────────────

@dataclass
class Comp:
    id: int
    year: Optional[int]
    model: str
    trim: Optional[str]
    trim_normalized: Optional[str]
    generation: str
    sold_price: Optional[int]     # None = reserve not met
    sold_date: Optional[str]
    mileage: Optional[int]
    source: str
    source_weight: float
    is_rnm: bool                  # reserve not met = floor signal only
    listing_url: Optional[str]


@dataclass
class FMVResult:
    model: str
    year: Optional[int]
    trim: Optional[str]
    generation: str

    # Core FMV outputs
    weighted_median: Optional[int]   # primary FMV estimate
    weighted_mean: Optional[int]     # secondary
    price_low: Optional[int]         # 25th percentile of sold comps
    price_high: Optional[int]        # 75th percentile of sold comps
    rnm_floor: Optional[int]         # median of RNM high bids (what market refused to pay)

    # Data quality
    comp_count: int                  # number of sold comps used
    rnm_count: int                   # number of RNM records
    confidence: str                  # HIGH / MEDIUM / LOW / NONE
    date_range: Optional[str]        # e.g. "2024-03 to 2026-03"

    # The comps themselves (for report display)
    comps: list = field(default_factory=list)
    rnm_comps: list = field(default_factory=list)


# ── Model normalization for DB queries ───────────────────────────────────────
# Some scrapers emit "718 Cayman", "718 Boxster", "718 Spyder" as the model
# field, but sold comps store 718-era cars as model="718".  This maps listing
# model names to the canonical model name(s) used in sold_comps.
_MODEL_QUERY_MAP = {
    "718 cayman": "718",
    "718 boxster": "718",
    "718 spyder":  "718",
}


def _query_model(model: str, generation: str = "") -> str:
    """Return the sold_comps model name to use when querying for this listing.

    Handles two cases:
    1. Scrapers that emit "718 Cayman" / "718 Boxster" / "718 Spyder" as model
       — BaT stores 718-era cars as model="718".
    2. Scrapers that emit plain "Cayman" or "Boxster" for 2017+ cars (718-era)
       — same issue, detected via the generation bucket.
    """
    key = (model or "").lower().strip()
    if key in _MODEL_QUERY_MAP:
        return _MODEL_QUERY_MAP[key]
    # Plain "Cayman" or "Boxster" for 718-era cars: sold comps use model="718"
    if generation in ("718_cayman", "718_boxster") and key in ("cayman", "boxster"):
        return "718"
    return model


# Source credibility weights
_SOURCE_WEIGHTS = {
    "bat":              1.0,
    "bring a trailer":  1.0,
    "bringatrailer":    1.0,
    "classic.com":      1.0,
    "cars & bids":      1.0,
    "carsandbids":      1.0,
    "pcarmarket":       0.8,
    "pca mart":         0.6,
    "ebay":             0.5,
}


def _source_weight(source: str) -> float:
    return _SOURCE_WEIGHTS.get((source or "").lower().strip(), 0.7)


def _recency_weight(sold_date: Optional[str], today: Optional[date] = None) -> float:
    """
    Decay weight by age. Comps within 6 months = full weight.
    Decays linearly to 0.3 at 24 months, then stays at 0.3.
    """
    if not sold_date:
        return 0.5
    today = today or date.today()
    try:
        comp_date = date.fromisoformat(sold_date[:10])
    except ValueError:
        return 0.5
    age_months = (today - comp_date).days / 30.44
    if age_months <= 6:
        return 1.0
    if age_months >= 24:
        return 0.3
    # Linear decay from 1.0 at 6mo to 0.3 at 24mo
    return 1.0 - (0.7 * (age_months - 6) / 18)


def _weighted_percentile(values_weights: list, percentile: float) -> Optional[int]:
    """Compute weighted percentile. values_weights = [(value, weight), ...]"""
    if not values_weights:
        return None
    sorted_vw = sorted(values_weights, key=lambda x: x[0])
    total_weight = sum(w for _, w in sorted_vw)
    if total_weight == 0:
        return None
    target = total_weight * percentile / 100
    cumulative = 0
    for val, w in sorted_vw:
        cumulative += w
        if cumulative >= target:
            return int(val)
    return int(sorted_vw[-1][0])


def _trim_match_score(target_trim: Optional[str], comp_trim: Optional[str]) -> float:
    """
    How well does comp_trim match target_trim?
    Returns 1.0 (exact), 0.7 (family match), 0.4 (same gen, no trim), 0.0 (mismatch).
    """
    t = normalize_trim(target_trim)
    c = normalize_trim(comp_trim)

    if t is None and c is None:
        return 0.6   # both unknown — ok match
    if t is None:
        # Target trim unknown — assume base model. Penalize high-performance
        # trims so GT3/Turbo/GT4 comps don't inflate base-model FMVs.
        # A no-trim eBay 911 listing is almost always a base Carrera.
        if c is None:
            return 0.5
        _HIGH_PERF = {"GT3", "GT3 RS", "GT3 Touring", "GT3 Cup",
                      "GT2", "GT2 RS", "GT4", "GT4 RS",
                      "Turbo", "Turbo S", "Turbo Coupe", "Turbo Cabriolet",
                      "Speedster", "Sport Classic", "S/T", "Dakar",
                      "RS America", "America Roadster",
                      "Spyder", "Spyder RS",
                      "GTS", "Targa 4 GTS", "Carrera GTS",
                      "Singer", "918 Spyder", "Carrera GT"}
        if c in _HIGH_PERF:
            return 0.0  # exclude — high-perf comp irrelevant for no-trim listing
        return 0.5  # unknown target vs known base-ish comp — decent match
    if c is None:
        return 0.4   # known target vs unknown comp — weak match
    if t.lower() == c.lower():
        return 1.0   # exact

    # Reject restomod sentinel — never comparable to stock
    if t.startswith("_restomod") or c.startswith("_restomod"):
        return 0.0

    # Body-style variants of the same base trim score 0.9 (near-exact).
    # BaT comps include body style ("Coupe", "Cabriolet", "Targa") in the trim
    # field while dealer/scraper listings typically omit it.  A listing with
    # trim="Carrera" is the same car as a BaT comp "Carrera Coupe G50" or
    # "Carrera Cabriolet" — treat as near-exact so the exact_trim_comps
    # narrowing fires and Singer/restomod comps don't pollute the pool.
    _BODY_STYLE_VARIANTS = {
        "Carrera":    {"Carrera Coupe", "Carrera Cabriolet", "Carrera Targa"},
        "Carrera S":  {"Carrera S Coupe", "Carrera S Cabriolet"},
        "Carrera 4":  {"Carrera 4 Coupe", "Carrera 4 Cabriolet"},
        "Carrera 4S": {"Carrera 4S Coupe", "Carrera 4S Cabriolet"},
        "Carrera GTS":{"Carrera GTS Coupe", "Carrera GTS Cabriolet"},
        "Turbo":      {"Turbo Coupe", "Turbo Cabriolet", "Turbo Targa"},
        "Turbo S":    {"Turbo S Coupe", "Turbo S Cabriolet"},
        "GT3":        {"GT3 6-Speed"},
        "GT3 Touring":{"GT3 Touring 6-Speed"},
    }
    # Check both directions: listing trim → comp body variant, and vice-versa
    for base, variants in _BODY_STYLE_VARIANTS.items():
        if (t == base and c in variants) or (c == base and t in variants):
            return 0.9
        if t in variants and c in variants:
            return 0.9  # both are body-style variants of same base

    # Family matching — GT3/GT3 Touring/GT3 RS are related
    gt3_family = {"GT3", "GT3 Touring", "GT3 RS", "GT3 Cup"}
    gt2_family = {"GT2", "GT2 RS"}
    gt4_family = {"GT4", "GT4 RS"}

    carrera_family = {"Carrera", "Carrera S", "Carrera 4", "Carrera 4S",
                      "Carrera GTS", "Carrera Targa", "Carrera RS",
                      "Carrera T", "Targa 4S", "Targa 4 GTS",
                      "Carrera Coupe", "Carrera Cabriolet"}
    turbo_family = {"Turbo", "Turbo S", "Turbo Coupe", "Turbo Cabriolet", "Turbo Targa"}
    spyder_family = {"Spyder", "Spyder RS"}
    mid_engine_s_family = {"S", "GTS", "T", "Cayman", "Boxster"}

    for family in (gt3_family, gt2_family, gt4_family, turbo_family,
                   carrera_family, spyder_family, mid_engine_s_family):
        if t in family and c in family:
            return 0.7

    return 0.0   # different trim families — not comparable


def get_fmv(
    conn,
    year: Optional[int],
    model: str,
    trim: Optional[str] = None,
    months_back: int = 24,
    min_comps: int = 1,
    since_date: Optional[str] = None,
    until_date: Optional[str] = None,
) -> FMVResult:
    """
    Calculate FMV for a given Porsche.

    Matching strategy (in order):
    1. Exact generation + exact trim — best comps
    2. Exact generation + trim family — broader comps
    3. Exact generation, any trim — generation baseline
    4. Adjacent generation — last resort

    Returns FMVResult with weighted_median as the primary FMV estimate.

    Args:
        since_date: Optional ISO date string 'YYYY-MM-DD'. When provided,
                    overrides months_back as the lower bound for comp dates.
        until_date: Optional ISO date string 'YYYY-MM-DD'. When provided,
                    restricts comps to on or before this date (useful for
                    point-in-time / historical FMV queries).
    """
    target_gen   = get_generation(year, model, trim or "")
    norm_trim    = normalize_trim(trim)
    today        = date.today()

    # Normalize listing model to the model name used in sold_comps.
    # "718 Cayman" / "718 Boxster" → "718"; also plain "Cayman"/"Boxster" for
    # 718-era (2017+) cars where BaT stores the comp as model="718".
    comp_model = _query_model(model, target_gen)

    # Date window: since_date overrides months_back when explicitly provided
    if since_date:
        cutoff_date = since_date
    else:
        cutoff_date = (today - timedelta(days=months_back * 30)).isoformat()

    # ── Pull all comps for this model ────────────────────────────────────────
    if until_date:
        rows = conn.execute(
            """SELECT id, year, model, trim, sold_price, sold_date, mileage, source, listing_url
               FROM sold_comps
               WHERE LOWER(model) = LOWER(?)
                 AND sold_date >= ?
                 AND sold_date <= ?
               ORDER BY sold_date DESC""",
            (comp_model, cutoff_date, until_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, year, model, trim, sold_price, sold_date, mileage, source, listing_url
               FROM sold_comps
               WHERE LOWER(model) = LOWER(?)
                 AND sold_date >= ?
               ORDER BY sold_date DESC""",
            (comp_model, cutoff_date),
        ).fetchall()

    # Build Comp objects with scores
    all_comps = []
    for row in rows:
        row_gen = get_generation(row[1], row[2], row[3] or "")
        is_rnm  = row[4] is None
        src_w   = _source_weight(row[7])
        rec_w   = _recency_weight(row[5])
        all_comps.append(Comp(
            id=row[0], year=row[1], model=row[2], trim=row[3],
            trim_normalized=normalize_trim(row[3]),
            generation=row_gen,
            sold_price=row[4], sold_date=row[5], mileage=row[6],
            source=row[7], source_weight=src_w, is_rnm=is_rnm,
            listing_url=row[8],
        ))

    # ── Separate sold vs RNM ────────────────────────────────────────────────
    sold_comps = [c for c in all_comps if not c.is_rnm and c.sold_price]
    rnm_comps  = [c for c in all_comps if c.is_rnm]

    # ── Match sold comps by generation + trim ────────────────────────────────
    def score_comp(comp: Comp) -> float:
        gen_match   = 1.0 if comp.generation == target_gen else 0.3
        trim_score  = _trim_match_score(norm_trim, comp.trim_normalized)
        recency     = _recency_weight(comp.sold_date)
        source_w    = comp.source_weight
        # Exclude comps with zero trim match score. This fires when:
        # 1. Known target vs known comp in different families (GT3 RS vs Sport Classic)
        # 2. Unknown target (None) vs high-performance comp (GT3, Turbo, etc.)
        #    — _trim_match_score returns 0.0 for high-perf comps when target is None
        if trim_score == 0.0 and comp.trim_normalized:
            return 0.0
        return gen_match * (0.4 + 0.6 * trim_score) * recency * source_w

    # Score all sold comps; keep those with meaningful scores
    scored = [(c, score_comp(c)) for c in sold_comps]
    scored = [(c, s) for c, s in scored if s > 0.05]
    scored.sort(key=lambda x: -x[1])

    # Prefer exact generation comps. Only fall back to adjacent generation
    # if we have NO same-gen comps AND the target trim is unknown (None).
    # If we know the trim, cross-gen fallback produces misleading FMVs.
    # EXCEPTION: GT variants (GT2, GT2 RS, GT3, GT3 RS, GT4, GT4 RS) share
    # technology and pricing continuity across adjacent generations. When a
    # new-gen GT car has < 3 same-gen sold comps, allow cross-gen fallback
    # using only the same trim family — e.g. 992 GT2 RS falls back to 991
    # GT2 RS comps rather than 992 gen-pool comps (which are 80% Carreras).
    _GT_TRIMS = {"GT2", "GT2 RS", "GT3", "GT3 RS", "GT3 Touring",
                 "GT4", "GT4 RS", "Spyder RS"}
    exact_gen = [(c, s) for c, s in scored if c.generation == target_gen]
    if exact_gen:
        use_comps = exact_gen
    elif norm_trim in _GT_TRIMS and len(exact_gen) < 3:
        # GT trim with thin same-gen coverage — cross-gen fallback restricted
        # to the same trim family only (no dilution from unrelated gen comps)
        cross_gen_gt = [(c, s) for c, s in scored
                        if _trim_match_score(norm_trim, c.trim_normalized) >= 0.7]
        use_comps = cross_gen_gt if cross_gen_gt else scored
    elif norm_trim is None:
        # No trim known — cross-gen fallback is acceptable
        use_comps = scored
    else:
        # Trim is known but no same-gen comps matched (likely unrecognized trim
        # string from scraper).  Try two broader fallback tiers before NONE:
        #   Tier A — same-gen comps that share a trim family (ts > 0)
        #            or have no normalized trim at all.
        #   Tier B — pure generation baseline: all same-gen comps equally.
        # This prevents garbage-trim listings from getting NONE confidence when
        # there is plenty of generation-level comp data available.
        #
        # Always build both tiers. Use Tier A only when it yields >= 3 comps
        # (a meaningful family-level signal); otherwise fall through to the
        # broader gen baseline so a single no-trim comp doesn't block hundreds
        # of useful same-generation comps.
        gen_family = []
        gen_all = []
        for c in sold_comps:
            if c.generation != target_gen:
                continue
            ts = _trim_match_score(norm_trim, c.trim_normalized)
            rec_w = _recency_weight(c.sold_date)
            src_w = c.source_weight
            fb_all = src_w * rec_w * 0.4
            if fb_all > 0.05:
                gen_all.append((c, fb_all))
            if ts > 0.0 or c.trim_normalized is None:
                fb_fam = src_w * rec_w * (0.4 + 0.4 * ts)
                if fb_fam > 0.05:
                    gen_family.append((c, fb_fam))

        use_comps = gen_family if len(gen_family) >= 3 else gen_all

    if not use_comps:
        # No comps found at all
        return FMVResult(
            model=model, year=year, trim=trim, generation=target_gen,
            weighted_median=None, weighted_mean=None,
            price_low=None, price_high=None, rnm_floor=None,
            comp_count=0, rnm_count=len(rnm_comps),
            confidence="NONE", date_range=None,
            comps=[], rnm_comps=rnm_comps,
        )

    # ── Exact-trim preference ────────────────────────────────────────────────
    # When we have enough exact-trim comps, restrict use_comps to those.
    # This prevents a large pool of lower-priced family comps (e.g. 450 base
    # Carrera comps) from diluting the FMV of a rarer trim (e.g. Carrera GTS).
    # The threshold matches the HIGH-confidence cutoff so we only tighten when
    # the exact-trim signal is strong enough to stand alone.
    exact_trim_comps = [(c, s) for c, s in use_comps
                        if _trim_match_score(norm_trim, c.trim_normalized) >= 1.0]
    if len(exact_trim_comps) >= 4:
        use_comps = exact_trim_comps

    # ── Weighted statistics ──────────────────────────────────────────────────
    prices_weights = [(c.sold_price, s) for c, s in use_comps]
    total_weight   = sum(s for _, s in prices_weights)
    weighted_mean  = int(sum(p * w for p, w in prices_weights) / total_weight)
    weighted_median = _weighted_percentile(prices_weights, 50)
    price_low      = _weighted_percentile(prices_weights, 25)
    price_high     = _weighted_percentile(prices_weights, 75)

    # ── RNM floor (what market bid but seller rejected) ─────────────────────
    # Filter RNM to same generation + trim family
    relevant_rnm = []
    for c in rnm_comps:
        if c.generation == target_gen:
            ts = _trim_match_score(norm_trim, c.trim_normalized)
            if ts > 0.3:
                relevant_rnm.append(c)

    rnm_floor = None
    if relevant_rnm:
        # Use median of RNM high bids from bat_reserve_not_met
        rnm_bids = []
        try:
            rnm_urls = [c.listing_url for c in relevant_rnm if c.listing_url]
            if rnm_urls:
                placeholders = ",".join("?" * len(rnm_urls))
                rnm_rows = conn.execute(
                    f"SELECT high_bid FROM bat_reserve_not_met WHERE listing_url IN ({placeholders})",
                    rnm_urls
                ).fetchall()
                rnm_bids = [r[0] for r in rnm_rows if r[0]]
        except Exception:
            pass
        if rnm_bids:
            rnm_bids.sort()
            rnm_floor = rnm_bids[len(rnm_bids) // 2]

    # ── Confidence ───────────────────────────────────────────────────────────
    # exact_trim_comps already computed above (and use_comps may have been
    # narrowed to them when >= 5 were available).
    comp_count = len(use_comps)

    oldest_date = min((c.sold_date for c, _ in use_comps if c.sold_date), default=None)
    newest_date = max((c.sold_date for c, _ in use_comps if c.sold_date), default=None)

    if len(exact_trim_comps) >= 4:
        confidence = "HIGH"
    elif len(exact_trim_comps) >= 2 or comp_count >= 4:
        confidence = "MEDIUM"
    elif comp_count >= 1:
        confidence = "LOW"
    else:
        confidence = "NONE"

    date_range = None
    if oldest_date and newest_date:
        date_range = f"{oldest_date[:7]} to {newest_date[:7]}"

    log.debug(
        "FMV [%s %s %s gen=%s]: %d comps, median=$%s, confidence=%s",
        year, model, trim, target_gen, comp_count,
        f"{weighted_median:,}" if weighted_median else "N/A", confidence
    )

    return FMVResult(
        model=model, year=year, trim=trim, generation=target_gen,
        weighted_median=weighted_median,
        weighted_mean=weighted_mean,
        price_low=price_low,
        price_high=price_high,
        rnm_floor=rnm_floor,
        comp_count=comp_count,
        rnm_count=len(relevant_rnm),
        confidence=confidence,
        date_range=date_range,
        comps=[c for c, _ in use_comps],
        rnm_comps=relevant_rnm,
    )


def get_deal_score(listing_price: int, fmv: FMVResult) -> Optional[dict]:
    """
    Compare a listing price to FMV. Returns a deal assessment dict.

    Returns None if FMV confidence is NONE.

    Output:
        pct_vs_fmv:   e.g. -0.08 = 8% below FMV (a deal)
        vs_fmv_str:   e.g. "-8% vs FMV"
        deal_flag:    "DEAL" / "FAIR" / "ABOVE" / "WATCH"
        alert_tier1:  bool — should alert for Tier 1 car (any price)
        alert_tier2:  bool — should alert for Tier 2 car (5%+ below FMV)
    """
    if fmv.confidence == "NONE" or not fmv.weighted_median:
        return None

    pct = (listing_price - fmv.weighted_median) / fmv.weighted_median

    if pct <= -0.10:
        flag = "DEAL"       # 10%+ below FMV — strong buy signal
    elif pct <= -0.05:
        flag = "WATCH"      # 5-10% below — worth watching
    elif pct <= 0.05:
        flag = "FAIR"       # within 5% — market price
    else:
        flag = "ABOVE"      # above FMV

    vs_str = f"{pct:+.0%} vs FMV (${fmv.weighted_median:,})"

    # Alert thresholds from WATCHLIST.md
    # Tier 1: alert at market or below (any listing is notable)
    # Tier 2: alert only at 5%+ below FMV
    return {
        "pct_vs_fmv":  round(pct, 4),
        "vs_fmv_str":  vs_str,
        "deal_flag":   flag,
        "fmv":         fmv.weighted_median,
        "price_low":   fmv.price_low,
        "price_high":  fmv.price_high,
        "confidence":  fmv.confidence,
        "comp_count":  fmv.comp_count,
    }


# ── Convenience: score all active listings ────────────────────────────────────

def score_active_listings(
    conn,
    since_date: Optional[str] = None,
    until_date: Optional[str] = None,
) -> list:
    """
    Run FMV + deal scoring on every active listing in the DB.
    Returns list of dicts with listing data + deal_score attached.
    Called by dashboard.py and notify_gunther.py.

    Args:
        since_date: Optional 'YYYY-MM-DD' — restrict comps to on/after this date.
        until_date: Optional 'YYYY-MM-DD' — restrict comps to on/before this date.
    """
    listings = conn.execute(
        """SELECT id, dealer, year, model, trim, price, tier, mileage, listing_url,
                  date_first_seen, source_category, image_url, image_url_cdn,
                  created_at, auction_ends_at
           FROM listings WHERE status='active' AND price IS NOT NULL AND price > 0
           ORDER BY tier, year DESC"""
    ).fetchall()

    results = []
    fmv_cache = {}

    for row in listings:
        lid, dealer, year, model, trim, price, tier, mileage, url, first_seen, src_cat, image_url, image_url_cdn, created_at, auction_ends_at = row

        cache_key = (model, year, normalize_trim(trim))
        if cache_key not in fmv_cache:
            fmv_cache[cache_key] = get_fmv(
                conn, year=year, model=model, trim=trim,
                since_date=since_date, until_date=until_date,
            )
        fmv = fmv_cache[cache_key]

        deal = get_deal_score(price, fmv) if price else None

        # Flat convenience keys so callers can do s.get('confidence') etc.
        # without navigating nested objects.
        flat_confidence = fmv.confidence
        flat_flag = deal["deal_flag"] if deal else None
        flat_discount_pct = abs(deal["pct_vs_fmv"]) * 100.0 if deal else 0.0

        results.append({
            "id":           lid,
            "dealer":       dealer,
            "year":         year,
            "model":        model,
            "trim":         trim,
            "price":        price,
            "tier":         tier,
            "mileage":      mileage,
            "listing_url":  url,
            "image_url":    image_url or "",
            "image_url_cdn": image_url_cdn or "",
            "date_first_seen": first_seen,
            "created_at":   created_at,
            "auction_ends_at": auction_ends_at,
            "source_category": src_cat,
            "fmv":          fmv,
            "deal_score":   deal,
            # Flat shortcuts
            "confidence":   flat_confidence,
            "flag":         flat_flag,
            "discount_pct": flat_discount_pct,
        })

    return results


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import db

    db.init_db()
    with db.get_conn() as conn:
        # Test a few cars
        test_cases = [
            (2018, "911", "GT3 Touring"),
            (1996, "911", "Turbo"),
            (1998, "911", "Carrera S"),
            (2023, "911", "Sport Classic"),
            (2016, "Cayman", "GT4"),
            (1987, "911", "Carrera"),
        ]

        print(f"\n{'Car':<40} {'FMV':>10} {'Low':>10} {'High':>10} {'Comps':>6} {'Conf':<8} {'RNM floor':>10}")
        print("─" * 100)
        for year, model, trim in test_cases:
            r = get_fmv(conn, year=year, model=model, trim=trim)
            fmv_str   = f"${r.weighted_median:,}" if r.weighted_median else "N/A"
            low_str   = f"${r.price_low:,}"       if r.price_low       else "—"
            high_str  = f"${r.price_high:,}"      if r.price_high      else "—"
            rnm_str   = f"${r.rnm_floor:,}"       if r.rnm_floor       else "—"
            car_str   = f"{year} {model} {trim}"
            print(f"{car_str:<40} {fmv_str:>10} {low_str:>10} {high_str:>10} {r.comp_count:>6} {r.confidence:<8} {rnm_str:>10}")

        print()
        print("Active listing deal scores:")
        scored = score_active_listings(conn)
        t1 = [s for s in scored if s["tier"] == "TIER1" and s["deal_score"]]
        t2_deals = [s for s in scored if s["tier"] == "TIER2"
                    and s["deal_score"] and s["deal_score"]["pct_vs_fmv"] <= -0.05]

        print(f"\nTIER1 listings with FMV ({len(t1)} of {sum(1 for s in scored if s['tier']=='TIER1')}):")
        for s in t1[:10]:
            d = s["deal_score"]
            print(f"  {s['year']} {s['model']} {s['trim'] or '':<25} "
                  f"ask=${s['price']:,}  {d['vs_fmv_str']}  [{d['deal_flag']}]  conf={d['confidence']}")

        print(f"\nTIER2 deals (5%+ below FMV) — {len(t2_deals)} found:")
        for s in t2_deals[:10]:
            d = s["deal_score"]
            print(f"  {s['year']} {s['model']} {s['trim'] or '':<25} "
                  f"ask=${s['price']:,}  {d['vs_fmv_str']}  conf={d['confidence']}")

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
    HIGH   — 5+ sold comps within 24 months for this segment
    MEDIUM — 2–4 sold comps, or 5+ but older than 12 months
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

    if model_lower == "718":
        # 718-badged cars are all 2017+; use trim to split Cayman vs Boxster/Spyder
        if "spyder" in trim_lower:  return "718_boxster"
        if "boxster" in trim_lower: return "718_boxster"
        return "718_cayman"  # Cayman, GT4, GT4 RS, etc.

    if model_lower == "914":  return "914"
    if model_lower == "912":  return "912"
    if model_lower == "356":  return "356"

    return model_lower or "unknown"


# ── Trim normalization ────────────────────────────────────────────────────────

# Maps messy/variant trim strings to a canonical form for grouping
_TRIM_ALIASES = {
    # GT3 family
    "gt3 rs weissach":          "GT3 RS",
    "gt3 rs tribute to carrera rs": "GT3 RS",
    "gt3 rs":                   "GT3 RS",
    "gt3 touring 6-speed":      "GT3 Touring",
    "gt3 touring":              "GT3 Touring",
    "gt3 6-speed":              "GT3",
    "gt3 cup":                  "GT3 Cup",
    "gt3":                      "GT3",
    # GT2 family
    "gt2 rs weissach":          "GT2 RS",
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
    "turbo coupe 5-speed":      "Turbo",
    "turbo coupe":              "Turbo",
    "turbo cabriolet":          "Turbo",
    "turbo targa":              "Turbo",
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
    "carrera cabriolet 6-speed": "Carrera",
    "carrera cabriolet g50":    "Carrera",
    "carrera cabriolet":        "Carrera",
    "carrera targa g50":        "Carrera Targa",
    "carrera targa 5-speed":    "Carrera Targa",
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
    "targa 5-speed":            "Targa",
    "targa":                    "Targa",
    # Air-cooled
    "sc 5-speed":               "SC",
    "sc":                       "SC",
    "rs america":               "RS America",
    # Cayman/Boxster
    "r":                        "R",
}


def normalize_trim(trim: Optional[str]) -> Optional[str]:
    """Return canonical trim name for grouping. None if unknown."""
    if not trim:
        return None
    key = trim.lower().strip()
    return _TRIM_ALIASES.get(key, trim.strip())


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
    if t is None or c is None:
        return 0.4   # one known, one not
    if t.lower() == c.lower():
        return 1.0   # exact

    # Family matching — GT3/GT3 Touring/GT3 RS are related
    gt3_family = {"GT3", "GT3 Touring", "GT3 RS", "GT3 Cup"}
    gt2_family = {"GT2", "GT2 RS"}
    gt4_family = {"GT4", "GT4 RS"}
    turbo_family = {"Turbo", "Turbo S"}
    carrera_family = {"Carrera", "Carrera S", "Carrera 4", "Carrera 4S",
                      "Carrera GTS", "Carrera Targa", "Carrera RS"}

    for family in (gt3_family, gt2_family, gt4_family, turbo_family, carrera_family):
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
            (model, cutoff_date, until_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, year, model, trim, sold_price, sold_date, mileage, source, listing_url
               FROM sold_comps
               WHERE LOWER(model) = LOWER(?)
                 AND sold_date >= ?
               ORDER BY sold_date DESC""",
            (model, cutoff_date),
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
        # If we know the target trim and the comp trim is a complete mismatch,
        # exclude it — don't use a GT3 RS to price a Sport Classic.
        if norm_trim and comp.trim_normalized and trim_score == 0.0:
            return 0.0
        return gen_match * (0.4 + 0.6 * trim_score) * recency * source_w

    # Score all sold comps; keep those with meaningful scores
    scored = [(c, score_comp(c)) for c in sold_comps]
    scored = [(c, s) for c, s in scored if s > 0.05]
    scored.sort(key=lambda x: -x[1])

    # Prefer exact generation comps. Only fall back to adjacent generation
    # if we have NO same-gen comps AND the target trim is unknown (None).
    # If we know the trim, cross-gen fallback produces misleading FMVs.
    exact_gen = [(c, s) for c, s in scored if c.generation == target_gen]
    if exact_gen:
        use_comps = exact_gen
    elif norm_trim is None:
        # No trim known — cross-gen fallback is acceptable
        use_comps = scored
    else:
        # Trim is known but no same-gen comps exist — report NONE, not garbage
        use_comps = []

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
    exact_trim_comps = [(c, s) for c, s in use_comps
                        if _trim_match_score(norm_trim, c.trim_normalized) >= 0.9]
    comp_count = len(use_comps)

    oldest_date = min((c.sold_date for c, _ in use_comps if c.sold_date), default=None)
    newest_date = max((c.sold_date for c, _ in use_comps if c.sold_date), default=None)

    if len(exact_trim_comps) >= 5:
        confidence = "HIGH"
    elif len(exact_trim_comps) >= 2 or comp_count >= 5:
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
                  date_first_seen, source_category, image_url
           FROM listings WHERE status='active' AND price IS NOT NULL AND price > 0
           ORDER BY tier, year DESC"""
    ).fetchall()

    results = []
    fmv_cache = {}

    for row in listings:
        lid, dealer, year, model, trim, price, tier, mileage, url, first_seen, src_cat, image_url = row

        cache_key = (model, year, normalize_trim(trim))
        if cache_key not in fmv_cache:
            fmv_cache[cache_key] = get_fmv(
                conn, year=year, model=model, trim=trim,
                since_date=since_date, until_date=until_date,
            )
        fmv = fmv_cache[cache_key]

        deal = get_deal_score(price, fmv) if price else None

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
            "date_first_seen": first_seen,
            "source_category": src_cat,
            "fmv":          fmv,
            "deal_score":   deal,
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

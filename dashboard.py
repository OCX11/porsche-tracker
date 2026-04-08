"""Generate a self-contained HTML dashboard from the database."""
import re
import subprocess
from datetime import datetime
from pathlib import Path
from db import get_conn, get_dashboard_data, source_category
import dealer_weights as dw
import fmv as fmv_engine

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"


# ── Source health ─────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# (display_name, log_file, short_desc, launchd_label, stale_minutes)
# stale_minutes: how old a log timestamp can be before we consider it stale
# Use log_file=None for entries handled specially in build_source_health().
_SOURCES = [
    ("Main Scraper",   "scraper.log",          "Dealers + BaT + PCA Mart",         "com.porschetracker.scrape",          45),
    ("Distill Poller", "distill_poller.log",   "Reads Distill DB every 60s",       "com.porschetracker.distill-poller",   5),
    # "Distill" = combined Receiver + Watcher — handled specially below
    ("Distill",        None,                   "Webhook receiver + drop processor", None,                                 90),
    ("Archive Capture","archive_capture.log",  "HTML + screenshot capture",         "com.porschetracker.archive-capture", 30),
]

# The two Distill launchd labels — used for PID check in the combined pill.
_DISTILL_LABELS = [
    "com.porschetracker.distill-receiver",
    "com.porschetracker.distill-watcher",
]
# The two log files whose most-recent timestamp drives the combined Distill pill.
_DISTILL_LOGS = ["distill_receiver.log", "distill_watcher.log"]


def _launchd_pid(label: str):
    """Return PID string if job is running, else None."""
    try:
        out = subprocess.check_output(
            ["launchctl", "list", label],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        # Format: "PID" = 1234;
        m = re.search(r'"PID"\s*=\s*(\d+)', out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _last_log_ts(log_file: str):
    """Return (datetime, last_line_str) for the most recent timestamped line."""
    path = LOG_DIR / log_file
    if not path.exists():
        return None, ""
    try:
        lines = path.read_text(errors="replace").splitlines()
        for line in reversed(lines[-100:]):
            m = _TS_RE.match(line)
            if m:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                return ts, line.strip()
    except Exception:
        pass
    return None, ""


def _fmt_age(minutes: float) -> str:
    if minutes < 2:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m ago" if m else f"{h}h ago"


def build_source_health():
    """Return list of status dicts for each data source."""
    now = datetime.now()
    results = []
    for name, log_file, desc, label, stale_mins in _SOURCES:

        # Combined Distill pill — pick most-recent timestamp across both logs,
        # check both launchd labels for a running PID.
        if log_file is None and name == "Distill":
            ts_candidates = [_last_log_ts(lf) for lf in _DISTILL_LOGS]
            # Most recent non-None timestamp wins
            best_ts, best_line = None, ""
            for ts_c, line_c in ts_candidates:
                if ts_c is not None and (best_ts is None or ts_c > best_ts):
                    best_ts, best_line = ts_c, line_c
            ts, last_line = best_ts, best_line
            # Any running PID counts
            pid = next((p for lbl in _DISTILL_LABELS for p in [_launchd_pid(lbl)] if p), None)
        else:
            ts, last_line = _last_log_ts(log_file)
            pid = _launchd_pid(label)

        has_error = False
        if last_line:
            has_error = any(w in last_line for w in ("ERROR", "CRITICAL", "Traceback", "FAILED"))

        if ts is None:
            status = "unknown"
            age    = "no logs"
        else:
            age_mins = (now - ts).total_seconds() / 60
            age = _fmt_age(age_mins)
            if has_error:
                status = "error"
            elif age_mins > stale_mins:
                status = "stale"
            else:
                status = "ok"

        # If launchd says the process is running, override stale→ok for persistent daemons
        if status == "stale" and pid:
            status = "ok"

        results.append({
            "name":      name,
            "desc":      desc,
            "status":    status,
            "age":       age,
            "pid":       pid or "",
            "last_line": last_line[40:120] if last_line else "",  # strip timestamp prefix
        })
    return results

DASH_PATH = Path(__file__).parent / "static" / "dashboard.html"

_TOOLTIPS = {
    # Main table columns
    "dealer":        "The dealership or marketplace where this car is listed. Dealers are tracked individually; auction sites like BaT and pcarmarket are included as separate sources.",
    "year":          "Model year of the vehicle as listed by the seller.",
    "make":          "Vehicle manufacturer — always Porsche for this tracker.",
    "title":         "Listing title as published by the seller. Replaces the parsed model field — shows exactly what the seller wrote.",
    "trim":          "Sub-model or variant (GT3, Turbo S, Targa, Spyder, etc.) as listed by the seller. May be blank if not specified.",
    "miles":         "Odometer reading in miles as reported by the seller. Not verified — treat high-mileage listings with skepticism if mileage seems unusually low.",
    "price":         "Current asking price for dealer/retail listings. For auctions, shows Current Bid (active) or Sold price.",
    "days_on_site":  "Calendar days from when this listing was first seen in a scrape to the most recent confirmation. Longer = harder to sell, possibly overpriced or has hidden issues.",
    "first_seen":    "Date this listing first appeared in a scrape run. Not necessarily the day the car was listed — we may have missed it earlier.",
    "vin":           "17-character Vehicle Identification Number. Blank if the seller didn't publish it. Used to deduplicate listings across scrape runs.",
    "link":          "Direct link to the listing page. Opens in a new tab.",
    "fmv":           "Fair Market Value estimate based on recent BaT sold comps. Shows % above/below FMV. DEAL = 10%+ below, WATCH = 5-10% below. Requires sufficient comp data to be meaningful.",
    # Stat boxes
    "active_count":  "Total cars currently active across all tracked dealers and marketplaces.",
    "new_count":     "Cars seen for the first time today. A new scrape run may pick up listings that were posted days ago.",
    "sold_count":    "Cars that were active in the last scrape but not found today — assumed sold or delisted.",
    "sitting_count": "Active listings that have been visible for 30 or more days without selling. Often indicates overpricing.",
    "dealer_count":  "Number of distinct dealers and marketplaces with active inventory.",
    "deal_count":    "Listings priced below FMV. GT/Collector cars: 5%+ below FMV. Standard cars: 10%+ below FMV. Based on sold comp data — confidence shown per listing.",
}


def fmt_price(p):
    if p is None:
        return "—"
    return f"${p:,.0f}"


def fmt_miles(m):
    if m is None:
        return "—"
    return f"{m:,.0f}"


def tip(key):
    text = _TOOLTIPS.get(key, "")
    if not text:
        return ""
    safe = text.replace('"', "&quot;")
    return f'<span class="tip" data-tip="{safe}">ℹ</span>'


def generate():
    with get_conn() as conn:
        d = get_dashboard_data(conn)
        scored_listings = fmv_engine.score_active_listings(conn)

        # Comps by generation — query fresh here (not in get_dashboard_data to keep it focused)
        gen_rows = conn.execute("""
            SELECT
                generation,
                COUNT(*)                                             AS comp_count,
                CAST(AVG(sold_price) AS INTEGER)                    AS avg_price,
                MIN(sold_price)                                      AS min_price,
                MAX(sold_price)                                      AS max_price,
                SUM(CASE WHEN LOWER(transmission)='manual' THEN 1 ELSE 0 END) AS manual_count,
                SUM(CASE WHEN transmission IS NOT NULL THEN 1 ELSE 0 END)     AS trans_known
            FROM sold_comps
            WHERE sold_price IS NOT NULL
              AND generation IS NOT NULL
            GROUP BY generation
            ORDER BY avg_price DESC
        """).fetchall()
        gen_stats = [dict(r) for r in gen_rows]

        # Recent sold comps per generation (24mo) for the by-gen panel inline comps
        recent_comps_rows = conn.execute("""
            SELECT generation, year, trim, mileage, sold_price, sold_date,
                   listing_url, transmission, source
            FROM sold_comps
            WHERE sold_price IS NOT NULL
              AND generation IS NOT NULL
              AND sold_date >= date('now', '-24 months')
            ORDER BY sold_date DESC
        """).fetchall()
        recent_comps_by_gen = {}
        for r in recent_comps_rows:
            g = r["generation"]
            recent_comps_by_gen.setdefault(g, []).append(dict(r))

    # Generation derivation from year+model (for active listings that lack a gen column)
    def _gen_from_year_model(year, model):
        if year is None:
            return "Unknown"
        m = (model or "").lower()
        if "boxster" in m and year <= 2004:
            return "986"
        if ("boxster" in m and year <= 2012) or ("cayman" in m and year <= 2012):
            return "987"
        if ("boxster" in m and year <= 2016) or ("cayman" in m and year <= 2016):
            return "981"
        if "boxster" in m or "cayman" in m or "718" in m:
            return "718/982"
        if year <= 1977: return "930"
        if year <= 1989: return "G-Series"
        if year <= 1993: return "964"
        if year <= 1998: return "993"
        if year <= 2004: return "996"
        if year <= 2008: return "997.1"
        if year <= 2012: return "997.2"
        if year <= 2015: return "991.1"
        if year <= 2019: return "991.2"
        return "992"

    # Build FMV lookup: listing_url → deal_score dict
    _fmv_by_url = {}
    for s in scored_listings:
        if s.get("listing_url") and s.get("deal_score"):
            _fmv_by_url[s["listing_url"]] = s["deal_score"]

    # Deal counts for stat cards
    deals_t1  = [s for s in scored_listings if s["tier"] == "TIER1"
                 and s.get("deal_score") and s["deal_score"]["deal_flag"] in ("DEAL", "WATCH")]
    deals_t2  = [s for s in scored_listings if s["tier"] == "TIER2"
                 and s.get("deal_score") and s["deal_score"]["deal_flag"] == "DEAL"]
    deal_count = len(deals_t1) + len(deals_t2)

    weights = dw.load_weights()
    source_health = build_source_health()

    def _trim_cell(trim, url):
        if url:
            return f'<a href="{url}" target="_blank">{trim}</a>'
        return trim

    _DEAL_BADGE = {
        "DEAL":  '<span class="fmv-badge fmv-deal">DEAL</span>',
        "WATCH": '<span class="fmv-badge fmv-watch">WATCH</span>',
        "FAIR":  '',
        "ABOVE": '',
    }

    def _fmv_cell(car):
        url = car.get("listing_url") or ""
        ds  = _fmv_by_url.get(url)
        if not ds:
            return '<span style="color:var(--muted);font-size:10px">—</span>'
        badge = _DEAL_BADGE.get(ds["deal_flag"], "")
        pct   = ds["pct_vs_fmv"]
        sign  = "+" if pct > 0 else ""
        color = "var(--green)" if pct <= -0.05 else ("var(--muted)" if abs(pct) <= 0.05 else "var(--orange)")
        pct_str = f'{sign}{pct:.0%}'
        fmv_str = f'${ds["fmv"]:,}'
        conf    = ds["confidence"]
        return (f'<span style="font-size:11px;color:{color}">{pct_str}</span> '
                f'<span style="font-size:10px;color:var(--muted)">({fmv_str})</span> '
                f'{badge}')

    def row(car, highlight=""):
        days = car.get("days_on_site", 0) or 0
        days_cls = "days-hot" if days >= 60 else ("days-warm" if days >= 30 else "")
        url = car.get("listing_url") or ""
        cat = car.get("source_category") or source_category(car.get("dealer", ""))
        badge = f'<span class="badge-cat badge-{cat}">{cat}</span>'
        dealer = car.get("dealer", "")
        wbadge = dw.tier_badge_html(dealer, weights)
        tier = car.get("tier") or "TIER2"
        tier_badge = '<span class="badge badge-tier1">GT</span>' if tier == "TIER1" else ""
        row_cls = f"{highlight} tier1-row" if tier == "TIER1" else highlight
        _trim  = car.get("trim") or "—"
        # Auction-aware price cell
        p = car.get("price")
        if cat == "AUCTION":
            status = (car.get("status") or "active").lower()
            if status == "sold":
                price_cell = f'<span style="color:var(--green)">Sold: {fmt_price(p)}</span>'
            else:
                price_cell = f'<span style="color:var(--accent)">Current Bid: {fmt_price(p)}</span>'
        else:
            price_cell = fmt_price(p)
        fmv_cell = _fmv_cell(car)
        return (f'<tr class="{row_cls.strip()}" data-category="{cat}" data-tier="{tier}">'
                f'<td class="col-dealer" title="{dealer}">{dealer}{badge}{wbadge}{tier_badge}</td>'
                f'<td class="{days_cls}">{days}</td>'
                f'<td>{car.get("year","")}</td>'
                f'<td class="col-trim" title="{_trim}">{_trim_cell(_trim, url)}</td>'
                f'<td>{fmt_miles(car.get("mileage"))}</td>'
                f'<td>{price_cell}</td>'
                f'<td>{fmv_cell}</td>'
                f'<td>{car.get("date_first_seen","")}</td>'
                f'</tr>')

    active          = d["active"]
    new_today       = d["new_today"]
    sold_today      = d["sold_today"]
    active_auctions = d.get("active_auctions", [])

    # Filter out Holt Motorsports (per user request) and pre-1984 cars
    def _display_filter(c):
        if (c.get("dealer") or "").lower() == "holt motorsports":
            return False
        if (c.get("year") or 9999) < 1984:
            return False
        return True
    active          = [c for c in active          if _display_filter(c)]
    new_today       = [c for c in new_today        if _display_filter(c)]
    sold_today      = [c for c in sold_today       if _display_filter(c)]
    active_auctions = [c for c in active_auctions  if _display_filter(c)]

    sitting_30 = [c for c in active if (c.get("days_on_site") or 0) >= 30]

    main_rows  = "\n".join(row(c) for c in sorted(active, key=lambda x: x.get("date_first_seen") or "", reverse=True))
    new_rows   = "\n".join(row(c, "highlight-new") for c in sorted(new_today[:40], key=lambda x: x.get("date_first_seen") or "", reverse=True))
    long_rows  = "\n".join(
        row(c) for c in sorted(active, key=lambda x: x.get("days_on_site") or 0, reverse=True)[:20]
    )

    def auction_row(a):
        cat   = a.get("source_category") or "AUCTION"
        tier  = a.get("tier") or "TIER2"
        dealer = a.get("dealer", "")
        url   = a.get("listing_url") or ""
        badge = f'<span class="badge-cat badge-{cat}">{cat}</span>'
        tier_badge = '<span class="badge badge-tier1">GT</span>' if tier == "TIER1" else ""
        cur   = a.get("current_bid")
        prev  = a.get("prev_bid")
        if cur and prev and cur != prev:
            bid_cell = (f'{fmt_price(cur)} '
                        f'<span style="color:var(--green);font-size:10px">'
                        f'▲ from {fmt_price(prev)}</span>')
        elif cur:
            bid_cell = fmt_price(cur)
        else:
            bid_cell = '<span style="color:var(--muted)">No bids</span>'
        _trim  = a.get("trim") or "—"
        row_cls = "tier1-row" if tier == "TIER1" else ""
        return (f'<tr class="{row_cls}" data-category="{cat}" data-tier="{tier}">'
                f'<td class="col-dealer" title="{dealer}">{dealer}{badge}{tier_badge}</td>'
                f'<td style="text-align:right">{a.get("days_on_site","")}</td>'
                f'<td>{a.get("year","")}</td>'
                f'<td class="col-trim" title="{_trim}">{_trim_cell(_trim, url)}</td>'
                f'<td style="text-align:right">{bid_cell}</td>'
                f'<td style="text-align:right">{fmt_miles(a.get("mileage"))}</td>'
                f'<td>{a.get("date_first_seen","")}</td>'
                f'</tr>')

    auction_rows = "\n".join(auction_row(a) for a in sorted(active_auctions, key=lambda x: x.get("date_first_seen") or "", reverse=True))

    reserve_not_met = d.get("reserve_not_met", [])
    rnm_count = len(reserve_not_met)

    def rnm_row(r):
        bid = r.get("high_bid")
        bid_cell = (f'<span style="color:var(--muted)">High Bid: {fmt_price(bid)}'
                    f' <span style="font-size:10px;color:var(--red)">(RNM)</span></span>')
        return (f'<tr>'
                f'<td>{r.get("auction_date") or "—"}</td>'
                f'<td>{r.get("year") or "—"}</td>'
                f'<td style="text-align:right">{bid_cell}</td>'
                f'</tr>')

    rnm_rows = "\n".join(rnm_row(r) for r in reserve_not_met)

    # ── Deals rows (below FMV) ────────────────────────────────────────────────
    def deal_row(s):
        ds    = s["deal_score"]
        tier  = s["tier"] or "TIER2"
        url   = s.get("listing_url") or ""
        dealer = s.get("dealer", "")
        cat   = s.get("source_category") or source_category(dealer)
        badge = f'<span class="badge-cat badge-{cat}">{cat}</span>'
        tier_badge = '<span class="badge badge-tier1">GT</span>' if tier == "TIER1" else ""
        row_cls = "tier1-row" if tier == "TIER1" else ""
        flag  = ds["deal_flag"]
        flag_badge = _DEAL_BADGE.get(flag, "")
        pct   = ds["pct_vs_fmv"]
        color = "var(--green)" if pct <= -0.10 else "var(--yellow)"
        conf  = ds["confidence"]
        return (
            f'<tr class="{row_cls}">'
            f'<td class="col-dealer" title="{dealer}">{dealer}{badge}{tier_badge}</td>'
            f'<td>{s.get("year","")}</td>'
            f'<td class="col-trim">{_trim_cell(s.get("trim") or "—", url)}</td>'
            f'<td>{fmt_miles(s.get("mileage"))}</td>'
            f'<td>{fmt_price(s.get("price"))}</td>'
            f'<td style="color:{color}">{pct:+.0%}</td>'
            f'<td>{fmt_price(ds["fmv"])} <span style="font-size:10px;color:var(--muted)">{conf}</span></td>'
            f'<td>{flag_badge}</td>'
            f'</tr>'
        )

    all_deals = sorted(
        deals_t1 + deals_t2,
        key=lambda s: s["deal_score"]["pct_vs_fmv"]
    )
    deals_rows = "\n".join(deal_row(s) for s in all_deals)

    # ── By Generation panel ───────────────────────────────────────────────────
    # Group active listings by derived generation
    from collections import defaultdict
    active_by_gen = defaultdict(list)
    for c in active:
        g = _gen_from_year_model(c.get("year"), c.get("model"))
        active_by_gen[g].append(c)

    # Gen order: newest/most valuable first, then classics
    _GEN_ORDER = ["992","991.2","991.1","997.2","997.1","996","993","964","G-Series",
                  "930","718/982","981","987","986","Unknown"]

    def _gen_panel_html():
        parts = []
        for gen in _GEN_ORDER:
            listings = active_by_gen.get(gen, [])
            comps    = recent_comps_by_gen.get(gen, [])
            if not listings and not comps:
                continue

            # Stats for the gen header card
            prices    = [c.get("price") for c in listings if c.get("price")]
            med_ask   = sorted(prices)[len(prices)//2] if prices else None
            comp_prices = [c["sold_price"] for c in comps if c.get("sold_price")]
            med_comp  = sorted(comp_prices)[len(comp_prices)//2] if comp_prices else None
            mt_comps  = [c for c in comps if (c.get("transmission") or "").lower() == "manual"]
            mt_pct    = f"{100*len(mt_comps)//len(comps)}%" if comps else "—"

            # vs FMV delta for header
            if med_ask and med_comp:
                delta = (med_ask - med_comp) / med_comp
                sign  = "+" if delta >= 0 else ""
                d_col = "var(--green)" if delta <= -0.05 else ("var(--red)" if delta >= 0.10 else "var(--muted)")
                delta_html = f'<span style="color:{d_col};font-size:11px">{sign}{delta:.0%} vs comps</span>'
            else:
                delta_html = ""

            slug = gen.replace(".", "-").replace("/", "-")

            # Gen header card
            parts.append(f"""
<div class="gen-card" id="gc-{slug}">
  <div class="gen-card-header" onclick="toggleGen('{slug}')">
    <div class="gen-card-left">
      <span class="gen-name">{gen}</span>
      <div class="gen-chips">
        <span class="gen-chip chip-active">{len(listings)} active</span>
        <span class="gen-chip chip-comps">{len(comps)} comps (24mo)</span>
        {f'<span class="gen-chip chip-ask">Median Ask {fmt_price(med_ask)}</span>' if med_ask else ''}
        {f'<span class="gen-chip chip-comp">Comp Median {fmt_price(med_comp)}</span>' if med_comp else ''}
        {f'<span class="gen-chip chip-mt">MT {mt_pct}</span>' if comps else ''}
        {delta_html}
      </div>
    </div>
    <span class="gen-chevron" id="chev-{slug}">▸</span>
  </div>
  <div class="gen-card-body" id="gb-{slug}" style="display:none">""")

            if listings:
                parts.append(f"""
    <div class="gen-section-label">Active Listings ({len(listings)})</div>
    <table class="gen-tbl">
      <thead><tr>
        <th>Source</th><th>Year</th><th>Trim</th>
        <th style="text-align:right">Miles</th>
        <th style="text-align:right">Price / Bid</th>
        <th style="text-align:right">vs FMV</th>
        <th>Since</th>
      </tr></thead>
      <tbody>""")
                for c in sorted(listings, key=lambda x: x.get("price") or 0):
                    url    = c.get("listing_url") or ""
                    _trim  = c.get("trim") or "—"
                    trim_cell = f'<a href="{url}" target="_blank">{_trim}</a>' if url else _trim
                    cat    = c.get("source_category") or source_category(c.get("dealer",""))
                    badge  = f'<span class="badge-cat badge-{cat}">{cat}</span>'
                    tier   = c.get("tier") or "TIER2"
                    tier_b = '<span class="badge badge-tier1">GT</span>' if tier=="TIER1" else ""
                    dealer = c.get("dealer","")
                    fmv_c  = _fmv_cell(c)
                    days   = c.get("days_on_site") or 0
                    days_c = f'<span class="days-hot">{days}d</span>' if days>=60 else (f'<span class="days-warm">{days}d</span>' if days>=30 else f'{days}d')
                    # Auction-aware price cell — same logic as main table
                    p = c.get("price")
                    if cat == "AUCTION":
                        status = (c.get("status") or "active").lower()
                        if status == "sold":
                            price_cell = f'<span style="color:var(--green)">Sold: {fmt_price(p)}</span>'
                        else:
                            price_cell = f'<span style="color:var(--accent)">Current Bid: {fmt_price(p)}</span>'
                    else:
                        price_cell = fmt_price(p)
                    parts.append(
                        f'<tr class="{"tier1-row" if tier=="TIER1" else ""}" data-category="{cat}" data-tier="{tier}">'
                        f'<td class="col-dealer" title="{dealer}">{dealer}{badge}{tier_b}</td>'
                        f'<td>{c.get("year","")}</td>'
                        f'<td class="col-trim" title="{_trim}">{trim_cell}</td>'
                        f'<td style="text-align:right">{fmt_miles(c.get("mileage"))}</td>'
                        f'<td style="text-align:right">{price_cell}</td>'
                        f'<td style="text-align:right">{fmv_c}</td>'
                        f'<td>{c.get("date_first_seen","")}</td>'
                        f'</tr>'
                    )
                parts.append("      </tbody></table>")

            if comps:
                show_n = min(10, len(comps))
                parts.append(f"""
    <div class="gen-section-label" style="margin-top:10px">
      Recent Sold Comps — last 24 months ({len(comps)} total, showing {show_n} most recent)
    </div>
    <table class="gen-tbl gen-comps-tbl">
      <thead><tr>
        <th>Source</th><th>Year</th><th>Trim</th>
        <th style="text-align:right">Miles</th>
        <th style="text-align:right">Sold Price</th>
        <th>Trans</th><th>Sold Date</th>
      </tr></thead>
      <tbody>""")
                for c in comps[:show_n]:
                    url   = c.get("listing_url") or ""
                    _trim = c.get("trim") or "—"
                    tc    = f'<a href="{url}" target="_blank">{_trim}</a>' if url else _trim
                    trans = c.get("transmission") or "—"
                    mt_hi = ' style="color:var(--green)"' if trans.lower()=="manual" else ""
                    parts.append(
                        f'<tr>'
                        f'<td class="col-dealer">{c.get("source","")}</td>'
                        f'<td>{c.get("year","")}</td>'
                        f'<td class="col-trim" title="{_trim}">{tc}</td>'
                        f'<td style="text-align:right">{fmt_miles(c.get("mileage"))}</td>'
                        f'<td style="text-align:right;color:var(--green)">{fmt_price(c.get("sold_price"))}</td>'
                        f'<td{mt_hi}>{trans}</td>'
                        f'<td>{c.get("sold_date","")}</td>'
                        f'</tr>'
                    )
                parts.append("      </tbody></table>")

            parts.append("  </div>\n</div>")  # close gen-card-body + gen-card

        return "\n".join(parts)

    by_gen_panel_html = _gen_panel_html()

    def gen_row(g):
        mt_pct = ""
        if g["trans_known"] and g["trans_known"] > 0:
            pct = g["manual_count"] / g["trans_known"] * 100
            mt_pct = f"{pct:.0f}%"
        else:
            mt_pct = '<span style="color:var(--muted)">—</span>'
        return (
            f'<tr>'
            f'<td><strong>{g["generation"]}</strong></td>'
            f'<td style="text-align:right">{g["comp_count"]}</td>'
            f'<td style="text-align:right">{fmt_price(g["avg_price"])}</td>'
            f'<td style="text-align:right">{fmt_price(g["min_price"])}</td>'
            f'<td style="text-align:right">{fmt_price(g["max_price"])}</td>'
            f'<td style="text-align:right">{mt_pct}</td>'
            f'</tr>'
        )

    gen_table_rows = "\n".join(gen_row(g) for g in gen_stats)

    def sold_price_cell(r):
        p = r["price"]
        cat = r.get("source_category") or source_category(r.get("dealer", ""))
        if cat == "AUCTION":
            return f'<span style="color:var(--green)">Sold: {fmt_price(p)}</span>'
        return fmt_price(p)

    sold_rows  = "\n".join(
        f'<tr><td class="col-dealer" title="{r["dealer"]}">{r["dealer"]}</td>'
        f'<td>{r["days_on_site"]}</td>'
        f'<td>{r["year"]}</td>'
        f'<td class="col-trim">{_trim_cell(r.get("trim") or "—", r.get("listing_url") or "")}</td>'
        f'<td>{fmt_miles(r.get("mileage"))}</td>'
        f'<td>{sold_price_cell(r)}</td></tr>'
        for r in sorted(sold_today, key=lambda x: x.get("date_first_seen") or "", reverse=True)
    )
    dealer_rows = "\n".join(
        f'<tr><td class="col-dealer" title="{r["dealer"]}">{r["dealer"]}</td>'
        f'<td>{r["cnt"]}</td></tr>'
        for r in d["dealer_counts"]
        if (r["dealer"] or "").lower() != "holt motorsports"
    )

    total         = len(active)
    tier1_count   = sum(1 for c in active if c.get("tier") == "TIER1")
    new_count     = len(new_today)
    new_t1        = sum(1 for c in new_today if c.get("tier") == "TIER1")
    sold_count    = len(sold_today)
    sitting_cnt   = len(sitting_30)
    dealer_cnt    = len([r for r in d["dealer_counts"] if (r["dealer"] or "").lower() != "holt motorsports"])
    auction_count = len(active_auctions)

    def _source_pill(s):
        status = s["status"]
        pid_str = f"  PID {s['pid']}" if s["pid"] else "  (not running)"
        tip_text = (
            f"{s['desc']}{pid_str}&#10;"
            f"Last activity: {s['age']}&#10;"
            f"{s['last_line']}"
        ).replace('"', "&quot;")
        return (
            f'<div class="source-pill sp-{status}" data-tip="{tip_text}">'
            f'<span class="sp-dot"></span>'
            f'<span class="sp-name">{s["name"]}</span>'
            f'<span class="sp-age">{s["age"]}</span>'
            f'</div>'
        )

    source_pills = "\n".join(_source_pill(s) for s in source_health)

    def th(label, tip_key, col_idx, tbl_id):
        return (f'<th onclick="sortTable(\'{tbl_id}\',{col_idx})">'
                f'{label}{tip(tip_key)}</th>')

    main_header = (
        f'<tr>'
        f'{th("Dealer","dealer",0,"tbl-all")}'
        f'{th("Days on Site","days_on_site",1,"tbl-all")}'
        f'{th("Year","year",2,"tbl-all")}'
        f'{th("Trim","trim",3,"tbl-all")}'
        f'{th("Miles","miles",4,"tbl-all")}'
        f'{th("Price / Bid","price",5,"tbl-all")}'
        f'{th("vs FMV","fmv",6,"tbl-all")}'
        f'{th("First Seen","first_seen",7,"tbl-all")}'
        f'</tr>'
    )

    def std_header(tbl_id):
        return (
            f'<tr>'
            f'<th onclick="sortTable(\'{tbl_id}\',0)">Dealer{tip("dealer")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',1)">Days on Site{tip("days_on_site")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',2)">Year{tip("year")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',3)">Trim{tip("trim")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',4)">Miles{tip("miles")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',5)">Price / Bid{tip("price")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',6)">vs FMV{tip("fmv")}</th>'
            f'<th onclick="sortTable(\'{tbl_id}\',7)">First Seen{tip("first_seen")}</th>'
            f'</tr>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Porsche Competition Inventory Track &amp; Market Analysis</title>
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --surface2: #21253a;
    --border: #2e3250; --text: #e8eaf0; --muted: #8890b0;
    --accent: #a855f7; --green: #22c55e; --red: #ef4444;
    --yellow: #f59e0b; --blue: #3b82f6; --orange: #f97316;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          font-size: 13px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border);
            padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }}
  header h1 {{ font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: .5px; }}
  header .meta {{ color: var(--muted); font-size: 11px; }}

  /* Nav */
  .report-bar {{ background: var(--surface); border-bottom: 1px solid var(--border);
                 padding: 8px 24px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .report-link {{ background: var(--surface2); border: 1px solid var(--border);
                  border-radius: 6px; padding: 5px 13px; font-size: 12px;
                  color: var(--muted); text-decoration: none; transition: color .15s, border-color .15s; }}
  .report-link:hover {{ color: var(--accent); border-color: var(--accent); text-decoration: none; }}

  /* Panel breadcrumb nav — shown at top of every non-main panel */
  .panel-nav {{
    display: flex; align-items: center; gap: 12px;
    padding: 8px 24px 10px; border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }}
  .panel-nav a {{
    color: var(--muted); font-size: 12px; text-decoration: none;
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 11px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--surface2);
    transition: color .15s, border-color .15s;
  }}
  .panel-nav a:hover {{ color: var(--accent); border-color: var(--accent); text-decoration: none; }}
  .panel-nav .sep {{ color: var(--border); font-size: 14px; }}
  .panel-nav .view-name {{
    font-size: 12px; font-weight: 600; color: var(--text);
    text-transform: uppercase; letter-spacing: .5px;
  }}

  /* Category + Tier filter bars */
  .filter-row {{ display: flex; gap: 6px; padding: 8px 24px; align-items: center;
                 border-bottom: 1px solid var(--border); }}
  .filter-row + .filter-row {{ border-top: none; }}
  .filter-row span {{ font-size: 11px; color: var(--muted); margin-right: 4px; min-width: 50px; }}
  .cat-btn {{ padding: 4px 14px; border-radius: 20px; border: 1px solid var(--border);
              background: transparent; color: var(--muted); font-size: 11px; font-weight: 600;
              cursor: pointer; transition: all .15s; letter-spacing: .03em; }}
  .cat-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .cat-btn.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
  .tier-btn {{ padding: 4px 14px; border-radius: 20px; border: 1px solid var(--border);
               background: transparent; color: var(--muted); font-size: 11px; font-weight: 600;
               cursor: pointer; transition: all .15s; letter-spacing: .03em; }}
  .tier-btn:hover {{ border-color: #d97706; color: #d97706; }}
  .tier-btn.active {{ background: #d97706; border-color: #d97706; color: #fff; }}

  /* TIER1 row highlight */
  .tier1-row {{ border-left: 3px solid #d97706 !important; }}

  /* Tier badge */
  .badge-tier1 {{ background: rgba(217,119,6,.25); color: #fbbf24;
                  font-weight: 700; letter-spacing: .04em; }}

  /* Stat boxes — clickable panel selectors */
  .stats {{ display: flex; gap: 12px; padding: 16px 24px; flex-wrap: wrap; }}
  .stat-card {{
    background: var(--surface); border: 2px solid var(--border);
    border-radius: 10px; padding: 14px 20px; min-width: 120px;
    cursor: pointer; transition: border-color .15s, background .15s; user-select: none;
    position: relative;
  }}
  .stat-card:hover {{ border-color: var(--accent); background: var(--surface2); }}
  .stat-card.active {{ border-color: var(--accent); background: rgba(168,85,247,.08); }}
  .stat-card .val {{ font-size: 30px; font-weight: 700; }}
  .stat-card .lbl {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}
  .stat-card .tip-anchor {{ position: absolute; top: 8px; right: 10px; }}

  /* Panels */
  .panel {{ display: none; padding: 16px 24px; }}
  .panel.active {{ display: block; }}
  .search-bar {{ margin-bottom: 12px; }}
  .search-bar input {{
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 7px 12px; border-radius: 6px;
    width: 320px; font-size: 12px; outline: none;
  }}
  .search-bar input:focus {{ border-color: var(--accent); }}
  .section-title {{
    font-size: 12px; font-weight: 600; color: var(--muted);
    margin-bottom: 10px; text-transform: uppercase; letter-spacing: .5px;
  }}
  .empty {{ color: var(--muted); padding: 24px; text-align: center; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; table-layout: auto; }}
  thead th {{
    background: var(--surface2); color: var(--muted); text-align: left;
    padding: 8px 10px; cursor: pointer; user-select: none;
    white-space: nowrap; border-bottom: 1px solid var(--border);
  }}
  thead th:hover {{ color: var(--text); }}
  thead th::after {{ content: ' ↕'; opacity: .3; }}
  thead th.sort-asc::after {{ content: ' ↑'; opacity: 1; color: var(--accent); }}
  thead th.sort-desc::after {{ content: ' ↓'; opacity: 1; color: var(--accent); }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:hover {{ background: var(--surface2); }}
  td {{ padding: 7px 10px; vertical-align: middle; white-space: nowrap; }}
  td a {{ color: var(--blue); text-decoration: none; }}
  td a:hover {{ text-decoration: underline; }}
  /* Truncating columns — overflow hidden with hover tooltip via title attr */
  td.col-dealer {{ max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.col-trim   {{ max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  /* Right-align numeric columns: Days on Site, Miles, Price */
  td:nth-child(2), th:nth-child(2),
  td:nth-child(5), th:nth-child(5),
  td:nth-child(6), th:nth-child(6) {{ text-align: right; }}
  /* Link column stays compact */
  td:last-child, th:last-child {{ min-width: 48px; }}
  .days-hot {{ color: var(--red); font-weight: 700; }}
  .days-warm {{ color: var(--yellow); font-weight: 600; }}
  .highlight-new td {{ background: rgba(34,197,94,.05); }}

  /* Badges */
  .badge {{ display: inline-block; font-size: 10px; padding: 1px 5px;
            border-radius: 4px; margin-left: 4px; }}
  .badge-cat {{ display: inline-block; font-size: 9px; padding: 1px 5px;
               border-radius: 3px; font-weight: 700; letter-spacing: .04em;
               margin-left: 4px; vertical-align: middle; }}
  .badge-DEALER  {{ background: rgba(59,130,246,.18);  color: #60a5fa; }}
  .badge-AUCTION {{ background: rgba(168,85,247,.18);  color: #c084fc; }}
  .badge-RETAIL  {{ background: rgba(34,197,94,.18);   color: #4ade80; }}

  /* FMV deal badges */
  .fmv-badge {{ display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 3px;
                font-weight: 700; letter-spacing: .05em; margin-left: 3px; vertical-align: middle; }}
  .fmv-deal  {{ background: rgba(34,197,94,.22);  color: #4ade80; }}
  .fmv-watch {{ background: rgba(245,158,11,.22); color: #fbbf24; }}

  /* Dealer credibility weight badges */
  .badge-weight {{ display: inline-block; font-size: 9px; padding: 1px 4px; border-radius: 3px;
                  font-weight: 700; letter-spacing: .04em; margin-left: 4px;
                  vertical-align: middle; cursor: help; position: relative; }}
  .badge-weight-high   {{ background: rgba(34,197,94,.18);  color: #4ade80; }}
  .badge-weight-medium {{ background: rgba(245,158,11,.18); color: #fbbf24; }}
  .badge-weight-low    {{ background: rgba(239,68,68,.18);  color: #f87171; }}
  .badge-weight::after {{ content: attr(data-wtip); position: absolute; bottom: calc(100% + 5px);
                         left: 50%; transform: translateX(-50%); background: #1a1a1a;
                         color: #e0e0e0; padding: 7px 10px; border-radius: 6px; font-size: 11px;
                         line-height: 1.5; width: 260px; white-space: normal; z-index: 9999;
                         pointer-events: none; opacity: 0; transition: opacity .15s;
                         border: 1px solid #333; font-weight: 400; letter-spacing: 0; }}
  .badge-weight:hover::after {{ opacity: 1; }}

  /* Tooltips */
  .tip {{
    position: relative; display: inline-block; cursor: help;
    margin-left: 4px; font-size: 10px; color: var(--muted);
    vertical-align: middle; opacity: .6;
  }}
  .tip:hover {{ opacity: 1; }}
  .tip::after {{
    content: attr(data-tip);
    position: absolute; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%);
    background: #1e2235; color: var(--text);
    padding: 8px 11px; border-radius: 7px; font-size: 11px;
    line-height: 1.55; width: 270px; white-space: normal;
    z-index: 9999; pointer-events: none;
    opacity: 0; transition: opacity .15s;
    border: 1px solid var(--border);
    font-weight: 400; text-transform: none; letter-spacing: 0;
  }}
  .tip:hover::after {{ opacity: 1; }}

  /* Two-col layout for dealers panel */
  .two-col {{ display: grid; grid-template-columns: 1fr 2fr; gap: 16px; }}

  /* "How this works" collapsible */
  details {{ margin: 24px; border: 1px solid var(--border); border-radius: 8px; }}
  summary {{
    padding: 14px 18px; cursor: pointer; font-size: 13px; font-weight: 600;
    color: var(--muted); list-style: none; display: flex; justify-content: space-between;
  }}
  summary::after {{ content: '▸'; }}
  details[open] summary::after {{ content: '▾'; }}
  summary:hover {{ color: var(--text); }}
  details .def-body {{ padding: 0 18px 18px; }}
  details h3 {{ font-size: 12px; font-weight: 600; color: var(--accent);
               margin: 14px 0 5px; text-transform: uppercase; letter-spacing: .05em; }}
  details p {{ font-size: 12px; color: var(--muted); line-height: 1.6; margin-bottom: 6px; }}
  details ul {{ font-size: 12px; color: var(--muted); line-height: 1.8;
               padding-left: 18px; margin-bottom: 6px; }}

  /* Source health bar */
  .source-bar {{
    display: flex; gap: 10px; padding: 10px 24px 12px;
    flex-wrap: wrap; border-bottom: 1px solid var(--border);
    align-items: center;
  }}
  .source-bar-label {{
    font-size: 10px; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em;
    margin-right: 4px; white-space: nowrap;
  }}
  .source-pill {{
    display: flex; align-items: center; gap: 7px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 12px; cursor: default;
    position: relative; transition: border-color .15s;
  }}
  .source-pill:hover {{ border-color: var(--accent); }}
  .source-pill .sp-dot {{
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }}
  .sp-ok      .sp-dot {{ background: var(--green); box-shadow: 0 0 5px var(--green); }}
  .sp-stale   .sp-dot {{ background: var(--yellow); }}
  .sp-error   .sp-dot {{ background: var(--red);    box-shadow: 0 0 5px var(--red); }}
  .sp-unknown .sp-dot {{ background: var(--muted);  }}
  .source-pill .sp-name {{
    font-size: 11px; font-weight: 600; color: var(--text); white-space: nowrap;
  }}
  .source-pill .sp-age {{
    font-size: 10px; color: var(--muted); white-space: nowrap;
  }}
  .sp-ok    .sp-age {{ color: var(--green);  }}
  .sp-stale .sp-age {{ color: var(--yellow); }}
  .sp-error .sp-age {{ color: var(--red);    }}
  /* Tooltip on hover */
  .source-pill::after {{
    content: attr(data-tip);
    position: absolute; bottom: calc(100% + 6px); left: 0;
    background: #1e2235; color: var(--text);
    padding: 7px 11px; border-radius: 7px; font-size: 11px;
    line-height: 1.5; width: 280px; white-space: normal;
    z-index: 9999; pointer-events: none;
    opacity: 0; transition: opacity .15s;
    border: 1px solid var(--border);
  }}
  .source-pill:hover::after {{ opacity: 1; }}

  @media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  /* ── By Generation panel ─────────────────────────────────────────────── */
  .gen-card {{
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--surface); margin-bottom: 10px; overflow: hidden;
    transition: border-color .15s;
  }}
  .gen-card:hover {{ border-color: #4e5580; }}
  .gen-card-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px; cursor: pointer; user-select: none;
    transition: background .15s;
  }}
  .gen-card-header:hover {{ background: var(--surface2); }}
  .gen-card-left {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  .gen-name {{
    font-size: 16px; font-weight: 700; color: var(--accent);
    min-width: 60px; letter-spacing: .03em;
  }}
  .gen-chips {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .gen-chip {{
    font-size: 11px; font-weight: 600; padding: 3px 10px;
    border-radius: 20px; border: 1px solid var(--border);
    white-space: nowrap;
  }}
  .chip-active  {{ color: #a78bfa; border-color: rgba(167,139,250,.3); background: rgba(167,139,250,.08); }}
  .chip-comps   {{ color: var(--muted); border-color: var(--border); }}
  .chip-ask     {{ color: var(--text); border-color: var(--border); }}
  .chip-comp    {{ color: var(--green); border-color: rgba(34,197,94,.3); background: rgba(34,197,94,.06); }}
  .chip-mt      {{ color: #60a5fa; border-color: rgba(96,165,250,.3); background: rgba(96,165,250,.06); }}
  .gen-chevron {{
    font-size: 14px; color: var(--muted); transition: transform .2s; flex-shrink: 0;
  }}
  .gen-chevron.open {{ transform: rotate(90deg); color: var(--accent); }}
  .gen-card-body {{ padding: 0 20px 16px; border-top: 1px solid var(--border); }}
  .gen-section-label {{
    font-size: 10px; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em;
    padding: 12px 0 6px;
  }}
  .gen-tbl {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 4px; }}
  .gen-tbl thead th {{
    background: var(--surface2); color: var(--muted); text-align: left;
    padding: 6px 8px; white-space: nowrap; border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 11px;
  }}
  .gen-tbl tbody tr {{ border-bottom: 1px solid var(--border); }}
  .gen-tbl tbody tr:hover {{ background: rgba(255,255,255,.03); }}
  .gen-tbl td {{ padding: 6px 8px; vertical-align: middle; white-space: nowrap; }}
  .gen-tbl td a {{ color: var(--blue); text-decoration: none; }}
  .gen-tbl td a:hover {{ text-decoration: underline; }}
  .gen-comps-tbl tbody tr {{ opacity: .85; }}
  .gen-comps-tbl tbody tr:hover {{ opacity: 1; background: rgba(34,197,94,.04); }}
</style>
</head>
<body>

<header>
  <h1>Porsche Competition Inventory Track &amp; Market Analysis</h1>
  <div class="meta">Generated {d['generated_at']} &nbsp;|&nbsp; {total} active listings</div>
</header>

<div class="report-bar">
  <a class="report-link" href="#" onclick="showPanel('all'); return false;">Dashboard</a>
  <a class="report-link" href="#" onclick="showPanel('by-gen'); return false;">By Generation</a>
  <a class="report-link" href="#" onclick="showPanel('comps-gen'); return false;">Comps by Gen</a>
  <a class="report-link" href="weekly_report.html">Market Report</a>
  <a class="report-link" href="daily_report.html">Daily Auctions</a>
  <a class="report-link" href="monthly_report.html">Monthly Report</a>
</div>

<div class="filter-row">
  <span>SOURCE:</span>
  <button class="cat-btn active" onclick="filterCat('all')">All Sources</button>
  <button class="cat-btn" onclick="filterCat('AUCTION')">Auction</button>
  <button class="cat-btn" onclick="filterCat('RETAIL')">Retail</button>
  <button class="cat-btn" onclick="filterCat('DEALER')">Dealer</button>
</div>
<div class="filter-row">
  <span>TIER:</span>
  <button class="tier-btn active" onclick="filterTier('all')">All</button>
  <button class="tier-btn" onclick="filterTier('TIER1')">GT / Collector</button>
  <button class="tier-btn" onclick="filterTier('TIER2')">Standard</button>
</div>

<div class="source-bar">
  <span class="source-bar-label">Pipelines</span>
{source_pills}
</div>

<div class="stats">
  <div class="stat-card active" onclick="showPanel('all')" id="sc-all">
    <span class="tip-anchor">{tip("active_count")}</span>
    <div class="val" style="color:var(--accent)">{total}</div>
    <div class="lbl">Active Listings</div>
    <div style="font-size:10px;color:#d97706;margin-top:2px">{tier1_count} GT/Collector</div>
  </div>
  <div class="stat-card" onclick="showPanel('new')" id="sc-new">
    <span class="tip-anchor">{tip("new_count")}</span>
    <div class="val" style="color:var(--green)">{new_count}</div>
    <div class="lbl">New Today</div>
    <div style="font-size:10px;color:#d97706;margin-top:2px">{new_t1} GT/Collector</div>
  </div>
  <div class="stat-card" onclick="showPanel('sold')" id="sc-sold">
    <span class="tip-anchor">{tip("sold_count")}</span>
    <div class="val" style="color:var(--yellow)">{sold_count}</div>
    <div class="lbl">No Longer Available</div>
  </div>
  <div class="stat-card" onclick="showPanel('longest')" id="sc-longest">
    <span class="tip-anchor">{tip("sitting_count")}</span>
    <div class="val" style="color:var(--orange)">{sitting_cnt}</div>
    <div class="lbl">Sitting 30+ Days</div>
  </div>
  <div class="stat-card" onclick="showPanel('dealers')" id="sc-dealers">
    <span class="tip-anchor">{tip("dealer_count")}</span>
    <div class="val" style="color:var(--blue)">{dealer_cnt}</div>
    <div class="lbl">Dealers</div>
  </div>
  <div class="stat-card" onclick="showPanel('auctions')" id="sc-auctions">
    <div class="val" style="color:var(--accent)">{auction_count}</div>
    <div class="lbl">Active Auctions</div>
  </div>
  <div class="stat-card" onclick="showPanel('rnm')" id="sc-rnm">
    <div class="val" style="color:var(--muted)">{rnm_count}</div>
    <div class="lbl">Reserve Not Met</div>
  </div>
  <div class="stat-card" onclick="showPanel('deals')" id="sc-deals">
    <span class="tip-anchor">{tip("deal_count")}</span>
    <div class="val" style="color:var(--green)">{deal_count}</div>
    <div class="lbl">Deals Below FMV</div>
    <div style="font-size:10px;color:#d97706;margin-top:2px">{len(deals_t1)} GT/Collector</div>
  </div>
</div>

<!-- ALL INVENTORY -->
<div id="panel-all" class="panel active">
  <div class="search-bar">
    <input type="text" id="search-all" placeholder="Search dealer, year, trim…"
           oninput="filterTable('tbl-all','search-all')">
  </div>
  <table id="tbl-all">
    <thead>{main_header}</thead>
    <tbody>{main_rows or '<tr><td colspan="8" class="empty">No active listings — run the scraper first.</td></tr>'}</tbody>
  </table>
</div>

<!-- NEW TODAY -->
<div id="panel-new" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">New Today</span>
  </div>
  <p class="section-title">New listings first seen {d['today']}</p>
  <table id="tbl-new">
    <thead>{std_header("tbl-new")}</thead>
    <tbody>{new_rows or '<tr><td colspan="7" class="empty">No new listings today.</td></tr>'}</tbody>
  </table>
</div>

<!-- ACTIVE AUCTIONS -->
<div id="panel-auctions" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Active Auctions</span>
  </div>
  <p class="section-title">Live auctions — BaT, pcarmarket and other auction sources. Bid amounts go up as auctions progress — these are NOT price drops.</p>
  <table id="tbl-auctions">
    <thead><tr>
      <th onclick="sortTable('tbl-auctions',0)">Source</th>
      <th onclick="sortTable('tbl-auctions',1)" style="text-align:right">Days Active</th>
      <th onclick="sortTable('tbl-auctions',2)">Year</th>
      <th onclick="sortTable('tbl-auctions',3)">Trim</th>
      <th onclick="sortTable('tbl-auctions',4)" style="text-align:right">Current Bid</th>
      <th onclick="sortTable('tbl-auctions',5)" style="text-align:right">Miles</th>
      <th onclick="sortTable('tbl-auctions',6)">First Seen</th>
    </tr></thead>
    <tbody>{auction_rows or '<tr><td colspan="7" class="empty">No active auctions.</td></tr>'}</tbody>
  </table>
</div>

<!-- RESERVE NOT MET -->
<div id="panel-rnm" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Reserve Not Met</span>
  </div>
  <p class="section-title">BaT auctions that ended without meeting reserve — showing 10 most recent. High bid reflects market floor interest.</p>
  <table id="tbl-rnm">
    <thead><tr>
      <th onclick="sortTable('tbl-rnm',0)">Date</th>
      <th onclick="sortTable('tbl-rnm',1)">Year</th>
      <th onclick="sortTable('tbl-rnm',2)" style="text-align:right">High Bid</th>
    </tr></thead>
    <tbody>{rnm_rows or '<tr><td colspan="3" class="empty">No reserve-not-met data yet — run apify_backfill.py first.</td></tr>'}</tbody>
  </table>
</div>

<!-- DEALS BELOW FMV -->
<div id="panel-deals" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Deals Below FMV</span>
  </div>
  <p class="section-title">Listings priced below Fair Market Value. GT/Collector cars flagged at 5%+ below FMV; standard cars at 10%+ below. FMV is based on BaT sold comps — confidence shown per row. Data improves as more comps accumulate.</p>
  <table id="tbl-deals">
    <thead><tr>
      <th onclick="sortTable('tbl-deals',0)">Dealer</th>
      <th onclick="sortTable('tbl-deals',1)">Year</th>
      <th onclick="sortTable('tbl-deals',2)">Trim</th>
      <th onclick="sortTable('tbl-deals',3)">Miles</th>
      <th onclick="sortTable('tbl-deals',4)" style="text-align:right">Ask</th>
      <th onclick="sortTable('tbl-deals',5)" style="text-align:right">vs FMV</th>
      <th onclick="sortTable('tbl-deals',6)" style="text-align:right">FMV Est.</th>
      <th onclick="sortTable('tbl-deals',7)">Flag</th>
    </tr></thead>
    <tbody>{deals_rows or '<tr><td colspan="8" class="empty">No deals found — more comp data needed. Check back after backfill completes.</td></tr>'}</tbody>
  </table>
</div>

<!-- BY GENERATION (combined active + comps) -->
<div id="panel-by-gen" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">By Generation</span>
  </div>
  <p class="section-title" style="padding:10px 0 12px">Active listings and recent sold comps grouped by generation — click any row to expand. Comp median is 24-month rolling. Active listings respect the Source / Tier filters above.</p>
  <div id="by-gen-content">
{by_gen_panel_html}
  </div>
</div>

<!-- COMPS BY GENERATION -->
<div id="panel-comps-gen" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Comps by Generation</span>
  </div>
  <p class="section-title">Sold comp summary grouped by Porsche generation — avg / min / max sale price and manual transmission prevalence. Only rows with a known sale price and decoded generation are included.</p>
  <table id="tbl-comps-gen">
    <thead><tr>
      <th onclick="sortTable('tbl-comps-gen',0)">Generation</th>
      <th onclick="sortTable('tbl-comps-gen',1)" style="text-align:right">Comps</th>
      <th onclick="sortTable('tbl-comps-gen',2)" style="text-align:right">Avg Sale</th>
      <th onclick="sortTable('tbl-comps-gen',3)" style="text-align:right">Min Sale</th>
      <th onclick="sortTable('tbl-comps-gen',4)" style="text-align:right">Max Sale</th>
      <th onclick="sortTable('tbl-comps-gen',5)" style="text-align:right">Manual %</th>
    </tr></thead>
    <tbody>{gen_table_rows or '<tr><td colspan="6" class="empty">No generation data yet — run decode_vin_generation.py after VIN enrichment.</td></tr>'}</tbody>
  </table>
</div>

<!-- SOLD TODAY -->
<div id="panel-sold" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">No Longer Available</span>
  </div>
  <p class="section-title">Cars that disappeared from listings today — assumed sold or delisted</p>
  <table id="tbl-sold">
    <thead><tr>
      <th onclick="sortTable('tbl-sold',0)">Dealer{tip("dealer")}</th>
      <th onclick="sortTable('tbl-sold',1)">Days on Site{tip("days_on_site")}</th>
      <th onclick="sortTable('tbl-sold',2)">Year{tip("year")}</th>
      <th onclick="sortTable('tbl-sold',3)">Trim{tip("trim")}</th>
      <th onclick="sortTable('tbl-sold',4)">Miles{tip("miles")}</th>
      <th onclick="sortTable('tbl-sold',5)">Last Price{tip("price")}</th>
    </tr></thead>
    <tbody>{sold_rows or '<tr><td colspan="6" class="empty">No sold listings today.</td></tr>'}</tbody>
  </table>
</div>

<!-- LONGEST SITTING -->
<div id="panel-longest" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Sitting 30+ Days</span>
  </div>
  <p class="section-title">Top 20 active listings by days on site — potential overprice candidates</p>
  <table id="tbl-longest">
    <thead>{std_header("tbl-longest")}</thead>
    <tbody>{long_rows or '<tr><td colspan="7" class="empty">No data yet.</td></tr>'}</tbody>
  </table>
</div>

<!-- DEALERS -->
<div id="panel-dealers" class="panel">
  <div class="panel-nav">
    <a href="#" onclick="showPanel('all'); return false;">&#8592; Dashboard</a>
    <span class="sep">/</span>
    <span class="view-name">Dealers</span>
  </div>
  <div class="two-col">
    <div>
      <p class="section-title">Inventory by Dealer / Source</p>
      <table id="tbl-dealers">
        <thead><tr>
          <th onclick="sortTable('tbl-dealers',0)">Dealer{tip("dealer")}</th>
          <th onclick="sortTable('tbl-dealers',1)">Active Listings</th>
        </tr></thead>
        <tbody>{dealer_rows or '<tr><td colspan="2" class="empty">No data yet.</td></tr>'}</tbody>
      </table>
    </div>
    <div>
      <p class="section-title">Sold / Delisted Today</p>
      <table>
        <thead><tr>
          <th>Dealer</th><th>Days on Site</th><th>Year</th><th>Trim</th>
          <th>Miles</th><th>Last Price{tip("price")}</th>
        </tr></thead>
        <tbody>{sold_rows or '<tr><td colspan="6" class="empty">No sold listings today.</td></tr>'}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- HOW THIS WORKS -->
<details>
  <summary>How this works</summary>
  <div class="def-body">
    <h3>Data Sources</h3>
    <p>This tracker scrapes 11 independent Porsche dealers plus three open marketplaces (Bring a Trailer, PCA Mart, pcarmarket) every 20 minutes during peak hours (7am–10pm) and hourly overnight. Only Porsche 911, Boxster, and Cayman models from 1986–2024 are tracked. Cayenne, Macan, Panamera, and Taycan are excluded.</p>

    <h3>Source Categories</h3>
    <ul>
      <li><strong>DEALER</strong> — independent specialty dealers (Holt, LBI, Ryan Friedman, etc.) who buy and consign cars.</li>
      <li><strong>AUCTION</strong> — online auction platforms (Bring a Trailer, pcarmarket) where reserve prices and time-limited bidding apply.</li>
      <li><strong>RETAIL</strong> — classified ad marketplaces (PCA Mart, Rennlist) where private sellers and dealers list at fixed prices.</li>
    </ul>

    <h3>Days on Site</h3>
    <p>Counted from the first scrape date to the most recent confirmation. This is not the same as how long the car has actually been for sale — we may have first seen it days or weeks after it was listed.</p>

    <h3>Sold / Delisted</h3>
    <p>A listing is marked "sold" when it was active in the previous scrape but cannot be found today. This may mean it sold, was delisted, or the scraper failed to find it. No actual sale price is recorded unless it appears as a sold comp from BaT or another source.</p>

    <h3>Fair Market Value (FMV)</h3>
    <p>FMV estimates on the Market Report page are calculated as the median sold price for each generation segment, derived from completed BaT auctions and other sold comp sources. A minimum of 3 sold comps are required before an FMV estimate is shown. As more comps accumulate over time, estimates will become more reliable.</p>

    <h3>Deal / Overpriced Flags</h3>
    <p>On the Market Report, a listing is flagged as a <strong>DEAL</strong> if it is priced at 85% or less of the FMV median for its generation. It is flagged <strong>OVERPRICED</strong> if it is 125% or more of FMV. These flags require sufficient comp data to be meaningful.</p>
  </div>
</details>

<script>
const PANELS = ['all','new','auctions','rnm','deals','by-gen','comps-gen','sold','longest','dealers'];

function showPanel(name) {{
  PANELS.forEach(p => {{
    document.getElementById('panel-' + p).classList.toggle('active', p === name);
    const sc = document.getElementById('sc-' + p);
    if (sc) sc.classList.toggle('active', p === name);
  }});
  _applyFilters();
}}

let activeCat = 'all';
function filterCat(cat) {{
  activeCat = cat;
  document.querySelectorAll('.cat-btn').forEach(b => {{
    const matches =
      (cat === 'all' && b.textContent.trim() === 'All Sources') ||
      (cat === 'AUCTION' && b.textContent.trim() === 'Auction') ||
      (cat === 'RETAIL'  && b.textContent.trim() === 'Retail')  ||
      (cat === 'DEALER'  && b.textContent.trim() === 'Dealer');
    b.classList.toggle('active', matches);
  }});
  _applyFilters();
}}

let activeTier = 'all';
function filterTier(tier) {{
  activeTier = tier;
  document.querySelectorAll('.tier-btn').forEach(b => {{
    const matches =
      (tier === 'all'   && b.textContent.trim() === 'All') ||
      (tier === 'TIER1' && b.textContent.trim() === 'GT / Collector') ||
      (tier === 'TIER2' && b.textContent.trim() === 'Standard');
    b.classList.toggle('active', matches);
  }});
  _applyFilters();
}}

function _rowVisible(r) {{
  const catOk  = activeCat  === 'all' || r.dataset.category === activeCat;
  const tierOk = activeTier === 'all' || r.dataset.tier === activeTier;
  return catOk && tierOk;
}}

function _applyFilters() {{
  document.querySelectorAll('tbody tr[data-category]').forEach(r => {{
    r.style.display = _rowVisible(r) ? '' : 'none';
  }});
}}

// keep old name working (called from inline oninput)
function _applyCatFilter() {{ _applyFilters(); }}

function filterTable(tableId, inputId) {{
  const q = document.getElementById(inputId).value.toLowerCase();
  const rows = document.querySelectorAll('#' + tableId + ' tbody tr');
  rows.forEach(r => {{
    const baseOk = _rowVisible(r);
    const textOk = r.textContent.toLowerCase().includes(q);
    r.style.display = (baseOk && textOk) ? '' : 'none';
  }});
}}

function parseCell(td) {{
  const txt = td.textContent.trim().replace(/[$,]/g,'');
  const n = parseFloat(txt);
  return isNaN(n) ? txt.toLowerCase() : n;
}}

let sortState = {{}};
function sortTable(tableId, col) {{
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  const key = tableId + '_' + col;
  const asc = sortState[key] !== 'asc';
  sortState[key] = asc ? 'asc' : 'desc';
  tbl.querySelectorAll('thead th').forEach((th, i) => {{
    th.classList.remove('sort-asc','sort-desc');
    if (i === col) th.classList.add(asc ? 'sort-asc' : 'sort-desc');
  }});
  const tbody = tbl.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {{
    const av = parseCell(a.cells[col]);
    const bv = parseCell(b.cells[col]);
    if (av < bv) return asc ? -1 : 1;
    if (av > bv) return asc ? 1 : -1;
    return 0;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function toggleGen(slug) {{
  const body  = document.getElementById('gb-' + slug);
  const chev  = document.getElementById('chev-' + slug);
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  if (chev) chev.classList.toggle('open', open);
  if (open) _applyFilters();   // re-apply source/tier filters inside expanded gen
}}
</script>
</body>
</html>"""

    DASH_PATH.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {DASH_PATH}")
    return DASH_PATH


if __name__ == "__main__":
    generate()

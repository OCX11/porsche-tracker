"""
Market analysis report generator.

Produces static/market_report.html with:
- Inventory overview by model/generation
- Sold comp pricing by model/year band
- FMV estimates vs current asking prices
- Days-on-market statistics
- Overpriced / deal flags per listing
"""
import json
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

import db
import dealer_weights as dw

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
OUTPUT = STATIC_DIR / "market_report.html"


# ---------------------------------------------------------------------------
# Generation bucket helpers
# ---------------------------------------------------------------------------

def _generation(year, model):
    m = (model or "").lower()
    if "boxster" in m or "cayman" in m or "718" in m:
        if year and year >= 2017:
            return "718 Boxster/Cayman (982)"
        if year and year >= 2013:
            return "981 Boxster/Cayman"
        if year and year >= 2005:
            return "987 Boxster/Cayman"
        if year and year >= 1997:
            return "986 Boxster"
        return "Boxster/Cayman (other)"
    # 911
    if year:
        if year >= 2019:
            return "992 (2019+)"
        if year >= 2012:
            return "991 (2012–2019)"
        if year >= 2005:
            return "997 (2005–2012)"
        if year >= 1999:
            return "996 (1999–2004)"
        if year >= 1995:
            return "993 (1995–1998)"
        if year >= 1989:
            return "964 (1989–1994)"
        if year >= 1984:
            return "3.2 Carrera (1984–1989)"
        if year >= 1974:
            return "930 Turbo era"
        if year >= 1965:
            return "Early 911 (1965–1973)"
    return "Other"


def _trim_label(trim, model):
    """Normalize trim for grouping — extract GT/Turbo/Spyder variants."""
    if not trim:
        return ""
    t = trim.lower()
    for variant in ["gt3 rs", "gt3", "gt2 rs", "gt2", "turbo s", "turbo",
                     "gts", "4s", "targa 4s", "targa 4", "targa",
                     "spyder", "club sport", "r", "cabriolet", "coupe"]:
        if variant in t:
            return variant.upper().replace("RS", "RS").replace("GT", "GT")
    return trim[:30]


def _price_band(price):
    if price is None:
        return "No price"
    if price < 20000:
        return "< $20k"
    if price < 40000:
        return "$20–40k"
    if price < 60000:
        return "$40–60k"
    if price < 80000:
        return "$60–80k"
    if price < 100000:
        return "$80–100k"
    if price < 150000:
        return "$100–150k"
    if price < 200000:
        return "$150–200k"
    if price < 300000:
        return "$200–300k"
    return "$300k+"


# ---------------------------------------------------------------------------
# FMV estimation
# ---------------------------------------------------------------------------

def _compute_fmv(active, sold_comps, weights=None, tier=None):
    """
    Build FMV lookup: generation → weighted-mean sold price.

    Each sold comp is weighted by its source's credibility weight (from
    dealer_weights.json). A source at 0.3 contributes 30% as much as one
    at 1.0. Minimum 3 comps (by effective weight sum ≥ 1.5) required.

    If `tier` is 'TIER1' or 'TIER2', only sold comps matching that tier
    are included — FMVs are never blended across tiers.
    """
    w = weights or {}
    by_gen = defaultdict(list)  # gen -> [(price, weight)]
    for c in sold_comps:
        price = c.get("sold_price")
        if not price or not c.get("year"):
            continue
        if tier and c.get("tier") and c["tier"] != tier:
            continue
        gen = _generation(c["year"], c.get("model"))
        src_w = dw.get_weight(c.get("source", ""), w)
        by_gen[gen].append((price, src_w))

    fmv = {}
    for gen, pairs in by_gen.items():
        if len(pairs) < 3:
            continue
        prices = [p for p, _ in pairs]
        ws     = [wt for _, wt in pairs]
        total_w = sum(ws)
        if total_w < 1.5:          # require meaningful effective weight
            continue
        wmean = int(sum(p * wt for p, wt in pairs) / total_w)
        fmv[gen] = {
            "median":         int(statistics.median(prices)),
            "weighted_mean":  wmean,
            "mean":           int(statistics.mean(prices)),
            "low":            min(prices),
            "high":           max(prices),
            "count":          len(prices),
            "eff_weight":     round(total_w, 1),
        }
    return fmv


def _get_car_fmv(car, fmv_t1, fmv_t2):
    """Return the tier-appropriate FMV dict entry for a car."""
    tier = car.get("tier") or "TIER2"
    fmv = fmv_t1 if tier == "TIER1" else fmv_t2
    gen = _generation(car.get("year"), car.get("model"))
    return fmv.get(gen)


def _deal_flag(car, fmv_t1, fmv_t2):
    """Return 'DEAL', 'OVERPRICED', or '' using tier-appropriate FMV."""
    price = car.get("price")
    if not price:
        return ""
    stats = _get_car_fmv(car, fmv_t1, fmv_t2)
    if not stats or stats["count"] < 3:
        return ""
    benchmark = stats.get("weighted_mean") or stats["median"]
    ratio = price / benchmark
    if ratio <= 0.85:
        return "DEAL"
    if ratio >= 1.25:
        return "OVERPRICED"
    return ""


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _fmt_price(p):
    if p is None:
        return "—"
    try:
        return f"${float(p):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_miles(m):
    if m is None:
        return "—"
    try:
        return f"{int(m):,}"
    except (TypeError, ValueError):
        return "—"


def _pct(num, denom):
    if not denom:
        return "0%"
    return f"{100 * num / denom:.0f}%"


def _esc(s):
    if not s:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f0f0f; color: #e0e0e0; font-size: 14px; line-height: 1.5; }
h1 { font-size: 22px; color: #fff; padding: 24px 24px 8px; }
h2 { font-size: 16px; color: #ccc; padding: 20px 24px 10px; border-top: 1px solid #2a2a2a; }
h3 { font-size: 13px; color: #aaa; padding: 12px 24px 6px; text-transform: uppercase;
     letter-spacing: 0.05em; }
.meta { font-size: 12px; color: #666; padding: 0 24px 16px; }
.section { padding: 0 24px 24px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 6px 8px; background: #1a1a1a; color: #888;
     font-weight: 500; white-space: nowrap; position: sticky; top: 0; }
td { padding: 5px 8px; border-bottom: 1px solid #1e1e1e; vertical-align: top; }
tr:hover td { background: #181818; }
a { color: #5b9bd5; text-decoration: none; }
a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 3px;
         font-size: 11px; font-weight: 600; }
.deal { background: #1a3a1a; color: #4caf50; }
.overpriced { background: #3a1a1a; color: #ef5350; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
             gap: 12px; padding: 0 24px 20px; }
.stat-box { background: #1a1a1a; border-radius: 6px; padding: 14px 16px; }
.stat-val { font-size: 24px; font-weight: 700; color: #fff; }
.stat-lbl { font-size: 11px; color: #666; margin-top: 2px; }
.fmv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 10px; padding: 0 24px 20px; }
.fmv-box { background: #1a1a1a; border-radius: 6px; padding: 12px 16px; }
.fmv-gen { font-size: 13px; color: #ccc; font-weight: 600; margin-bottom: 6px; }
.fmv-row { display: flex; justify-content: space-between; font-size: 12px;
           color: #888; margin-top: 2px; }
.fmv-val { color: #e0e0e0; }
.tbl-wrap { overflow-x: auto; }
.days-bar { display: inline-block; height: 8px; background: #2a5a8a;
            border-radius: 2px; vertical-align: middle; margin-right: 6px; }
.badge-weight { display: inline-block; font-size: 9px; padding: 1px 4px; border-radius: 3px;
                font-weight: 700; letter-spacing: .04em; margin-left: 4px;
                vertical-align: middle; cursor: help; position: relative; }
.badge-weight-high   { background: rgba(34,197,94,.18);  color: #4ade80; }
.badge-weight-medium { background: rgba(245,158,11,.18); color: #fbbf24; }
.badge-weight-low    { background: rgba(239,68,68,.18);  color: #f87171; }
.badge-weight::after { content: attr(data-wtip); position: absolute; bottom: calc(100% + 5px);
                       left: 50%; transform: translateX(-50%); background: #1a1a1a;
                       color: #e0e0e0; padding: 7px 10px; border-radius: 6px; font-size: 11px;
                       line-height: 1.5; width: 260px; white-space: normal; z-index: 9999;
                       pointer-events: none; opacity: 0; transition: opacity .15s;
                       border: 1px solid #333; font-weight: 400; letter-spacing: 0; }
.badge-weight:hover::after { opacity: 1; }
.cat-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; padding: 0 24px 20px; }
.cat-box { background: #1a1a1a; border-radius: 6px; padding: 14px 16px; }
.cat-row { display: flex; justify-content: space-between; font-size: 12px; color: #888; margin-top: 3px; }
.cat-val { color: #e0e0e0; }
.badge-DEALER  { background: rgba(59,130,246,.18); color: #60a5fa; }
.badge-AUCTION { background: rgba(168,85,247,.18); color: #c084fc; }
.badge-RETAIL  { background: rgba(34,197,94,.18);  color: #4ade80; }
.tier-header { font-size: 15px; font-weight: 700; padding: 14px 24px 6px;
               border-left: 4px solid #d97706; margin: 10px 0 0; }
.tier-header.tier2 { border-left-color: #4b5563; }
.badge-tier1 { display:inline-block; background: rgba(217,119,6,.25); color: #fbbf24;
               padding: 1px 6px; border-radius:3px; font-size:11px; font-weight:700;
               margin-right:4px; }
"""


def _stat_box(val, label):
    return f'<div class="stat-box"><div class="stat-val">{_esc(val)}</div><div class="stat-lbl">{_esc(label)}</div></div>'


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _section_overview(active, sold_comps, fmv_t1, fmv_t2):
    by_gen = defaultdict(list)
    for c in active:
        gen = _generation(c.get("year"), c.get("model"))
        by_gen[gen].append(c)

    priced = [c for c in active if c.get("price")]
    median_ask = int(statistics.median([c["price"] for c in priced])) if priced else 0
    avg_days = int(statistics.mean([c["days_on_site"] for c in active
                                    if c.get("days_on_site")])) if active else 0
    deals = sum(1 for c in active if _deal_flag(c, fmv_t1, fmv_t2) == "DEAL")
    overpriced = sum(1 for c in active if _deal_flag(c, fmv_t1, fmv_t2) == "OVERPRICED")
    tier1_count = sum(1 for c in active if c.get("tier") == "TIER1")

    html = '<div class="stat-grid">'
    html += _stat_box(len(active), "Active Listings")
    html += _stat_box(tier1_count, "GT / Collector (T1)")
    html += _stat_box(len(sold_comps), "Sold Comps")
    html += _stat_box(_fmt_price(median_ask), "Median Ask")
    html += _stat_box(f"{avg_days}d", "Avg Days on Market")
    html += _stat_box(deals, "Deals (≤85% FMV)")
    html += _stat_box(overpriced, "Overpriced (≥125% FMV)")
    html += "</div>"

    # By generation table
    html += '<h3>Inventory by Generation</h3><div class="section"><div class="tbl-wrap"><table>'
    html += "<tr><th>Generation</th><th>Count</th><th>Median Ask</th><th>Range</th><th>Avg Days</th></tr>"
    for gen, cars in sorted(by_gen.items(), key=lambda x: -len(x[1])):
        prices = [c["price"] for c in cars if c.get("price")]
        med = _fmt_price(int(statistics.median(prices))) if prices else "—"
        rng = f"{_fmt_price(min(prices))} – {_fmt_price(max(prices))}" if prices else "—"
        days_list = [c["days_on_site"] for c in cars if c.get("days_on_site")]
        avg_d = f"{int(statistics.mean(days_list))}d" if days_list else "—"
        html += f"<tr><td>{_esc(gen)}</td><td>{len(cars)}</td><td>{med}</td><td>{rng}</td><td>{avg_d}</td></tr>"
    html += "</table></div></div>"
    return html


def _fmv_boxes(fmv):
    if not fmv:
        return "<p class='meta' style='padding:8px 24px'>No comps yet for this tier.</p>"
    html = '<div class="fmv-grid">'
    for gen, stats in sorted(fmv.items()):
        wm = stats.get("weighted_mean")
        wm_str = _fmt_price(wm) if wm else "—"
        eff_w = stats.get("eff_weight", 0)
        html += f'''<div class="fmv-box">
<div class="fmv-gen">{_esc(gen)}</div>
<div class="fmv-row" style="font-size:13px;color:#e0e0e0;margin-bottom:4px">
  <span style="font-weight:600">Weighted FMV</span>
  <span class="fmv-val" style="font-weight:700;color:#fff">{wm_str}</span>
</div>
<div class="fmv-row"><span>Unweighted median</span><span class="fmv-val">{_fmt_price(stats["median"])}</span></div>
<div class="fmv-row"><span>Range</span><span class="fmv-val">{_fmt_price(stats["low"])} – {_fmt_price(stats["high"])}</span></div>
<div class="fmv-row"><span>Comps</span><span class="fmv-val">{stats["count"]}</span></div>
<div class="fmv-row"><span>Eff. weight sum</span><span class="fmv-val">{eff_w:.1f}</span></div>
</div>'''
    html += "</div>"
    return html


def _section_fmv(fmv_t1, fmv_t2):
    html  = '<div class="tier-header">GT / Collector (Tier 1) — FMV by Generation</div>'
    html += _fmv_boxes(fmv_t1)
    html += '<div class="tier-header tier2">Standard (Tier 2) — FMV by Generation</div>'
    html += _fmv_boxes(fmv_t2)
    return html


def _listings_table(cars, fmv_t1, fmv_t2, weights=None):
    """Render a sorted table of active listings with deal flags."""
    w = weights or {}
    rows = []
    for c in cars:
        flag = _deal_flag(c, fmv_t1, fmv_t2)
        badge = ""
        if flag == "DEAL":
            badge = '<span class="badge deal">DEAL</span> '
        elif flag == "OVERPRICED":
            badge = '<span class="badge overpriced">OVERPRICED</span> '
        url = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{_esc(url)}" target="_blank">{_esc(name)}</a>' if url else _esc(name)

        fmv_stats = _get_car_fmv(c, fmv_t1, fmv_t2)
        fmv_val = (fmv_stats or {}).get("weighted_mean") or (fmv_stats or {}).get("median")
        fmv_str = _fmt_price(fmv_val) if fmv_val else "—"

        dealer = c.get("dealer", "")
        dealer_cell = _esc(dealer) + dw.tier_badge_html(dealer, w)

        rows.append((flag, c.get("price") or 0, [
            badge + link,
            dealer_cell,
            _esc(_generation(c.get("year"), c.get("model"))),
            _fmt_price(c.get("price")),
            fmv_str,
            _fmt_miles(c.get("mileage")),
            str(c.get("days_on_site") or "—"),
        ]))

    rows.sort(key=lambda r: (0 if r[0] == "DEAL" else 1 if r[0] == "" else 2, r[1]))

    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += ("<tr><th>Vehicle</th><th>Dealer/Source</th><th>Generation</th>"
             "<th>Ask</th><th>Wtd FMV</th><th>Miles</th><th>Days</th></tr>")
    for _, _, cols in rows:
        html += "<tr>" + "".join(f"<td>{c}</td>" for c in cols) + "</tr>"
    html += "</table></div></div>"
    return html


def _section_active_listings(active, fmv_t1, fmv_t2, weights=None):
    tier1 = [c for c in active if c.get("tier") == "TIER1"]
    tier2 = [c for c in active if c.get("tier") != "TIER1"]

    html  = f'<div class="tier-header"><span class="badge-tier1">GT</span> GT / Collector — {len(tier1)} listings</div>'
    html += _listings_table(tier1, fmv_t1, fmv_t2, weights) if tier1 else "<p class='meta' style='padding:8px 24px'>No GT/Collector listings currently active.</p>"
    html += f'<div class="tier-header tier2">Standard — {len(tier2)} listings</div>'
    html += _listings_table(tier2, fmv_t1, fmv_t2, weights) if tier2 else "<p class='meta' style='padding:8px 24px'>No standard listings currently active.</p>"
    return html


def _section_sold_comps(sold_comps, weights=None):
    if not sold_comps:
        return "<p class='meta' style='padding:16px 24px'>No sold comps yet. Run: python main.py --comps</p>"

    w = weights or {}
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += "<tr><th>Vehicle</th><th>Source</th><th>Generation</th><th>Sold Price</th><th>Miles</th><th>Sold Date</th></tr>"
    for c in sold_comps[:200]:  # cap display
        url = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{_esc(url)}" target="_blank">{_esc(name)}</a>' if url else _esc(name)
        gen = _generation(c.get("year"), c.get("model"))
        source = c.get("source", "")
        source_cell = _esc(source) + dw.tier_badge_html(source, w)
        html += (f"<tr><td>{link}</td><td>{source_cell}</td>"
                 f"<td>{_esc(gen)}</td><td>{_fmt_price(c.get('sold_price'))}</td>"
                 f"<td>{_fmt_miles(c.get('mileage'))}</td><td>{_esc(c.get('sold_date',''))}</td></tr>")
    html += "</table></div></div>"
    if len(sold_comps) > 200:
        html += f"<p class='meta' style='padding:0 24px 16px'>(Showing 200 of {len(sold_comps)} comps)</p>"
    return html


def _section_category_breakdown(active, sold_comps):
    cats = ["DEALER", "AUCTION", "RETAIL"]
    active_by_cat = defaultdict(list)
    for c in active:
        active_by_cat[c.get("source_category") or "DEALER"].append(c)
    sold_by_cat = defaultdict(list)
    for c in sold_comps:
        sold_by_cat[c.get("source_category") or "DEALER"].append(c)

    html = '<div class="cat-grid">'
    for cat in cats:
        a = active_by_cat.get(cat, [])
        s = sold_by_cat.get(cat, [])
        prices = [c["price"] for c in a if c.get("price")]
        med_ask = _fmt_price(int(statistics.median(prices))) if prices else "—"
        days = [c["days_on_site"] for c in a if c.get("days_on_site")]
        avg_days = f"{int(statistics.mean(days))}d" if days else "—"
        sold_ps = [c["sold_price"] for c in s if c.get("sold_price")]
        med_sold = _fmt_price(int(statistics.median(sold_ps))) if sold_ps else "—"
        html += f'''<div class="cat-box">
<div style="margin-bottom:8px"><span class="badge badge-{cat}">{cat}</span></div>
<div class="cat-row"><span>Active listings</span><span class="cat-val">{len(a)}</span></div>
<div class="cat-row"><span>Median ask</span><span class="cat-val">{med_ask}</span></div>
<div class="cat-row"><span>Avg days listed</span><span class="cat-val">{avg_days}</span></div>
<div class="cat-row"><span>Sold comps</span><span class="cat-val">{len(s)}</span></div>
<div class="cat-row"><span>Median sold</span><span class="cat-val">{med_sold}</span></div>
</div>'''
    html += '</div>'
    return html


def _section_hagerty_valuations(hagerty_vals, active, fmv_t1):
    """
    Three-way comparison: Hagerty Good/Excellent vs BaT FMV median vs dealer asking prices.
    Groups by generation; shows Hagerty values alongside BaT comps and active inventory stats.
    """
    if not hagerty_vals:
        return "<p class='meta' style='padding:16px 24px'>No Hagerty valuations yet. Run: python main.py --hagerty</p>"

    # Build lookup: (year, model, trim) → {good, excellent, url}
    hag_by_gen = {}
    for v in hagerty_vals:
        gen = v.get("generation") or _generation(v.get("year"), v.get("model"))
        if gen not in hag_by_gen:
            hag_by_gen[gen] = []
        hag_by_gen[gen].append(v)

    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += ('<thead><tr>'
             '<th>Vehicle</th>'
             '<th>Hagerty #3 Good</th>'
             '<th>Hagerty #2 Excellent</th>'
             '<th>BaT Median (FMV)</th>'
             '<th>Active Asking (median)</th>'
             '<th>Dealer premium vs Good</th>'
             '</tr></thead><tbody>')

    for gen, vals in sorted(hag_by_gen.items()):
        bat_med = fmv_t1.get(gen, {}).get("median")
        # Active asking prices for this generation
        gen_active = [c for c in active if _generation(c.get("year"), c.get("model")) == gen]
        ask_prices = [c["price"] for c in gen_active if c.get("price")]
        ask_med = int(statistics.median(ask_prices)) if ask_prices else None

        for v in sorted(vals, key=lambda x: (x.get("year", 0), x.get("trim", ""))):
            veh = f"{v['year']} Porsche {v['model']} {v.get('trim') or ''}".strip()
            url = v.get("hagerty_url") or "#"
            link = f'<a href="{_esc(url)}" target="_blank">{_esc(veh)}</a>'

            good = v.get("condition_good_price")
            exc  = v.get("condition_excellent_price")

            # Dealer premium vs Hagerty Good
            if good and ask_med:
                prem = (ask_med - good) / good * 100
                prem_str = f'<span style="color:{"#ef5350" if prem > 20 else "#4caf50" if prem < 5 else "#e0e0e0"}">{prem:+.0f}%</span>'
            else:
                prem_str = "—"

            exc_str = _fmt_price(exc) if exc else '<span style="color:#555;font-size:10px">locked*</span>'

            html += (f'<tr><td>{link}</td>'
                     f'<td>{_fmt_price(good)}</td>'
                     f'<td>{exc_str}</td>'
                     f'<td>{_fmt_price(bat_med)}</td>'
                     f'<td>{_fmt_price(ask_med)}</td>'
                     f'<td>{prem_str}</td></tr>')

    html += '</tbody></table></div>'
    html += '<p class="meta" style="padding:8px 0 0">* Excellent price requires a free Hagerty account. Set HAGERTY_SESSION_TOKEN env var to unlock.</p>'
    html += '</div>'
    return html


def _section_days_stats(days_stats):
    if not days_stats:
        return ""
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += "<tr><th>Model</th><th>Sold Count</th><th>Avg Days</th><th>Min</th><th>Max</th></tr>"
    max_days = max((r.get("avg_days") or 0) for r in days_stats) or 1
    for r in days_stats:
        avg = r.get("avg_days") or 0
        bar_w = int(120 * avg / max_days)
        bar = f'<span class="days-bar" style="width:{bar_w}px"></span>'
        html += (f"<tr><td>{_esc(r.get('model',''))}</td>"
                 f"<td>{r.get('cnt','')}</td>"
                 f"<td>{bar}{avg:.0f}d</td>"
                 f"<td>{r.get('min_days','')}</td>"
                 f"<td>{r.get('max_days','')}</td></tr>")
    html += "</table></div></div>"
    return html


# ---------------------------------------------------------------------------
# Main generate()
# ---------------------------------------------------------------------------

def generate() -> Path:
    with db.get_conn() as conn:
        data = db.get_market_data(conn)

    active      = data["active"]
    sold_comps  = data["sold_comps"]
    days_stats  = data["days_stats"]
    hagerty_vals = data.get("hagerty", [])
    generated_at = data["generated_at"]

    weights = dw.load_weights()
    fmv_t1 = _compute_fmv(active, sold_comps, weights, tier="TIER1")
    fmv_t2 = _compute_fmv(active, sold_comps, weights, tier="TIER2")

    tier1_count = sum(1 for c in active if c.get("tier") == "TIER1")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Porsche Market Analysis</title>
<style>{CSS}</style>
</head>
<body>
<h1>Porsche Market Analysis Report</h1>
<p class="meta">Generated {_esc(generated_at)} &nbsp;·&nbsp; {len(active)} active listings ({tier1_count} GT/Collector) &nbsp;·&nbsp; {len(sold_comps)} sold comps</p>

<h2>Market Overview</h2>
{_section_overview(active, sold_comps, fmv_t1, fmv_t2)}

<h2>Inventory by Source Category</h2>
{_section_category_breakdown(active, sold_comps)}

<h2>Fair Market Value Estimates</h2>
<p class="meta" style="padding:0 24px 10px">FMV computed separately per tier — never blended. Requires ≥3 sold comps per generation per tier.</p>
{_section_fmv(fmv_t1, fmv_t2)}

<h2>Hagerty Reference Values</h2>
<p class="meta" style="padding:0 24px 10px">Good (#3) and Excellent (#2) condition prices from Hagerty Valuation Tool vs BaT comps and dealer asking prices</p>
{_section_hagerty_valuations(hagerty_vals, active, fmv_t1)}

<h2>Active Inventory ({len(active)} listings)</h2>
{_section_active_listings(active, fmv_t1, fmv_t2, weights)}

<h2>Sold Comps ({len(sold_comps)} records)</h2>
{_section_sold_comps(sold_comps, weights)}

<h2>Days on Market — Sold Cars</h2>
{_section_days_stats(days_stats)}

</body>
</html>"""

    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    path = generate()
    print(f"Market report: file://{path}")

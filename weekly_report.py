"""
Weekly market report (generated every Monday).

Shows:
- Price movement by model/generation vs prior week
- Fastest-selling cars this week (lowest days_on_site at time of sale)
- Sitting inventory — listed 30+ days with no sale
- Best deals that appeared this week (new listings priced ≤85% of FMV)
- New listings this week

Archives: static/weekly_YYYY-MM-DD.html  (keeps last 8 weeks)
Output:   static/weekly_report.html      (always the latest)
"""
import shutil
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict
import statistics

import db
from _report_base import (
    html_shell, esc, fmt_price, fmt_miles, pct_change,
    generation, group_by_generation, safe_median, safe_mean,
    section_category_breakdown,
)

STATIC  = Path(__file__).parent / "static"
OUTPUT  = STATIC / "weekly_report.html"
ARCHIVE_MAX = 8


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _load(conn, today):
    week_ago      = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    two_weeks_ago = (date.fromisoformat(today) - timedelta(days=14)).isoformat()

    # Active listings right now
    active = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status='active' ORDER BY year DESC, model, price"
    ).fetchall()]

    # Listings that sold this week
    sold_week = [dict(r) for r in conn.execute(
        """SELECT * FROM listings
           WHERE status='sold' AND date_last_seen>=? AND date_last_seen<=?
           ORDER BY days_on_site""",
        (week_ago, today)
    ).fetchall()]

    # New listings this week
    new_week = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE date_first_seen>=? AND date_first_seen<=? ORDER BY dealer",
        (week_ago, today)
    ).fetchall()]

    # Price history this week (for movement calculation)
    ph_this = [dict(r) for r in conn.execute(
        """SELECT ph.price, ph.recorded_at, l.year, l.model, l.make, l.tier
           FROM price_history ph JOIN listings l ON l.id=ph.listing_id
           WHERE ph.recorded_at>=?""",
        (week_ago,)
    ).fetchall()]

    # Price history prior week
    ph_prior = [dict(r) for r in conn.execute(
        """SELECT ph.price, ph.recorded_at, l.year, l.model, l.make, l.tier
           FROM price_history ph JOIN listings l ON l.id=ph.listing_id
           WHERE ph.recorded_at>=? AND ph.recorded_at<?""",
        (two_weeks_ago, week_ago)
    ).fetchall()]

    # Sold comps this week (for FMV)
    comps_week = [dict(r) for r in conn.execute(
        "SELECT * FROM sold_comps WHERE scraped_at>=? ORDER BY sold_price DESC",
        (week_ago,)
    ).fetchall()]

    # All sold comps (for FMV reference)
    all_comps = [dict(r) for r in conn.execute(
        "SELECT * FROM sold_comps WHERE sold_price IS NOT NULL ORDER BY sold_price DESC"
    ).fetchall()]

    return active, sold_week, new_week, ph_this, ph_prior, comps_week, all_comps


# ---------------------------------------------------------------------------
# Price movement
# ---------------------------------------------------------------------------

def _section_tier_segment(label, color, cars_new, cars_sold, cars_sitting, fmv):
    """Compact summary section for one tier segment."""
    t1_new = len(cars_new)
    t1_sold = len(cars_sold)
    t1_sit  = len(cars_sitting)

    prices = [c["price"] for c in cars_new if c.get("price")]
    med_ask = fmt_price(safe_median(prices)) if prices else "—"

    sold_ps = [c["price"] for c in cars_sold if c.get("price")]
    med_sold = fmt_price(safe_median(sold_ps)) if sold_ps else "—"

    html  = f'<div style="border-left:4px solid {color};padding:10px 14px;margin:12px 0;background:#111">'
    html += f'<strong style="color:{color}">{label}</strong><br>'
    html += f'<span style="font-size:12px;color:#9ca3af">'
    html += f'New: <strong style="color:#e0e0e0">{t1_new}</strong> &nbsp;·&nbsp; '
    html += f'Sold: <strong style="color:#e0e0e0">{t1_sold}</strong> &nbsp;·&nbsp; '
    html += f'Sitting 30+: <strong style="color:#e0e0e0">{t1_sit}</strong> &nbsp;·&nbsp; '
    html += f'Median ask: <strong style="color:#e0e0e0">{med_ask}</strong> &nbsp;·&nbsp; '
    html += f'Median sold: <strong style="color:#e0e0e0">{med_sold}</strong>'
    html += '</span></div>'
    return html


def _price_movement(ph_this, ph_prior, tier=None):
    """Return list of (generation, this_median, prior_median, pct_html) sorted by gen."""
    def median_by_gen(ph_list):
        by_gen = defaultdict(list)
        for r in ph_list:
            if r.get("price"):
                if tier and r.get("tier") and r["tier"] != tier:
                    continue
                g = generation(r.get("year"), r.get("model"))
                by_gen[g].append(r["price"])
        return {g: safe_median(ps) for g, ps in by_gen.items()}

    this_meds  = median_by_gen(ph_this)
    prior_meds = median_by_gen(ph_prior)

    all_gens = sorted(set(this_meds) | set(prior_meds))
    rows = []
    for g in all_gens:
        t = this_meds.get(g)
        p = prior_meds.get(g)
        rows.append((g, t, p, pct_change(p, t)))
    return rows


# ---------------------------------------------------------------------------
# FMV from sold comps
# ---------------------------------------------------------------------------

def _fmv_map(comps, tier=None):
    """Build gen→median sold price map, optionally filtered by tier."""
    by_gen = defaultdict(list)
    for c in comps:
        if c.get("sold_price"):
            if tier and c.get("tier") and c["tier"] != tier:
                continue
            g = generation(c.get("year"), c.get("model"))
            by_gen[g].append(c["sold_price"])
    return {g: safe_median(ps) for g, ps in by_gen.items() if len(ps) >= 2}


# ---------------------------------------------------------------------------
# HTML sections
# ---------------------------------------------------------------------------

def _section_movement(rows):
    if not rows:
        return '<p class="empty">Price movement requires at least two weeks of data.</p>'
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += ('<thead><tr><th>Generation</th><th>This Week (median)</th>'
             '<th>Prior Week (median)</th><th>Change</th></tr></thead><tbody>')
    for g, this_m, prior_m, chg_html in rows:
        html += (f"<tr><td>{esc(g)}</td>"
                 f"<td>{fmt_price(this_m)}</td>"
                 f"<td>{fmt_price(prior_m)}</td>"
                 f"<td>{chg_html}</td></tr>")
    html += "</tbody></table></div></div>"
    return html


def _section_fastest(sold_week):
    if not sold_week:
        return '<p class="empty">No sales recorded this week yet.</p>'
    fast = sorted(
        [c for c in sold_week if c.get("days_on_site") is not None],
        key=lambda c: c["days_on_site"]
    )[:15]
    if not fast:
        return '<p class="empty">No sales with timing data this week.</p>'
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += '<thead><tr><th>Vehicle</th><th>Dealer</th><th>Price</th><th>Days on Market</th></tr></thead><tbody>'
    for c in fast:
        url  = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        days = c.get("days_on_site", 0)
        days_cls = ' style="color:#34d399;font-weight:700"' if days <= 3 else ""
        html += (f"<tr><td>{link}</td><td>{esc(c.get('dealer',''))}</td>"
                 f"<td>{fmt_price(c.get('price'))}</td>"
                 f"<td{days_cls}>{days}d</td></tr>")
    html += "</tbody></table></div></div>"
    return html


def _section_sitting(active):
    sitting = sorted(
        [c for c in active if (c.get("days_on_site") or 0) >= 30],
        key=lambda c: -(c.get("days_on_site") or 0)
    )
    if not sitting:
        return '<p class="empty">No active listings have been sitting 30+ days.</p>'
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += '<thead><tr><th>Vehicle</th><th>Dealer</th><th>Price</th><th>Days Sitting</th><th>First Seen</th></tr></thead><tbody>'
    for c in sitting:
        url  = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        days = c.get("days_on_site", 0)
        days_cls = ' style="color:#ef5350;font-weight:700"' if days >= 60 else ' style="color:#f59e0b"'
        html += (f"<tr><td>{link}</td><td>{esc(c.get('dealer',''))}</td>"
                 f"<td>{fmt_price(c.get('price'))}</td>"
                 f"<td{days_cls}>{days}d</td>"
                 f"<td>{esc(c.get('date_first_seen',''))}</td></tr>")
    html += "</tbody></table></div></div>"
    return html


def _section_deals(new_week, fmv_t1, fmv_t2):
    deals = []
    for c in new_week:
        p = c.get("price")
        if not p:
            continue
        g    = generation(c.get("year"), c.get("model"))
        tier = c.get("tier") or "TIER2"
        med  = (fmv_t1 if tier == "TIER1" else fmv_t2).get(g)
        if med and p <= med * 0.85:
            pct = (med - p) / med * 100
            deals.append((c, pct, med, tier))
    deals.sort(key=lambda x: -x[1])
    if not deals:
        return '<p class="empty">No standout deals identified this week (needs sold comp data for FMV).</p>'
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += '<thead><tr><th>Vehicle</th><th>Dealer</th><th>Ask</th><th>FMV</th><th>Discount</th></tr></thead><tbody>'
    for c, pct, med, tier in deals:
        url  = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        gt_badge = '<span style="background:rgba(217,119,6,.25);color:#fbbf24;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:700;margin-left:4px">GT</span>' if tier == "TIER1" else ""
        html += (f'<tr><td>{link} <span class="badge deal">DEAL</span>{gt_badge}</td>'
                 f'<td>{esc(c.get("dealer",""))}</td>'
                 f'<td>{fmt_price(c.get("price"))}</td>'
                 f'<td>{fmt_price(med)}</td>'
                 f'<td style="color:#4caf50;font-weight:700">-{pct:.0f}%</td></tr>')
    html += "</tbody></table></div></div>"
    return html


def _new_listings_rows(cars):
    rows = ""
    for c in sorted(cars, key=lambda x: x.get("date_first_seen", ""), reverse=True):
        url  = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        rows += (f"<tr><td>{link} <span class=\"badge new-badge\">NEW</span></td>"
                 f"<td>{esc(c.get('dealer',''))}</td>"
                 f"<td>{fmt_price(c.get('price'))}</td>"
                 f"<td>{fmt_miles(c.get('mileage'))}</td>"
                 f"<td>{esc(c.get('date_first_seen',''))}</td></tr>")
    return rows


_NEW_HDR = '<thead><tr><th>Vehicle</th><th>Dealer</th><th>Price</th><th>Miles</th><th>First Seen</th></tr></thead>'
_GT_TIER_HDR = '<div style="border-left:3px solid #d97706;padding-left:10px;margin:10px 0 4px"><strong style="color:#fbbf24">GT / Collector</strong></div>'
_STD_TIER_HDR = '<div style="border-left:3px solid #4b5563;padding-left:10px;margin:10px 0 4px"><strong style="color:#9ca3af">Standard</strong></div>'


def _section_new_listings(new_week):
    if not new_week:
        return '<p class="empty">No new listings this week.</p>'
    tier1 = [c for c in new_week if c.get("tier") == "TIER1"]
    tier2 = [c for c in new_week if c.get("tier") != "TIER1"]

    def tbl(cars):
        r = _new_listings_rows(cars)
        if not r:
            return '<p class="empty" style="margin:0 0 8px">None this week.</p>'
        return f'<div class="tbl-wrap"><table>{_NEW_HDR}<tbody>{r}</tbody></table></div>'

    html  = f'<div class="section">{_GT_TIER_HDR}{tbl(tier1)}{_STD_TIER_HDR}{tbl(tier2)}</div>'
    return html


def _archive_links():
    files = sorted(STATIC.glob("weekly_2*.html"), reverse=True)[:ARCHIVE_MAX]
    if not files:
        return ""
    links = "".join(f'<a href="{f.name}">{f.stem.replace("weekly_","")}</a>' for f in files)
    return f'<h3>Previous Weeks</h3><div class="archive-list">{links}</div>'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(today=None) -> Path:
    today = today or date.today().isoformat()

    with db.get_conn() as conn:
        active, sold_week, new_week, ph_this, ph_prior, comps_week, all_comps = _load(conn, today)

    fmv_t1     = _fmv_map(all_comps, tier="TIER1")
    fmv_t2     = _fmv_map(all_comps, tier="TIER2")
    movement_t1 = _price_movement(ph_this, ph_prior, tier="TIER1")
    movement_t2 = _price_movement(ph_this, ph_prior, tier="TIER2")
    week_start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()

    t1_new  = [c for c in new_week  if c.get("tier") == "TIER1"]
    t1_sold = [c for c in sold_week if c.get("tier") == "TIER1"]
    t1_sit  = [c for c in active    if c.get("tier") == "TIER1" and (c.get("days_on_site") or 0) >= 30]

    body = (
        f'<h1>Weekly Market Report</h1>'
        f'<p class="meta">{week_start} → {today} &nbsp;·&nbsp; '
        f'{len(active)} active &nbsp;·&nbsp; '
        f'{len(sold_week)} sold this week &nbsp;·&nbsp; '
        f'{len(new_week)} new listings</p>'

        f'<div class="stat-row">'
        f'<div class="stat"><div class="v">{len(active)}</div><div class="l">Active Listings</div></div>'
        f'<div class="stat"><div class="v" style="color:#34d399">{len(new_week)}</div><div class="l">New This Week</div></div>'
        f'<div class="stat"><div class="v" style="color:#f97316">{len(sold_week)}</div><div class="l">Sold This Week</div></div>'
        f'<div class="stat"><div class="v" style="color:#ef5350">'
        f'{len([c for c in active if (c.get("days_on_site") or 0) >= 30])}</div>'
        f'<div class="l">Sitting 30+ Days</div></div>'
        f'</div>'

        + f'<h2>Segment Summary</h2>'
        + '<div class="section">'
        + _section_tier_segment("GT / Collector (Tier 1)", "#d97706", t1_new, t1_sold, t1_sit, fmv_t1)
        + _section_tier_segment("Standard (Tier 2)", "#6b7280",
                                [c for c in new_week  if c.get("tier") != "TIER1"],
                                [c for c in sold_week if c.get("tier") != "TIER1"],
                                [c for c in active    if c.get("tier") != "TIER1" and (c.get("days_on_site") or 0) >= 30],
                                fmv_t2)
        + '</div>'

        + f'<h2>Inventory by Source Category</h2>'
        + section_category_breakdown(active, sold_week)

        + f'<h2>Price Movement — GT / Collector</h2>'
        + _section_movement(movement_t1)

        + f'<h2>Price Movement — Standard</h2>'
        + _section_movement(movement_t2)

        + f'<h2>Fastest-Selling Cars This Week</h2>'
        + _section_fastest(sold_week)

        + f'<h2>Sitting Inventory (30+ Days)</h2>'
        + _section_sitting(active)

        + f'<h2>Best Deals This Week</h2>'
        + _section_deals(new_week, fmv_t1, fmv_t2)

        + f'<h2>New Listings This Week ({len(new_week)})</h2>'
        + _section_new_listings(new_week)

        + _archive_links()
    )

    html = html_shell(f"Weekly Report — {today}", body, active_nav="weekly_report")
    OUTPUT.write_text(html, encoding="utf-8")

    # Archive copy named by the Monday of this week
    d = date.fromisoformat(today)
    monday = d - timedelta(days=d.weekday())  # 0=Mon
    archive = STATIC / f"weekly_{monday.isoformat()}.html"
    shutil.copy2(OUTPUT, archive)

    # Prune old archives
    old = sorted(STATIC.glob("weekly_2*.html"), reverse=True)[ARCHIVE_MAX:]
    for f in old:
        f.unlink(missing_ok=True)

    return OUTPUT


if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    path = generate()
    print(f"Weekly report: file://{path}")

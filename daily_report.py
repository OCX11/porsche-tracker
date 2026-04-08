"""
Daily auction results report.

Shows:
- What sold today on BaT / pcarmarket / PCA Mart (sold price, source, link)
- What didn't meet reserve / went unsold (sold_price IS NULL but scraped today)
- Notable results (record price, surprising no-sells)
- Dealer inventory sold today (from tracked listings)

Output: static/daily_report.html  (overwritten each run)
"""
from datetime import date
from pathlib import Path

import db
import report as rpt
import dealer_weights as dw
from _report_base import (
    html_shell, esc, fmt_price, fmt_miles, generation,
    group_by_generation, safe_median,
)

STATIC = Path(__file__).parent / "static"
OUTPUT = STATIC / "daily_report.html"


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _load(conn, today):
    # Sold comps scraped today
    comps_today = [dict(r) for r in conn.execute(
        "SELECT * FROM sold_comps WHERE scraped_at=? ORDER BY sold_price DESC NULLS LAST",
        (today,)
    ).fetchall()]

    # Split: sold (have price) vs no-sell / reserve not met (no price)
    sold     = [c for c in comps_today if c.get("sold_price")]
    no_sell  = [c for c in comps_today if not c.get("sold_price")]

    # Dealer inventory marked sold today
    dealer_sold = [dict(r) for r in conn.execute(
        """SELECT l.*, ph.price AS last_price
           FROM listings l
           LEFT JOIN price_history ph
             ON ph.listing_id=l.id
             AND ph.recorded_at=(SELECT MAX(recorded_at) FROM price_history WHERE listing_id=l.id AND recorded_at<?  )
           WHERE l.status='sold' AND l.date_last_seen=?
           ORDER BY l.dealer""",
        (today, today)
    ).fetchall()]

    # High bids this week for reference (recent sold_comps with prices)
    recent_high = [dict(r) for r in conn.execute(
        """SELECT * FROM sold_comps WHERE sold_price IS NOT NULL
           ORDER BY sold_price DESC LIMIT 5"""
    ).fetchall()]

    # New dealer inventory arrivals today
    new_today = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE date_first_seen=? ORDER BY tier, dealer",
        (today,)
    ).fetchall()]

    return sold, no_sell, dealer_sold, recent_high, new_today


# ---------------------------------------------------------------------------
# Notable flags
# ---------------------------------------------------------------------------

def _notable_flags(sold):
    """Return list of (comp, reason_html) for eye-catching results."""
    if not sold:
        return []
    flags = []
    prices = [c["sold_price"] for c in sold if c.get("sold_price")]
    if not prices:
        return []
    high = max(prices)
    low  = min(prices)
    for c in sold:
        p = c.get("sold_price")
        if not p:
            continue
        reasons = []
        if p == high and len(prices) > 1:
            reasons.append(f'<span class="badge record">Highest today: {fmt_price(p)}</span>')
        if p == low and len(prices) > 1 and p < 20000:
            reasons.append(f'<span class="badge nosell">Lowest: {fmt_price(p)}</span>')
        if p >= 200000:
            reasons.append(f'<span class="badge record">$200k+ result</span>')
        if reasons:
            flags.append((c, " ".join(reasons)))
    return flags


# ---------------------------------------------------------------------------
# HTML sections
# ---------------------------------------------------------------------------

_GT_BADGE = '<span style="display:inline-block;background:rgba(217,119,6,.25);color:#fbbf24;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:700;margin-right:4px">GT</span>'


def _section_sold(sold):
    if not sold:
        return '<p class="empty">No new sold results scraped today. Run: python main.py --comps</p>'
    rows = ""
    for c in sold:
        url = c.get("listing_url", "")
        name = " ".join(filter(None, [
            str(c.get("year") or ""),
            c.get("make") or "",
            c.get("model") or "",
            c.get("trim") or "",
        ])).strip() or c.get("title") or "Unknown"
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        gen  = generation(c.get("year"), c.get("model"))
        gt_badge = _GT_BADGE if c.get("tier") == "TIER1" else ""
        rows += (f"<tr>"
                 f"<td>{gt_badge}{link}</td>"
                 f"<td>{esc(gen)}</td>"
                 f"<td>{esc(c.get('source',''))}</td>"
                 f"<td><strong>{fmt_price(c.get('sold_price'))}</strong></td>"
                 f"<td>{fmt_miles(c.get('mileage'))}</td>"
                 f"<td>{esc(c.get('sold_date',''))}</td>"
                 f"</tr>")
    return (f'<div class="section"><div class="tbl-wrap"><table>'
            f'<thead><tr><th>Vehicle</th><th>Generation</th><th>Source</th>'
            f'<th>Sold Price</th><th>Miles</th><th>Date</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')


def _arrivals_rows(cars, fmv_t1, fmv_t2):
    if not cars:
        return ""
    rows = ""
    for c in cars:
        url = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        fmv_stats = rpt._get_car_fmv(c, fmv_t1, fmv_t2)
        fmv_val = (fmv_stats or {}).get("weighted_mean") or (fmv_stats or {}).get("median")
        fmv_str = fmt_price(fmv_val) if fmv_val else "—"
        flag_str = ""
        if fmv_val and c.get("price"):
            ratio = c["price"] / fmv_val
            if ratio <= 0.95:
                flag_str = f'<span class="badge deal">-{int((1-ratio)*100)}% vs FMV</span>'
        rows += (f"<tr>"
                 f"<td>{link}</td>"
                 f"<td>{esc(c.get('dealer',''))}</td>"
                 f"<td>{fmt_price(c.get('price'))}</td>"
                 f"<td>{fmv_str}</td>"
                 f"<td>{fmt_miles(c.get('mileage'))}</td>"
                 f"<td>{flag_str}</td>"
                 f"</tr>")
    return rows


def _section_arrivals(new_today, fmv_t1, fmv_t2):
    tier1 = [c for c in new_today if c.get("tier") == "TIER1"]
    tier2 = [c for c in new_today if c.get("tier") != "TIER1"]

    hdr = ('<tr><th>Vehicle</th><th>Dealer</th><th>Price</th>'
           '<th>FMV</th><th>Miles</th><th>Flag</th></tr>')

    def tbl(rows_html):
        if not rows_html:
            return '<p class="empty" style="margin:0 0 12px">None today.</p>'
        return (f'<div class="tbl-wrap"><table><thead>{hdr}</thead>'
                f'<tbody>{rows_html}</tbody></table></div>')

    html  = (f'<div style="border-left:3px solid #d97706;padding-left:12px;margin:12px 0 4px">'
             f'<strong style="color:#fbbf24">GT / Collector Arrivals ({len(tier1)})</strong></div>')
    html += tbl(_arrivals_rows(tier1, fmv_t1, fmv_t2))
    html += (f'<div style="border-left:3px solid #4b5563;padding-left:12px;margin:12px 0 4px">'
             f'<strong style="color:#9ca3af">Standard Arrivals ({len(tier2)})</strong></div>')
    html += tbl(_arrivals_rows(tier2, fmv_t1, fmv_t2))
    return f'<div class="section">{html}</div>'


def _section_no_sell(no_sell):
    if not no_sell:
        return '<p class="empty">No unsold / reserve-not-met results today.</p>'
    rows = ""
    for c in no_sell:
        url = c.get("listing_url", "")
        name = " ".join(filter(None, [
            str(c.get("year") or ""),
            c.get("make") or "",
            c.get("model") or "",
            c.get("trim") or "",
        ])).strip() or c.get("title") or "Unknown"
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        rows += (f"<tr>"
                 f"<td>{link}</td>"
                 f"<td>{esc(c.get('source',''))}</td>"
                 f'<td><span class="badge nosell">Reserve not met / No sale</span></td>'
                 f"<td>{fmt_miles(c.get('mileage'))}</td>"
                 f"</tr>")
    return (f'<div class="section"><div class="tbl-wrap"><table>'
            f'<thead><tr><th>Vehicle</th><th>Source</th><th>Status</th><th>Miles</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')


def _section_notable(flags):
    if not flags:
        return '<p class="empty">No notable results flagged today.</p>'
    rows = ""
    for c, badges in flags:
        url  = c.get("listing_url", "")
        name = (c.get("title") or
                " ".join(filter(None, [str(c.get("year") or ""),
                                       c.get("make") or "",
                                       c.get("model") or ""])))
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        rows += f"<tr><td>{link}</td><td>{fmt_price(c.get('sold_price'))}</td><td>{badges}</td></tr>"
    return (f'<div class="section"><div class="tbl-wrap"><table>'
            f'<thead><tr><th>Vehicle</th><th>Price</th><th>Note</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')


def _section_dealer_sold(dealer_sold):
    if not dealer_sold:
        return '<p class="empty">No tracked dealer listings sold today.</p>'
    rows = ""
    for c in dealer_sold:
        url = c.get("listing_url", "")
        name = f"{c.get('year','')} {c.get('make','')} {c.get('model','')} {c.get('trim') or ''}".strip()
        link = f'<a href="{esc(url)}" target="_blank">{esc(name)}</a>' if url else esc(name)
        rows += (f"<tr><td>{link}</td>"
                 f"<td>{esc(c.get('dealer',''))}</td>"
                 f"<td>{fmt_price(c.get('price'))}</td>"
                 f"<td>{c.get('days_on_site') or '—'}</td>"
                 f"</tr>")
    return (f'<div class="section"><div class="tbl-wrap"><table>'
            f'<thead><tr><th>Vehicle</th><th>Dealer</th><th>Last Price</th><th>Days Listed</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(today=None) -> Path:
    today = today or date.today().isoformat()
    with db.get_conn() as conn:
        sold, no_sell, dealer_sold, _, new_today = _load(conn, today)
        all_sold_comps = [dict(r) for r in conn.execute(
            "SELECT * FROM sold_comps ORDER BY sold_date DESC"
        ).fetchall()]

    weights = dw.load_weights()
    fmv_t1 = rpt._compute_fmv([], all_sold_comps, weights, tier="TIER1")
    fmv_t2 = rpt._compute_fmv([], all_sold_comps, weights, tier="TIER2")

    flags = _notable_flags(sold)
    total_sold   = len(sold)
    total_nosell = len(no_sell)
    total_dealer = len(dealer_sold)
    total_new    = len(new_today)
    new_t1       = sum(1 for c in new_today if c.get("tier") == "TIER1")

    body = (
        f'<h1>Daily Auction Report</h1>'
        f'<p class="meta">{today} &nbsp;·&nbsp; '
        f'{total_sold} sold &nbsp;·&nbsp; '
        f'{total_nosell} unsold/no-reserve &nbsp;·&nbsp; '
        f'{total_dealer} dealer sales &nbsp;·&nbsp; '
        f'{total_new} new inventory ({new_t1} GT)</p>'

        f'<div class="stat-row">'
        f'<div class="stat"><div class="v">{total_sold}</div><div class="l">Sold at Auction</div></div>'
        f'<div class="stat"><div class="v" style="color:#f97316">{total_nosell}</div>'
        f'<div class="l">Reserve Not Met</div></div>'
        f'<div class="stat"><div class="v" style="color:#60a5fa">{total_dealer}</div>'
        f'<div class="l">Dealer Inventory Sold</div></div>'
        f'<div class="stat"><div class="v" style="color:#d97706">{new_t1}</div>'
        f'<div class="l">GT Arrivals Today</div></div>'
        f'</div>'

        f'<h2>New Inventory Today ({total_new})</h2>'
        + _section_arrivals(new_today, fmv_t1, fmv_t2)

        + f'<h2>Sold at Auction ({total_sold})</h2>'
        + _section_sold(sold)

        + f'<h2>Reserve Not Met / Unsold ({total_nosell})</h2>'
        + _section_no_sell(no_sell)

        + f'<h2>Notable Results</h2>'
        + _section_notable(flags)

        + f'<h2>Dealer Inventory Sold Today ({total_dealer})</h2>'
        + _section_dealer_sold(dealer_sold)
    )

    html = html_shell(f"Daily Report — {today}", body, active_nav="daily_report")
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    path = generate()
    print(f"Daily report: file://{path}")

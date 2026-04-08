"""
live_feed.py — Real-time actionable listings dashboard.

Shows only feed_type='live' listings from actionable marketplace sources
(BaT, pcarmarket, Rennlist, PCA Mart, classic.com, Cars & Bids).

Designed for fast triage: within a few minutes of a new listing appearing
you can see it here with an inline FMV delta, source badge, time-since-listed,
and direct link.

Output: data/live_feed.html
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from db import get_conn, init_db
import fmv as fmv_engine

BASE_DIR  = Path(__file__).parent
OUT_PATH  = BASE_DIR / "docs" / "live_feed.html"

# ── Constants ─────────────────────────────────────────────────────────────────

# How many hours back to show in the feed (72h = 3 days rolling window)
FEED_WINDOW_HOURS = 72

# Source badge colors (background, text)
_SOURCE_COLORS = {
    "bring a trailer": ("#e8f4f8", "#1a5276"),
    "bat":             ("#e8f4f8", "#1a5276"),
    "bringatrailer":   ("#e8f4f8", "#1a5276"),
    "pcarmarket":      ("#eaf4ea", "#1e8449"),
    "cars & bids":     ("#fff3e0", "#e65100"),
    "carsandbids":     ("#fff3e0", "#e65100"),
    "classic.com":     ("#f3e5f5", "#6a1b9a"),
    "rennlist":        ("#fce4ec", "#880e4f"),
    "pca mart":        ("#e3f2fd", "#1565c0"),
}

_AUCTION_SOURCES = frozenset({
    "bring a trailer", "bat", "bringatrailer",
    "pcarmarket", "cars & bids", "carsandbids", "classic.com",
})

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_price(p) -> str:
    if p is None:
        return "—"
    try:
        return f"${float(p):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_miles(m) -> str:
    if m is None:
        return "—"
    try:
        return f"{int(m):,} mi"
    except (TypeError, ValueError):
        return "—"


def _time_since(dt_str: str) -> str:
    """Human-readable age from a datetime string."""
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        # Try date-only
        try:
            dt = datetime.combine(date.fromisoformat(dt_str[:10]), datetime.min.time())
        except ValueError:
            return "—"
    delta = datetime.now() - dt
    total_min = int(delta.total_seconds() / 60)
    if total_min < 2:
        return "just now"
    if total_min < 60:
        return f"{total_min}m ago"
    h = total_min // 60
    m = total_min % 60
    if h < 24:
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    d = h // 24
    return f"{d}d ago"


def _source_badge(dealer: str) -> str:
    """HTML span badge for the source."""
    d = (dealer or "").lower().strip()
    bg, fg = _SOURCE_COLORS.get(d, ("#f5f5f5", "#424242"))
    label = _html.escape(dealer or "Unknown")
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
        f'font-size:0.78em;font-weight:600;white-space:nowrap">{label}</span>'
    )


def _is_auction(dealer: str) -> bool:
    return (dealer or "").lower().strip() in _AUCTION_SOURCES


def _delta_chip(price, fmv_val, confidence) -> str:
    """Return an HTML chip showing % vs FMV, or empty string."""
    if not price or not fmv_val or confidence == "NONE":
        return ""
    try:
        pct = (float(price) - float(fmv_val)) / float(fmv_val) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return ""

    if abs(pct) < 2:
        color = "#757575"
        label = "≈ FMV"
    elif pct < -10:
        color = "#27ae60"
        label = f"↓{abs(pct):.0f}% vs FMV"
    elif pct < 0:
        color = "#2ecc71"
        label = f"↓{abs(pct):.0f}% vs FMV"
    elif pct > 15:
        color = "#e74c3c"
        label = f"↑{pct:.0f}% vs FMV"
    else:
        color = "#e67e22"
        label = f"↑{pct:.0f}% vs FMV"

    return (
        f'<span style="background:{color}20;color:{color};border:1px solid {color}40;'
        f'padding:2px 7px;border-radius:8px;font-size:0.78em;font-weight:600;'
        f'white-space:nowrap;margin-left:6px">{label}</span>'
    )


def _confidence_dot(conf: str) -> str:
    colors = {"HIGH": "#27ae60", "MEDIUM": "#f39c12", "LOW": "#e74c3c", "NONE": "#bdc3c7"}
    c = colors.get(conf, "#bdc3c7")
    titles = {"HIGH": "High confidence (5+ comps)", "MEDIUM": "Medium confidence (2–4 comps)",
              "LOW": "Low confidence (1 comp)", "NONE": "No comps available"}
    t = titles.get(conf, conf)
    return (
        f'<span title="{t}" style="display:inline-block;width:8px;height:8px;'
        f'border-radius:50%;background:{c};margin-right:4px"></span>'
    )


# ── Data ──────────────────────────────────────────────────────────────────────

def get_live_feed_data(conn) -> list[dict]:
    """Return live listings from the past FEED_WINDOW_HOURS hours."""
    cutoff = (datetime.now() - timedelta(hours=FEED_WINDOW_HOURS)).isoformat()

    rows = conn.execute("""
        SELECT l.*,
               (SELECT COUNT(*) FROM price_history ph WHERE ph.listing_id=l.id) AS price_changes,
               (SELECT MIN(ph2.price) FROM price_history ph2 WHERE ph2.listing_id=l.id AND ph2.price > 0) AS lowest_price
        FROM listings l
        WHERE l.feed_type = 'live'
          AND l.status = 'active'
          AND l.created_at >= ?
        ORDER BY l.created_at DESC
    """, (cutoff,)).fetchall()

    return [dict(r) for r in rows]


def get_recent_sold_live(conn) -> list[dict]:
    """Return recently sold/archived live-feed listings (last 7 days)."""
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    rows = conn.execute("""
        SELECT * FROM listings
        WHERE feed_type = 'live'
          AND status = 'sold'
          AND date_last_seen >= ?
        ORDER BY date_last_seen DESC
        LIMIT 20
    """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# ── HTML generation ────────────────────────────────────────────────────────────

def _card_html(car: dict, conn) -> str:
    """Build a single listing card."""
    dealer   = car.get("dealer", "")
    year     = car.get("year")
    model    = car.get("model", "")
    trim     = car.get("trim", "") or ""
    price    = car.get("price")
    mileage  = car.get("mileage")
    url      = car.get("listing_url", "") or ""
    img_url  = car.get("image_url", "") or ""
    created  = car.get("created_at", "") or car.get("date_first_seen", "")
    location = car.get("location", "") or ""
    trans    = car.get("transmission", "") or ""

    age_str  = _time_since(created)
    badge    = _source_badge(dealer)
    is_auc   = _is_auction(dealer)

    # FMV lookup
    fmv_result = fmv_engine.get_fmv(conn, year=year, model=model, trim=trim)
    fmv_val    = fmv_result.weighted_median if fmv_result else None
    confidence = fmv_result.confidence      if fmv_result else "NONE"
    comp_count = fmv_result.comp_count      if fmv_result else 0

    delta_chip = _delta_chip(price, fmv_val, confidence)
    conf_dot   = _confidence_dot(confidence)

    # Price display
    if is_auc:
        price_label = "Current Bid" if price else "Reserve"
        price_color = "#7d3c98"
    else:
        price_label = "Asking"
        price_color = "#2c3e50"

    # Title
    title_parts = [str(year) if year else "?", model, trim]
    title = " ".join(p for p in title_parts if p).strip()

    # Image block
    if img_url:
        img_block = (
            f'<a href="{_html.escape(url)}" target="_blank" rel="noopener">'
            f'<img src="{_html.escape(img_url)}" alt="{_html.escape(title)}" '
            f'style="width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;'
            f'display:block;background:#f0f0f0"></a>'
        )
    else:
        img_block = (
            f'<a href="{_html.escape(url)}" target="_blank" rel="noopener">'
            f'<div style="width:100%;height:100px;background:#ecf0f1;border-radius:6px 6px 0 0;'
            f'display:flex;align-items:center;justify-content:center;color:#95a5a6;font-size:0.9em">'
            f'No image</div></a>'
        )

    # Meta line
    meta_parts = []
    if mileage:
        meta_parts.append(_fmt_miles(mileage))
    if trans:
        meta_parts.append(_html.escape(trans))
    if location:
        meta_parts.append(f"📍 {_html.escape(location)}")
    meta_line = " &nbsp;·&nbsp; ".join(meta_parts) if meta_parts else ""

    # FMV line
    if fmv_val and confidence != "NONE":
        fmv_line = (
            f'<div style="margin-top:6px;font-size:0.8em;color:#7f8c8d">'
            f'{conf_dot}FMV: <strong>{_fmt_price(fmv_val)}</strong> '
            f'({comp_count} comp{"s" if comp_count != 1 else ""})'
            f'</div>'
        )
    else:
        fmv_line = (
            f'<div style="margin-top:6px;font-size:0.8em;color:#bdc3c7">'
            f'{conf_dot}No FMV data yet</div>'
        )

    return f"""
<div class="card" onclick="window.open('{_html.escape(url)}','_blank')" style="cursor:pointer">
  {img_block}
  <div class="card-body">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;margin-bottom:4px">
      <div class="card-title">
        <a href="{_html.escape(url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none"
           onclick="event.stopPropagation()">{_html.escape(title)}</a>
      </div>
      <div style="font-size:0.75em;color:#95a5a6;white-space:nowrap;padding-top:2px">{age_str}</div>
    </div>
    <div style="margin-bottom:6px">{badge}</div>
    <div style="display:flex;align-items:baseline;gap:4px;flex-wrap:wrap">
      <span style="font-size:0.75em;color:#95a5a6">{price_label}:</span>
      <span style="font-size:1.25em;font-weight:700;color:{price_color}">{_fmt_price(price)}</span>
      {delta_chip}
    </div>
    {fmv_line}
    {f'<div style="font-size:0.78em;color:#7f8c8d;margin-top:5px">{meta_line}</div>' if meta_line else ''}
  </div>
</div>"""


def generate() -> str:
    """Generate live_feed.html and return its path."""
    init_db()
    with get_conn() as conn:
        listings  = get_live_feed_data(conn)
        sold_live = get_recent_sold_live(conn)
        now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Partition by source
        source_groups: dict[str, list] = {}
        for car in listings:
            src = car.get("dealer", "Unknown")
            source_groups.setdefault(src, []).append(car)

        # Sorted by source priority for display
        _SOURCE_ORDER = [
            "Bring a Trailer", "pcarmarket", "Cars & Bids",
            "classic.com", "Rennlist", "PCA Mart",
        ]
        sorted_sources = sorted(
            source_groups.keys(),
            key=lambda s: _SOURCE_ORDER.index(s) if s in _SOURCE_ORDER else 99
        )

        # Build cards
        if listings:
            cards_html = '\n'.join(_card_html(c, conn) for c in listings)
        else:
            cards_html = (
                '<div style="grid-column:1/-1;text-align:center;padding:60px 20px;color:#95a5a6">'
                '<div style="font-size:3em;margin-bottom:12px">📭</div>'
                '<div style="font-size:1.1em">No new live listings in the past 72 hours.</div>'
                '<div style="margin-top:8px;font-size:0.9em">New listings from BaT, pcarmarket, '
                'Rennlist, PCA Mart, classic.com, and Cars & Bids will appear here automatically.</div>'
                '</div>'
            )

        # Source breakdown chips
        source_chips = ""
        for src in sorted_sources:
            cnt = len(source_groups[src])
            bg, fg = _SOURCE_COLORS.get(src.lower().strip(), ("#f5f5f5", "#424242"))
            source_chips += (
                f'<span style="background:{bg};color:{fg};padding:4px 12px;border-radius:12px;'
                f'font-size:0.85em;font-weight:600">'
                f'{_html.escape(src)} ({cnt})</span>\n'
            )

        # Recently sold section
        sold_rows = ""
        for car in sold_live:
            year  = car.get("year", "?")
            model = car.get("model", "")
            trim  = car.get("trim", "") or ""
            price = car.get("price")
            url   = car.get("listing_url", "") or ""
            sold  = car.get("date_last_seen", "") or ""
            badge = _source_badge(car.get("dealer", ""))
            sold_rows += f"""
<tr>
  <td>{year}</td>
  <td>{_html.escape(model)} {_html.escape(trim)}</td>
  <td>{badge}</td>
  <td style="font-weight:600">{_fmt_price(price)}</td>
  <td>{sold}</td>
  <td><a href="{_html.escape(url)}" target="_blank" rel="noopener"
         style="color:#3498db;text-decoration:none">→ Link</a></td>
</tr>"""

        sold_section = ""
        if sold_live:
            sold_section = f"""
<section style="margin-top:40px">
  <h2 style="font-size:1.1em;font-weight:700;color:#2c3e50;margin-bottom:12px">
    Recently Sold / Ended (Last 7 Days)
  </h2>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    <thead>
      <tr style="background:#f8f9fa;border-bottom:2px solid #dee2e6">
        <th style="padding:8px;text-align:left">Year</th>
        <th style="padding:8px;text-align:left">Model</th>
        <th style="padding:8px;text-align:left">Source</th>
        <th style="padding:8px;text-align:right">Price</th>
        <th style="padding:8px;text-align:left">Sold Date</th>
        <th style="padding:8px;text-align:left">Link</th>
      </tr>
    </thead>
    <tbody>{sold_rows}</tbody>
  </table>
  </div>
</section>"""

        total = len(listings)
        window_label = f"Last {FEED_WINDOW_HOURS}h"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Live Feed — Porsche Tracker</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f0f2f5;
    color: #2c3e50;
    min-height: 100vh;
  }}
  .top-bar {{
    background: #1a252f;
    color: #fff;
    padding: 0 24px;
    height: 52px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  }}
  .top-bar-left {{
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .logo {{
    font-size: 1.1em;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: #fff;
    text-decoration: none;
  }}
  .nav-link {{
    color: rgba(255,255,255,0.7);
    text-decoration: none;
    font-size: 0.85em;
    padding: 4px 10px;
    border-radius: 6px;
    transition: all 0.15s;
  }}
  .nav-link:hover, .nav-link.active {{
    background: rgba(255,255,255,0.15);
    color: #fff;
  }}
  .top-bar-right {{
    font-size: 0.75em;
    color: rgba(255,255,255,0.5);
  }}
  .live-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    background: #2ecc71;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50%       {{ opacity: 0.6; transform: scale(1.3); }}
  }}
  .page-header {{
    background: linear-gradient(135deg, #1a252f 0%, #2c3e50 100%);
    color: #fff;
    padding: 28px 24px 24px;
  }}
  .page-header h1 {{
    font-size: 1.6em;
    font-weight: 700;
    margin-bottom: 6px;
  }}
  .page-header .subtitle {{
    color: rgba(255,255,255,0.65);
    font-size: 0.9em;
  }}
  .stats-bar {{
    background: rgba(255,255,255,0.1);
    border-radius: 8px;
    padding: 10px 16px;
    margin-top: 16px;
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
  }}
  .stat {{
    display: flex;
    flex-direction: column;
    gap: 2px;
  }}
  .stat-value {{
    font-size: 1.4em;
    font-weight: 700;
    line-height: 1;
  }}
  .stat-label {{
    font-size: 0.72em;
    color: rgba(255,255,255,0.55);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .source-chips {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 14px;
  }}
  .main-content {{
    max-width: 1400px;
    margin: 24px auto;
    padding: 0 20px;
  }}
  .section-title {{
    font-size: 0.8em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #7f8c8d;
    margin-bottom: 14px;
  }}
  .cards-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
  }}
  .card {{
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    overflow: hidden;
    transition: transform 0.15s, box-shadow 0.15s;
    border: 1px solid #e8ecef;
  }}
  .card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  }}
  .card-body {{
    padding: 12px 14px 14px;
  }}
  .card-title {{
    font-size: 0.95em;
    font-weight: 600;
    color: #2c3e50;
    line-height: 1.3;
  }}
  .empty-state {{
    text-align: center;
    padding: 60px 20px;
    color: #95a5a6;
  }}
  @media (max-width: 600px) {{
    .cards-grid {{ grid-template-columns: 1fr; }}
    .page-header {{ padding: 20px 16px; }}
    .main-content {{ padding: 0 12px; }}
  }}
</style>
</head>
<body>

<nav class="top-bar">
  <div class="top-bar-left">
    <a href="dashboard.html" class="logo">🏎 Porsche Tracker</a>
    <a href="dashboard.html" class="nav-link">Dashboard</a>
    <a href="live_feed.html"  class="nav-link active"><span class="live-dot"></span>Live Feed</a>
  </div>
  <div class="top-bar-right">Auto-refreshes every 2 min &nbsp;·&nbsp; {now_str}</div>
</nav>

<div class="page-header">
  <h1><span class="live-dot"></span> Live Feed</h1>
  <div class="subtitle">
    New listings from actionable marketplace sources — updated every cron cycle.
    Only American market sources.
  </div>
  <div class="stats-bar">
    <div class="stat">
      <span class="stat-value">{total}</span>
      <span class="stat-label">Active ({window_label})</span>
    </div>
    <div class="stat">
      <span class="stat-value">{len(source_groups)}</span>
      <span class="stat-label">Sources</span>
    </div>
    <div class="stat">
      <span class="stat-value">{len(sold_live)}</span>
      <span class="stat-label">Sold (7d)</span>
    </div>
  </div>
  <div class="source-chips">
    {source_chips if source_chips else '<span style="color:rgba(255,255,255,0.4);font-size:0.85em">No active listings</span>'}
  </div>
</div>

<div class="main-content">
  <div class="section-title">
    Active Listings &nbsp;·&nbsp; {window_label} window &nbsp;·&nbsp;
    Sorted newest first
  </div>
  <div class="cards-grid">
    {cards_html}
  </div>

  {sold_section}

  <div style="margin-top:48px;padding:20px;background:#fff;border-radius:8px;
              border:1px solid #e8ecef;font-size:0.82em;color:#7f8c8d">
    <strong>Sources monitored:</strong>
    Bring a Trailer &nbsp;·&nbsp; pcarmarket &nbsp;·&nbsp; Cars &amp; Bids &nbsp;·&nbsp;
    classic.com &nbsp;·&nbsp; Rennlist &nbsp;·&nbsp; PCA Mart
    <br><br>
    <strong>How it works:</strong>
    New listings are ingested via dedicated scrapers (BaT, pcarmarket) and Distill webhooks
    (Rennlist, PCA Mart, classic.com). The FMV delta uses a weighted median of 24-month sold comps
    from auction sources only.
    <br><br>
    <em>This page auto-refreshes every 2 minutes.</em>
  </div>
</div>

</body>
</html>"""

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    return str(OUT_PATH)


if __name__ == "__main__":
    path = generate()
    print(f"Live feed: file://{path}")

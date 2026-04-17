"""
auction_dashboard.py — Dedicated auction watcher page.

Output: docs/auctions.html

Sections (by auction_ends_at):
  - Ending Soon   < 3 hr
  - Later Today   3–24 hr
  - Coming Up     1–7 days
  - No End Time   auction_ends_at IS NULL (buy-now / unlisted end)

Sort: auction_ends_at ASC within each section.
Cards: big countdown, current bid, FMV delta, source badge, thumbnail.
"""
from __future__ import annotations

import html as _html
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from db import get_conn, init_db
import fmv as fmv_engine

BASE_DIR = Path(__file__).parent
OUT_PATH = BASE_DIR / "docs" / "auctions.html"

# ── Formatting helpers (mirror new_dashboard.py) ──────────────────────────────

def _p(v) -> str:
    if v is None: return "—"
    try:    return f"${float(v):,.0f}"
    except: return "—"

def _m(v) -> str:
    if v is None: return "—"
    try:    return f"{int(v):,}"
    except: return "—"

def _h(s) -> str:
    return _html.escape(str(s or ""))

# Source badge config — same as new_dashboard.py
_BADGE_CFG = {
    "bring a trailer": ("#1e3a5f", "#60a5fa", "BaT"),
    "bat":             ("#1e3a5f", "#60a5fa", "BaT"),
    "pcarmarket":      ("#14532d", "#4ade80", "pcarmarket"),
    "cars & bids":     ("#431407", "#fb923c", "C&B"),
    "cars and bids":   ("#431407", "#fb923c", "C&B"),
    "carsandbids":     ("#431407", "#fb923c", "C&B"),
    "classic.com":     ("#3b0764", "#c084fc", "classic"),
}

def _badge(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    bg, fg, label = _BADGE_CFG.get(k, ("#1e2535", "#94a3b8", (dealer or "?")[:14]))
    return f'<span class="badge" style="background:{bg};color:{fg}">{_h(label)}</span>'

def _gen(year, model):
    if not year: return "Unknown"
    y = int(year); m = (model or "").lower()
    if "911" in m or m in ("911","930","964","993","996","997","991","992"):
        if y <= 1989: return "G-Series" if y >= 1984 else "G-Series"
        if y <= 1994: return "964"
        if y <= 1998: return "993"
        if y <= 2004: return "996"
        if y <= 2008: return "997.1"
        if y <= 2012: return "997.2"
        if y <= 2016: return "991.1"
        if y <= 2019: return "991.2"
        return "992"
    if "718" in m or "boxster" in m or "cayman" in m:
        if y <= 2004: return "986"
        if y <= 2012: return "987"
        if y <= 2016: return "981"
        return "718/982"
    return "Unknown"

def _fmv_delta_html(price, fmv_val, conf) -> str:
    """Compact % chip."""
    if not price or not fmv_val or conf == "NONE": return ""
    try:
        pct = (float(price) - float(fmv_val)) / float(fmv_val) * 100
    except: return ""
    if abs(pct) < 2:    cls, txt = "delta-flat",  "≈ FMV"
    elif pct < -10:     cls, txt = "delta-great", f"↓{abs(pct):.0f}%"
    elif pct < 0:       cls, txt = "delta-good",  f"↓{abs(pct):.0f}%"
    elif pct > 15:      cls, txt = "delta-high",  f"↑{pct:.0f}%"
    else:               cls, txt = "delta-mid",   f"↑{pct:.0f}%"
    return f'<span class="delta {cls}" title="{pct:+.1f}% vs FMV · Est. FMV {_p(fmv_val)}">{txt}</span>'

def _fmv_block_html(price, fmv_val, conf, comp_count) -> str:
    """Full FMV line for auction card."""
    if not fmv_val or conf == "NONE":
        return '<div class="fmv-line fmv-none">FMV: not enough comps yet</div>'
    try:
        pct = (float(price) - float(fmv_val)) / float(fmv_val) * 100 if price else None
    except:
        pct = None
    fmv_str = _p(fmv_val)
    comp_str = f"{comp_count} comp{'s' if comp_count != 1 else ''}"
    if pct is None:
        rel = ""; cls = "fmv-neutral"
    elif abs(pct) < 2:
        rel = "at market"; cls = "fmv-neutral"
    elif pct < -10:
        rel = f"<strong>{abs(pct):.0f}% below FMV</strong> 🔥"; cls = "fmv-great"
    elif pct < 0:
        rel = f"{abs(pct):.0f}% below FMV"; cls = "fmv-good"
    elif pct > 15:
        rel = f"<strong>{pct:.0f}% above FMV</strong>"; cls = "fmv-high"
    else:
        rel = f"{pct:.0f}% above FMV"; cls = "fmv-mid"
    conf_dot = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "⚪")
    return (f'<div class="fmv-line {cls}">'
            f'{conf_dot} Est. FMV <span class="fmv-val">{fmv_str}</span>'
            f'{(" · " + rel) if rel else ""}'
            f' <span class="fmv-comps">({comp_str})</span>'
            f'</div>')

# ── Auction card builder ──────────────────────────────────────────────────────

_PLACEHOLDER_SVG = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='200'%3E"
                    "%3Crect width='400' height='200' fill='%231e2530'/%3E"
                    "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
                    "font-family='sans-serif' font-size='13' fill='%234b5563'%3ENo photo%3C/text%3E%3C/svg%3E")

def _auction_card(car: dict, fmv_score: dict) -> str:
    dealer   = car.get("dealer", "")
    year     = car.get("year", "")
    model    = car.get("model", "") or ""
    trim     = car.get("trim", "") or ""
    price    = car.get("price")
    mileage  = car.get("mileage")
    url      = car.get("listing_url", "") or "#"
    img      = car.get("image_url", "") or ""
    ends_at  = car.get("auction_ends_at") or ""
    tier     = car.get("tier", "") or ""
    trans    = car.get("transmission", "") or ""

    # Rewrite PCA Mart local paths (unlikely in auctions but safe)
    if img and img.startswith("/static/img_cache/"):
        img = "img_cache/" + img.split("/")[-1]

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)

    delta_html = _fmv_delta_html(price, fmv_val, conf)
    fmv_html   = _fmv_block_html(price, fmv_val, conf, comp_count)

    # Tier badge
    tier_html = ""
    if tier == "TIER1":
        tier_html = '<span class="tier-badge">GT / Collector</span>'

    # Image
    is_pca = "mart.pca.org" in img
    if img and is_pca:
        img_id = f"pcaimg_{abs(hash(img)) % 999999}"
        img_html = (
            f'<div class="card-img-wrap">'
            f'<img id="{img_id}" src="{_PLACEHOLDER_SVG}" alt="{_h(str(year)+" "+model)}" class="card-img">'
            f'<script>(function(){{'
            f'var x=new XMLHttpRequest();x.open("GET","{_h(img)}",true);'
            f'x.setRequestHeader("Referer","https://mart.pca.org/");'
            f'x.responseType="blob";'
            f'x.onload=function(){{if(x.status==200){{var u=URL.createObjectURL(x.response);document.getElementById("{img_id}").src=u;}}}};'
            f'x.send();'
            f'}})();</script>'
            f'</div>'
        )
    elif img:
        img_html = (
            f'<div class="card-img-wrap">'
            f'<img src="{_h(img)}" alt="{_h(str(year)+" "+model)}" class="card-img" loading="lazy" '
            f'onerror="this.src=\'{_PLACEHOLDER_SVG}\';this.classList.add(\'img-fallback\')">'
            f'</div>'
        )
    else:
        img_html = (
            f'<div class="card-img-wrap">'
            f'<img src="{_PLACEHOLDER_SVG}" alt="No photo" class="card-img img-fallback">'
            f'</div>'
        )

    # Mileage / transmission chips
    chips = []
    if trans:   chips.append(_h(trans))
    if mileage: chips.append(f"{_m(mileage)} mi")
    chips_html = " · ".join(chips)

    # Countdown / no-end-time label
    if ends_at:
        countdown_html = (
            f'<div class="countdown-wrap">'
            f'<span class="countdown-label">Ends in</span>'
            f'<span class="countdown-timer" data-ends="{_h(ends_at)}">…</span>'
            f'</div>'
        )
    else:
        countdown_html = '<div class="countdown-wrap"><span class="countdown-label no-end">No end time listed</span></div>'

    return (
        f'<div class="auc-card" data-ends="{_h(ends_at)}" data-price="{price or 0}" '
        f'onclick="window.open(\'{_h(url)}\',\'_blank\')">\n'
        f'  {img_html}\n'
        f'  <div class="card-body">\n'
        f'    <div class="card-top-row">\n'
        f'      {_badge(dealer)}\n'
        f'      <span class="gen-label">{_h(_gen(year, model))}</span>\n'
        f'    </div>\n'
        f'    <div class="card-title">{year} Porsche {_h(model)}{(" " + _h(trim)) if trim else ""}</div>\n'
        f'    {tier_html}\n'
        f'    {countdown_html}\n'
        f'    <div class="bid-row">\n'
        f'      <span class="bid-label">Current Bid</span>\n'
        f'      <span class="bid-val">{_p(price)}</span>\n'
        f'      {delta_html}\n'
        f'    </div>\n'
        f'    {fmv_html}\n'
        f'    <div class="card-meta">{chips_html}</div>\n'
        f'  </div>\n'
        f'</div>'
    )

# ── Section builder ───────────────────────────────────────────────────────────

def _section(title: str, subtitle: str, cards_html: str, icon: str, count: int) -> str:
    if not cards_html:
        empty = (f'<div class="empty"><div class="empty-icon">🔍</div>'
                 f'<div class="empty-text">No auctions in this window</div></div>')
        cards_html = empty
    return (
        f'<div class="section">\n'
        f'  <div class="section-hdr">\n'
        f'    <div class="section-hdr-left">\n'
        f'      <span class="section-icon">{icon}</span>\n'
        f'      <div>\n'
        f'        <div class="section-title">{title} <span class="section-count">{count}</span></div>\n'
        f'        <div class="section-sub">{subtitle}</div>\n'
        f'      </div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'  <div class="cards-grid">\n'
        f'    {cards_html}\n'
        f'  </div>\n'
        f'</div>'
    )

# ── Main generate ─────────────────────────────────────────────────────────────

def generate() -> str:
    init_db()
    now_utc = datetime.now(timezone.utc)

    with get_conn() as conn:
        fmv_scored_list = fmv_engine.score_active_listings(conn)
        fmv_by_id = {}
        for row in fmv_scored_list:
            fmv_obj = row.get("fmv")
            if fmv_obj:
                fmv_by_id[row["id"]] = {
                    "fmv":        getattr(fmv_obj, "weighted_median", None),
                    "confidence": getattr(fmv_obj, "confidence", "NONE"),
                    "comp_count": getattr(fmv_obj, "comp_count", 0),
                }
            else:
                fmv_by_id[row["id"]] = {"fmv": None, "confidence": "NONE", "comp_count": 0}

        rows = conn.execute("""
            SELECT * FROM listings
            WHERE source_category='AUCTION' AND status='active'
        """).fetchall()
        cars = [dict(r) for r in rows]

    for c in cars:
        c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})

    # Parse ends_at into datetime for bucketing
    def _parse_ends(ends_str):
        if not ends_str:
            return None
        try:
            # Handle trailing Z
            s = ends_str.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except Exception:
            return None

    ending_soon  = []   # < 3 hr
    later_today  = []   # 3–24 hr
    coming_up    = []   # 1–7 days
    no_end_time  = []   # null / unparseable

    three_hr  = now_utc + timedelta(hours=3)
    one_day   = now_utc + timedelta(hours=24)
    seven_day = now_utc + timedelta(days=7)

    for c in cars:
        ends_dt = _parse_ends(c.get("auction_ends_at"))
        c["_ends_dt"] = ends_dt
        if ends_dt is None:
            no_end_time.append(c)
        elif ends_dt <= now_utc:
            # Already ended — skip (scraper hasn't cleaned up yet)
            no_end_time.append(c)
        elif ends_dt <= three_hr:
            ending_soon.append(c)
        elif ends_dt <= one_day:
            later_today.append(c)
        elif ends_dt <= seven_day:
            coming_up.append(c)
        else:
            coming_up.append(c)  # beyond 7 days — include anyway

    def _sort_key(c):
        d = c.get("_ends_dt")
        if d is None:
            return datetime(9999, 12, 31, tzinfo=timezone.utc)
        return d

    ending_soon.sort(key=_sort_key)
    later_today.sort(key=_sort_key)
    coming_up.sort(key=_sort_key)

    def _build_cards(lst):
        return "\n".join(_auction_card(c, c["_fmv"]) for c in lst)

    s_ending  = _section("Ending Soon",  "Under 3 hours",  _build_cards(ending_soon), "🔥", len(ending_soon))
    s_today   = _section("Later Today",  "3–24 hours",     _build_cards(later_today), "⏰", len(later_today))
    s_coming  = _section("Coming Up",    "1–7 days",       _build_cards(coming_up),   "📅", len(coming_up))
    s_noend   = _section("No End Time",  "Buy-now / end time not listed", _build_cards(no_end_time), "🏷", len(no_end_time))

    total = len(cars)
    now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    html = _build_html(s_ending, s_today, s_coming, s_noend, total, now_str)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[auction_dashboard] wrote {OUT_PATH} ({total} auctions)")
    return html




# ── HTML template ─────────────────────────────────────────────────────────────

def _build_html(s_ending, s_today, s_coming, s_noend, total, now_str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Auctions">
<meta name="theme-color" content="#0f1117">
<title>Auction Watcher · Porsche Tracker</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;background:#0f1117;color:#e2e8f0}}
a{{color:inherit;text-decoration:none}}
button{{cursor:pointer;border:none;background:none;font:inherit;color:inherit}}

/* ── Topbar ── */
.topbar{{height:52px;min-height:52px;background:#161b27;border-bottom:1px solid #2d3748;display:flex;align-items:center;justify-content:space-between;padding:0 20px;gap:16px;position:sticky;top:0;z-index:50}}
.topbar-left{{display:flex;align-items:center;gap:20px}}
.logo{{font-size:1.05em;font-weight:700;color:#f1f5f9;letter-spacing:-0.3px;white-space:nowrap}}
.logo span{{color:#ef4444}}
.nav-tabs{{display:flex;gap:2px}}
.nav-tab{{padding:6px 14px;border-radius:6px;font-size:0.88em;font-weight:500;color:#94a3b8;transition:all .15s;cursor:pointer;display:inline-flex;align-items:center;gap:5px}}
.nav-tab:hover{{background:#2d3748;color:#f1f5f9}}
.nav-tab.active{{background:#3b82f6;color:#fff}}
.topbar-right{{display:flex;align-items:center;gap:12px;font-size:0.78em;color:#475569;white-space:nowrap}}

/* ── Page body ── */
.page-body{{max-width:1400px;margin:0 auto;padding:20px 20px 40px}}

/* ── Hero stats bar ── */
.stats-bar{{
  display:flex;align-items:center;justify-content:space-between;
  background:#161b27;border:1px solid #2d3748;border-radius:12px;
  padding:16px 24px;margin-bottom:24px;flex-wrap:wrap;gap:12px;
}}
.stats-group{{display:flex;gap:24px;flex-wrap:wrap}}
.stat-item{{display:flex;flex-direction:column;gap:2px}}
.stat-val{{font-size:1.6em;font-weight:700;line-height:1;color:#f1f5f9}}
.stat-val.orange{{color:#fb923c}}
.stat-val.blue{{color:#60a5fa}}
.stat-val.green{{color:#4ade80}}
.stat-lbl{{font-size:0.7em;color:#475569;text-transform:uppercase;letter-spacing:.5px}}
.live-badge{{display:flex;align-items:center;gap:6px;font-size:0.78em;color:#4ade80;background:#14532d;padding:5px 10px;border-radius:8px;font-weight:600}}
.live-dot{{width:7px;height:7px;border-radius:50%;background:#4ade80;animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.5;transform:scale(1.3)}}}}

/* ── Section ── */
.section{{margin-bottom:32px}}
.section-hdr{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}}
.section-hdr-left{{display:flex;align-items:center;gap:12px}}
.section-icon{{font-size:1.4em}}
.section-title{{font-size:1.05em;font-weight:700;color:#f1f5f9}}
.section-count{{display:inline-block;background:#2d3748;color:#94a3b8;font-size:0.75em;font-weight:600;padding:1px 7px;border-radius:10px;margin-left:6px;vertical-align:middle}}
.section-sub{{font-size:0.78em;color:#475569;margin-top:2px}}
.section.ending-soon .section-title{{color:#fb923c}}
.section.ending-soon .section-count{{background:#431407;color:#fb923c}}

/* ── Cards grid ── */
.cards-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}

/* ── Auction card ── */
.auc-card{{
  background:#161b27;border:1px solid #2d3748;border-radius:12px;
  overflow:hidden;cursor:pointer;
  transition:box-shadow .15s,transform .15s,border-color .15s;
  display:flex;flex-direction:column;
}}
.auc-card:hover{{box-shadow:0 6px 28px rgba(0,0,0,.5);transform:translateY(-2px);border-color:#3b82f6}}
.ending-soon .auc-card{{border-color:#431407}}
.ending-soon .auc-card:hover{{border-color:#fb923c;box-shadow:0 6px 28px rgba(251,146,60,.15)}}

.card-img-wrap{{width:100%;height:185px;overflow:hidden;background:#1e2535;flex-shrink:0}}
.card-img{{width:100%;height:185px;object-fit:cover;display:block;transition:transform .2s}}
.auc-card:hover .card-img{{transform:scale(1.03)}}
.img-fallback{{opacity:0.5}}

.card-body{{padding:12px 14px 14px;display:flex;flex-direction:column;gap:0;flex:1}}
.card-top-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px}}
.gen-label{{font-size:0.72em;color:#475569;white-space:nowrap}}
.card-title{{font-size:0.94em;font-weight:600;color:#f1f5f9;margin-bottom:4px;line-height:1.3}}
.tier-badge{{display:inline-block;font-size:0.68em;font-weight:700;background:#451a03;color:#fbbf24;padding:2px 7px;border-radius:4px;margin-bottom:7px;text-transform:uppercase;letter-spacing:.5px;border:1px solid #78350f}}

/* ── Countdown ── */
.countdown-wrap{{
  background:#1a1f2e;border:1px solid #2d3748;border-radius:8px;
  padding:8px 12px;margin-bottom:9px;display:flex;align-items:baseline;gap:8px;
}}
.countdown-label{{font-size:0.72em;color:#64748b;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}}
.countdown-timer{{font-size:1.35em;font-weight:700;color:#fb923c;font-variant-numeric:tabular-nums;letter-spacing:1px}}
.countdown-timer.urgent{{color:#ef4444;animation:urgentPulse 1s infinite}}
@keyframes urgentPulse{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
.countdown-timer.done{{color:#475569}}
.no-end{{font-size:0.82em;color:#475569;font-style:italic}}

/* ── Bid row ── */
.bid-row{{display:flex;align-items:baseline;gap:8px;margin-bottom:7px;flex-wrap:wrap}}
.bid-label{{font-size:0.72em;color:#64748b}}
.bid-val{{font-size:1.3em;font-weight:700;color:#a78bfa}}

/* ── FMV line ── */
.fmv-line{{font-size:0.78em;padding:5px 8px;border-radius:5px;margin-bottom:6px;line-height:1.4}}
.fmv-val{{font-weight:700;font-size:1.05em}}
.fmv-comps{{opacity:0.65}}
.fmv-none   {{background:#1e2535;color:#475569}}
.fmv-neutral{{background:#1e2535;color:#94a3b8}}
.fmv-great  {{background:#14532d;color:#86efac}}
.fmv-good   {{background:#14532d;color:#4ade80}}
.fmv-mid    {{background:#431407;color:#fdba74}}
.fmv-high   {{background:#450a0a;color:#fca5a5}}

/* ── Delta badges ── */
.delta{{font-size:0.72em;font-weight:700;padding:2px 6px;border-radius:5px;white-space:nowrap}}
.delta-great{{background:#14532d;color:#4ade80}}
.delta-good {{background:#14532d;color:#86efac}}
.delta-flat {{background:#1e2535;color:#64748b}}
.delta-mid  {{background:#431407;color:#fdba74}}
.delta-high {{background:#450a0a;color:#fca5a5}}

/* ── Source badge ── */
.badge{{font-size:0.72em;font-weight:600;padding:2px 7px;border-radius:8px;display:inline-block;white-space:nowrap}}

/* ── Card meta ── */
.card-meta{{font-size:0.75em;color:#475569;margin-top:auto;padding-top:4px}}

/* ── Empty state ── */
.empty{{grid-column:1/-1;text-align:center;padding:40px 20px;color:#475569}}
.empty-icon{{font-size:2.5em;margin-bottom:10px}}
.empty-text{{font-size:0.95em;font-weight:500;color:#64748b}}

/* ── Scrollbar ── */
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:#0f1117}}
::-webkit-scrollbar-thumb{{background:#2d3748;border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:#3b4a5e}}

/* ── Responsive ── */
@media(max-width:640px){{
  .topbar-right{{display:none}}
  .stats-group{{gap:14px}}
  .page-body{{padding:12px 12px 32px}}
  .cards-grid{{grid-template-columns:1fr}}
}}
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-left">
    <div class="logo">🏎 Porsche <span>Tracker</span></div>
    <nav class="nav-tabs">
      <a class="nav-tab" href="index.html">New Listings</a>
      <span class="nav-tab active">🔨 Auctions</span>
      <a class="nav-tab" href="index.html#comps">Sold Comps</a>
      <a class="nav-tab" href="search.html">🔍 Search</a>
    </nav>
  </div>
  <div class="topbar-right">
    <span>Updated: {now_str}</span>
    <span>Auto-refresh 2 min</span>
  </div>
</header>

<div class="page-body">

  <div class="stats-bar">
    <div class="stats-group">
      <div class="stat-item">
        <span class="stat-val orange" id="stat-total">{total}</span>
        <span class="stat-lbl">Active Auctions</span>
      </div>
      <div class="stat-item">
        <span class="stat-val" id="stat-ending">…</span>
        <span class="stat-lbl">Ending &lt; 3 hr</span>
      </div>
      <div class="stat-item">
        <span class="stat-val blue" id="stat-today">…</span>
        <span class="stat-lbl">Later Today</span>
      </div>
      <div class="stat-item">
        <span class="stat-val green" id="stat-coming">…</span>
        <span class="stat-lbl">Coming Up</span>
      </div>
    </div>
    <div class="live-badge"><span class="live-dot"></span> Live Countdown</div>
  </div>

  <div id="section-ending" class="section ending-soon">
    {s_ending}
  </div>

  <div id="section-today" class="section">
    {s_today}
  </div>

  <div id="section-coming" class="section">
    {s_coming}
  </div>

  <div id="section-noend" class="section">
    {s_noend}
  </div>

</div><!-- /page-body -->

<script>
// ── Countdown engine ──────────────────────────────────────────────────────────
function fmtCountdown(secs) {{
  if (secs <= 0) return 'ENDED';
  var d = Math.floor(secs / 86400);
  var h = Math.floor((secs % 86400) / 3600);
  var m = Math.floor((secs % 3600) / 60);
  var s = Math.floor(secs % 60);
  if (d > 0) return d + 'd ' + pad(h) + ':' + pad(m) + ':' + pad(s);
  return pad(h) + ':' + pad(m) + ':' + pad(s);
}}
function pad(n) {{ return n < 10 ? '0' + n : '' + n; }}

function tickAll() {{
  var now = Date.now();
  var timers = document.querySelectorAll('.countdown-timer[data-ends]');
  var endingSoon = 0;
  timers.forEach(function(el) {{
    var endMs = new Date(el.dataset.ends).getTime();
    var secs = Math.floor((endMs - now) / 1000);
    if (secs <= 0) {{
      el.textContent = 'ENDED';
      el.classList.add('done');
      el.classList.remove('urgent');
    }} else {{
      el.textContent = fmtCountdown(secs);
      el.classList.remove('done');
      if (secs < 3600) {{
        el.classList.add('urgent');
        endingSoon++;
      }} else {{
        el.classList.remove('urgent');
      }}
    }}
  }});

  // Update stat counters from live DOM
  var todayTimers = document.querySelectorAll('#section-today .countdown-timer[data-ends]');
  var comingTimers = document.querySelectorAll('#section-coming .countdown-timer[data-ends]');
  var es = document.getElementById('stat-ending');
  var et = document.getElementById('stat-today');
  var ec = document.getElementById('stat-coming');
  if (es) es.textContent = endingSoon;
  if (et) et.textContent = todayTimers.length;
  if (ec) ec.textContent = comingTimers.length;
}}

tickAll();
setInterval(tickAll, 1000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    generate()

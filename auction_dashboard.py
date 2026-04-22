"""
auction_dashboard.py — PTOX11 auction watcher, redesigned.

Design system matches new_dashboard.py (PTOX11 redesign):
  --red #D6293E · --bg #0A0A0C · Syne + DM Mono fonts
  Horizontal card layout: image left, timer + bid right
  Urgency red bar on image bottom for ending-soon

Output: docs/auctions.html

Sections:
  Ending Soon  < 3 hr
  Later Today  3–24 hr
  Coming Up    1–7 days
  No End Time  auction_ends_at IS NULL
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from db import get_conn, init_db
import fmv as fmv_engine

BASE_DIR = Path(__file__).parent
OUT_PATH = BASE_DIR / "docs" / "auctions.html"

# ── Formatting helpers ────────────────────────────────────────────────────────

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

_BADGE_CFG = {
    "bring a trailer": ("#0D1F35", "#60a5fa", "BaT"),
    "bat":             ("#0D1F35", "#60a5fa", "BaT"),
    "pcarmarket":      ("#0A1F14", "#4ade80", "pcarmarket"),
    "cars & bids":     ("#1F0D03", "#fb923c", "C&B"),
    "cars and bids":   ("#1F0D03", "#fb923c", "C&B"),
    "carsandbids":     ("#1F0D03", "#fb923c", "C&B"),
    "classic.com":     ("#1A0B2E", "#c084fc", "classic"),
}

def _badge(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    bg, fg, label = _BADGE_CFG.get(k, ("#18181F", "#6B6B7D", (dealer or "?")[:14]))
    return f'<span class="badge" style="background:{bg};color:{fg}">{_h(label)}</span>'

def _gen(year, model):
    if not year: return "Unknown"
    y = int(year); m = (model or "").lower()
    if "911" in m or m in ("911","930","964","993","996","997","991","992"):
        if y <= 1989: return "G-Series"
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
        return "718"
    return "Unknown"

def _fmv_pct(price, fmv_val):
    if not price or not fmv_val:
        return None
    try:
        return (float(price) - float(fmv_val)) / float(fmv_val) * 100
    except Exception:
        return None

def _delta_badge(pct):
    if pct is None: return ""
    if abs(pct) < 2:    cls, txt = "delta-flat",  "&#x2248;FMV"
    elif pct < -10:     cls, txt = "delta-great", f"&#x2193;{abs(pct):.0f}%"
    elif pct < 0:       cls, txt = "delta-good",  f"&#x2193;{abs(pct):.0f}%"
    elif pct > 15:      cls, txt = "delta-high",  f"&#x2191;{pct:.0f}%"
    else:               cls, txt = "delta-mid",   f"&#x2191;{pct:.0f}%"
    return f'<span class="delta {cls}">{txt}</span>'

def _fmv_line(price, fmv_val, conf, comp_count) -> str:
    if not fmv_val or conf == "NONE":
        return '<div class="fmv-none"><span class="fmv-none-dot"></span>No FMV &mdash; insufficient comps</div>'
    pct = _fmv_pct(price, fmv_val)
    fmv_str  = _p(fmv_val)
    comp_str = f"{comp_count} comp{'s' if comp_count != 1 else ''}"
    if pct is None:       rel = ""; cls = "fmv-neutral"
    elif abs(pct) < 2:    rel = "at market"; cls = "fmv-neutral"
    elif pct < -10:       rel = f"<strong>{abs(pct):.0f}% below</strong>"; cls = "fmv-great"
    elif pct < 0:         rel = f"{abs(pct):.0f}% below"; cls = "fmv-good"
    elif pct > 15:        rel = f"<strong>{pct:.0f}% above</strong>"; cls = "fmv-high"
    else:                 rel = f"{pct:.0f}% above"; cls = "fmv-mid"
    conf_span = {"HIGH": '<span class="conf-high">HIGH</span>',
                 "MEDIUM": '<span class="conf-med">MED</span>',
                 "LOW": '<span class="conf-low">LOW</span>'}.get(conf, "")
    return (f'<div class="fmv-line {cls}">'
            f'FMV {fmv_str} {conf_span}'
            f'{(" &middot; " + rel) if rel else ""}'
            f' &middot; <span class="fmv-comps">{comp_str}</span>'
            f'</div>')

# ── Auction card (horizontal layout) ─────────────────────────────────────────

_PLACEHOLDER = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='200'%3E"
                "%3Crect width='400' height='200' fill='%2318181F'/%3E"
                "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
                "font-family='monospace' font-size='12' fill='%2325252E'%3ENo photo%3C/text%3E%3C/svg%3E")

def _auction_card(car: dict, fmv_score: dict, urgent: bool = False) -> str:
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

    if img and img.startswith("/static/img_cache/"):
        img = "img_cache/" + img.split("/")[-1]

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)
    pct        = _fmv_pct(price, fmv_val) if conf != "NONE" else None

    gen_str    = _gen(year, model)

    # Auction FMV phasing (65% threshold)
    _fmv_hidden = False
    if ends_at and fmv_val and conf != "NONE":
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            _ends = _dt.fromisoformat(ends_at.replace("Z", "+00:00"))
            _now = _dt.now(_tz.utc)
            _left = _ends - _now
            if _left.total_seconds() > 0:
                if _left > _td(hours=24):
                    delta_html = ""
                    fmv_html = '<span style="color:#555;font-size:11px">Auction in progress</span>'
                    _fmv_hidden = True
                else:
                    _bid_pct = (float(price) / float(fmv_val) * 100) if price and fmv_val else 0
                    if _bid_pct >= 65:
                        delta_html = _delta_badge(pct)
                        fmv_html = _fmv_line(price, fmv_val, conf, comp_count)
                    else:
                        delta_html = ""
                        fmv_html = '<span style="color:#555;font-size:11px">Auction ending soon</span>'
                        _fmv_hidden = True
            else:
                delta_html = _delta_badge(pct)
                fmv_html = _fmv_line(price, fmv_val, conf, comp_count)
        except Exception:
            delta_html = _delta_badge(pct)
            fmv_html = _fmv_line(price, fmv_val, conf, comp_count)
    else:
        delta_html = _delta_badge(pct)
        fmv_html = _fmv_line(price, fmv_val, conf, comp_count)

    # Tier badge
    tier_html = ""
    if tier == "TIER1":
        tier_html = '<span class="tier-badge">GT / Collector</span>'

    # Image
    is_pca = "mart.pca.org" in img
    urgency_bar = '<div class="urgency-bar"></div>' if urgent else ""
    if img and is_pca:
        img_id = f"pcaimg_{abs(hash(img)) % 999999}"
        img_html = (
            f'<div class="img-col">'
            f'<img id="{img_id}" src="{_PLACEHOLDER}" alt="{_h(str(year)+" "+model)}" class="auc-img">'
            f'<script>(function(){{'
            f'var x=new XMLHttpRequest();x.open("GET","{_h(img)}",true);'
            f'x.setRequestHeader("Referer","https://mart.pca.org/");'
            f'x.responseType="blob";'
            f'x.onload=function(){{if(x.status==200){{var u=URL.createObjectURL(x.response);document.getElementById("{img_id}").src=u;}}}};'
            f'x.send();'
            f'}})();</script>'
            f'{urgency_bar}'
            f'</div>'
        )
    elif img:
        img_html = (
            f'<div class="img-col">'
            f'<img src="{_h(img)}" alt="{_h(str(year)+" "+model)}" class="auc-img" loading="lazy" '
            f'onerror="this.src=\'{_PLACEHOLDER}\'">'
            f'{urgency_bar}'
            f'</div>'
        )
    else:
        img_html = (
            f'<div class="img-col">'
            f'<img src="{_PLACEHOLDER}" alt="No photo" class="auc-img">'
            f'{urgency_bar}'
            f'</div>'
        )

    # Chips
    chips = []
    if trans:   chips.append(_h(trans))
    if mileage: chips.append(f"{_m(mileage)} mi")
    chips_html = " &middot; ".join(chips)

    # Timer
    if ends_at:
        timer_html = (
            f'<span class="countdown-timer" data-ends="{_h(ends_at)}">…</span>'
        )
    else:
        timer_html = '<span class="no-end">No end time</span>'

    urgent_cls = " urgent" if urgent else ""

    return (
        f'<div class="auc-card{urgent_cls}" onclick="openListing(\'{_h(url)}\')">\n'
        f'  {img_html}\n'
        f'  <div class="auc-body">\n'
        f'    <div class="auc-top-row">\n'
        f'      <div style="display:flex;align-items:center;gap:6px">'
        f'{_badge(dealer)}'
        f'<span class="gen-label">{_h(gen_str)}</span>'
        f'</div>\n'
        f'      {timer_html}\n'
        f'    </div>\n'
        f'    <div class="auc-title">{year} Porsche {_h(model)}{(" " + _h(trim)) if trim else ""}</div>\n'
        f'    {tier_html}\n'
        f'    <div class="auc-bid-row">\n'
        f'      <span class="bid-label">Current Bid</span>\n'
        f'      <span class="bid-val">{_p(price)}</span>\n'
        f'      {delta_html}\n'
        f'    </div>\n'
        f'    {fmv_html}\n'
        f'    <div class="auc-meta">{chips_html}</div>\n'
        f'  </div>\n'
        f'</div>'
    )

# ── Section builder ───────────────────────────────────────────────────────────

def _section(title, subtitle, cards_html, icon, count, sec_cls="") -> str:
    if not cards_html:
        cards_html = ('<div class="empty">'
                      '<div class="empty-icon">&#x1F50D;</div>'
                      '<div class="empty-text">No auctions in this window</div>'
                      '</div>')
    return (
        f'<div class="section {sec_cls}">\n'
        f'  <div class="section-hdr">\n'
        f'    <div style="display:flex;align-items:center;gap:10px">\n'
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

        rows = conn.execute(
            "SELECT * FROM listings WHERE source_category='AUCTION' AND status='active'"
        ).fetchall()
        cars = [dict(r) for r in rows]

        n_listings_total = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE status='active'"
        ).fetchone()[0]
        n_comps_total = conn.execute(
            "SELECT COUNT(*) FROM sold_comps WHERE sold_price IS NOT NULL"
        ).fetchone()[0]

        # Recently ended auctions (sold in last 48 hours)
        ended_rows = conn.execute(
            """SELECT * FROM listings
               WHERE source_category='AUCTION' AND status='sold'
               AND auction_ends_at IS NOT NULL
               AND auction_ends_at >= datetime('now', '-48 hours')
               ORDER BY auction_ends_at DESC"""
        ).fetchall()
        ended_cars = [dict(r) for r in ended_rows]

    def _parse_ends(s):
        if not s: return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    for c in cars:
        c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})

    for c in ended_cars:
        c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})
        c["_ends_dt"] = _parse_ends(c.get("auction_ends_at"))

    ending_soon = []
    later_today = []
    coming_up   = []
    no_end_time = []

    three_hr  = now_utc + timedelta(hours=3)
    one_day   = now_utc + timedelta(hours=24)

    for c in cars:
        ends_dt = _parse_ends(c.get("auction_ends_at"))
        c["_ends_dt"] = ends_dt
        if ends_dt is None or ends_dt <= now_utc:
            no_end_time.append(c)
        elif ends_dt <= three_hr:
            ending_soon.append(c)
        elif ends_dt <= one_day:
            later_today.append(c)
        else:
            coming_up.append(c)

    def _sort_key(c):
        d = c.get("_ends_dt")
        return d if d else datetime(9999, 12, 31, tzinfo=timezone.utc)

    ending_soon.sort(key=_sort_key)
    later_today.sort(key=_sort_key)
    coming_up.sort(key=_sort_key)

    def _cards(lst, urgent=False):
        return "\n".join(_auction_card(c, c["_fmv"], urgent=urgent) for c in lst)

    s_ending = _section("Ending Soon", "Under 3 hours",             _cards(ending_soon, urgent=True), "&#x1F525;", len(ending_soon), "ending-soon")
    s_today  = _section("Later Today", "3 &ndash; 24 hours",        _cards(later_today),               "&#x23F0;",  len(later_today))
    s_coming = _section("Coming Up",   "Beyond 24 hours",           _cards(coming_up),                 "&#x1F4C5;", len(coming_up))
    s_noend  = _section("No End Time", "Buy-now / end time unknown", _cards(no_end_time),               "&#x1F3F7;", len(no_end_time))
    s_ended  = _section("Recently Ended", "Last 48 hours &mdash; final prices", _cards(ended_cars), "&#x1F3C1;", len(ended_cars), "ended")

    total   = len(cars)
    now_str = now_utc.strftime("%b %d, %Y %H:%M UTC")

    html = _build_html(s_ending, s_today, s_coming, s_noend, s_ended, total, len(ending_soon), len(later_today), len(coming_up), len(ended_cars), now_str, n_listings_total, n_comps_total)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[auction_dashboard] wrote {OUT_PATH} ({total} auctions)")
    return html


# ── HTML template ─────────────────────────────────────────────────────────────

def _build_html(s_ending, s_today, s_coming, s_noend, s_ended, total, n_ending, n_today, n_coming, n_ended, now_str, n_listings_total=0, n_comps_total=0) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PTOX Auctions">
<meta name="theme-color" content="#0A0A0C">
<title>PTOX11 &mdash; Auction Watcher</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');

:root {{
  --red:    #c0392b;
  --bg:     #0d0d0d;
  --bg2:    #141414;
  --bg3:    #1c1c1c;
  --border: #2a2a2a;
  --text:   #e8e4df;
  --muted:  #7a7570;
  --green:  #4ade80;
  --yellow: #EAB308;
}}

*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.5; }}
a {{ color:inherit; text-decoration:none; }}

/* ── Topbar ── */
.topbar {{
  height:52px; background:#141414; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  padding:0 24px; position:sticky; top:0; z-index:50;
}}
.topbar-left {{ display:flex; align-items:center; gap:4px; }}
.logo {{ font-family:'Syne',sans-serif; font-size:14px; font-weight:800; color:var(--text); letter-spacing:2px; margin-right:16px; white-space:nowrap; flex-shrink:0; }}
.logo span {{ color:var(--red); }}
.pill-group {{ display:flex; gap:4px; flex:1; justify-content:center; }}
.pill {{ padding:6px 0; min-width:78px; border-radius:14px; font-size:13px; font-weight:600; color:#666; background:transparent; border:1px solid transparent; text-align:center; display:flex; flex-direction:column; align-items:center; gap:1px; text-decoration:none; cursor:pointer; transition:all 0.15s; }}
.pill.active {{ color:var(--text); background:#222; border-color:#444; }}
.pill .pill-count {{ font-size:10px; font-weight:500; color:#555; }}
.pill.active .pill-count {{ color:#999; }}
.more-btn {{ padding:7px 11px; border-radius:14px; font-size:11px; font-weight:500; color:#666; background:transparent; border:1px solid #333; display:flex; align-items:center; gap:3px; cursor:pointer; flex-shrink:0; }}
.more-btn:hover {{ border-color:#555; color:#aaa; }}
.dropdown-overlay {{ display:none; }}
.dropdown-overlay.show {{ display:block; }}
.dropdown {{ position:fixed; right:14px; top:54px; background:#222; border:1px solid #333; border-radius:12px; padding:6px; min-width:180px; box-shadow:0 8px 32px rgba(0,0,0,0.5); z-index:200; }}
.dd-item {{ padding:10px 14px; font-size:14px; color:#ccc; border-radius:8px; cursor:pointer; display:flex; align-items:center; gap:10px; }}
.dd-item:hover {{ background:#2a2a2a; }}
.dd-icon {{ font-size:15px; width:20px; text-align:center; }}
.dd-divider {{ height:1px; background:#333; margin:4px 10px; }}
.dd-backdrop {{ position:fixed; inset:0; z-index:199; }}
.topbar-right {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); display:flex; align-items:center; gap:16px; }}
.live-dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--green); margin-right:5px; animation:pulse 1.5s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}

/* ── Stats strip ── */
.stats-strip {{ display:flex; gap:1px; background:var(--border); border-bottom:1px solid var(--border); }}
.stat-cell {{ flex:1; background:var(--bg2); padding:12px 16px; text-align:center; }}
.stat-num {{ font-family:'DM Mono',monospace; font-size:20px; font-weight:500; color:var(--text); letter-spacing:-1px; line-height:1; }}
.stat-num.red {{ color:var(--red); }}
.stat-num.yellow {{ color:var(--yellow); }}
.stat-num.green {{ color:var(--green); }}
.stat-lbl {{ font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); margin-top:4px; text-transform:uppercase; letter-spacing:0.5px; }}

/* ── Page body ── */
.page-body {{ max-width:1300px; margin:0 auto; padding:24px 20px 48px; }}

/* ── Section ── */
.section {{ margin-bottom:36px; }}
.section-hdr {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }}
.section-icon {{ font-size:1.2em; }}
.section-title {{ font-family:'Syne',sans-serif; font-size:15px; font-weight:700; color:var(--text); }}
.section-count {{ display:inline-block; background:var(--bg3); border:1px solid var(--border); color:var(--muted); font-family:'DM Mono',monospace; font-size:9px; padding:1px 7px; border-radius:10px; margin-left:6px; vertical-align:middle; }}
.section-sub {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-top:2px; }}
.ending-soon .section-title {{ color:var(--red); }}
.ending-soon .section-count {{ background:#1A0508; border-color:#3A0A12; color:var(--red); }}

/* ── Cards grid ── */
.cards-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(380px,1fr)); gap:10px; }}

/* ── Auction card — horizontal ── */
.auc-card {{
  background:var(--bg2); border:1px solid var(--border); border-radius:6px;
  overflow:hidden; cursor:pointer; display:flex; height:110px;
  transition:border-color 0.15s, transform 0.15s, box-shadow 0.15s;
}}
.auc-card:hover {{ border-color:var(--red); transform:translateY(-1px); box-shadow:0 4px 16px rgba(214,41,62,0.12); }}
.auc-card.urgent {{ border-color:#2A0810; }}
.auc-card.urgent:hover {{ border-color:var(--red); }}

.img-col {{ width:110px; min-width:110px; position:relative; overflow:hidden; background:var(--bg3); flex-shrink:0; }}
.auc-img {{ width:100%; height:100%; object-fit:cover; display:block; opacity:0.88; transition:transform 0.2s; }}
.auc-card:hover .auc-img {{ transform:scale(1.04); }}
.urgency-bar {{ position:absolute; bottom:0; left:0; right:0; height:3px; background:var(--red); }}

.auc-body {{ padding:10px 12px; flex:1; display:flex; flex-direction:column; gap:0; min-width:0; }}
.auc-top-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; }}
.gen-label {{ font-family:'DM Mono',monospace; font-size:9px; color:#4B4B5D; }}
.auc-title {{ font-family:'DM Sans',sans-serif; font-size:12px; color:#C0C0D0; margin-bottom:4px; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.tier-badge {{ display:inline-block; font-family:'DM Mono',monospace; font-size:8px; font-weight:500; background:#1A0A00; color:var(--yellow); padding:2px 6px; border-radius:3px; margin-bottom:4px; text-transform:uppercase; border:1px solid #3A2000; letter-spacing:0.5px; }}
.auc-bid-row {{ display:flex; align-items:baseline; gap:7px; margin-bottom:4px; }}
.bid-label {{ font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); }}
.bid-val {{ font-family:'DM Mono',monospace; font-size:15px; font-weight:500; color:#fff; letter-spacing:-0.5px; }}
.auc-meta {{ font-family:'DM Mono',monospace; font-size:9px; color:#3B3B4D; margin-top:auto; }}

/* ── Countdown timer ── */
.countdown-timer {{
  font-family:'DM Mono',monospace; font-size:12px; font-weight:500;
  color:var(--red); letter-spacing:0.5px; font-variant-numeric:tabular-nums;
}}
.countdown-timer.urgent-tick {{ animation:urgPulse 1s infinite; }}
@keyframes urgPulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
.countdown-timer.done {{ color:var(--muted); }}
.no-end {{ font-family:'DM Mono',monospace; font-size:10px; color:#3B3B4D; }}

/* ── FMV line ── */
.fmv-line {{ font-family:'DM Mono',monospace; font-size:9px; padding:3px 7px; border-radius:3px; margin-bottom:3px; }}
.fmv-none {{ display:flex; align-items:center; gap:4px; font-family:'DM Mono',monospace; font-size:9px; color:#3B3B4D; margin-bottom:3px; }}
.fmv-none-dot {{ width:4px; height:4px; border-radius:50%; background:var(--border); flex-shrink:0; }}
.fmv-comps {{ opacity:0.6; }}
.fmv-neutral {{ background:transparent; color:var(--muted); }}
.fmv-great  {{ background:transparent; color:var(--green); }}
.fmv-good   {{ background:transparent; color:#86EFAC; }}
.fmv-mid    {{ background:transparent; color:var(--yellow); }}
.fmv-high   {{ background:transparent; color:#F87171; }}
.conf-high {{ color:var(--green); }}
.conf-med  {{ color:var(--yellow); }}
.conf-low  {{ color:#F87171; }}

/* ── Delta badges ── */
.delta {{ font-family:'DM Mono',monospace; font-size:10px; font-weight:500; padding:2px 6px; border-radius:3px; }}
.delta-great {{ background:#052210; color:var(--green); }}
.delta-good  {{ background:#052210; color:#86EFAC; }}
.delta-flat  {{ background:var(--bg3); color:var(--muted); }}
.delta-mid   {{ background:#1A1000; color:var(--yellow); }}
.delta-high  {{ background:#1A0508; color:#F87171; }}

/* ── Badge ── */
.badge {{ font-family:'DM Mono',monospace; font-size:9px; font-weight:500; padding:2px 6px; border-radius:3px; }}

/* ── Empty ── */
.empty {{ grid-column:1/-1; text-align:center; padding:40px 20px; }}
.empty-icon {{ font-size:2em; margin-bottom:8px; }}
.empty-text {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); }}

/* ── Scrollbar ── */
::-webkit-scrollbar {{ width:5px; height:5px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:3px; }}
::-webkit-scrollbar-thumb:hover {{ background:var(--muted); }}

@media(max-width:640px) {{
  .topbar-right {{ display:none; }}
  .cards-grid {{ grid-template-columns:1fr; }}
  .page-body {{ padding:12px 12px 32px; }}
}}
</style>
</head>
<body>

<header class="topbar">
  <a class="logo" href="index.html" style="text-decoration:none;">PTO<span>X</span></a>
  <div class="pill-group">
    <a class="pill" href="index.html">
      <span class="pill-label">Listings</span>
      <span class="pill-count">{n_listings_total:,} active</span>
    </a>
    <span class="pill active">
      <span class="pill-label">Auctions</span>
      <span class="pill-count">{total:,} active</span>
    </span>
    <a class="pill" href="index.html#comps">
      <span class="pill-label">Comps</span>
      <span class="pill-count">{n_comps_total:,} total</span>
    </a>
  </div>
  <button class="more-btn" onclick="toggleDropdown()">More &#x25BE;</button>
  <div class="topbar-right">
    <span><span class="live-dot"></span>Live Countdown</span>
    <span>{now_str}</span>
    <span>Auto-refresh 2 min</span>
  </div>
</header>
<div class="dropdown-overlay" id="dd-overlay">
  <div class="dd-backdrop" onclick="closeDropdown()"></div>
  <div class="dropdown">
    <div class="dd-item"><span class="dd-icon">&#x2605;</span> My Cars</div>
    <a class="dd-item" href="search.html"><span class="dd-icon">&#x1F50D;</span> Search</a>
    <div class="dd-divider"></div>
    <a class="dd-item" href="market_report.html"><span class="dd-icon">&#x1F4CA;</span> Market Reports</a>
    <a class="dd-item" href="notify.html"><span class="dd-icon">&#x1F514;</span> Notifications</a>
    <div class="dd-divider"></div>
    <div class="dd-item"><span class="dd-icon">&#x2699;&#xFE0F;</span> Settings</div>
  </div>
</div>

<!-- Stats strip -->
<div class="stats-strip">
  <div class="stat-cell">
    <div class="stat-num red">{total}</div>
    <div class="stat-lbl">Active Auctions</div>
  </div>
  <div class="stat-cell">
    <div class="stat-num" id="stat-ending">{n_ending}</div>
    <div class="stat-lbl">Ending &lt; 3hr</div>
  </div>
  <div class="stat-cell">
    <div class="stat-num yellow">{n_today}</div>
    <div class="stat-lbl">Later Today</div>
  </div>
  <div class="stat-cell">
    <div class="stat-num green">{n_coming}</div>
    <div class="stat-lbl">Coming Up</div>
  </div>
</div>

<div class="page-body">
  {s_ending}
  {s_today}
  {s_coming}
  {s_noend}
{s_ended}
</div>

<script>
function pad(n) {{ return n < 10 ? '0' + n : '' + n; }}

function fmtCountdown(secs) {{
  if (secs <= 0) return 'ENDED';
  var d = Math.floor(secs / 86400);
  var h = Math.floor((secs % 86400) / 3600);
  var m = Math.floor((secs % 3600) / 60);
  var s = Math.floor(secs % 60);
  if (d > 0) return d + 'd ' + pad(h) + ':' + pad(m) + ':' + pad(s);
  return pad(h) + ':' + pad(m) + ':' + pad(s);
}}

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
      el.classList.remove('urgent-tick');
    }} else {{
      el.textContent = fmtCountdown(secs);
      el.classList.remove('done');
      if (secs < 3600) {{
        el.classList.add('urgent-tick');
        endingSoon++;
      }} else {{
        el.classList.remove('urgent-tick');
      }}
    }}
  }});
  var es = document.getElementById('stat-ending');
  if (es) es.textContent = endingSoon;
}}

tickAll();
setInterval(tickAll, 1000);

// ── PWA-safe listing navigation ───────────────────────────────────────────────
function openListing(url) {{
  window.open(url, '_blank');
}}

window.addEventListener('pageshow', function(e) {{
  if (e.persisted) {{ /* page restored from bfcache — no action needed */ }}
}});

function toggleDropdown() {{
  document.getElementById('dd-overlay').classList.toggle('show');
}}
function closeDropdown() {{
  document.getElementById('dd-overlay').classList.remove('show');
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    generate()

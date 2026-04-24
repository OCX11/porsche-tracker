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

def _p_short(v) -> str:
    if v is None: return "—"
    try:
        n = float(v)
        if n >= 1_000_000:
            m = n / 1_000_000
            return f"${m:.1f}M" if m != int(m) else f"${int(m)}M"
        if n >= 1_000:
            return f"${int(round(n / 1000))}K"
        return f"${int(n):,}"
    except Exception:
        return "—"

def _m(v) -> str:
    if v is None: return "—"
    try:    return f"{int(v):,}"
    except: return "—"

def _h(s) -> str:
    return _html.escape(str(s or ""))

def _dedup_model_trim(model: str, trim: str) -> str:
    m = (model or "").strip()
    t = (trim or "").strip()
    if t and m and t.lower().startswith(m.lower()):
        t = t[len(m):].lstrip()
    return (m + (" " + t if t else "")).strip()

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

def _badge_label(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    return _BADGE_CFG.get(k, ("#18181F", "#6B6B7D", (dealer or "?")[:14]))[2]

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

def _fmv_line(price, fmv_val, conf, comp_count, price_low=None, price_high=None) -> str:
    if not fmv_val or conf == "NONE":
        return '<div class="fmv-none"><span class="fmv-none-dot"></span>No FMV &mdash; insufficient comps</div>'
    pct = _fmv_pct(price, fmv_val)
    fmv_str  = _p_short(fmv_val)
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
    range_str = ""
    if conf in ("HIGH", "MEDIUM") and price_low and price_high:
        range_str = f' &middot; <span class="fmv-range">{_p_short(price_low)}&ndash;{_p_short(price_high)}</span>'
    return (f'<div class="fmv-line {cls}">'
            f'FMV {fmv_str} {conf_span}'
            f'{(" &middot; " + rel) if rel else ""}'
            f'{range_str}'
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
    price_low  = fmv_score.get("price_low")
    price_high = fmv_score.get("price_high")
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
                        fmv_html = _fmv_line(price, fmv_val, conf, comp_count, price_low, price_high)
                    else:
                        delta_html = ""
                        fmv_html = '<span style="color:#555;font-size:11px">Auction ending soon</span>'
                        _fmv_hidden = True
            else:
                delta_html = _delta_badge(pct)
                fmv_html = _fmv_line(price, fmv_val, conf, comp_count, price_low, price_high)
        except Exception:
            delta_html = _delta_badge(pct)
            fmv_html = _fmv_line(price, fmv_val, conf, comp_count, price_low, price_high)
    else:
        delta_html = _delta_badge(pct)
        fmv_html = _fmv_line(price, fmv_val, conf, comp_count, price_low, price_high)

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
    src_label  = _badge_label(dealer)

    return (
        f'<div class="auc-card{urgent_cls}"'
        f' data-gen="{_h(gen_str)}"'
        f' data-src="{_h(src_label)}"'
        f' data-tier="{_h(tier)}"'
        f' data-price="{price or 0}"'
        f' data-ends="{_h(ends_at)}"'
        f' onclick="openListing(\'{_h(url)}\')">\n'
        f'  {img_html}\n'
        f'  <div class="auc-body">\n'
        f'    <div class="auc-top-row">\n'
        f'      <div style="display:flex;align-items:center;gap:6px">'
        f'{_badge(dealer)}'
        f'<span class="gen-label">{_h(gen_str)}</span>'
        f'</div>\n'
        f'      {timer_html}\n'
        f'    </div>\n'
        f'    <div class="auc-title">{year} Porsche {_h(_dedup_model_trim(model, trim))}</div>\n'
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

def _section(title, subtitle, cards_html, icon, count, sec_cls="", hide_if_empty=False) -> str:
    if not cards_html:
        if hide_if_empty:
            return ""
        cards_html = ('<div class="empty">'
                      '<div class="empty-icon">&#x1F50D;</div>'
                      '<div class="empty-text">No auctions in this window</div>'
                      '</div>')
    cls = "section" + (" " + sec_cls if sec_cls else "")
    return (
        f'<div class="{cls}">\n'
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
                    "price_low":  getattr(fmv_obj, "price_low", None),
                    "price_high": getattr(fmv_obj, "price_high", None),
                }
            else:
                fmv_by_id[row["id"]] = {"fmv": None, "confidence": "NONE", "comp_count": 0, "price_low": None, "price_high": None}

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
        n_new_today = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE status='active' AND date(date_first_seen)=date('now')"
        ).fetchone()[0]
        active_price_rows = conn.execute(
            "SELECT id, price FROM listings WHERE status='active' AND price IS NOT NULL"
        ).fetchall()
        n_deals = sum(
            1 for r in active_price_rows
            if fmv_by_id.get(r[0], {}).get("fmv") and
               fmv_by_id.get(r[0], {}).get("confidence", "NONE") != "NONE" and
               float(r[1]) < float(fmv_by_id[r[0]]["fmv"]) * 0.90
        )

        # Recently Completed — archived in last 48h, auction end time already passed
        ended_rows = conn.execute(
            """SELECT * FROM listings
               WHERE source_category='AUCTION' AND status='sold'
               AND archived_at >= datetime('now', '-48 hours')
               AND (auction_ends_at IS NULL OR auction_ends_at <= datetime('now'))
               ORDER BY archived_at DESC"""
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
    live_auction = []
    no_end_time  = []

    one_day = now_utc + timedelta(hours=24)

    for c in cars:
        ends_dt = _parse_ends(c.get("auction_ends_at"))
        c["_ends_dt"] = ends_dt
        c["_gen"] = _gen(c.get("year"), c.get("model"))
        c["_src"] = _badge_label(c.get("dealer", ""))
        if ends_dt is None or ends_dt <= now_utc:
            no_end_time.append(c)
        elif ends_dt <= one_day:
            ending_soon.append(c)
        else:
            live_auction.append(c)

    # Unique generations and sources for filter chips
    all_gens = sorted(set(c["_gen"] for c in cars if c["_gen"] and c["_gen"] != "Unknown"))
    all_srcs = sorted(set(c["_src"] for c in cars if c["_src"]))
    gen_chips_html = "".join(
        f'<button class="auc-chip" data-val="{_h(g)}" data-filter="gen" onclick="toggleAucChip(this)">{_h(g)}</button>'
        for g in all_gens
    )
    src_chips_html = "".join(
        f'<button class="auc-chip" data-val="{_h(s)}" data-filter="src" onclick="toggleAucChip(this)">{_h(s)}</button>'
        for s in all_srcs
    )

    def _sort_key(c):
        d = c.get("_ends_dt")
        return d if d else datetime(9999, 12, 31, tzinfo=timezone.utc)

    ending_soon.sort(key=_sort_key)
    live_auction.sort(key=_sort_key)

    def _cards(lst, urgent=False):
        return "\n".join(_auction_card(c, c["_fmv"], urgent=urgent) for c in lst)

    s_ending = _section("Ending Soon",   "Less than 24 hours",         _cards(ending_soon, urgent=True), "&#x1F525;", len(ending_soon),  "ending-soon", hide_if_empty=True)
    s_live   = _section("Live Auctions", "Ending beyond 24 hours",     _cards(live_auction),             "&#x1F7E2;", len(live_auction))
    s_noend  = _section("No End Time",   "Buy-now / end time unknown",  _cards(no_end_time),              "&#x1F3F7;", len(no_end_time))
    s_ended  = _section("Recently Completed", "Last 48 hours &mdash; final prices", _cards(ended_cars),  "&#x1F3C1;", len(ended_cars), "ended")

    total   = len(cars)
    now_str = now_utc.strftime("%b %d, %Y %H:%M UTC")

    html = _build_html(s_ending, s_live, s_noend, s_ended, total, len(ending_soon), len(live_auction), len(ended_cars), now_str, n_listings_total, n_comps_total, n_new_today, n_deals, gen_chips_html, src_chips_html)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[auction_dashboard] wrote {OUT_PATH} ({total} auctions)")
    return html


# ── HTML template ─────────────────────────────────────────────────────────────

def _build_html(s_ending, s_live, s_noend, s_ended, total, n_ending, n_live, n_ended, now_str, n_listings_total=0, n_comps_total=0, n_new_today=0, n_deals=0, gen_chips_html="", src_chips_html="") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script>(function(){{var t=localStorage.getItem('ptox_theme');if(t)document.documentElement.dataset.theme=t;}})()</script>
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- pull-to-refresh replaces meta refresh -->
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
[data-theme="racing"] {{ --red:#e53e3e; --bg:#0c0809; --bg2:#160d0e; --bg3:#1e1213; --border:#2e1a1a; --text:#ede8e3; --muted:#7a6a6a; }}
[data-theme="gulf"]   {{ --red:#2563eb; --bg:#08100c; --bg2:#0e1810; --bg3:#142016; --border:#1a3020; --text:#e2ede8; --muted:#5a7a6a; }}
[data-theme="olive"]  {{ --red:#65a30d; --bg:#0a0c08; --bg2:#12140e; --bg3:#1a1c14; --border:#252a1a; --text:#e4e8de; --muted:#6a7258; }}
[data-theme="purple"] {{ --red:#7c3aed; --bg:#09080d; --bg2:#100e16; --bg3:#17141e; --border:#221e2e; --text:#e6e2ee; --muted:#6a5a7a; }}
[data-theme="light"]  {{ --red:#c0392b; --bg:#f5f4f2; --bg2:#edebe8; --bg3:#e2dfdb; --border:#ccc9c4; --text:#1a1814; --muted:#7a756e; }}

*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.5; }}
a {{ color:inherit; text-decoration:none; }}

/* ── Topbar ── */
.topbar {{
  height:52px; background:#141414; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  padding:0 24px; position:sticky; top:0; z-index:50;
}}
.logo {{ font-family:'Syne',sans-serif; font-size:14px; font-weight:800; color:#fff; letter-spacing:6px; white-space:nowrap; flex-shrink:0; text-decoration:none; }}
.logo span {{ color:#c0392b; }}
.stats-bar {{ display:flex; gap:1px; margin:0 12px 8px; background:#2a2a2a; border-radius:14px; overflow:hidden; border:1px solid #2a2a2a; }}
.stat-cell {{ flex:1; padding:12px 8px 10px; text-align:center; background:#141414; cursor:pointer; transition:background 0.15s; position:relative; text-decoration:none; color:inherit; }}
.stat-cell:first-child {{ border-radius:13px 0 0 13px; }}
.stat-cell:last-child {{ border-radius:0 13px 13px 0; }}
.stat-cell:hover {{ background:#1c1c1c; }}
.stat-cell.active {{ background:#1e1e1e; }}
.stat-cell.active::after {{ content:''; position:absolute; bottom:0; left:0; right:0; height:2px; background:#c0392b; }}
.stat-cell + .stat-cell {{ border-left:1px solid #2a2a2a; }}
.stat-number {{ font-size:22px; font-weight:700; letter-spacing:-0.5px; line-height:1.1; color:#e8e4df; }}
.stat-number.green {{ color:#4ade80; }}
.stat-number.red {{ color:#c0392b; }}
.stat-label {{ font-size:9px; font-weight:600; letter-spacing:1.5px; text-transform:uppercase; color:#666; margin-top:3px; }}
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
.filter-bar {{ background:var(--bg2); border-bottom:1px solid var(--border); padding:10px 16px; display:flex; flex-wrap:wrap; align-items:center; gap:8px; }}
.filter-bar-group {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; flex:1; }}
.auc-chip {{ padding:4px 10px; border-radius:12px; border:1px solid var(--border); background:var(--bg3); color:var(--muted); font-family:'DM Mono',monospace; font-size:10px; font-weight:600; cursor:pointer; transition:all 0.15s; white-space:nowrap; }}
.auc-chip:hover {{ color:var(--text); border-color:#555; }}
.auc-chip.active {{ background:#1A0810; border-color:var(--red); color:var(--red); }}
.sort-select {{ padding:4px 8px; border:1px solid var(--border); border-radius:6px; background:var(--bg3); color:var(--muted); font-family:'DM Mono',monospace; font-size:10px; cursor:pointer; outline:none; }}
.sort-select:focus {{ border-color:var(--red); }}
.filter-count {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-left:auto; white-space:nowrap; }}
.live-dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--green); margin-right:5px; animation:pulse 1.5s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}

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
.auc-title {{ font-family:'DM Sans',sans-serif; font-size:12px; color:#C0C0D0; margin-bottom:4px; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-height:1.3em; }}
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
  .stats-bar {{ margin:0 8px 8px; }}
  .stat-number {{ font-size:18px; }}
}}
</style>
</head>
<body>

<header class="topbar">
  <a class="logo" href="index.html">PTOX<span>11</span></a>
  <button class="more-btn" onclick="toggleDropdown()">More &#x25BE;</button>
</header>
<div class="stats-bar">
  <a class="stat-cell" href="index.html" style="text-decoration:none;color:inherit">
    <div class="stat-number">{n_listings_total:,}</div>
    <div class="stat-label">Active</div>
  </a>
  <a class="stat-cell" href="index.html" style="text-decoration:none;color:inherit">
    <div class="stat-number">{n_new_today}</div>
    <div class="stat-label">New Today</div>
  </a>
  <div class="stat-cell active">
    <div class="stat-number red">{total}</div>
    <div class="stat-label">Auctions</div>
  </div>
  <a class="stat-cell" href="index.html#comps" style="text-decoration:none;color:inherit">
    <div class="stat-number">{n_comps_total:,}</div>
    <div class="stat-label">Comps</div>
  </a>
  <a class="stat-cell" href="index.html" style="text-decoration:none;color:inherit">
    <div class="stat-number green">{n_deals}</div>
    <div class="stat-label">Deals</div>
  </a>
</div>
<div class="dropdown-overlay" id="dd-overlay">
  <div class="dd-backdrop" onclick="closeDropdown()"></div>
  <div class="dropdown">
    <div class="dd-item"><span class="dd-icon">&#x2605;</span> My Cars</div>
    <a class="dd-item" href="search.html"><span class="dd-icon">&#x1F50D;</span> Search</a>
    <div class="dd-divider"></div>
    <a class="dd-item" href="calculator.html"><span class="dd-icon">&#x1F4B0;</span> FMV Calculator</a>
    <a class="dd-item" href="market_report.html"><span class="dd-icon">&#x1F4CA;</span> Market Reports</a>
    <a class="dd-item" href="notify.html"><span class="dd-icon">&#x1F514;</span> Notifications</a>
    <div class="dd-divider"></div>
    <div class="dd-item"><span class="dd-icon">&#x1F3A8;</span> Theme</div>
    <div class="dd-item"><span class="dd-icon">&#x2699;&#xFE0F;</span> Settings</div>
  </div>
</div>

<div class="filter-bar">
  <div class="filter-bar-group" id="gen-chips">
    {gen_chips_html}
  </div>
  <div class="filter-bar-group" id="src-chips">
    {src_chips_html}
  </div>
  <select class="sort-select" id="auc-sort" onchange="applyAucFilters()">
    <option value="ends_asc">Ending Soonest</option>
    <option value="ends_desc">Ending Latest</option>
    <option value="price_asc">Price Low→High</option>
    <option value="price_desc">Price High→Low</option>
    <option value="new_first">Newest Listed</option>
  </select>
  <span class="filter-count" id="auc-filter-count"></span>
</div>

<div class="page-body" id="page-body">
  {s_ending}
  {s_live}
  {s_noend}
{s_ended}
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
      if (!el.dataset.localSet) {{
        var endDate = new Date(el.dataset.ends);
        el.title = 'Ends ' + endDate.toLocaleString([], {{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}});
        el.dataset.localSet = '1';
      }}
      el.classList.remove('done');
      if (secs < 3600) {{
        el.classList.add('urgent-tick');
        endingSoon++;
      }} else {{
        el.classList.remove('urgent-tick');
      }}
    }}
  }});
}}

tickAll();
setInterval(tickAll, 1000);

// ── Pull-to-refresh ──────────────────────────────────────────────────────────
(function() {{
  var PTR_THRESHOLD = 80;
  var startY = 0;
  var pulling = false;
  var indicator = null;

  function getIndicator() {{
    if (!indicator) {{
      indicator = document.createElement('div');
      indicator.id = 'ptr-indicator';
      indicator.style.cssText = [
        'position:fixed','top:0','left:0','right:0',
        'height:4px',
        'background:linear-gradient(90deg,#e00400,#ffcc00)',
        'transform:scaleX(0)','transform-origin:left',
        'transition:transform 0.15s ease,opacity 0.3s ease',
        'z-index:9999','pointer-events:none','opacity:0'
      ].join(';');
      document.body.appendChild(indicator);
    }}
    return indicator;
  }}

  function setProgress(ratio) {{
    var el = getIndicator();
    el.style.opacity = ratio > 0 ? '1' : '0';
    el.style.transform = 'scaleX(' + Math.min(ratio, 1) + ')';
    el.style.transition = ratio > 0 ? 'none' : 'transform 0.15s ease,opacity 0.3s ease';
  }}

  function triggerRefresh() {{
    setProgress(1);
    getIndicator().style.background = '#00c853';
    setTimeout(function() {{ location.reload(); }}, 300);
  }}

  document.addEventListener('touchstart', function(e) {{
    if (window.scrollY === 0 && e.touches.length === 1) {{
      startY = e.touches[0].clientY;
      pulling = true;
    }}
  }}, {{passive: true}});

  document.addEventListener('touchmove', function(e) {{
    if (!pulling) return;
    var dy = e.touches[0].clientY - startY;
    if (dy <= 0) {{ pulling = false; setProgress(0); return; }}
    setProgress(Math.min(dy / PTR_THRESHOLD, 1));
  }}, {{passive: true}});

  document.addEventListener('touchend', function(e) {{
    if (!pulling) return;
    var dy = (e.changedTouches[0].clientY - startY);
    pulling = false;
    if (dy >= PTR_THRESHOLD) {{ triggerRefresh(); }}
    else {{ setProgress(0); }}
  }}, {{passive: true}});
}})();

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

// ── Auction filter + sort ────────────────────────────────────────────────────
var _aucActiveGens = [];
var _aucActiveSrcs = [];

function toggleAucChip(btn) {{
  var filter = btn.dataset.filter;
  var val    = btn.dataset.val;
  btn.classList.toggle('active');
  if (filter === 'gen') {{
    if (_aucActiveGens.includes(val)) _aucActiveGens = _aucActiveGens.filter(function(x){{ return x !== val; }});
    else _aucActiveGens.push(val);
  }} else {{
    if (_aucActiveSrcs.includes(val)) _aucActiveSrcs = _aucActiveSrcs.filter(function(x){{ return x !== val; }});
    else _aucActiveSrcs.push(val);
  }}
  applyAucFilters();
}}

function applyAucFilters() {{
  var cards = Array.from(document.querySelectorAll('.auc-card'));
  var sortVal = (document.getElementById('auc-sort') || {{}}).value || 'ends_asc';
  var visible = 0;

  cards.forEach(function(card) {{
    var gen   = card.dataset.gen   || '';
    var src   = card.dataset.src   || '';
    var genOk = _aucActiveGens.length === 0 || _aucActiveGens.includes(gen);
    var srcOk = _aucActiveSrcs.length === 0 || _aucActiveSrcs.includes(src);
    var show  = genOk && srcOk;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  }});

  // Sort within each section grid
  document.querySelectorAll('.cards-grid').forEach(function(grid) {{
    var visCards = Array.from(grid.querySelectorAll('.auc-card')).filter(function(c){{ return c.style.display !== 'none'; }});
    visCards.sort(function(a, b) {{
      if (sortVal === 'ends_asc')   return (a.dataset.ends || 'z') < (b.dataset.ends || 'z') ? -1 : 1;
      if (sortVal === 'ends_desc')  return (a.dataset.ends || '') > (b.dataset.ends || '') ? -1 : 1;
      if (sortVal === 'price_asc')  return parseInt(a.dataset.price||0) - parseInt(b.dataset.price||0);
      if (sortVal === 'price_desc') return parseInt(b.dataset.price||0) - parseInt(a.dataset.price||0);
      return 0;
    }});
    visCards.forEach(function(c){{ grid.appendChild(c); }});
  }});

  var countEl = document.getElementById('auc-filter-count');
  if (countEl && (_aucActiveGens.length > 0 || _aucActiveSrcs.length > 0)) {{
    countEl.textContent = visible + ' shown';
  }} else if (countEl) {{
    countEl.textContent = '';
  }}
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    generate()

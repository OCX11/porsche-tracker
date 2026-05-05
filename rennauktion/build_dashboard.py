"""
auction_dashboard.py — PTOX11 auction watcher, Concept D redesign.

Layout: Wide horizontal cards — 200px image left, bid + FMV side by side,
urgency stripe on left edge (color-coded), timer top-right.

Design decisions (2026-04-30):
  - Concept D selected: wide horiz, 200px image, bid + FMV inline at same level
  - FMV always shown as stable reference estimate alongside current bid — no phased reveal
  - FMV shown as "~$XX,XXX" estimate (not % delta from bid — bid moves constantly)
  - Auction house filter chips: BaT, C&B, pcarmarket only (no PCA Mart — it's not an auction)
  - Sort: ending soonest (default), FMV estimate, current bid, mileage
  - Urgency stripe: red < 3hr, amber < 24hr, green 3d+
  - Color-coded countdown timer: red / amber / green matching stripe

Output: docs/auctions.html
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.db import get_conn, init_db
import core.fmv as fmv_engine

BASE_DIR = Path(__file__).parent.parent  # project root
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
    return f'<span class="src-badge" style="background:{bg};color:{fg}">{_h(label)}</span>'

def _badge_label(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    return _BADGE_CFG.get(k, ("#18181F", "#6B6B7D", (dealer or "?")[:14]))[2]

def _gen(year, model):
    if not year: return ""
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
    return ""

# ── Urgency tier ─────────────────────────────────────────────────────────────

def _urgency(ends_dt, now_utc):
    """Returns: 'critical' | 'soon' | 'live' | 'noend'"""
    if ends_dt is None or ends_dt <= now_utc:
        return "noend"
    secs = (ends_dt - now_utc).total_seconds()
    if secs < 3 * 3600:   return "critical"
    if secs < 24 * 3600:  return "soon"
    return "live"

_URGENCY_BORDER = {
    "critical": "#c0392b",
    "soon":     "#3a3000",
    "live":     "#0f2a0f",
    "noend":    "var(--border)",
}

_URGENCY_TIMER_CLASS = {
    "critical": "timer-red",
    "soon":     "timer-amber",
    "live":     "timer-green",
    "noend":    "timer-muted",
}


# ── Placeholder SVG ──────────────────────────────────────────────────────────

_PLACEHOLDER = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='220'%3E"
                "%3Crect width='400' height='220' fill='%2318181F'/%3E"
                "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
                "font-family='monospace' font-size='12' fill='%2325252E'%3ENo photo%3C/text%3E%3C/svg%3E")

# ── FMV display (always shown — stable reference, not delta from bid) ─────────

def _fmv_display(fmv_val, conf, comp_count) -> str:
    """Returns the FMV estimate string for inline display next to current bid."""
    if not fmv_val or conf == "NONE":
        return '<span class="fmv-none">FMV: no data</span>'
    conf_span = {
        "HIGH":   '<span class="conf-pip conf-high"></span>',
        "MEDIUM": '<span class="conf-pip conf-med"></span>',
        "LOW":    '<span class="conf-pip conf-low"></span>',
    }.get(conf, "")
    fmv_str = _p_short(fmv_val)
    comp_str = f"{comp_count}c"
    return (
        f'<span class="fmv-est">'
        f'~{fmv_str}'
        f'</span>'
        f'<span class="fmv-meta">{conf_span}{comp_str}</span>'
    )

# ── Auction card — Concept D + hover expand with dot graph ───────────────────

def _auction_card(car: dict, fmv_score: dict, is_hero: bool = False) -> str:
    import json as _json
    dealer   = car.get("dealer", "")
    year     = car.get("year", "")
    model    = car.get("model", "") or ""
    trim     = car.get("trim", "") or ""
    price    = car.get("price")
    mileage  = car.get("mileage")
    url      = car.get("listing_url", "") or "#"
    img      = car.get("image_url", "") or ""
    ends_at  = car.get("auction_ends_at") or ""
    trans    = car.get("transmission", "") or ""
    color    = car.get("color", "") or ""
    body     = car.get("body_style", "") or ""
    tier     = car.get("tier", "") or ""
    listed   = car.get("date_first_seen", "") or ""
    notes    = car.get("notes", "") or ""

    # CDN image path fix
    if img and img.startswith("/static/img_cache/"):
        img = "img_cache/" + img.split("/")[-1]

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)
    fmv_low    = fmv_score.get("price_low")
    fmv_high   = fmv_score.get("price_high")
    comp_prices = fmv_score.get("comp_prices", [])
    comp_dots   = fmv_score.get("comp_dots", [])

    gen_str   = _gen(year, model)
    src_label = _badge_label(dealer)

    # Parse end time
    ends_dt = None
    if ends_at:
        try:
            ends_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc)
    urg = _urgency(ends_dt, now_utc)
    timer_cls = _URGENCY_TIMER_CLASS[urg]

    urg_label = {
        "critical": "ENDING NOW",
        "soon":     "ENDING TODAY",
        "live":     "",
        "noend":    "NO END TIME",
    }.get(urg, "")

    # Image tag
    is_pca = "mart.pca.org" in img
    if img and is_pca:
        img_id = f"pcaimg_{abs(hash(img)) % 999999}"
        img_tag = (
            f'<img id="{img_id}" src="{_PLACEHOLDER}" alt="" class="auc-img">'
            f'<script>(function(){{'
            f'var x=new XMLHttpRequest();x.open("GET","{_h(img)}",true);'
            f'x.setRequestHeader("Referer","https://mart.pca.org/");'
            f'x.responseType="blob";'
            f'x.onload=function(){{if(x.status==200){{var u=URL.createObjectURL(x.response);document.getElementById("{img_id}").src=u;}}}};'
            f'x.send();'
            f'}})();</script>'
        )
    elif img:
        img_tag = f'<img src="{_h(img)}" alt="" class="auc-img" loading="lazy" onerror="this.src=\'{_PLACEHOLDER}\'">'
    else:
        img_tag = f'<img src="{_PLACEHOLDER}" alt="" class="auc-img">'

    # Meta
    meta_parts = []
    if trans:   meta_parts.append(_h(trans))
    if mileage: meta_parts.append(f"{_m(mileage)} mi")
    if color:   meta_parts.append(_h(color))
    meta_html = ' <span class="dot">&middot;</span> '.join(meta_parts)

    # Subtitle chips
    subtitle_parts = [_badge(dealer)]
    if gen_str:   subtitle_parts.append(f'<span class="gen-tag">{_h(gen_str)}</span>')
    if urg_label: subtitle_parts.append(f'<span class="urg-tag urg-{urg}">{urg_label}</span>')
    subtitle_html = ' '.join(subtitle_parts)

    # Timer
    timer_html = (f'<span class="countdown-timer {timer_cls}" data-ends="{_h(ends_at)}">…</span>'
                  if ends_at else '<span class="countdown-timer timer-muted">—</span>')

    # FMV display
    fmv_html = _fmv_display(fmv_val, conf, comp_count)

    # Sort values
    fmv_sort_val = int(fmv_val) if fmv_val and conf != "NONE" else 0

    # Deal badge for expand panel
    deal_badge_html = ""
    if fmv_val and price and conf != "NONE":
        pct = (float(price) - float(fmv_val)) / float(fmv_val)
        if pct <= -0.05:
            deal_badge_html = f'<span class="deal-badge">{abs(round(pct*100))}% below FMV</span>'
        elif pct >= 0.05:
            deal_badge_html = f'<span class="above-badge">{round(pct*100)}% above FMV</span>'

    # Expand panel stats
    days_listed = ""
    if listed:
        try:
            from datetime import date
            d = date.fromisoformat(listed[:10])
            days_listed = str((date.today() - d).days)
        except Exception:
            pass

    fmv_range_str = (f"{_p_short(fmv_low)} &ndash; {_p_short(fmv_high)}"
                     if fmv_low and fmv_high else "—")

    # Tag chips for expand
    tag_chips = []
    if gen_str:  tag_chips.append(gen_str)
    if body:     tag_chips.append(_h(body))
    tags_html = "".join(f'<span class="etag">{t}</span>' for t in tag_chips[:6])

    listing_id = car.get("id", "")
    price_int = int(price) if price else 0

    hero_cls = " auc-card--hero" if is_hero else ""

    return f'''<div class="auc-card{hero_cls} auc-urg-{urg}"
 data-gen="{_h(gen_str)}" data-src="{_h(src_label)}" data-tier="{_h(tier)}"
 data-price="{price or 0}" data-fmv="{fmv_sort_val}"
 data-mileage="{int(mileage) if mileage else 999999}"
 data-ends="{_h(ends_at)}" data-listed="{_h(listed)}" data-url="{_h(url)}">
  <div class="card-main">
    <div class="img-wrap" onclick="window.open('{_h(url)}','_blank')">{img_tag}</div>
    <div class="card-body" onclick="cardClick(event,'{_h(url)}')">
      <div class="card-top">
        <div class="card-subtitle">{subtitle_html}</div>
        <div class="card-top-right">
          <button class="fav-btn" data-url="{_h(url)}" onclick="toggleFav(event,this)" title="Watch">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <circle cx="8" cy="8" r="4.5"/>
              <circle cx="8" cy="8" r="1.8" fill="var(--bg2)" stroke="none"/>
              <rect x="11.5" y="7" width="9" height="2.5" rx="1.25"/>
              <rect x="17" y="9.5" width="2.5" height="2.5" rx="0.8"/>
              <rect x="13.5" y="9.5" width="2.5" height="3.5" rx="0.8"/>
            </svg>
          </button>
          <div class="card-timer">{timer_html}</div>
        </div>
      </div>
      <div class="card-title">{year} Porsche {_h(_dedup_model_trim(model, trim))}</div>
      <div class="card-bottom">
        <div class="bid-block">
          <div class="val-label">Current Bid</div>
          <div class="bid-val">{_p(price)}</div>
        </div>
        <div class="divider-vert"></div>
        <div class="fmv-block">
          <div class="val-label">FMV Est.</div>
          <div class="fmv-row">{fmv_html}</div>
        </div>
        <div class="meta-block">{meta_html}</div>
        <div class="card-actions">
          <button class="save-btn" data-url="{_h(url)}" onclick="toggleFav(event,this)" title="Save">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <circle cx="8" cy="8" r="4.5"/>
              <circle cx="8" cy="8" r="1.8" fill="var(--bg2)" stroke="none"/>
              <rect x="11.5" y="7" width="9" height="2.5" rx="1.25"/>
              <rect x="17" y="9.5" width="2.5" height="2.5" rx="0.8"/>
              <rect x="13.5" y="9.5" width="2.5" height="3.5" rx="0.8"/>
            </svg>
            <span class="save-lbl">Save</span>
          </button>
          <a class="go-link" href="{_h(url)}" target="_blank" onclick="event.stopPropagation()">&#x2197; Go to auction</a>
        </div>
      </div>
    </div>
  </div>
  <div class="expand-panel">
    <div class="expand-inner">
      <div class="stat-row">
        <div class="estat"><div class="estat-label">FMV Range</div><div class="estat-val">{fmv_range_str}</div></div>
        <div class="estat"><div class="estat-label">Comps</div><div class="estat-val">{comp_count} sales</div></div>
        <div class="estat"><div class="estat-label">Days Listed</div><div class="estat-val">{days_listed or "—"}</div></div>
        <div class="estat"><div class="estat-label">Mileage</div><div class="estat-val">{_m(mileage)} mi</div></div>
        <div class="estat"><div class="estat-label">Transmission</div><div class="estat-val">{_h(trans) or "—"}</div></div>
        <div class="estat"><div class="estat-label">Color</div><div class="estat-val">{_h(color) or "—"}</div></div>
      </div>
      <div class="graph-wrap">
        <div class="graph-label">Sold comps vs FMV <span class="graph-sub">{comp_count} transactions</span></div>
        <svg class="graph-svg" data-lid="{listing_id}" data-fmv="{int(fmv_val) if fmv_val else 0}" data-bid="{price_int}" data-low="{int(fmv_low) if fmv_low else 0}" data-high="{int(fmv_high) if fmv_high else 0}"></svg>
      </div>
      <div class="expand-footer">
        <div class="etag-row">{tags_html}</div>
        <div class="expand-actions">{deal_badge_html}<a class="go-btn" href="{_h(url)}" target="_blank" onclick="event.stopPropagation()">&#x2197; Go to auction</a></div>
      </div>
    </div>
  </div>
</div>'''

    dealer   = car.get("dealer", "")
    year     = car.get("year", "")
    model    = car.get("model", "") or ""
    trim     = car.get("trim", "") or ""
    price    = car.get("price")
    mileage  = car.get("mileage")
    url      = car.get("listing_url", "") or "#"
    img      = car.get("image_url", "") or ""
    ends_at  = car.get("auction_ends_at") or ""
    trans    = car.get("transmission", "") or ""
    color    = car.get("color", "") or ""
    body     = car.get("body_style", "") or ""
    tier     = car.get("tier", "") or ""

    # CDN image path fix
    if img and img.startswith("/static/img_cache/"):
        img = "img_cache/" + img.split("/")[-1]

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)

    gen_str    = _gen(year, model)
    src_label  = _badge_label(dealer)

    # Parse end time
    ends_dt = None
    if ends_at:
        try:
            ends_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc)
    urg = _urgency(ends_dt, now_utc)
    stripe_color = _URGENCY_BORDER[urg]
    timer_cls    = _URGENCY_TIMER_CLASS[urg]

    # Urgency label
    urg_label = {
        "critical": "ENDING NOW",
        "soon":     "ENDING TODAY",
        "live":     "",
        "noend":    "NO END TIME",
    }.get(urg, "")

    # Image — PCA Mart needs auth header workaround
    is_pca = "mart.pca.org" in img
    if img and is_pca:
        img_id = f"pcaimg_{abs(hash(img)) % 999999}"
        img_tag = (
            f'<img id="{img_id}" src="{_PLACEHOLDER}" alt="" class="auc-img">'
            f'<script>(function(){{'
            f'var x=new XMLHttpRequest();x.open("GET","{_h(img)}",true);'
            f'x.setRequestHeader("Referer","https://mart.pca.org/");'
            f'x.responseType="blob";'
            f'x.onload=function(){{if(x.status==200){{var u=URL.createObjectURL(x.response);document.getElementById("{img_id}").src=u;}}}};'
            f'x.send();'
            f'}})();</script>'
        )
    elif img:
        img_tag = f'<img src="{_h(img)}" alt="" class="auc-img" loading="lazy" onerror="this.src=\'{_PLACEHOLDER}\'">'
    else:
        img_tag = f'<img src="{_PLACEHOLDER}" alt="" class="auc-img">'

    # Meta chips: transmission · mileage · body
    meta_parts = []
    if trans:   meta_parts.append(_h(trans))
    if mileage: meta_parts.append(f"{_m(mileage)} mi")
    if color:   meta_parts.append(_h(color))
    meta_html = ' <span class="dot">&middot;</span> '.join(meta_parts)

    # Subtitle: badge + gen
    subtitle_parts = [_badge(dealer)]
    if gen_str: subtitle_parts.append(f'<span class="gen-tag">{_h(gen_str)}</span>')
    if urg_label: subtitle_parts.append(f'<span class="urg-tag urg-{urg}">{urg_label}</span>')
    subtitle_html = ' '.join(subtitle_parts)

    # Timer
    if ends_at:
        timer_html = f'<span class="countdown-timer {timer_cls}" data-ends="{_h(ends_at)}">…</span>'
    else:
        timer_html = '<span class="countdown-timer timer-muted">—</span>'

    # FMV display
    fmv_html = _fmv_display(fmv_val, conf, comp_count)

    # data-fmv for sort
    fmv_sort_val = int(fmv_val) if fmv_val and conf != "NONE" else 0


# ── Section builder ───────────────────────────────────────────────────────────

def _section(title, subtitle, cards_html, icon, count, sec_cls="", hide_if_empty=False) -> str:
    if not cards_html:
        if hide_if_empty:
            return ""
        cards_html = ('<div class="empty-state">'
                      '<div class="empty-icon">&#x25CB;</div>'
                      '<div class="empty-text">No auctions in this window</div>'
                      '</div>')
    cls = "auc-section" + (" " + sec_cls if sec_cls else "")
    return (
        f'<div class="{cls}">\n'
        f'  <div class="section-hdr">\n'
        f'    <div class="section-hdr-left">\n'
        f'      <span class="section-icon">{icon}</span>\n'
        f'      <span class="section-title">{title}</span>\n'
        f'      <span class="section-count">{count}</span>\n'
        f'    </div>\n'
        f'    <div class="section-sub">{subtitle}</div>\n'
        f'  </div>\n'
        f'  <div class="cards-list">\n'
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
                    "fmv":         getattr(fmv_obj, "weighted_median", None),
                    "confidence":  getattr(fmv_obj, "confidence", "NONE"),
                    "comp_count":  getattr(fmv_obj, "comp_count", 0),
                    "price_low":   getattr(fmv_obj, "price_low", None),
                    "price_high":  getattr(fmv_obj, "price_high", None),
                    "comp_prices": sorted([
                        int(c.sold_price) for c in getattr(fmv_obj, "comps", [])
                        if getattr(c, "sold_price", None) and int(c.sold_price) > 0
                    ]),
                }
            else:
                fmv_by_id[row["id"]] = {"fmv": None, "confidence": "NONE", "comp_count": 0}

        # Build separate comp_dots dict — written to auction_comps.json, not inlined
        # Build a fast lookup from fmv_scored_list
        _fmv_comps_raw = {}
        for row in fmv_scored_list:
            fmv_obj = row.get("fmv")
            if fmv_obj:
                _fmv_comps_raw[row["id"]] = fmv_obj

        # Active auction listings only
        rows = conn.execute(
            "SELECT * FROM listings WHERE source_category='AUCTION' AND status='active'"
        ).fetchall()
        cars = [dict(r) for r in rows]
        _auction_ids = {c["id"] for c in cars}

        # Build comps_by_id only for displayed auction cars (keeps JSON small)
        comps_by_id = {}
        for lid, fmv_obj in _fmv_comps_raw.items():
            if lid not in _auction_ids:
                continue
            dots = [
                {
                    "price": int(c.sold_price),
                    "date":  getattr(c, "sold_date", None) or "",
                    "img":   getattr(c, "image_url", None) or "",
                    "url":   getattr(c, "listing_url", None) or "",
                    "year":  getattr(c, "year", None) or "",
                    "model": getattr(c, "model", None) or "",
                    "trim":  getattr(c, "trim", None) or "",
                    "mi":    getattr(c, "mileage", None) or "",
                }
                for c in getattr(fmv_obj, "comps", [])
                if getattr(c, "sold_price", None) and int(c.sold_price) > 0
                   and getattr(c, "sold_date", None)
            ][:80]
            if dots:
                comps_by_id[str(lid)] = dots

        # Stats for header bar
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

        # Recently ended — archived in last 7 days
        ended_rows = conn.execute(
            """SELECT * FROM listings
               WHERE source_category='AUCTION' AND status='sold'
               AND archived_at >= datetime('now', '-7 days')
               AND (auction_ends_at IS NULL OR auction_ends_at <= datetime('now'))
               ORDER BY archived_at DESC LIMIT 100"""
        ).fetchall()
        ended_cars = [dict(r) for r in ended_rows]

    def _parse_ends(s):
        if not s: return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    # Attach FMV + parsed end time to each car
    for c in cars:
        c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})
        c["_ends_dt"] = _parse_ends(c.get("auction_ends_at"))

    for c in ended_cars:
        c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})
        c["_ends_dt"] = _parse_ends(c.get("auction_ends_at"))

    # Bucket by urgency
    ending_critical = []
    ending_soon     = []
    live_auction    = []
    no_end_time     = []

    for c in cars:
        urg = _urgency(c["_ends_dt"], now_utc)
        if urg == "critical":  ending_critical.append(c)
        elif urg == "soon":    ending_soon.append(c)
        elif urg == "noend":   no_end_time.append(c)
        else:                  live_auction.append(c)

    # Sort each bucket by end time ascending
    def _sort_key(c):
        d = c.get("_ends_dt")
        return d if d else datetime(9999, 12, 31, tzinfo=timezone.utc)

    ending_critical.sort(key=_sort_key)
    ending_soon.sort(key=_sort_key)
    live_auction.sort(key=_sort_key)

    # Unique sources for filter chips — auction houses only
    _AUCTION_SOURCES = {"BaT", "C&B", "pcarmarket"}
    all_srcs = sorted(
        s for s in set(
            _badge_label(c.get("dealer", "")) for c in cars
        )
        if s in _AUCTION_SOURCES
    )
    src_chips_html = "".join(
        f'<button class="filter-chip" data-val="{_h(s)}" data-filter="src" onclick="toggleChip(this)">{_h(s)}</button>'
        for s in all_srcs
    )

    def _cards(lst, hero_first=False):
        out = []
        for i, c in enumerate(lst):
            out.append(_auction_card(c, c["_fmv"], is_hero=(hero_first and i == 0)))
        return "\n".join(out)

    # Hero = soonest card across critical → soon → live buckets
    _hero_in_critical = bool(ending_critical)
    _hero_in_soon     = bool(ending_soon) and not _hero_in_critical
    _hero_in_live     = bool(live_auction) and not _hero_in_critical and not _hero_in_soon

    s_critical = _section("Ending Now",    "< 3 hours",           _cards(ending_critical, hero_first=_hero_in_critical), "&#x25CF;", len(ending_critical), "sec-critical", hide_if_empty=True)
    s_ending   = _section("Ending Today",  "3 &ndash; 24 hours",  _cards(ending_soon,     hero_first=_hero_in_soon),     "&#x25CF;", len(ending_soon),     "sec-soon",     hide_if_empty=True)
    s_live     = _section("Live Auctions", "Ending beyond 24h",   _cards(live_auction,    hero_first=_hero_in_live),     "&#x25CB;", len(live_auction))
    s_noend    = _section("No End Time",   "End time unknown",     _cards(no_end_time),                                  "&#x25A1;", len(no_end_time),     "", hide_if_empty=True)
    s_ended    = _section("Recently Ended","Final hammer prices",  _cards(ended_cars),                                   "&#x25A0;", len(ended_cars),      "sec-ended",    hide_if_empty=True)

    total   = len(cars)
    now_str = now_utc.strftime("%b %d %H:%M UTC")

    html = _build_html(
        s_critical, s_ending, s_live, s_noend, s_ended,
        total, len(ending_critical), len(ending_soon), len(live_auction), len(ended_cars),
        now_str, n_listings_total, n_comps_total, n_new_today, n_deals,
        src_chips_html
    )
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[auction_dashboard] wrote {OUT_PATH} ({total} auctions)")

    # Write comp dots to separate JSON — fetched lazily by JS on card expand
    import json as _json
    comps_json_path = BASE_DIR / "docs" / "auction_comps.json"
    comps_json_path.write_text(_json.dumps(comps_by_id), encoding="utf-8")

    # ── Mirror to rennauktion repo ────────────────────────────────────────────
    _push_rennauktion(html)

    return html


def _push_rennauktion(html: str) -> None:
    """Write rebranded index.html to the rennauktion repo clone and git push."""
    import subprocess, shutil
    RENN_CLONE = Path("/tmp/rennauktion_clone")
    RENN_REPO  = "https://github.com/OCX11/rennauktion.git"

    try:
        # Clone if not present; pull if stale
        if not (RENN_CLONE / ".git").exists():
            subprocess.run(["git", "clone", RENN_REPO, str(RENN_CLONE)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(RENN_CLONE), "config",
                            "user.email", "openclawx1@protonmail.com"], check=True)
            subprocess.run(["git", "-C", str(RENN_CLONE), "config",
                            "user.name", "OCX11"], check=True)
        else:
            subprocess.run(["git", "-C", str(RENN_CLONE), "pull", "--ff-only"],
                           check=True, capture_output=True)

        # Rebrand: swap RennMarkt → RennAuktion in the live HTML
        branded = html\
            .replace('<tspan font-weight="800" fill="white">Renn</tspan>'
                     '<tspan font-weight="300" fill="#D85A30">Markt</tspan>',
                     '<tspan font-weight="800" fill="white">Renn</tspan>'
                     '<tspan font-weight="300" fill="#D85A30">Auktion</tspan>')\
            .replace('RennMarkt \u2014 Auction Watcher',
                     'RennAuktion \u2014 Porsche Auction Intelligence')\
            .replace('RennMarkt Auctions', 'RennAuktion')\
            .replace('<a class="dd-item" href="index.html">\U0001f3ce\ufe0f Market</a>',
                     '<a class="dd-item" href="https://www.rennmarkt.net">\U0001f3ce\ufe0f RennMarkt</a>')\
            .replace('<a class="logo" href="index.html">', '<a class="logo" href="/">')\
            .replace('href="index.html"', 'href="/"')

        # Fix nav links (point to rennmarkt.net equivalents) + add My Saved
        branded = branded \
            .replace('href="search.html"',       'href="https://rennmarkt.net/search.html"') \
            .replace('href="calculator.html"',   'href="https://rennmarkt.net/calculator.html"') \
            .replace('href="market_report.html"','href="https://rennmarkt.net/market_report.html"') \
            .replace('href="notify.html"',        'href="https://rennmarkt.net/notify.html"')

        # Add My Saved link to dropdown (after Market link, first occurrence only)
        branded = branded.replace(
            '<a class="dd-item" href="https://www.rennmarkt.net">\U0001f3ce\ufe0f RennMarkt</a>',
            '<a class="dd-item" href="https://www.rennmarkt.net">\U0001f3ce\ufe0f RennMarkt</a>\n    <a class="dd-item" href="saved.html">&#x2665; My Saved</a>',
            1
        )

        # Inject Supabase CDN + auth.js + live stats loader before </body>
        auth_scripts = (
            '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>\n'
            '<script src="/auth.js"></script>\n'
            '<script>\n'
            '(async()=>{try{const r=await fetch(\'https://raw.githubusercontent.com/OCX11/rennmarkt/main/docs/stats.json\',{cache:\'no-store\'});'
            'if(!r.ok)return;const s=await r.json();'
            'const fmt=n=>n>=1000?n.toLocaleString():String(n);'
            'const set=(id,v)=>{const el=document.getElementById(id);if(el&&v!=null)el.textContent=fmt(v);};'
            'set(\'s-active\',s.n_active);set(\'s-new\',s.n_new);set(\'s-auctions\',s.n_auctions);'
            'set(\'s-comps\',s.n_comps);set(\'s-deals\',s.n_deals);}catch(e){}})();\n'
            '</script>\n'
        )
        branded = branded.replace('</body>', auth_scripts + '</body>')

        out = RENN_CLONE / "index.html"
        out.write_text(branded, encoding="utf-8")

        subprocess.run(["git", "-C", str(RENN_CLONE), "add", "index.html"], check=True)
        result = subprocess.run(
            ["git", "-C", str(RENN_CLONE), "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode != 0:  # changes staged
            subprocess.run(
                ["git", "-C", str(RENN_CLONE), "commit", "-m", "Dashboard update"],
                check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(RENN_CLONE), "push"],
                check=True, capture_output=True
            )
            print("[rennauktion] pushed index.html")
        else:
            print("[rennauktion] no changes, skipping push")

    except Exception as exc:
        print(f"[rennauktion] push failed (non-fatal): {exc}")


# ── HTML template ─────────────────────────────────────────────────────────────

def _build_html(s_critical, s_ending, s_live, s_noend, s_ended,
                total, n_critical, n_ending, n_live, n_ended,
                now_str, n_listings_total, n_comps_total, n_new_today, n_deals,
                src_chips_html) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script>(function(){{var t=localStorage.getItem('ptox_theme');if(t)document.documentElement.dataset.theme=t;}})()</script>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="RennMarkt Auctions">
<meta name="theme-color" content="#0A0A0C">
<title>RennMarkt &mdash; Auction Watcher</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --red:    #c0392b;
  --bg:     #0d0d0d;
  --bg2:    #141414;
  --bg3:    #1c1c1c;
  --border: #242424;
  --border2:#2e2e2e;
  --text:   #e2ddd8;
  --muted:  #6a6560;
  --green:  #4ade80;
  --amber:  #EAB308;
  --blue:   #60a5fa;
}}
[data-theme="racing"] {{ --red:#e53e3e; --bg:#0c0809; --bg2:#160d0e; --bg3:#1e1213; --border:#2e1a1a; }}
[data-theme="gulf"]   {{ --red:#2563eb; --bg:#08100c; --bg2:#0e1810; --bg3:#142016; --border:#1a3020; }}
[data-theme="olive"]  {{ --red:#65a30d; --bg:#0a0c08; --bg2:#12140e; --bg3:#1a1c14; --border:#252a1a; }}
[data-theme="purple"] {{ --red:#7c3aed; --bg:#09080d; --bg2:#100e16; --bg3:#17141e; --border:#221e2e; }}
[data-theme="light"]  {{ --red:#c0392b; --bg:#f5f4f2; --bg2:#edebe8; --bg3:#e2dfdb; --border:#ccc9c4; --text:#1a1814; --muted:#7a756e; }}

*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.5; min-height:100vh; }}
a {{ color:inherit; text-decoration:none; }}

/* ── Topbar ── */
.topbar {{
  height:64px; background:var(--bg2); border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  padding:0 20px; position:sticky; top:0; z-index:50;
}}
.logo {{ display:flex; align-items:center; line-height:0; }}
.logo svg {{ height:52px; width:auto; }}
.topbar-right {{ display:flex; align-items:center; gap:12px; }}
.topbar-time {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); }}
.more-btn {{ padding:6px 10px; border-radius:6px; font-size:11px; color:var(--muted); background:transparent; border:1px solid var(--border2); cursor:pointer; }}
.more-btn:hover {{ color:var(--text); border-color:#444; }}

/* ── Stats bar ── */
.stats-bar {{ display:flex; gap:1px; background:var(--border); border-bottom:1px solid var(--border); }}
.stat-cell {{ flex:1; padding:10px 8px 9px; text-align:center; background:var(--bg2); cursor:pointer; transition:background 0.12s; text-decoration:none; color:inherit; position:relative; }}
.stat-cell:hover {{ background:var(--bg3); }}
.stat-cell.active {{ background:var(--bg3); }}
.stat-cell.active::after {{ content:''; position:absolute; bottom:0; left:0; right:0; height:2px; background:var(--red); }}
.stat-num {{ font-family:'DM Mono',monospace; font-size:18px; font-weight:500; letter-spacing:-0.5px; line-height:1.1; color:var(--text); }}
.stat-num.c-green {{ color:var(--green); }}
.stat-num.c-red   {{ color:var(--red); }}
.stat-lbl {{ font-size:9px; font-weight:500; letter-spacing:1.2px; text-transform:uppercase; color:var(--muted); margin-top:2px; }}

/* ── Filter + sort bar ── */
.filter-bar {{
  background:var(--bg2); border-bottom:1px solid var(--border);
  padding:8px 16px; display:flex; flex-wrap:wrap; align-items:center; gap:6px;
}}
.filter-section {{ display:flex; gap:5px; align-items:center; }}
.filter-label {{ font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); letter-spacing:1px; text-transform:uppercase; white-space:nowrap; margin-right:2px; }}
.filter-chip {{
  padding:3px 10px; border-radius:4px; border:1px solid var(--border2);
  background:transparent; color:var(--muted); font-family:'DM Mono',monospace;
  font-size:10px; font-weight:500; cursor:pointer; transition:all 0.12s; white-space:nowrap;
}}
.filter-chip:hover {{ color:var(--text); border-color:#444; }}
.filter-chip.active {{ background:rgba(192,57,43,0.12); border-color:var(--red); color:var(--red); }}
.filter-sep {{ width:1px; height:16px; background:var(--border2); margin:0 4px; }}
.sort-select {{
  padding:3px 8px; border:1px solid var(--border2); border-radius:4px;
  background:var(--bg3); color:var(--muted); font-family:'DM Mono',monospace;
  font-size:10px; cursor:pointer; outline:none;
}}
.sort-select:focus {{ border-color:var(--red); }}
.filter-count {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-left:auto; }}
.live-dot {{ display:inline-block; width:5px; height:5px; border-radius:50%; background:var(--green); margin-right:4px; animation:pulse 1.8s infinite; }}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}

/* ── Dropdown ── */
.dd-overlay {{ display:none; }}
.dd-overlay.show {{ display:block; }}
.dd {{ position:fixed; right:12px; top:50px; background:var(--bg3); border:1px solid var(--border2); border-radius:8px; padding:5px; min-width:170px; box-shadow:0 8px 24px rgba(0,0,0,0.5); z-index:200; }}
.dd-item {{ padding:9px 12px; font-size:13px; color:var(--muted); border-radius:5px; cursor:pointer; display:flex; align-items:center; gap:8px; }}
.dd-item:hover {{ background:var(--bg2); color:var(--text); }}
.dd-divider {{ height:1px; background:var(--border); margin:4px 8px; }}
.dd-backdrop {{ position:fixed; inset:0; z-index:199; }}

/* ── Page body ── */
.page {{ max-width:900px; margin:0 auto; padding:20px 16px 60px; }}

/* ── Section ── */
.auc-section {{ margin-bottom:28px; }}
.section-hdr {{
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border);
}}
.section-hdr-left {{ display:flex; align-items:center; gap:8px; }}
.section-icon {{ font-size:8px; }}
.section-title {{ font-family:'Syne',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.5px; color:var(--text); text-transform:uppercase; }}
.section-count {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); background:var(--bg3); border:1px solid var(--border); padding:1px 6px; border-radius:3px; }}
.section-sub {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); }}
.sec-critical .section-icon {{ color:var(--red); }}
.sec-critical .section-title {{ color:var(--red); }}
.sec-critical .section-count {{ background:rgba(192,57,43,0.1); border-color:rgba(192,57,43,0.3); color:var(--red); }}
.sec-soon .section-icon {{ color:var(--amber); }}
.sec-soon .section-title {{ color:var(--amber); }}
.sec-ended {{ opacity:0.7; }}

/* ── Cards list — single column ── */
.cards-list {{ display:flex; flex-direction:column; gap:6px; padding:4px 2px; }}

/* ── Auction card ── */
.auc-card {{
  background:var(--bg2); border:1px solid var(--border); border-radius:10px;
  overflow:hidden; cursor:default; display:flex; flex-direction:column;
  transition:border-color 0.35s cubic-bezier(0.34,1.56,0.64,1),
             box-shadow   0.35s cubic-bezier(0.34,1.56,0.64,1),
             transform    0.35s cubic-bezier(0.34,1.56,0.64,1);
  transform:translateY(0) scale(1);
}}
.auc-card:hover {{
  border-color:var(--red);
  box-shadow:0 12px 40px rgba(0,0,0,0.45), 0 4px 12px rgba(192,57,43,0.15);
  transform:translateY(-5px) scale(1.01);
}}
.auc-card--hero {{ border-color:rgba(192,57,43,0.3); }}

/* ── Card main row ── */
.card-main {{ display:flex; height:130px; flex-shrink:0; }}
.img-wrap {{ width:200px; min-width:200px; overflow:hidden; background:var(--bg3); flex-shrink:0; cursor:pointer; }}
.auc-img {{ width:100%; height:100%; object-fit:cover; display:block; opacity:0.87; transition:transform 0.35s cubic-bezier(0.34,1.56,0.64,1),opacity 0.2s; }}
.auc-card:hover .auc-img {{ transform:scale(1.08); opacity:0.95; }}
.card-body {{ flex:1; padding:11px 14px; display:flex; flex-direction:column; justify-content:space-between; min-width:0; cursor:pointer; }}
.card-top {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:4px; }}
.card-subtitle {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
.card-top-right {{ display:flex; align-items:center; gap:8px; flex-shrink:0; }}
.card-title {{ font-family:'DM Sans',sans-serif; font-size:16px; font-weight:500; color:#f0ece6; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:0; }}
.card-bottom {{ display:flex; align-items:center; gap:16px; }}
.bid-block, .fmv-block {{ flex-shrink:0; }}
.val-label {{ font-family:'DM Mono',monospace; font-size:11px; color:#888; letter-spacing:0.5px; margin-bottom:2px; }}
.bid-val {{ font-family:'DM Mono',monospace; font-size:22px; font-weight:500; color:#fff; letter-spacing:-0.5px; line-height:1.1; transition:color 0.2s ease, transform 0.2s ease; display:inline-block; }}
.auc-card:hover .bid-val {{ color:var(--red); transform:scale(1.04); }}
.fmv-row {{ display:flex; align-items:center; gap:5px; }}
.fmv-est {{ font-family:'DM Mono',monospace; font-size:17px; color:#9a958f; letter-spacing:-0.3px; }}
.fmv-meta {{ font-family:'DM Mono',monospace; font-size:11px; color:#666; display:flex; align-items:center; gap:3px; }}
.fmv-none {{ font-family:'DM Mono',monospace; font-size:12px; color:#555; }}
.divider-vert {{ width:1px; height:32px; background:var(--border2); flex-shrink:0; }}
.meta-block {{ font-family:'DM Mono',monospace; font-size:11px; color:#6a6560; margin-left:auto; text-align:right; line-height:1.7; }}
.dot {{ color:#444; }}
/* card-actions: stacks save + go-to vertically, right-aligned */
.card-actions {{ display:flex; flex-direction:column; align-items:flex-end; gap:5px; flex-shrink:0; }}
.save-btn {{
  display:inline-flex; align-items:center; gap:5px;
  background:transparent; border:1px solid #2a2a2a; border-radius:4px;
  padding:3px 9px; cursor:pointer; white-space:nowrap;
  font-family:'DM Mono',monospace; font-size:10px; color:#666;
  transition:all 0.15s;
}}
.save-btn:hover {{ color:var(--red); border-color:var(--red); }}
.save-btn.active {{ color:var(--red); border-color:var(--red); background:rgba(192,57,43,0.08); }}
.save-btn svg {{ width:12px; height:12px; flex-shrink:0; transition:fill 0.15s, stroke 0.15s; }}
.save-btn:not(.active) svg {{ fill:none; stroke:currentColor; stroke-width:1.5; }}
.save-btn.active svg {{ fill:var(--red); stroke:var(--red); stroke-width:1; }}
.save-lbl {{ font-size:10px; }}
.go-link {{ font-family:'DM Mono',monospace; font-size:10px; color:#555; border:1px solid #2a2a2a; border-radius:4px; padding:3px 9px; white-space:nowrap; text-decoration:none; transition:all 0.15s; flex-shrink:0; }}
.go-link:hover {{ color:var(--red); border-color:var(--red); }}

/* ── Expand panel ── */
.expand-panel {{ max-height:0; overflow:hidden; transition:max-height 0.35s cubic-bezier(0.4,0,0.2,1); border-top:0px solid var(--border); background:#0f0f0f; }}
.auc-card:hover .expand-panel {{ max-height:360px; border-top-width:1px; }}
.expand-inner {{ padding:14px 18px 16px; display:flex; flex-direction:column; gap:12px; }}
.stat-row {{ display:flex; gap:0; }}
.estat {{ flex:1; padding:0 14px; border-right:1px solid #1e1e1e; }}
.estat:first-child {{ padding-left:0; }}
.estat:last-child {{ border-right:none; }}
.estat-label {{ font-family:'DM Mono',monospace; font-size:10px; color:#5a5a5a; letter-spacing:.6px; text-transform:uppercase; margin-bottom:3px; }}
.estat-val {{ font-family:'DM Mono',monospace; font-size:13px; color:#c8c4be; font-weight:500; }}
.graph-wrap {{ width:100%; }}
.graph-label {{ font-family:'DM Mono',monospace; font-size:10px; color:#5a5a5a; letter-spacing:.6px; text-transform:uppercase; margin-bottom:6px; display:flex; justify-content:space-between; }}
.graph-sub {{ color:#555; letter-spacing:0; text-transform:none; }}
.graph-svg {{ width:100%; height:160px; display:block; }}
.expand-footer {{ display:flex; align-items:center; justify-content:space-between; }}
.etag-row {{ display:flex; gap:5px; flex-wrap:wrap; }}
.etag {{ font-family:'DM Mono',monospace; font-size:10px; color:#555; background:#1a1a1a; border:1px solid #242424; padding:2px 7px; border-radius:3px; }}
.expand-actions {{ display:flex; align-items:center; gap:10px; }}
.deal-badge {{ font-family:'DM Mono',monospace; font-size:10px; color:#4ade80; background:rgba(74,222,128,.08); border:1px solid rgba(74,222,128,.2); padding:3px 10px; border-radius:4px; }}
.above-badge {{ font-family:'DM Mono',monospace; font-size:10px; color:#F87171; background:rgba(248,113,113,.08); border:1px solid rgba(248,113,113,.2); padding:3px 10px; border-radius:4px; }}
.go-btn {{ display:inline-flex; align-items:center; gap:5px; font-family:'DM Mono',monospace; font-size:11px; color:#fff; background:var(--red); border:none; border-radius:5px; padding:6px 14px; cursor:pointer; text-decoration:none; transition:background .15s; }}
.go-btn:hover {{ background:#a93226; }}

/* ── Badges + tags ── */
.src-badge {{ font-family:'DM Mono',monospace; font-size:10px; font-weight:500; padding:2px 7px; border-radius:3px; }}
.gen-tag {{ font-family:'DM Mono',monospace; font-size:10px; color:#585450; }}
.urg-tag {{ font-family:'DM Mono',monospace; font-size:9px; font-weight:700; letter-spacing:1px; padding:1px 5px; border-radius:2px; }}
.urg-critical {{ color:var(--red); background:rgba(192,57,43,0.12); }}
.urg-soon {{ color:var(--amber); background:rgba(234,179,8,0.1); }}

/* ── Fav button ── */
.fav-btn {{ background:transparent; border:none; cursor:pointer; padding:0 2px; line-height:1; transition:transform 0.12s; flex-shrink:0; display:flex; align-items:center; }}
.fav-btn:hover {{ transform:scale(1.15); }}
.fav-btn svg {{ width:15px; height:15px; transition:fill 0.15s, stroke 0.15s; }}
.fav-btn:not(.active) svg {{ fill:none; stroke:#3a3a3a; stroke-width:1.5; }}
.fav-btn.active svg {{ fill:var(--red); stroke:var(--red); stroke-width:1; }}

/* View toggle tabs (Live / Ended / Favorites) */
.view-tabs {{ display:flex; gap:1px; background:var(--border); border-bottom:1px solid var(--border); }}
.view-tab {{
  flex:1; padding:9px 8px 8px; text-align:center; background:var(--bg2);
  cursor:pointer; border:none; color:var(--muted); font-family:'DM Mono',monospace;
  font-size:10px; font-weight:500; letter-spacing:0.8px; text-transform:uppercase;
  transition:background 0.12s,color 0.12s; position:relative;
}}
.view-tab:hover {{ background:var(--bg3); color:var(--text); }}
.view-tab.active {{ background:var(--bg3); color:var(--text); }}
.view-tab.active::after {{ content:''; position:absolute; bottom:0; left:0; right:0; height:2px; background:var(--red); }}
.view-tab .tab-count {{ font-size:9px; color:var(--muted); margin-left:5px; }}
.view-tab.active .tab-count {{ color:var(--red); }}

/* Ended section — muted bid label */
.sec-ended .val-label::after {{ content:' (final)'; }}

/* Confidence pip */
.conf-pip {{ display:inline-block; width:5px; height:5px; border-radius:50%; flex-shrink:0; }}
.conf-high {{ background:var(--green); }}
.conf-med  {{ background:var(--amber); }}
.conf-low  {{ background:#F87171; }}

/* Countdown timer */
.countdown-timer {{ font-family:'DM Mono',monospace; font-size:14px; font-weight:500; font-variant-numeric:tabular-nums; }}
.timer-red   {{ color:var(--red); animation:timerPulse 1s infinite; }}
.timer-amber {{ color:var(--amber); }}
.timer-green {{ color:var(--green); }}
.timer-muted {{ color:#555; }}
@keyframes timerPulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.45}} }}

/* Empty state */
.empty-state {{ padding:32px 20px; text-align:center; }}
.empty-icon {{ font-size:20px; color:var(--border2); margin-bottom:8px; }}
.empty-text {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); }}

/* Scrollbar */
::-webkit-scrollbar {{ width:4px; height:4px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:var(--border2); border-radius:2px; }}

@media(max-width:640px) {{
  .topbar-time {{ display:none; }}
  .img-wrap {{ width:120px; min-width:120px; }}
  .auc-card {{ height:auto; min-height:110px; }}
  .card-bottom {{ flex-wrap:wrap; gap:10px; }}
  .meta-block {{ width:100%; text-align:left; margin-left:0; }}
  .page {{ padding:12px 10px 48px; }}
  .stat-num {{ font-size:15px; }}
  .bid-val {{ font-size:16px; }}
  .fmv-est {{ font-size:13px; }}
}}
</style>
</head>
<body>

<header class="topbar">
  <a class="logo" href="index.html">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 280 64"><g transform="translate(8,6)"><circle cx="26" cy="26" r="22" fill="none" stroke="#242424" stroke-width="2.5"/><path d="M6,38 A22,22 0 0,1 43.5,8.5" fill="none" stroke="#D85A30" stroke-width="2.5" stroke-linecap="round"/><g stroke="#333" stroke-width="1.2" stroke-linecap="round"><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-80,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-55,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-30,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-5,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(20,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(45,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(70,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(95,26,26)"/></g><line x1="26" y1="26" x2="43.5" y2="8.5" stroke="white" stroke-width="1.8" stroke-linecap="round"/><circle cx="26" cy="26" r="4" fill="#D85A30"/><circle cx="26" cy="26" r="1.6" fill="#0d0d0d"/><text x="62" y="34" font-family="'Helvetica Neue',Arial,sans-serif" font-size="32" letter-spacing="-0.5"><tspan font-weight="800" fill="white">Renn</tspan><tspan font-weight="300" fill="#D85A30">Markt</tspan></text></g></svg>
  </a>
  <div class="topbar-right">
    <span class="topbar-time"><span class="live-dot"></span>{now_str}</span>
    <button class="more-btn" onclick="toggleDD()">More &#x25BE;</button>
  </div>
</header>

<div class="dd-overlay" id="dd-overlay">
  <div class="dd-backdrop" onclick="closeDD()"></div>
  <div class="dd">
    <a class="dd-item" href="index.html">&#x1F3CE; Market</a>
    <a class="dd-item" href="https://rennauktion.com/saved.html">&#x2665; My Saved</a>
    <a class="dd-item" href="search.html">&#x1F50D; Search</a>
    <div class="dd-divider"></div>
    <a class="dd-item" href="calculator.html">&#x1F4B0; FMV Calculator</a>
    <a class="dd-item" href="market_report.html">&#x1F4CA; Market Report</a>
    <a class="dd-item" href="notify.html">&#x1F514; Notifications</a>
    <div class="dd-divider"></div>
    <div class="dd-item" onclick="cycleTheme()">&#x1F3A8; Theme</div>
  </div>
</div>

<div class="stats-bar">
  <a class="stat-cell" href="index.html">
    <div class="stat-num" id="s-active">{n_listings_total:,}</div>
    <div class="stat-lbl">Active</div>
  </a>
  <a class="stat-cell" href="index.html">
    <div class="stat-num" id="s-new">{n_new_today}</div>
    <div class="stat-lbl">New Today</div>
  </a>
  <div class="stat-cell active">
    <div class="stat-num c-red" id="s-auctions">{total}</div>
    <div class="stat-lbl">Auctions</div>
  </div>
  <a class="stat-cell" href="index.html#comps">
    <div class="stat-num" id="s-comps">{n_comps_total:,}</div>
    <div class="stat-lbl">Comps</div>
  </a>
  <a class="stat-cell" href="index.html">
    <div class="stat-num c-green" id="s-deals">{n_deals}</div>
    <div class="stat-lbl">Deals</div>
  </a>
</div>

<div class="view-tabs">
  <button class="view-tab active" data-view="live" onclick="switchView('live',this)">
    Live <span class="tab-count" id="tab-count-live">{total}</span>
  </button>
  <button class="view-tab" data-view="ended" onclick="switchView('ended',this)">
    Ended <span class="tab-count" id="tab-count-ended">{n_ended}</span>
  </button>
  <button class="view-tab" data-view="favs" onclick="switchView('favs',this)">
    Saved <span class="tab-count" id="tab-count-favs">0</span>
  </button>
</div>

<div class="filter-bar">
  <span class="filter-label">Source</span>
  <div class="filter-section" id="src-chips">
    {src_chips_html}
  </div>
  <div class="filter-sep"></div>
  <span class="filter-label">Sort</span>
  <select class="sort-select" id="auc-sort" onchange="applyFilters()">
    <option value="ends_asc">Ending Soonest</option>
    <option value="listed_desc">Newest Listed</option>
    <option value="fmv_desc">FMV High&#x2192;Low</option>
    <option value="price_asc">Bid Low&#x2192;High</option>
    <option value="price_desc">Bid High&#x2192;Low</option>
    <option value="mileage_asc">Mileage Low&#x2192;High</option>
  </select>
  <span class="filter-count" id="filter-count"></span>
</div>

<div class="page" id="page-body">
  {s_critical}
  {s_ending}
  {s_live}
  {s_noend}
  {s_ended}
</div>

<script>
// ── Countdown ────────────────────────────────────────────────────────────────
function pad(n) {{ return n < 10 ? '0'+n : ''+n; }}

function fmtSecs(secs) {{
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
  document.querySelectorAll('.countdown-timer[data-ends]').forEach(function(el) {{
    var secs = Math.floor((new Date(el.dataset.ends).getTime() - now) / 1000);
    el.textContent = fmtSecs(secs);
    if (!el._titled) {{
      el.title = 'Ends ' + new Date(el.dataset.ends).toLocaleString([],{{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}});
      el._titled = true;
    }}
  }});
}}
tickAll();
setInterval(tickAll, 1000);

// ── Filter + sort ─────────────────────────────────────────────────────────────
var _srcs = [];
var _curView = 'live';

function toggleChip(btn) {{
  var val = btn.dataset.val;
  btn.classList.toggle('active');
  var idx = _srcs.indexOf(val);
  if (idx >= 0) _srcs.splice(idx, 1); else _srcs.push(val);
  applyFilters();
}}

function applyFilters() {{
  var sort = (document.getElementById('auc-sort')||{{}}).value || 'ends_asc';
  var cards = Array.from(document.querySelectorAll('.auc-card'));
  var vis = 0;
  var favs = _getFavs();

  cards.forEach(function(c) {{
    var srcOk = _srcs.length === 0 || _srcs.includes(c.dataset.src);
    var viewOk = true;
    if (_curView === 'live')  viewOk = !c.closest('.sec-ended');
    if (_curView === 'ended') viewOk = !!c.closest('.sec-ended');
    if (_curView === 'favs')  viewOk = favs.includes(c.dataset.url || '');
    var show = srcOk && viewOk;
    c.style.display = show ? '' : 'none';
    if (show) vis++;
  }});

  document.querySelectorAll('.cards-list').forEach(function(list) {{
    var shown = Array.from(list.querySelectorAll('.auc-card')).filter(function(c){{ return c.style.display !== 'none'; }});
    shown.sort(function(a,b) {{
      if (sort === 'ends_asc')    return (a.dataset.ends||'z') < (b.dataset.ends||'z') ? -1 : 1;
      if (sort === 'listed_desc') return (b.dataset.listed||'') > (a.dataset.listed||'') ? 1 : -1;
      if (sort === 'fmv_desc')    return +b.dataset.fmv - +a.dataset.fmv;
      if (sort === 'price_asc')   return +a.dataset.price - +b.dataset.price;
      if (sort === 'price_desc')  return +b.dataset.price - +a.dataset.price;
      if (sort === 'mileage_asc') return +a.dataset.mileage - +b.dataset.mileage;
      return 0;
    }});
    shown.forEach(function(c) {{ list.appendChild(c); }});
  }});

  var el = document.getElementById('filter-count');
  if (el) el.textContent = (_srcs.length || _curView !== 'live') ? vis + ' shown' : '';

  // Hide section headers when all their cards are hidden
  document.querySelectorAll('.auc-section').forEach(function(sec) {{
    var anyVis = Array.from(sec.querySelectorAll('.auc-card')).some(function(c){{ return c.style.display !== 'none'; }});
    sec.style.display = anyVis ? '' : 'none';
  }});

  // Favs empty state
  var favsEmpty = document.getElementById('favs-empty');
  if (_curView === 'favs' && vis === 0) {{
    if (!favsEmpty) {{
      favsEmpty = document.createElement('div');
      favsEmpty.id = 'favs-empty';
      favsEmpty.className = 'empty-state';
      favsEmpty.innerHTML = '<div class="empty-icon">&#x2661;</div><div class="empty-text">No saved lots yet — tap &#x2661; on any card</div>';
      document.getElementById('page-body').prepend(favsEmpty);
    }}
  }} else if (favsEmpty) {{
    favsEmpty.remove();
  }}
}}

// ── View tabs ─────────────────────────────────────────────────────────────────
function switchView(view, btn) {{
  _curView = view;
  document.querySelectorAll('.view-tab').forEach(function(t){{ t.classList.remove('active'); }});
  btn.classList.add('active');
  applyFilters();
}}

// ── Favorites ─────────────────────────────────────────────────────────────────
var _FAV_KEY = 'ptox_auction_favs';

function _getFavs() {{
  try {{ return JSON.parse(localStorage.getItem(_FAV_KEY) || '[]'); }} catch(e) {{ return []; }}
}}
function _setFavs(arr) {{
  try {{ localStorage.setItem(_FAV_KEY, JSON.stringify(arr)); }} catch(e) {{}}
}}

function _updateFavCount() {{
  var n = _getFavs().length;
  var el = document.getElementById('tab-count-favs');
  if (el) el.textContent = n || '0';
}}

function initFavs() {{
  var favs = _getFavs();
  favs.forEach(function(url) {{
    document.querySelectorAll('.fav-btn[data-url], .save-btn[data-url]').forEach(function(b) {{
      if (b.dataset.url === url) b.classList.add('active');
    }});
  }});
  _updateFavCount();
}}

function toggleFav(evt, btn) {{
  evt.stopPropagation();
  var url = btn.dataset.url;
  var favs = _getFavs();
  var idx = favs.indexOf(url);
  var adding = idx < 0;
  if (adding) favs.push(url); else favs.splice(idx, 1);
  _setFavs(favs);
  // sync all buttons for this url (fav-btn + save-btn)
  document.querySelectorAll('[data-url]').forEach(function(b) {{
    if (b.dataset.url === url && (b.classList.contains('fav-btn') || b.classList.contains('save-btn'))) {{
      b.classList.toggle('active', adding);
    }}
  }});
  _updateFavCount();
  if (_curView === 'favs') applyFilters();
}}

function cardClick(evt, url) {{
  if (evt.target.closest('.fav-btn')) return;
  window.open(url, '_blank');
}}


// ── Pull-to-refresh ───────────────────────────────────────────────────────────
(function() {{
  var PTR = 80, startY = 0, pulling = false, ind = null;
  function getInd() {{
    if (!ind) {{
      ind = document.createElement('div');
      ind.style.cssText = 'position:fixed;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#c0392b,#EAB308);transform:scaleX(0);transform-origin:left;transition:transform 0.12s ease,opacity 0.3s;z-index:9999;pointer-events:none;opacity:0';
      document.body.appendChild(ind);
    }}
    return ind;
  }}
  function setP(r) {{
    var el = getInd();
    el.style.opacity = r > 0 ? '1' : '0';
    el.style.transform = 'scaleX(' + Math.min(r,1) + ')';
    el.style.transition = r > 0 ? 'none' : 'transform 0.12s ease,opacity 0.3s';
  }}
  document.addEventListener('touchstart', function(e) {{
    if (window.scrollY === 0 && e.touches.length === 1) {{ startY = e.touches[0].clientY; pulling = true; }}
  }}, {{passive:true}});
  document.addEventListener('touchmove', function(e) {{
    if (!pulling) return;
    var dy = e.touches[0].clientY - startY;
    if (dy <= 0) {{ pulling = false; setP(0); return; }}
    setP(dy / PTR);
  }}, {{passive:true}});
  document.addEventListener('touchend', function(e) {{
    if (!pulling) return;
    pulling = false;
    if (e.changedTouches[0].clientY - startY >= PTR) {{
      setP(1);
      getInd().style.background = '#4ade80';
      setTimeout(function(){{ location.reload(); }}, 280);
    }} else {{ setP(0); }}
  }}, {{passive:true}});
}})();

// ── Dot graph — date × price scatter with photo tooltip (classic.com style) ──
var _ttDiv = null;
function _getTooltip() {{
  if (!_ttDiv) {{
    _ttDiv = document.createElement('div');
    _ttDiv.id = 'auc-tt';
    _ttDiv.style.cssText = [
      'position:fixed;z-index:9999;pointer-events:none;',
      'display:none;flex-direction:column;gap:0;',
      'background:#111;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;',
      'box-shadow:0 8px 32px rgba(0,0,0,0.7);width:220px;'
    ].join('');
    document.body.appendChild(_ttDiv);
  }}
  return _ttDiv;
}}

function _showTooltip(dot, evt) {{
  var tt = _getTooltip();
  var price = '$' + Number(dot.price).toLocaleString();
  var date  = dot.date ? dot.date.slice(0,10) : '';
  var label = [dot.year, dot.model, dot.trim].filter(Boolean).join(' ').slice(0,36);
  var mi    = dot.mi ? Number(dot.mi).toLocaleString() + ' mi' : '';
  tt.innerHTML = (dot.img
    ? '<img src="' + dot.img + '" style="width:100%;height:120px;object-fit:cover;display:block;opacity:0.9">'
    : '<div style="width:100%;height:80px;background:#1c1c1c"></div>')
    + '<div style="padding:10px 12px">'
    + '<div style="font-family:DM Mono,monospace;font-size:16px;font-weight:500;color:#fff;margin-bottom:4px">' + price + '</div>'
    + '<div style="font-family:DM Mono,monospace;font-size:10px;color:#888;margin-bottom:2px">' + date + (mi ? ' · ' + mi : '') + '</div>'
    + (label ? '<div style="font-family:DM Sans,sans-serif;font-size:11px;color:#666">' + label + '</div>' : '')
    + '</div>';
  tt.style.display = 'flex';
  _posTooltip(evt);
}}

function _posTooltip(evt) {{
  var tt = _getTooltip();
  var vw = window.innerWidth, vh = window.innerHeight;
  var ttW = 220, ttH = tt.offsetHeight || 200;
  var x = evt.clientX + 14, y = evt.clientY - ttH / 2;
  if (x + ttW > vw - 10) x = evt.clientX - ttW - 14;
  if (y < 10) y = 10;
  if (y + ttH > vh - 10) y = vh - ttH - 10;
  tt.style.left = x + 'px';
  tt.style.top  = y + 'px';
}}

function _hideTooltip() {{
  var tt = _getTooltip();
  tt.style.display = 'none';
}}

// Cache for auction_comps.json — fetched once, reused for all cards
var _COMPS_CACHE = null;
var _COMPS_PENDING = [];

function _getCompsCache(cb) {{
  if (_COMPS_CACHE !== null) {{ cb(_COMPS_CACHE); return; }}
  _COMPS_PENDING.push(cb);
  if (_COMPS_PENDING.length > 1) return; // already fetching
  fetch('auction_comps.json?v=' + Date.now(), {{cache: 'no-store'}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      _COMPS_CACHE = data;
      _COMPS_PENDING.forEach(function(fn) {{ fn(data); }});
      _COMPS_PENDING = [];
    }})
    .catch(function() {{
      _COMPS_CACHE = {{}};
      _COMPS_PENDING.forEach(function(fn) {{ fn({{}}); }});
      _COMPS_PENDING = [];
    }});
}}

function drawDotGraph(svg, card) {{
  if (svg._drawn) return;
  var lid   = svg.dataset.lid || '';
  var fmv   = +svg.dataset.fmv  || 0;
  var bid   = +svg.dataset.bid  || 0;
  var low   = +svg.dataset.low  || 0;
  var high  = +svg.dataset.high || 0;
  svg._drawn = true;

  _getCompsCache(function(cache) {{
    var dots = (cache[lid] || []);
    _drawDotGraphInner(svg, dots, fmv, bid, low, high);
  }});
}}

function _drawDotGraphInner(svg, dots, fmv, bid, low, high) {{
  if (!dots.length && !fmv) {{
    svg.setAttribute('viewBox','0 0 400 60');
    svg.setAttribute('width','100%'); svg.setAttribute('height','60');
    var t = document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x','50%'); t.setAttribute('y','50%');
    t.setAttribute('text-anchor','middle'); t.setAttribute('dominant-baseline','middle');
    t.setAttribute('fill','#444'); t.setAttribute('font-size','11');
    t.setAttribute('font-family','DM Mono,monospace');
    t.textContent = 'No comp data available';
    svg.appendChild(t);
    return;
  }}

  var W = svg.parentElement.getBoundingClientRect().width || 700;
  var H = 160;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.style.overflow = 'visible';

  var ns = 'http://www.w3.org/2000/svg';
  function mk(tag, attrs, parent) {{
    var e = document.createElementNS(ns, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    (parent || svg).appendChild(e);
    return e;
  }}

  // ── Scales ──
  // X = date (left=oldest, right=newest)
  var dates = dots.map(function(d) {{ return d.date ? new Date(d.date).getTime() : 0; }}).filter(Boolean);
  var prices = dots.map(function(d) {{ return d.price; }});
  if (bid) prices.push(bid);
  if (fmv) prices.push(fmv);

  var PAD_L = 42, PAD_R = 16, PAD_T = 18, PAD_B = 22;
  var plotW = W - PAD_L - PAD_R;
  var plotH = H - PAD_T - PAD_B;

  var minDate = dates.length ? Math.min.apply(null, dates) : Date.now() - 365*86400000;
  var maxDate = dates.length ? Math.max.apply(null, dates) : Date.now();
  var dateRange = maxDate - minDate || 1;

  // ── Outlier-resistant scale (95th percentile cap) ──
  var sorted = dots.map(function(d){{return d.price;}}).sort(function(a,b){{return a-b;}});
  var p05 = sorted[Math.floor(sorted.length*0.05)] || sorted[0];
  var p95 = sorted[Math.floor(sorted.length*0.95)] || sorted[sorted.length-1];
  var iqPad = (p95 - p05) * 0.12;
  // Include bid + fmv in scale but don't let outliers beyond 1.5×IQR dominate
  var scaleMin = Math.min(p05 - iqPad, bid || Infinity, fmv || Infinity) * 0.94;
  var scaleMax = Math.max(p95 + iqPad, bid || 0, fmv || 0) * 1.06;
  var priceRange = scaleMax - scaleMin || 1;

  function scaleX(ts) {{ return PAD_L + ((ts - minDate) / dateRange) * plotW; }}
  function scaleY(p)  {{ return PAD_T + plotH - ((p - scaleMin) / priceRange) * plotH; }}

  // ── Axes ──
  mk('line', {{x1:PAD_L, y1:PAD_T, x2:PAD_L, y2:PAD_T+plotH, stroke:'#222', 'stroke-width':1}});
  mk('line', {{x1:PAD_L, y1:PAD_T+plotH, x2:W-PAD_R, y2:PAD_T+plotH, stroke:'#222', 'stroke-width':1}});

  // Y axis labels (3 ticks) using clamped scale
  [scaleMin, (scaleMin+scaleMax)/2, scaleMax].forEach(function(p) {{
    var y = scaleY(p);
    mk('line', {{x1:PAD_L-4, y1:y, x2:PAD_L, y2:y, stroke:'#333', 'stroke-width':1}});
    var t = mk('text', {{x:PAD_L-6, y:y+3, 'text-anchor':'end', fill:'#555', 'font-family':'DM Mono,monospace', 'font-size':'9'}});
    t.textContent = '$' + Math.round(p/1000) + 'K';
  }});

  // X axis labels (date range)
  if (dates.length) {{
    var fmtD = function(ts) {{
      var d = new Date(ts);
      return (d.getMonth()+1) + '/' + d.getFullYear().toString().slice(2);
    }};
    [[minDate, 'start'], [maxDate, 'end']].forEach(function(pair) {{
      var tx = scaleX(pair[0]);
      var anchor = pair[1] === 'start' ? 'start' : 'end';
      var t = mk('text', {{x:tx, y:H-3, 'text-anchor':anchor, fill:'#444', 'font-family':'DM Mono,monospace', 'font-size':'9'}});
      t.textContent = fmtD(pair[0]);
    }});
  }}

  // ── FMV horizontal line ──
  if (fmv) {{
    var fy = scaleY(fmv);
    mk('line', {{x1:PAD_L, y1:fy, x2:W-PAD_R, y2:fy, stroke:'#3a3a3a', 'stroke-width':1, 'stroke-dasharray':'6,4'}});
    var ft = mk('text', {{x:W-PAD_R-4, y:fy-4, 'text-anchor':'end', fill:'#555', 'font-family':'DM Mono,monospace', 'font-size':'9'}});
    ft.textContent = 'FMV $' + Math.round(fmv/1000) + 'K';
  }}

  // ── Comp dots ──
  dots.forEach(function(dot) {{
    if (!dot.date) return;
    var ts  = new Date(dot.date).getTime();
    var cx  = scaleX(ts);
    var isOutlier = dot.price > scaleMax || dot.price < scaleMin;
    var cy  = isOutlier
      ? (dot.price > scaleMax ? PAD_T + 5 : PAD_T + plotH - 5)
      : scaleY(dot.price);

    if (isOutlier) {{
      // Diamond marker at edge — visually distinct, doesn't affect scale
      var s = 5;
      var diamond = mk('polygon', {{
        points: cx+','+( cy-s)+' '+(cx+s)+','+cy+' '+cx+','+(cy+s)+' '+(cx-s)+','+cy,
        fill:'#2a4a2a', stroke:'#4a8a4a', 'stroke-width':1, opacity:0.8, style:'cursor:pointer'
      }});
      diamond.addEventListener('mouseenter', function(e) {{ _showTooltip(dot, e); diamond.setAttribute('fill','#4ade80'); }});
      diamond.addEventListener('mousemove',  function(e) {{ _posTooltip(e); }});
      diamond.addEventListener('mouseleave', function()  {{ _hideTooltip(); diamond.setAttribute('fill','#2a4a2a'); }});
      if (dot.url) diamond.addEventListener('click', function(e) {{ e.stopPropagation(); window.open(dot.url,'_blank'); }});
    }} else {{
      var c = mk('circle', {{cx:cx, cy:cy, r:5.5, fill:'#1e3a1e', stroke:'#3a6a3a', 'stroke-width':1, opacity:0.85, style:'cursor:pointer'}});
      c.addEventListener('mouseenter', function(e) {{ _showTooltip(dot, e); c.setAttribute('fill','#4ade80'); c.setAttribute('r','7'); }});
      c.addEventListener('mousemove',  function(e) {{ _posTooltip(e); }});
      c.addEventListener('mouseleave', function()  {{ _hideTooltip(); c.setAttribute('fill','#1e3a1e'); c.setAttribute('r','5.5'); }});
      if (dot.url) c.addEventListener('click', function(e) {{ e.stopPropagation(); window.open(dot.url,'_blank'); }});
    }}
  }});

  // ── Current bid dot ──
  if (bid && fmv) {{
    var bx = scaleX(maxDate), by = scaleY(bid);
    mk('line', {{x1:bx, y1:PAD_T, x2:bx, y2:PAD_T+plotH, stroke:'#c0392b', 'stroke-width':1, 'stroke-dasharray':'3,2', opacity:0.5}});
    mk('circle', {{cx:bx, cy:by, r:7, fill:'#c0392b', stroke:'#ff6b6b', 'stroke-width':1.5}});
    var bt = mk('text', {{x:bx-10, y:by-11, 'text-anchor':'end', fill:'#c0392b', 'font-family':'DM Mono,monospace', 'font-size':'9', 'font-weight':'500'}});
    bt.textContent = 'Bid $' + Math.round(bid/1000) + 'K';
  }}

  svg._drawn = true;
}}

// Wire hover on every card to draw its graph
document.querySelectorAll('.auc-card').forEach(function(card) {{
  card.addEventListener('mouseenter', function() {{
    var svg = card.querySelector('.graph-svg');
    if (svg) setTimeout(function() {{ drawDotGraph(svg, card); }}, 40);
  }});
  card.addEventListener('mouseleave', function() {{ _hideTooltip(); }});
}});


// ── Nav + theme ───────────────────────────────────────────────────────────────
function openListing(url) {{ window.open(url, '_blank'); }}
function toggleDD() {{ document.getElementById('dd-overlay').classList.toggle('show'); }}
function closeDD()  {{ document.getElementById('dd-overlay').classList.remove('show'); }}

// Init favs on load
initFavs();

var _THEMES = ['', 'racing', 'gulf', 'olive', 'purple', 'light'];
function cycleTheme() {{
  var cur = document.documentElement.dataset.theme || '';
  var idx = (_THEMES.indexOf(cur) + 1) % _THEMES.length;
  var next = _THEMES[idx];
  document.documentElement.dataset.theme = next;
  localStorage.setItem('ptox_theme', next);
  closeDD();
}}
</script>
</body>

<script>
/* Live stats loader — overwrites baked-in counts with live stats.json on page load */
(async () => {{
  try {{
    const r = await fetch('/stats.json', {{cache:'no-store'}});
    if (!r.ok) return;
    const s = await r.json();
    const fmt = n => n >= 1000 ? n.toLocaleString() : String(n);
    const set = (id, val) => {{ const el = document.getElementById(id); if (el && val != null) el.textContent = fmt(val); }};
    set('s-active',   s.n_active);
    set('s-new',      s.n_new);
    set('s-auctions', s.n_auctions);
    set('s-comps',    s.n_comps);
    set('s-deals',    s.n_deals);
  }} catch(e) {{}}
}})();
</script>
</html>"""


if __name__ == "__main__":
    generate()

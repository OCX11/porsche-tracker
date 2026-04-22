"""
new_dashboard.py — PTOX11 redesigned dashboard.

Design system per ptox11_critique.html:
  - Colors: --red #D6293E · --bg #0A0A0C · --bg2 #111116 · --bg3 #18181F
  - Fonts: Syne (headings) · DM Mono (data) · DM Sans (body)
  - Nav: PTOX logo, Listings · Auctions · Comps · Market · Search
  - Cards: image overlays, FMV progress bar, chip filters

Output: docs/index.html
"""
from __future__ import annotations

import html as _html
import json
import re
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from db import get_conn, get_dashboard_data, init_db, source_category
import fmv as fmv_engine

BASE_DIR  = Path(__file__).parent
OUT_PATH  = BASE_DIR / "docs" / "index.html"
LOG_DIR   = BASE_DIR / "logs"

# ── Source health ─────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_SOURCES = [
    ("Main Scraper", "scraper.log", "Dealers + BaT + PCA", "com.porschetracker.scrape", 45),
    ("Archive", "archive_capture.log", "HTML+screenshot capture", "com.porschetracker.archive-capture", 30),
]

def _launchd_pid(label):
    try:
        out = subprocess.check_output(["launchctl", "list", label],
                                      stderr=subprocess.DEVNULL, text=True, timeout=3)
        m = re.search(r'"PID"\s*=\s*(\d+)', out)
        return m.group(1) if m else None
    except Exception:
        return None

def _last_log_ts(log_file):
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

def _fmt_age(minutes):
    if minutes < 2:   return "just now"
    if minutes < 60:  return f"{int(minutes)}m ago"
    h = int(minutes // 60); m = int(minutes % 60)
    return f"{h}h {m}m ago" if m else f"{h}h ago"

def _source_health():
    now = datetime.now()
    results = []
    for name, log_file, desc, label, stale_mins in _SOURCES:
        ts, last_line = _last_log_ts(log_file)
        pid = _launchd_pid(label)
        has_error = bool(last_line and any(w in last_line for w in ("ERROR", "CRITICAL", "FAILED")))
        if ts is None:
            status, age = "unknown", "no logs"
        else:
            age_mins = (now - ts).total_seconds() / 60
            age = _fmt_age(age_mins)
            status = "error" if has_error else ("stale" if age_mins > stale_mins else "ok")
        if status == "stale" and pid:
            status = "ok"
        results.append({"name": name, "status": status, "age": age})
    return results

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
    """Return 'model trim' with leading model word removed from trim if duplicated."""
    m = (model or "").strip()
    t = (trim or "").strip()
    if t and m and t.lower().startswith(m.lower()):
        t = t[len(m):].lstrip()
    return (m + (" " + t if t else "")).strip()

def _age_label(dt_str: str) -> str:
    if not dt_str: return ""
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        try: dt = datetime.combine(date.fromisoformat(dt_str[:10]), datetime.min.time())
        except: return ""
    delta = datetime.now() - dt
    mins = int(delta.total_seconds() / 60)
    if mins < 2:   return "just now"
    if mins < 60:  return f"{mins}m ago"
    h = mins // 60
    if h < 24:     return f"{h}h ago"
    return f"{h // 24}d ago"

# Source badge config
_BADGE_CFG = {
    "bring a trailer": ("#0D1F35", "#60a5fa", "BaT"),
    "bat":             ("#0D1F35", "#60a5fa", "BaT"),
    "pcarmarket":      ("#0A1F14", "#4ade80", "pcarmarket"),
    "cars & bids":     ("#1F0D03", "#fb923c", "C&B"),
    "carsandbids":     ("#1F0D03", "#fb923c", "C&B"),
    "cars and bids":   ("#1F0D03", "#fb923c", "C&B"),
    "classic.com":     ("#1A0B2E", "#c084fc", "classic"),
    "rennlist":        ("#1F0A10", "#f472b6", "Rennlist"),
    "pca mart":        ("#051520", "#38bdf8", "PCA Mart"),
    "autotrader":      ("#1F1600", "#fbbf24", "AutoTrader"),
    "cars.com":        ("#031208", "#86efac", "Cars.com"),
    "ebay motors":     ("#1F0F00", "#fb923c", "eBay"),
    "dupont registry": ("#1A0505", "#f87171", "DuPont"),
    "built for backroads": ("#0A1520", "#7dd3fc", "BfB"),
}
_AUCTION_SET = frozenset({"bring a trailer","bat","bringatrailer","pcarmarket","cars & bids","carsandbids","cars and bids","classic.com"})

def _badge(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    bg, fg, label = _BADGE_CFG.get(k, ("#18181F", "#6B6B7D", (dealer or "?")[:12]))
    return f'<span class="badge" style="background:{bg};color:{fg}">{_h(label)}</span>'

def _is_auction(dealer: str) -> bool:
    return (dealer or "").lower().strip() in _AUCTION_SET

def _fmv_pct(price, fmv_val):
    """Return float pct or None."""
    if not price or not fmv_val:
        return None
    try:
        return (float(price) - float(fmv_val)) / float(fmv_val) * 100
    except Exception:
        return None

def _delta_badge(pct):
    """Small badge for card overlay / price row."""
    if pct is None: return ""
    if abs(pct) < 2:    cls, txt = "delta-flat",  "≈FMV"
    elif pct < -10:     cls, txt = "delta-great", f"&#x2193;{abs(pct):.0f}%"
    elif pct < 0:       cls, txt = "delta-good",  f"&#x2193;{abs(pct):.0f}%"
    elif pct > 15:      cls, txt = "delta-high",  f"&#x2191;{pct:.0f}%"
    else:               cls, txt = "delta-mid",   f"&#x2191;{pct:.0f}%"
    return f'<span class="delta {cls}">{txt}</span>'

def _fmv_bar_block(price, fmv_val, conf, comp_count, price_low=None, price_high=None) -> str:
    """FMV progress bar block shown on every card."""
    if not fmv_val or conf == "NONE":
        return ('<div class="fmv-none">'
                '<span class="fmv-none-dot"></span>No FMV &mdash; insufficient comps'
                '</div>')
    pct = _fmv_pct(price, fmv_val)
    fmv_str = _p_short(fmv_val)
    comp_str = f"{comp_count} comp{'s' if comp_count != 1 else ''}"
    if conf in ("HIGH", "MEDIUM") and price_low and price_high:
        right_str = f"{_p_short(price_low)}&ndash;{_p_short(price_high)} &middot; {comp_str}"
    else:
        right_str = comp_str

    if pct is None:
        bar_w = 50; bar_cls = "bar-neutral"; delta_str = ""
    elif pct < -30:
        bar_w = max(5, int(50 + pct * 0.5)); bar_cls = "bar-great"; delta_str = f"&#x2193;{abs(pct):.0f}% vs FMV"
    elif pct < 0:
        bar_w = int(50 + pct * 0.5); bar_cls = "bar-good"; delta_str = f"&#x2193;{abs(pct):.0f}% vs FMV"
    elif abs(pct) < 2:
        bar_w = 50; bar_cls = "bar-neutral"; delta_str = "at market"
    elif pct < 15:
        bar_w = int(50 + pct * 0.5); bar_cls = "bar-mid"; delta_str = f"&#x2191;{pct:.0f}% vs FMV"
    else:
        bar_w = min(95, int(50 + pct * 0.5)); bar_cls = "bar-high"; delta_str = f"&#x2191;{pct:.0f}% vs FMV"

    bar_w = max(3, min(97, bar_w))

    return (f'<div class="fmv-block">'
            f'<div class="fmv-top-row">'
            f'<span class="fmv-label">Ask vs FMV</span>'
            f'<span class="fmv-delta-txt {bar_cls}">{delta_str}</span>'
            f'</div>'
            f'<div class="fmv-bar-wrap">'
            f'<div class="fmv-bar-fill {bar_cls}" style="width:{bar_w}%"></div>'
            f'<div class="fmv-bar-midline"></div>'
            f'</div>'
            f'<div class="fmv-bottom-row">'
            f'<span class="fmv-val-txt">FMV {fmv_str}</span>'
            f'<span class="fmv-comps-txt">{right_str}</span>'
            f'</div>'
            f'</div>')

# ── Generation helper ─────────────────────────────────────────────────────────

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


# ── Card builder ──────────────────────────────────────────────────────────────

def _card(car: dict, fmv_score: dict) -> str:
    dealer   = car.get("dealer", "")
    year     = car.get("year", "")
    model    = car.get("model", "") or ""
    trim     = car.get("trim", "") or ""
    price    = car.get("price")
    mileage  = car.get("mileage")
    url      = car.get("listing_url", "") or "#"
    img      = car.get("image_url", "") or ""
    if img and img.startswith("/static/img_cache/"):
        img = "img_cache/" + img.split("/")[-1]
    created  = car.get("created_at", "") or car.get("date_first_seen", "")
    location = car.get("location", "") or ""
    trans    = car.get("transmission", "") or ""
    days     = car.get("days_on_site") or 0
    dom_days = car.get("days_on_market") or 0
    tier     = car.get("tier", "") or ""
    auction_ends_at = car.get("auction_ends_at") or ""
    is_auc   = _is_auction(dealer)

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)
    price_low  = fmv_score.get("price_low")
    price_high = fmv_score.get("price_high")
    pct        = _fmv_pct(price, fmv_val) if conf != "NONE" else None
    # Auction FMV phasing: 3-phase approach (owner decision: 65% threshold)
    # Phase 1 (>24hr): hide FMV, show 'Auction in progress'
    # Phase 2 (<24hr): show FMV only if bid >65% of FMV
    # Phase 3 (ended): full FMV comparison
    _auction_fmv_hidden = False
    if is_auc and auction_ends_at and fmv_val and conf != "NONE":
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            _ends = _dt.fromisoformat(auction_ends_at.replace("Z", "+00:00"))
            _now = _dt.now(_tz.utc)
            _left = _ends - _now
            if _left.total_seconds() > 0:  # still active
                if _left > _td(hours=24):
                    fmv_bar = ('<div class="fmv-none">'
                               '<span class="fmv-none-dot" style="background:#555"></span>'
                               'Auction in progress'
                               '</div>')
                    _auction_fmv_hidden = True
                else:
                    _bid_pct = (float(price) / float(fmv_val) * 100) if price and fmv_val else 0
                    if _bid_pct >= 65:
                        fmv_bar = _fmv_bar_block(price, fmv_val, conf, comp_count, price_low, price_high)
                    else:
                        fmv_bar = ('<div class="fmv-none">'
                                   '<span class="fmv-none-dot" style="background:#555"></span>'
                                   'Auction ending soon'
                                   '</div>')
                        _auction_fmv_hidden = True
            else:
                fmv_bar = _fmv_bar_block(price, fmv_val, conf, comp_count)
        except Exception:
            fmv_bar = _fmv_bar_block(price, fmv_val, conf, comp_count)
    else:
        fmv_bar = _fmv_bar_block(price, fmv_val, conf, comp_count, price_low, price_high)
    age_str    = _age_label(created)
    gen_str    = _gen(year, model)
    # Badge label used for source chip filtering
    k = (dealer or "").lower().strip()
    src_label  = _BADGE_CFG.get(k, (None, None, (dealer or "")[:12]))[2]

    # Deal badge — only show if 10%+ below FMV
    deal_badge = ""
    if pct is not None and pct <= -10 and not _auction_fmv_hidden:
        deal_badge = f'<div class="img-deal-badge">{chr(8595)}{abs(pct):.0f}%</div>'

    # Gen badge on image
    gen_badge = f'<div class="img-gen-badge">{_h(gen_str)}</div>'

    # Tier badge
    tier_html = ""
    if tier == "TIER1":
        tier_html = '<span class="tier-badge">GT / Collector</span>'

    # Placeholder SVG — single quotes encoded as %27 so the string is safe inside onerror="this.src='...'"
    placeholder_svg = ("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27400%27 height=%27165%27%3E"
                       "%3Crect width=%27400%27 height=%27165%27 fill=%27%2318181F%27/%3E"
                       "%3Ctext x=%2750%25%27 y=%2750%25%27 dominant-baseline=%27middle%27 text-anchor=%27middle%27 "
                       "font-family=%27monospace%27 font-size=%2712%27 fill=%27%2325252E%27%3ENo photo%3C/text%3E%3C/svg%3E")

    if img:
        img_inner = (
            f'<img src="{_h(img)}" alt="{_h(str(year)+" "+model)}" class="card-img" loading="lazy" '
            f'onerror="this.src=\'{placeholder_svg}\'">'
        )
    else:
        img_inner = f'<img src="{placeholder_svg}" alt="No photo" class="card-img">'

    img_html = (
        f'<div class="card-img-wrap">'
        f'{img_inner}'
        f'{gen_badge}'
        f'{deal_badge}'
        f'</div>'
    )

    # Price label
    price_lbl = "Bid" if is_auc else "Ask"
    price_cls  = "price-auction" if is_auc else "price-ask"

    # Meta chips
    chips = []
    if trans:    chips.append(_h(trans))
    if mileage:  chips.append(f"{_m(mileage)} mi")
    if location: chips.append(_h(location[:22]))
    if dom_days > 0: chips.append(f'<span class="dom-chip">&#x23F1;{dom_days}d</span>')
    chips_html = " &middot; ".join(chips)

    days_html = ""
    if days and int(days) >= 30:
        days_html = f' &middot; <span class="days-stale">{days}d listed</span>'

    ends_html = ""
    if is_auc and auction_ends_at:
        ends_html = (f'<div class="auction-ends">'
                     f'Ends <span class="countdown" data-ends="{_h(auction_ends_at)}">…</span>'
                     f'</div>')

    return (
        f'<div class="card" '
        f'data-dealer="{_h(dealer)}" data-year="{year}" data-model="{_h(model)}" '
        f'data-gen="{_h(gen_str)}" data-tier="{_h(tier)}" data-price="{price or 0}" '
        f'data-src-label="{_h(src_label)}" '
        f'data-source-type="{"auction" if is_auc else "retail"}" '
        f'onclick="openListing(\'{_h(url)}\')">\n'
        f'  {img_html}\n'
        f'  <div class="card-body">\n'
        f'    <div class="card-top-row">'
        f'{_badge(dealer)}'
        f'<span class="card-age" data-created="{_h(created)}"></span>'
        f'</div>\n'
        f'    <div class="card-title">{year} Porsche {_h(_dedup_model_trim(model, trim))}</div>\n'
        f'    {tier_html}\n'
        f'    <div class="card-price-row">'
        f'<span class="price-lbl">{price_lbl}</span>'
        f'<span class="{price_cls}">{_p(price)}</span>'
        f'</div>\n'
        f'    {fmv_bar}\n'
        f'    {ends_html}\n'
        f'    <div class="card-meta">{chips_html}{days_html}</div>\n'
        f'  </div>\n'
        f'</div>'
    )

# ── Sold comp row ─────────────────────────────────────────────────────────────

def _comp_row(c: dict) -> str:
    source   = c.get("source", "")
    year     = c.get("year", "")
    model    = c.get("model", "") or ""
    trim     = c.get("trim", "") or ""
    price    = c.get("sold_price") or c.get("sale_price")
    mileage  = c.get("mileage")
    sold_dt  = (c.get("sold_date") or c.get("sale_date") or "")[:10]
    url      = c.get("listing_url", "") or "#"
    trans    = c.get("transmission", "") or ""
    gen      = c.get("generation") or _gen(year, model)

    return (f'<tr class="comp-row" data-gen="{_h(gen)}" data-year="{year}" data-model="{_h(model)}">\n'
            f'  <td>{_badge(source)}</td>\n'
            f'  <td>{year}</td>\n'
            f'  <td class="td-model">{_h(model)} {_h(trim)}</td>\n'
            f'  <td>{_h(gen)}</td>\n'
            f'  <td>{_h(trans) or "—"}</td>\n'
            f'  <td>{_m(mileage)}</td>\n'
            f'  <td class="td-price">{_p(price)}</td>\n'
            f'  <td>{sold_dt or "—"}</td>\n'
            f'  <td><a href="{_h(url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="tbl-link">&#x2192;</a></td>\n'
            f'</tr>')

# ── Health pills ──────────────────────────────────────────────────────────────

def _health_pills(health: list) -> str:
    out = []
    for s in health:
        cls = {"ok": "pill-ok", "stale": "pill-stale", "error": "pill-error"}.get(s["status"], "pill-unknown")
        out.append(f'<span class="health-pill {cls}">{_h(s["name"])} <span class="pill-age">{_h(s["age"])}</span></span>')
    return "\n".join(out)


# ── Main generate ─────────────────────────────────────────────────────────────

def generate() -> str:
    init_db()
    with get_conn() as conn:
        d = get_dashboard_data(conn)
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

        active = d["active"]
        def _keep(c):
            if (c.get("dealer") or "").lower() == "holt motorsports": return False
            yr = int(c.get("year") or 0)
            if yr < 1984 or yr > 2024: return False
            return True
        active = [c for c in active if _keep(c)]
        today_d = date.today()
        for c in active:
            c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})
            dfs = (c.get("date_first_seen") or "")[:10]
            if dfs:
                try:
                    c["days_on_market"] = (today_d - date.fromisoformat(dfs)).days
                except Exception:
                    c["days_on_market"] = 0
            else:
                c["days_on_market"] = 0

        active_sorted = sorted(active,
                               key=lambda c: c.get("created_at") or c.get("date_first_seen") or "",
                               reverse=True)

        cutoff = (date.today() - timedelta(days=730)).isoformat()
        comp_rows = conn.execute(
            "SELECT * FROM sold_comps WHERE sold_date >= ? AND sold_price IS NOT NULL ORDER BY sold_date DESC",
            (cutoff,)
        ).fetchall()
        comps = [dict(r) for r in comp_rows]

        auctions = [c for c in active if _is_auction(c.get("dealer", ""))]

        new_today  = [c for c in d["new_today"]  if _keep(c)]
        n_active   = len(active)
        n_new      = len(new_today)
        n_auctions = len(auctions)
        n_comps    = len(comps)
        n_deals    = sum(1 for c in active if (
                         c["_fmv"].get("fmv") and c.get("price") and
                         c["_fmv"]["confidence"] != "NONE" and
                         float(c["price"]) < float(c["_fmv"]["fmv"]) * 0.95))

        health     = _source_health()
        health_html = _health_pills(health)
        import json as _json
        card_items = []
        card_html_parts = []
        for c in active_sorted:
            fmv_s = c["_fmv"]
            html_c = _card(c, fmv_s)
            card_html_parts.append(html_c)
            pct = _fmv_pct(c.get("price"), fmv_s.get("fmv")) if fmv_s.get("confidence","NONE") != "NONE" else None
            card_items.append({
                "html": html_c,
                "yr":   int(c.get("year") or 0),
                "pr":   int(c.get("price") or 0),
                "gen":  _gen(c.get("year"), c.get("model")),
                "src":  _BADGE_CFG.get((c.get("dealer") or "").lower().strip(), (None,None,(c.get("dealer") or "")[:12]))[2],
                "tier": c.get("tier") or "",
                "deal": pct is not None and pct <= -10,
                "cool": ("air" if (int(c.get("year") or 0) <= 1998 and "911" in (c.get("model") or "").lower()) else ("water" if (int(c.get("year") or 0) >= 1999 and "911" in (c.get("model") or "").lower()) else None)),
                "dom":  c.get("days_on_market") or 0,
                "txt":  ((str(c.get("year") or "") + " " + (c.get("model") or "") + " " +
                          (c.get("dealer") or "") + " " + _gen(c.get("year"), c.get("model")))).lower(),
            })
        all_cards  = "\n".join(card_html_parts)
        card_data_json = _json.dumps(card_items, ensure_ascii=False)
        comp_rows_html = "\n".join(_comp_row(c) for c in comps)

        # Chip data — unique generations and sources
        generations = sorted(set(_gen(c.get("year"), c.get("model")) for c in active if c.get("year")))
        sources_raw = sorted(set(
            _BADGE_CFG.get((c.get("dealer") or "").lower().strip(), (None, None, (c.get("dealer") or "")[:12]))[2]
            for c in active if c.get("dealer")
        ))
        gen_chips_html    = "\n".join(f'<button class="chip" data-val="{_h(g)}" onclick="toggleChip(this,\'gen\')">{_h(g)}</button>' for g in generations)
        source_chips_html = "\n".join(f'<button class="chip" data-val="{_h(s)}" onclick="toggleChip(this,\'src\')">{_h(s)}</button>' for s in sources_raw)

        # Comp gen options for filter
        gen_opts = "\n".join(f'<option value="{_h(g)}">{_h(g)}</option>' for g in generations)

        now_str = datetime.now().strftime("%b %d, %Y %H:%M")

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PTOX">
<meta name="theme-color" content="#0A0A0C">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icons/icon-192.png">
<title>PTOX11 &mdash; Porsche Market Intelligence</title>
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
html,body {{ height:100%; background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.5; }}
a {{ color:inherit; text-decoration:none; }}
button {{ cursor:pointer; border:none; background:none; font:inherit; color:inherit; }}

/* ── Layout ── */
.app {{ display:flex; flex-direction:column; height:100vh; overflow:hidden; }}

/* ── Topbar / Nav ── */
.topbar {{
  height:52px; min-height:52px;
  background:#141414; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  padding:0 24px; gap:16px; z-index:50;
}}
.logo {{
  font-family:'Syne',sans-serif; font-size:14px; font-weight:800;
  color:var(--text); letter-spacing:2px; margin-right:16px; white-space:nowrap; flex-shrink:0;
}}
.logo span {{ color:var(--red); }}
.stats-bar {{ display:flex; gap:1px; margin:0 12px 8px; background:#1a1a1a; border-radius:14px; overflow:hidden; border:1px solid #2a2a2a; }}
.stat-cell {{ flex:1; padding:12px 8px 10px; text-align:center; background:#141414; cursor:pointer; transition:background 0.15s; position:relative; }}
.stat-cell:first-child {{ border-radius:13px 0 0 13px; }}
.stat-cell:last-child {{ border-radius:0 13px 13px 0; }}
.stat-cell:hover {{ background:#1c1c1c; }}
.stat-cell.active {{ background:#1e1e1e; }}
.stat-cell.active::after {{ content:''; position:absolute; bottom:0; left:20%; right:20%; height:2px; background:var(--red); border-radius:1px; }}
.stat-cell + .stat-cell {{ border-left:1px solid #2a2a2a; }}
.stat-number {{ font-size:22px; font-weight:700; letter-spacing:-0.5px; line-height:1.1; color:var(--text); }}
.stat-number.green {{ color:var(--green); }}
.stat-number.red {{ color:var(--red); }}
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
.topbar-right {{
  display:flex; align-items:center; gap:12px;
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted);
  white-space:nowrap;
}}
.health-pills {{ display:flex; gap:6px; flex-wrap:nowrap; }}
.health-pill {{
  padding:3px 8px; border-radius:3px; font-family:'DM Mono',monospace;
  font-size:10px; font-weight:500; display:inline-flex; align-items:center; gap:4px;
}}
.pill-ok     {{ background:#052210; color:#22C55E; }}
.pill-stale  {{ background:#1F1400; color:#EAB308; }}
.pill-error  {{ background:#200508; color:var(--red); }}
.pill-unknown{{ background:var(--bg3); color:var(--muted); }}
.pill-age {{ font-weight:400; opacity:0.7; }}

.body-area {{ display:flex; flex:1; overflow:hidden; }}

/* ── Sidebar ── */
.sidebar {{
  width:220px; min-width:220px;
  background:#0C0C12; border-right:1px solid var(--border);
  display:flex; flex-direction:column; overflow-y:auto;
  padding:20px 14px; gap:0;
}}
.sidebar-label {{
  font-family:'DM Mono',monospace; font-size:9px; font-weight:500;
  letter-spacing:1.5px; text-transform:uppercase;
  color:var(--muted); margin-bottom:10px;
}}
.filter-group {{ margin-bottom:20px; }}
.filter-group-label {{
  font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px;
  text-transform:uppercase; color:var(--muted); margin-bottom:8px; display:block;
}}
.chip-row {{ display:flex; flex-wrap:wrap; gap:5px; }}
.chip {{
  font-family:'DM Mono',monospace; font-size:10px;
  background:var(--bg3); border:1px solid var(--border);
  color:#9898B0; padding:4px 9px; border-radius:20px;
  cursor:pointer; transition:all 0.1s;
}}
.chip:hover {{ color:var(--text); border-color:var(--muted); }}
.chip.active {{
  background:#1A0810; border-color:var(--red); color:var(--red);
}}
.filter-range {{ display:flex; gap:6px; }}
.filter-range input {{
  width:100%; padding:6px 8px; border:1px solid var(--border); border-radius:4px;
  font-family:'DM Mono',monospace; font-size:10px; color:var(--text);
  background:var(--bg3); outline:none;
}}
.filter-range input:focus {{ border-color:var(--red); }}
.filter-range input::placeholder {{ color:var(--muted); }}
.filter-checkboxes {{ display:flex; flex-direction:column; gap:6px; }}
.filter-checkboxes label {{
  display:flex; align-items:center; gap:7px;
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); cursor:pointer;
}}
.filter-checkboxes input[type=checkbox] {{ width:13px; height:13px; accent-color:var(--red); }}
.reset-btn {{
  width:100%; padding:7px; border-radius:4px;
  background:var(--bg3); color:var(--muted);
  font-family:'DM Mono',monospace; font-size:10px; font-weight:500;
  border:1px solid var(--border); transition:all 0.1s; margin-top:4px;
}}
.reset-btn:hover {{ color:var(--text); border-color:var(--muted); }}

/* ── Main content ── */
.main {{ flex:1; display:flex; flex-direction:column; overflow:hidden; }}

/* ── Stats strip ── */
.stats-strip {{
  display:flex; gap:1px; background:var(--border);
  border-bottom:1px solid var(--border);
}}
.stat-cell {{
  flex:1; background:var(--bg2); padding:12px 16px; text-align:center;
}}
.stat-num {{
  font-family:'DM Mono',monospace; font-size:20px; font-weight:500;
  color:var(--text); letter-spacing:-1px; line-height:1;
}}
.stat-num.green  {{ color:var(--green); }}
.stat-num.yellow {{ color:var(--yellow); }}
.stat-lbl {{
  font-family:'DM Mono',monospace; font-size:9px; color:var(--muted);
  margin-top:4px; text-transform:uppercase; letter-spacing:0.5px;
}}

.main-toolbar {{
  padding:10px 20px; background:var(--bg2); border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between; gap:12px;
}}
.search-wrap {{ position:relative; }}
.search-input {{
  padding:8px 16px 8px 36px;
  border:1px solid rgba(255,255,255,0.12);
  border-radius:10px;
  font-family:'DM Mono',monospace; font-size:12px; width:260px;
  outline:none;
  background:rgba(255,255,255,0.07);
  color:var(--text);
  backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  box-shadow:0 2px 12px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.06);
  transition:all 0.15s;
}}
.search-input::placeholder {{ color:rgba(255,255,255,0.3); }}
.search-input:focus {{
  border-color:rgba(255,255,255,0.25);
  background:rgba(255,255,255,0.1);
  box-shadow:0 4px 20px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.1);
  width:300px;
}}
.search-icon {{ position:absolute; left:11px; top:50%; transform:translateY(-50%); color:rgba(255,255,255,0.35); font-size:13px; }}
.results-count {{
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted);
}}

.content-area {{ flex:1; overflow-y:auto; padding:20px; background:var(--bg); }}

/* ── Cards grid ── */
.cards-grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill, minmax(270px,1fr));
  gap:12px;
}}
.card {{
  background:var(--bg2); border:1px solid var(--border); border-radius:6px;
  overflow:hidden; cursor:pointer;
  transition:border-color 0.15s, transform 0.15s, box-shadow 0.15s;
}}
.card:hover {{
  border-color:var(--red); transform:translateY(-2px);
  box-shadow:0 4px 20px rgba(214,41,62,0.12);
}}
.card-img-wrap {{ position:relative; height:165px; overflow:hidden; background:var(--bg3); }}
.card-img {{ width:100%; height:165px; object-fit:cover; display:block; transition:transform 0.2s; opacity:0.92; }}
.card:hover .card-img {{ transform:scale(1.03); }}
.img-gen-badge {{
  position:absolute; top:8px; left:8px;
  background:rgba(0,0,0,0.72); backdrop-filter:blur(4px);
  font-family:'DM Mono',monospace; font-size:9px; color:#9090A8;
  padding:3px 7px; border-radius:3px; letter-spacing:0.5px;
}}
.img-deal-badge {{
  position:absolute; top:8px; right:8px;
  background:var(--red); color:#fff;
  font-family:'DM Mono',monospace; font-size:10px; font-weight:500;
  padding:3px 7px; border-radius:3px; letter-spacing:0.5px;
}}
.card-body {{ padding:11px 12px 12px; }}
.card-top-row {{
  display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;
}}
.card-age {{ font-family:'DM Mono',monospace; font-size:10px; color:#8A8A9E; }}
.card-title {{
  font-family:'DM Sans',sans-serif; font-size:13px; font-weight:500;
  color:#F0F0FC; margin-bottom:4px; line-height:1.35;
}}
.tier-badge {{
  display:inline-block; font-family:'DM Mono',monospace; font-size:9px; font-weight:500;
  background:#1A0A00; color:var(--yellow); padding:2px 7px; border-radius:3px;
  margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;
  border:1px solid #4A2800;
}}
.card-price-row {{
  display:flex; align-items:baseline; gap:6px; margin-bottom:8px;
}}
.price-lbl {{ font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); }}
.price-ask    {{ font-family:'DM Mono',monospace; font-size:17px; font-weight:600; color:#fff; letter-spacing:-0.5px; }}
.price-auction{{ font-family:'DM Mono',monospace; font-size:17px; font-weight:600; color:#C4B5FD; letter-spacing:-0.5px; }}

/* ── FMV bar ── */
.fmv-block {{ margin-bottom:7px; }}
.fmv-top-row {{ display:flex; justify-content:space-between; font-family:'DM Mono',monospace; font-size:10px; margin-bottom:5px; }}
.fmv-label {{ color:var(--muted); }}
.fmv-delta-txt {{ font-weight:500; }}
.fmv-delta-txt.bar-great {{ color:var(--green); }}
.fmv-delta-txt.bar-good  {{ color:#86EFAC; }}
.fmv-delta-txt.bar-neutral{{ color:var(--muted); }}
.fmv-delta-txt.bar-mid   {{ color:var(--yellow); }}
.fmv-delta-txt.bar-high  {{ color:#F87171; }}
.fmv-bar-wrap {{
  background:#1E1E28; height:4px; border-radius:2px;
  position:relative; overflow:hidden; margin-bottom:5px;
}}
.fmv-bar-fill {{ height:100%; border-radius:2px; transition:width 0.3s; }}
.fmv-bar-fill.bar-great {{ background:var(--green); }}
.fmv-bar-fill.bar-good  {{ background:#86EFAC; }}
.fmv-bar-fill.bar-neutral{{ background:var(--muted); }}
.fmv-bar-fill.bar-mid   {{ background:var(--yellow); }}
.fmv-bar-fill.bar-high  {{ background:#F87171; }}
.fmv-bar-midline {{
  position:absolute; left:50%; top:0; bottom:0;
  width:1px; background:rgba(255,255,255,0.12);
}}
.fmv-bottom-row {{ display:flex; justify-content:space-between; font-family:'DM Mono',monospace; font-size:10px; }}
.fmv-val-txt {{ color:#A0A0B4; }}
.fmv-comps-txt {{ color:#6B6B7D; }}
.fmv-none {{
  display:flex; align-items:center; gap:5px;
  font-family:'DM Mono',monospace; font-size:9px; color:#6B6B7D; margin-bottom:7px;
}}
.fmv-none-dot {{ width:5px; height:5px; border-radius:50%; background:var(--border); flex-shrink:0; }}

.auction-ends {{
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-bottom:5px;
}}
.countdown {{ color:var(--red); font-weight:500; }}
.card-meta {{ font-family:'DM Mono',monospace; font-size:10px; color:#8A8A9E; }}
.days-stale {{ color:#F87171; font-weight:500; }}
.dom-chip {{ color:#6B6B7D; }}
.badge {{ font-family:'DM Mono',monospace; font-size:10px; font-weight:500; padding:2px 7px; border-radius:3px; display:inline-block; }}

/* ── Table view (comps) ── */
.tbl-wrap {{ overflow-x:auto; background:var(--bg2); border:1px solid var(--border); border-radius:6px; }}
.tbl {{ width:100%; border-collapse:collapse; font-family:'DM Mono',monospace; font-size:11px; }}
.tbl thead tr {{ background:var(--bg3); border-bottom:1px solid var(--border); }}
.tbl th {{
  padding:9px 10px; text-align:left; font-weight:500;
  color:var(--muted); font-size:9px; text-transform:uppercase;
  letter-spacing:0.5px; white-space:nowrap; cursor:pointer; user-select:none;
}}
.tbl th:hover {{ color:var(--text); }}
.tbl td {{ padding:8px 10px; border-bottom:1px solid #1A1A22; vertical-align:middle; color:#B0B0C0; }}
.tbl tbody tr:hover {{ background:var(--bg3); }}
.tbl tbody tr:last-child td {{ border-bottom:none; }}
.td-price {{ font-weight:500; text-align:right; color:var(--text); }}
.td-model {{ max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.tbl-link {{ color:var(--muted); padding:2px 6px; border-radius:3px; transition:all 0.1s; }}
.tbl-link:hover {{ color:var(--text); background:var(--bg3); }}

/* ── Empty state ── */
.empty {{ grid-column:1/-1; text-align:center; padding:60px 20px; color:var(--muted); }}
.empty-icon {{ font-size:2.5em; margin-bottom:10px; }}
.empty-text {{ font-family:'DM Mono',monospace; font-size:12px; color:var(--muted); }}

/* ── View panels ── */
.view {{ display:none; }}
.view.active {{ display:block; }}

/* ── Section header ── */
.section-header {{
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:14px; flex-wrap:wrap; gap:8px;
}}
.section-title {{
  font-family:'Syne',sans-serif; font-size:15px; font-weight:700; color:var(--text);
}}
.section-sub {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-top:2px; }}

/* ── Market report cards ── */
.report-card {{
  background:var(--bg2); border:1px solid var(--border); border-radius:6px;
  padding:20px; display:block; transition:border-color 0.1s; color:var(--text);
}}
.report-card:hover {{ border-color:var(--red); }}
.report-icon {{ font-size:1.4em; margin-bottom:8px; }}
.report-title {{ font-family:'Syne',sans-serif; font-size:14px; font-weight:700; margin-bottom:4px; }}
.report-sub {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); }}

/* ── Scrollbar ── */
::-webkit-scrollbar {{ width:5px; height:5px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:3px; }}
::-webkit-scrollbar-thumb:hover {{ background:var(--muted); }}

/* ── Mobile filter drawer ── */
.filter-fab {{
  display:none; align-items:center; gap:6px;
  padding:7px 14px; border-radius:8px;
  background:var(--bg3); border:1px solid var(--border);
  font-family:'DM Mono',monospace; font-size:11px; color:var(--muted);
  cursor:pointer; transition:all 0.1s; white-space:nowrap;
}}
.filter-fab:hover {{ color:var(--text); border-color:var(--muted); }}
.filter-fab.has-filters {{ border-color:var(--red); color:var(--red); background:#1A0810; }}
.drawer-overlay {{
  display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:200;
}}
.drawer-overlay.open {{ display:block; }}
.filter-drawer {{
  position:fixed; bottom:0; left:0; right:0;
  background:#0C0C12; border-top:1px solid var(--border);
  border-radius:16px 16px 0 0; z-index:201;
  padding:16px 20px 40px;
  transform:translateY(100%); transition:transform 0.25s ease;
  max-height:92vh; overflow-y:auto;
}}
.filter-drawer.open {{ transform:translateY(0); }}
.drawer-handle {{
  width:36px; height:4px; background:var(--border);
  border-radius:2px; margin:0 auto 16px; display:block;
}}
.drawer-title {{
  font-family:'DM Mono',monospace; font-size:12px; font-weight:600;
  color:var(--text); text-transform:uppercase; letter-spacing:1px; margin-bottom:16px;
}}
.drawer-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.drawer-section {{ display:flex; flex-direction:column; gap:7px; }}
.drawer-section-label {{
  font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1px;
  text-transform:uppercase; color:var(--text); font-weight:500;
}}
.drawer-chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
.drawer-chips .chip {{
  font-size:13px; padding:9px 16px; border-radius:24px;
  border-color:var(--border); color:var(--text);
}}
.drawer-chips .chip.active {{
  background:#2A0815; border-color:var(--red); color:var(--red);
}}
.drawer-range {{ display:flex; gap:6px; }}
.drawer-range input {{
  width:100%; padding:12px 12px; border:1px solid var(--border); border-radius:8px;
  font-family:'DM Mono',monospace; font-size:14px; color:var(--text);
  background:var(--bg2); outline:none;
}}
.drawer-range input:focus {{ border-color:var(--red); }}
.drawer-checkboxes {{ display:flex; flex-direction:column; gap:14px; }}
.drawer-checkboxes label {{
  display:flex; align-items:center; gap:12px;
  font-family:'DM Mono',monospace; font-size:15px; color:var(--text);
}}
.drawer-checkboxes input[type=checkbox] {{ width:20px; height:20px; accent-color:var(--red); }}
.drawer-actions {{ display:flex; gap:8px; margin-top:18px; }}
.drawer-apply {{
  flex:1; padding:16px; border-radius:10px; background:var(--red); color:#fff;
  font-family:'DM Mono',monospace; font-size:15px; font-weight:600; border:none; cursor:pointer;
}}
.drawer-reset {{
  padding:16px 20px; border-radius:10px; background:var(--bg3); color:var(--text);
  font-family:'DM Mono',monospace; font-size:15px; border:1px solid var(--border); cursor:pointer;
}}

@media(max-width:768px) {{
  .sidebar {{ display:none; }}
  .topbar-right {{ display:none; }}
  .filter-fab {{ display:flex; }}
  .search-input {{ width:150px; }}
  .search-input:focus {{ width:190px; }}
  .logo {{ margin-right:6px; font-size:13px; }}
  .topbar {{ padding:8px 12px; }}
  .stats-bar {{ margin:0 8px 8px; }}
  .stat-number {{ font-size:18px; }}
  .pill {{ min-width:60px; font-size:12px; }}
}}
</style>
</head>
<body>
<div class="app">

<!-- ── Nav ── -->
<header class="topbar">
  <a class="logo" href="index.html" style="cursor:pointer;text-decoration:none;">PTOX<span>11</span></a>
  <button class="more-btn" onclick="toggleDropdown()">More &#x25BE;</button>
</header>
<div class="stats-bar">
  <div class="stat-cell active" onclick="switchView('listings',this)">
    <div class="stat-number">{n_active:,}</div>
    <div class="stat-label">Active</div>
  </div>
  <div class="stat-cell" onclick="switchView('listings',this);filterToday()">
    <div class="stat-number">{n_new}</div>
    <div class="stat-label">New Today</div>
  </div>
  <a class="stat-cell" href="auctions.html" style="text-decoration:none;color:inherit">
    <div class="stat-number red">{n_auctions}</div>
    <div class="stat-label">Auctions</div>
  </a>
  <div class="stat-cell" onclick="switchView('comps',this)">
    <div class="stat-number">{n_comps:,}</div>
    <div class="stat-label">Comps</div>
  </div>
  <div class="stat-cell" onclick="switchView('listings',this);filterDeals()">
    <div class="stat-number green">{n_deals}</div>
    <div class="stat-label">Deals</div>
  </div>
</div>
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

<div class="body-area">

<!-- ── Sidebar ── -->
<aside class="sidebar">
  <div class="filter-group">
    <span class="filter-group-label">Cooling</span>
    <div class="chip-row">
      <button class="chip" data-val="Air-cooled" onclick="toggleCooling(this,&apos;air&apos;)" title="911s 1998 &amp; earlier">Air-cooled</button>
      <button class="chip" data-val="Water-cooled" onclick="toggleCooling(this,&apos;water&apos;)" title="911s 1999 &amp; newer">Water-cooled</button>
    </div>
  </div>

  <div class="filter-group">
    <span class="filter-group-label">Generation</span>
    <div class="chip-row" id="gen-chips">
      {gen_chips_html}
    </div>
  </div>

  <div class="filter-group">
    <span class="filter-group-label">Source</span>
    <div class="chip-row" id="src-chips">
      {source_chips_html}
    </div>
  </div>

  <div class="filter-group">
    <span class="filter-group-label">Year</span>
    <div class="filter-range">
      <input type="number" id="f-year-min" placeholder="From" min="1984" max="2024" oninput="applyFilters()">
      <input type="number" id="f-year-max" placeholder="To"   min="1984" max="2024" oninput="applyFilters()">
    </div>
  </div>

  <div class="filter-group">
    <span class="filter-group-label">Price ($)</span>
    <div class="filter-range">
      <input type="number" id="f-price-min" placeholder="Min" oninput="applyFilters()">
      <input type="number" id="f-price-max" placeholder="Max" oninput="applyFilters()">
    </div>
  </div>

  <div class="filter-group">
    <span class="filter-group-label">Type</span>
    <div class="filter-checkboxes">
      <label><input type="checkbox" id="f-deals" onchange="applyFilters()"> Deals only (&darr;5%+ FMV)</label>
      <label><input type="checkbox" id="f-tier1" onchange="applyFilters()"> GT / Collector</label>
    </div>
  </div>

  <button class="reset-btn" onclick="resetFilters()">&#x21BA; Reset Filters</button>
</aside>

<!-- ── Main ── -->
<main class="main">

  <!-- Stats strip -->
  <div class="stats-strip">
    <div class="stat-cell">
      <div class="stat-num">{n_active:,}</div>
      <div class="stat-lbl">Active</div>
    </div>
    <div class="stat-cell">
      <div class="stat-num">{n_new:,}</div>
      <div class="stat-lbl">New Today</div>
    </div>
    <div class="stat-cell">
      <div class="stat-num yellow">{n_auctions}</div>
      <div class="stat-lbl">Auctions</div>
    </div>
    <div class="stat-cell">
      <div class="stat-num">{n_comps:,}</div>
      <div class="stat-lbl">Comps</div>
    </div>
    <div class="stat-cell">
      <div class="stat-num green">{n_deals}</div>
      <div class="stat-lbl">Deals</div>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="main-toolbar">
    <div style="display:flex;align-items:center;gap:10px;">
      <div class="search-wrap">
        <span class="search-icon">&#x1F50D;</span>
        <input class="search-input" type="text" id="search-box" placeholder="Year, model, trim&hellip;" oninput="applyFilters()">
      </div>
      <button class="filter-fab" id="filter-fab" onclick="openDrawer()">&#x25A6; Filters</button>
    </div>
    <div style="display:flex;align-items:center;gap:10px;">
      <select id="sort-select" onchange="applyFilters()" style="padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;outline:none;background:var(--bg3);color:var(--text)">
        <option value="new">Newest First</option>
        <option value="price_asc">Price &#x2191;</option>
        <option value="price_desc">Price &#x2193;</option>
        <option value="dom_desc">Longest Listed</option>
      </select>
      <span class="results-count" id="results-count"></span>
    </div>
  </div>

  <div class="content-area">

    <!-- ── View: Listings ── -->
    <div class="view active" id="view-listings">
      <div class="section-header">
        <div>
          <div class="section-title">All Active Listings</div>
          <div class="section-sub">Newest first &middot; All 10 sources &middot; Filters apply</div>
        </div>
      </div>
      <div class="cards-grid" id="cards-grid">
        {all_cards if all_cards else '<div class="empty"><div class="empty-icon">&#x1F4ED;</div><div class="empty-text">No listings found</div></div>'}
      </div>
    </div>

    <!-- ── View: Comps ── -->
    <div class="view" id="view-comps">
      <div class="section-header">
        <div>
          <div class="section-title">Sold Comps</div>
          <div class="section-sub">24-month rolling &middot; BaT, pcarmarket, C&amp;B &middot; {n_comps:,} records</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="text" id="comp-search" placeholder="Filter comps&hellip;"
            style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;outline:none;width:180px;background:var(--bg3);color:var(--text)"
            oninput="filterComps()">
          <select id="comp-gen-filter"
            style="padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;outline:none;background:var(--bg3);color:var(--text)"
            onchange="filterComps()">
            <option value="">All Gens</option>
            {gen_opts}
          </select>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl" id="comps-tbl">
          <thead>
            <tr>
              <th>Source</th>
              <th onclick="sortComps('year')">Year &updownarrow;</th>
              <th>Model</th>
              <th onclick="sortComps('gen')">Gen &updownarrow;</th>
              <th>Trans</th>
              <th onclick="sortComps('mileage')">Miles &updownarrow;</th>
              <th onclick="sortComps('price')" style="text-align:right">Sold $ &updownarrow;</th>
              <th onclick="sortComps('date')">Date &updownarrow;</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="comps-body">
            {comp_rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── View: Market ── -->
    <div class="view" id="view-market">
      <div class="section-header">
        <div>
          <div class="section-title">Market Reports</div>
          <div class="section-sub">Generated reports</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px">
        <a href="market_report.html" class="report-card">
          <div class="report-icon">&#x1F4CA;</div>
          <div class="report-title">Market Analysis</div>
          <div class="report-sub">FMV distribution, segment breakdown</div>
        </a>
        <a href="daily_report.html" class="report-card">
          <div class="report-icon">&#x1F528;</div>
          <div class="report-title">Daily Auctions</div>
          <div class="report-sub">Today&apos;s BaT / pcarmarket activity</div>
        </a>
        <a href="weekly_report.html" class="report-card">
          <div class="report-icon">&#x1F4C5;</div>
          <div class="report-title">Weekly Report</div>
          <div class="report-sub">Week-over-week trends</div>
        </a>
        <a href="monthly_report.html" class="report-card">
          <div class="report-icon">&#x1F4C8;</div>
          <div class="report-title">Monthly Report</div>
          <div class="report-sub">Macro trends and market direction</div>
        </a>
      </div>
    </div>

  </div><!-- /content-area -->
</main>
</div><!-- /body-area -->
</div><!-- /app -->

<!-- ── Mobile filter drawer ── -->
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>
<div class="filter-drawer" id="filter-drawer">
  <span class="drawer-handle"></span>
  <div class="drawer-title">Filters</div>
  <div class="drawer-grid">
    <div class="drawer-section" style="grid-column:1/-1">
      <span class="drawer-section-label">Cooling</span>
      <div class="drawer-chips">
        <button class="chip" data-val="Air-cooled" onclick="toggleCooling(this,&apos;air&apos;)" title="911s 1998 &amp; earlier">Air-cooled</button>
        <button class="chip" data-val="Water-cooled" onclick="toggleCooling(this,&apos;water&apos;)" title="911s 1999 &amp; newer">Water-cooled</button>
      </div>
    </div>
    <div class="drawer-section">
      <span class="drawer-section-label">Generation</span>
      <div class="drawer-chips" id="d-gen-chips">
        {gen_chips_html}
      </div>
    </div>
    <div class="drawer-section">
      <span class="drawer-section-label">Source</span>
      <div class="drawer-chips" id="d-src-chips">
        {source_chips_html}
      </div>
    </div>
    <div class="drawer-section">
      <span class="drawer-section-label">Year</span>
      <div class="drawer-range">
        <input type="number" id="d-year-min" placeholder="From" min="1984" max="2024" oninput="syncFromDrawer()">
        <input type="number" id="d-year-max" placeholder="To"   min="1984" max="2024" oninput="syncFromDrawer()">
      </div>
    </div>
    <div class="drawer-section">
      <span class="drawer-section-label">Price ($)</span>
      <div class="drawer-range">
        <input type="number" id="d-price-min" placeholder="Min" oninput="syncFromDrawer()">
        <input type="number" id="d-price-max" placeholder="Max" oninput="syncFromDrawer()">
      </div>
    </div>
    <div class="drawer-section" style="grid-column:1/-1">
      <span class="drawer-section-label">Type</span>
      <div class="drawer-checkboxes">
        <label><input type="checkbox" id="d-deals" onchange="syncFromDrawer()"> Deals only (&darr;5%+ FMV)</label>
        <label><input type="checkbox" id="d-tier1" onchange="syncFromDrawer()"> GT / Collector</label>
      </div>
    </div>
  </div>
  <div class="drawer-actions">
    <button class="drawer-apply" onclick="closeDrawer()">Apply</button>
    <button class="drawer-reset" onclick="resetFilters();closeDrawer()">Reset</button>
  </div>
</div>

<script>
// ── Card data (no DOM scanning — filter works on this array) ──────────────────
var CARD_DATA = {card_data_json};
var visibleCards = [];
var renderedCount = 0;
var PAGE = 48;

// ── Chip filter state ─────────────────────────────────────────────────────────
var activeGens = [];
var activeSrcs = [];
var activeCooling = null;

function toggleCooling(btn, type) {{
  if (activeCooling === type) {{
    activeCooling = null;
    document.querySelectorAll('.chip[data-val="Air-cooled"],.chip[data-val="Water-cooled"]').forEach(function(c) {{ c.classList.remove('active'); }});
  }} else {{
    document.querySelectorAll('.chip[data-val="Air-cooled"],.chip[data-val="Water-cooled"]').forEach(function(c) {{ c.classList.remove('active'); }});
    activeCooling = type;
    btn.classList.add('active');
  }}
  applyFilters();
  updateFabState();
}}

function toggleChip(btn, type) {{
  btn.classList.toggle('active');
  var val = btn.dataset.val;
  document.querySelectorAll('.chip[data-val="' + val + '"]').forEach(function(c) {{
    if (c !== btn) c.classList.toggle('active', btn.classList.contains('active'));
  }});
  if (type === 'gen') {{
    var idx = activeGens.indexOf(val);
    if (idx > -1) activeGens.splice(idx,1); else activeGens.push(val);
  }} else {{
    var idx = activeSrcs.indexOf(val);
    if (idx > -1) activeSrcs.splice(idx,1); else activeSrcs.push(val);
  }}
  applyFilters();
  updateFabState();
}}

// ── Main filter — works on CARD_DATA array, not DOM ───────────────────────────
function applyFilters() {{
  var q       = (document.getElementById('search-box').value || '').toLowerCase();
  var yMin    = parseInt(document.getElementById('f-year-min').value)  || 0;
  var yMax    = parseInt(document.getElementById('f-year-max').value)  || 9999;
  var pMin    = parseInt(document.getElementById('f-price-min').value) || 0;
  var pMax    = parseInt(document.getElementById('f-price-max').value) || 999999999;
  var dealsOnly = document.getElementById('f-deals').checked;
  var tier1Only = document.getElementById('f-tier1').checked;

  visibleCards = CARD_DATA.filter(function(d) {{
    if (q && d.txt.indexOf(q) === -1) return false;
    if (d.yr < yMin || d.yr > yMax) return false;
    if (pMin && d.pr < pMin) return false;
    if (pMax < 999999999 && d.pr > pMax) return false;
    if (activeGens.length && activeGens.indexOf(d.gen) === -1) return false;
    if (activeSrcs.length && activeSrcs.indexOf(d.src) === -1) return false;
    if (dealsOnly && !d.deal) return false;
    if (tier1Only && d.tier !== 'TIER1') return false;
    if (activeCooling && d.cool !== activeCooling) return false;
    return true;
  }});

  var _sortEl = document.getElementById('sort-select');
  var _sortVal = _sortEl ? _sortEl.value : 'new';
  if (_sortVal === 'price_asc') {{
    visibleCards.sort(function(a, b) {{ return (a.pr || 0) - (b.pr || 0); }});
  }} else if (_sortVal === 'price_desc') {{
    visibleCards.sort(function(a, b) {{ return (b.pr || 0) - (a.pr || 0); }});
  }} else if (_sortVal === 'dom_desc') {{
    visibleCards.sort(function(a, b) {{ return (b.dom || 0) - (a.dom || 0); }});
  }}

  renderedCount = 0;
  renderCards();

  var rc = document.getElementById('results-count');
  if (rc) rc.textContent = visibleCards.length + ' listing' + (visibleCards.length !== 1 ? 's' : '');
}}

function renderCards() {{
  var grid = document.getElementById('cards-grid');
  if (!grid) return;
  var next = Math.min(renderedCount + PAGE, visibleCards.length);
  var html = '';
  for (var i = renderedCount; i < next; i++) {{
    html += visibleCards[i].html;
  }}
  if (renderedCount === 0) {{
    grid.innerHTML = html || '<div class="empty"><div class="empty-icon">&#x1F4ED;</div><div class="empty-text">No listings match</div></div>';
  }} else {{
    grid.insertAdjacentHTML('beforeend', html);
  }}
  renderedCount = next;
  // Restart countdowns for newly inserted auction cards
  startCountdowns();
}}

// ── Infinite scroll ───────────────────────────────────────────────────────────
var _ca = document.querySelector('.content-area');
if (_ca) {{
  _ca.addEventListener('scroll', function() {{
    if (renderedCount >= visibleCards.length) return;
    if (_ca.scrollTop + _ca.clientHeight >= _ca.scrollHeight - 600) {{
      renderCards();
    }}
  }}, {{passive: true}});
}}

function resetFilters() {{
  activeGens = []; activeSrcs = [];
  activeCooling = null;
  document.querySelectorAll('.chip').forEach(function(c) {{ c.classList.remove('active'); }});
  ['f-year-min','f-year-max','f-price-min','f-price-max',
   'd-year-min','d-year-max','d-price-min','d-price-max'].forEach(function(id) {{
    var el = document.getElementById(id); if (el) el.value = '';
  }});
  ['f-deals','f-tier1','d-deals','d-tier1'].forEach(function(id) {{
    var el = document.getElementById(id); if (el) el.checked = false;
  }});
  document.getElementById('search-box').value = '';
  applyFilters();
  updateFabState();
}}

// ── Drawer sync ───────────────────────────────────────────────────────────────
var _SYNC = [['d-year-min','f-year-min'],['d-year-max','f-year-max'],
             ['d-price-min','f-price-min'],['d-price-max','f-price-max'],
             ['d-deals','f-deals'],['d-tier1','f-tier1']];

function syncFromDrawer() {{
  _SYNC.forEach(function(p) {{
    var s = document.getElementById(p[0]), d = document.getElementById(p[1]);
    if (!s || !d) return;
    if (s.type === 'checkbox') d.checked = s.checked; else d.value = s.value;
  }});
  applyFilters(); updateFabState();
}}

function openDrawer() {{
  _SYNC.forEach(function(p) {{
    var s = document.getElementById(p[1]), d = document.getElementById(p[0]);
    if (!s || !d) return;
    if (s.type === 'checkbox') d.checked = s.checked; else d.value = s.value;
  }});
  document.getElementById('filter-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}}

function closeDrawer() {{
  document.getElementById('filter-drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
  document.body.style.overflow = '';
}}

function updateFabState() {{
  var fab = document.getElementById('filter-fab');
  if (!fab) return;
  var on = activeGens.length || activeSrcs.length ||
    document.getElementById('f-year-min').value ||
    document.getElementById('f-price-min').value ||
    document.getElementById('f-deals').checked ||
    document.getElementById('f-tier1').checked;
  fab.classList.toggle('has-filters', !!(on || activeCooling));
}}

// ── View switcher ─────────────────────────────────────────────────────────────
var _currentView = 'listings';
function switchView(name, btn) {{
  document.querySelectorAll('.view').forEach(function(v) {{ v.classList.remove('active'); }});
  document.querySelectorAll('.pill').forEach(function(b) {{ b.classList.remove('active'); }});
  var v = document.getElementById('view-' + name);
  if (v) v.classList.add('active');
  if (btn) btn.classList.add('active');
  _currentView = name;
  if (name === 'listings') startCountdowns();
}}

function filterToday() {{
  // TODO: filter cards to today's listings
}}
function filterDeals() {{
  // TODO: filter cards to deals only (10%+ below FMV)
}}
function toggleDropdown() {{
  document.getElementById('dd-overlay').classList.toggle('show');
}}
function closeDropdown() {{
  document.getElementById('dd-overlay').classList.remove('show');
}}

// ── Smart auto-refresh (no reload, no filter wipe) ───────────────────────────
function _autoRefresh() {{
  fetch(location.href + '?_nc=' + Date.now(), {{cache:'no-store'}})
    .then(function(r) {{ return r.text(); }})
    .then(function(html) {{
      var m = html.match(/var CARD_DATA = (\[[\s\S]*?\]);/);
      if (!m) return;
      var fresh = JSON.parse(m[1]);
      if (fresh.length === CARD_DATA.length) return;
      CARD_DATA = fresh;
      applyFilters();
    }}).catch(function() {{}});
}}
setInterval(_autoRefresh, 180000);


// ── Pull-to-refresh ───────────────────────────────────────────────────────────
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
    _autoRefresh();
    setTimeout(function() {{ setProgress(0); }}, 800);
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

// ── Auction countdown ─────────────────────────────────────────────────────────
function startCountdowns() {{
  document.querySelectorAll('.countdown[data-ends]').forEach(function(el) {{
    if (el._ticking) return;
    el._ticking = true;
    function tick() {{
      var ends = new Date(el.dataset.ends.replace(' ','T') + 'Z');
      var diff = Math.max(0, ends - Date.now());
      if (diff === 0) {{ el.textContent = 'Ended'; return; }}
      var h = Math.floor(diff/3600000);
      var m = Math.floor((diff%3600000)/60000);
      var s = Math.floor((diff%60000)/1000);
      el.textContent = h ? h+'h '+m+'m' : m ? m+'m '+s+'s' : s+'s';
      setTimeout(tick, 1000);
    }}
    tick();
  }});
}}

// ── Comps filter / sort ───────────────────────────────────────────────────────
function filterComps() {{
  var q   = (document.getElementById('comp-search').value || '').toLowerCase();
  var gen = document.getElementById('comp-gen-filter').value.toLowerCase();
  document.querySelectorAll('#comps-body .comp-row').forEach(function(r) {{
    var txt  = r.textContent.toLowerCase();
    var rgen = (r.dataset.gen || '').toLowerCase();
    r.style.display = ((!q || txt.indexOf(q) > -1) && (!gen || rgen === gen)) ? '' : 'none';
  }});
}}

var _compSort = {{col:'date', asc:false}};
function sortComps(col) {{
  _compSort.asc = (_compSort.col === col) ? !_compSort.asc : false;
  _compSort.col = col;
  var rows = Array.from(document.querySelectorAll('#comps-body .comp-row'));
  rows.sort(function(a,b) {{
    var cells = {{year:1,gen:3,mileage:5,price:6,date:7}};
    var ci = cells[col] !== undefined ? cells[col] : 7;
    var av = a.cells[ci] ? a.cells[ci].textContent.replace(/[$,]/g,'') : '';
    var bv = b.cells[ci] ? b.cells[ci].textContent.replace(/[$,]/g,'') : '';
    var an = parseFloat(av), bn = parseFloat(bv);
    var cmp = (!isNaN(an) && !isNaN(bn)) ? (an-bn) : av.localeCompare(bv);
    return _compSort.asc ? cmp : -cmp;
  }});
  var body = document.getElementById('comps-body');
  rows.forEach(function(r) {{ body.appendChild(r); }});
}}

// ── PWA-safe listing navigation ───────────────────────────────────────────────
function openListing(url) {{
  // Always open in a new window/tab. On iOS PWA standalone mode,
  // this opens Safari as an overlay — closing it returns to the PWA.
  // Do NOT use window.location.href — that replaces the PWA page
  // and causes a white screen when navigating back.
  window.open(url, '_blank');
}}

window.addEventListener('pageshow', function(e) {{
  if (e.persisted) {{ /* page restored from bfcache — no action needed */ }}
}});

// ── Live timestamps (client-side, updates every 60s) ─────────────────────────
function _fmtAge(isoStr) {{
  if (!isoStr) return '';
  var dt = new Date(isoStr.replace(' ', 'T'));
  if (isNaN(dt)) return '';
  var mins = Math.floor((Date.now() - dt) / 60000);
  if (mins < 2)  return 'just now';
  if (mins < 60) return mins + 'm ago';
  var h = Math.floor(mins / 60);
  if (h < 24)    return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}}
function updateTimestamps() {{
  document.querySelectorAll('.card-age[data-created]').forEach(function(el) {{
    el.textContent = _fmtAge(el.dataset.created);
  }});
}}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function() {{
  if (window.location.hash === '#comps') {{
    switchView('comps', document.getElementById('pill-comps'));
  }}
  applyFilters();
  startCountdowns();
  updateTimestamps();
  setInterval(updateTimestamps, 60000);
}});
</script>
</html>"""

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    return str(OUT_PATH)


if __name__ == "__main__":
    path = generate()
    print(f"Dashboard: file://{path}")

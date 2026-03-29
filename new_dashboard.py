"""
new_dashboard.py — Redesigned unified dashboard.

Layout inspired by porsche-db.com:
  - Clean white, minimal chrome
  - Left sidebar: filters + nav
  - Main area defaults to "New Listings" (live feed view — newest first, all sources)
  - Secondary views: Market Analysis, Auctions, Sold Comps
  - Single self-contained HTML file, no external dependencies

Output: static/index.html
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
OUT_PATH  = BASE_DIR / "static" / "index.html"
LOG_DIR   = BASE_DIR / "logs"

# ── Source health (reused from dashboard.py) ─────────────────────────────────

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_SOURCES = [
    ("Main Scraper",   "scraper.log",          "Dealers + BaT + PCA",     "com.porschetracker.scrape",          45),
    ("Distill Poller", "distill_poller.log",   "Reads Distill DB",        "com.porschetracker.distill-poller",   5),
    ("Distill",        None,                   "Webhook receiver",        None,                                 90),
    ("Archive",        "archive_capture.log",  "HTML+screenshot capture", "com.porschetracker.archive-capture", 30),
]
_DISTILL_LABELS = ["com.porschetracker.distill-receiver", "com.porschetracker.distill-watcher"]
_DISTILL_LOGS   = ["distill_receiver.log", "distill_watcher.log"]

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
        if log_file is None and name == "Distill":
            candidates = [_last_log_ts(lf) for lf in _DISTILL_LOGS]
            best_ts, best_line = None, ""
            for ts_c, line_c in candidates:
                if ts_c and (best_ts is None or ts_c > best_ts):
                    best_ts, best_line = ts_c, line_c
            ts, last_line = best_ts, best_line
            pid = next((p for lbl in _DISTILL_LABELS for p in [_launchd_pid(lbl)] if p), None)
        else:
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

def _m(v) -> str:
    if v is None: return "—"
    try:    return f"{int(v):,}"
    except: return "—"

def _h(s) -> str:
    return _html.escape(str(s or ""))

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

# Source badge config — dark-mode friendly (semi-transparent tinted backgrounds)
_BADGE_CFG = {
    "bring a trailer": ("#1e3a5f", "#60a5fa", "BaT"),
    "bat":             ("#1e3a5f", "#60a5fa", "BaT"),
    "pcarmarket":      ("#14532d", "#4ade80", "pcarmarket"),
    "cars & bids":     ("#431407", "#fb923c", "C&B"),
    "carsandbids":     ("#431407", "#fb923c", "C&B"),
    "classic.com":     ("#3b0764", "#c084fc", "classic"),
    "rennlist":        ("#4c0519", "#f472b6", "Rennlist"),
    "pca mart":        ("#0c4a6e", "#38bdf8", "PCA Mart"),
    "autotrader":      ("#3f2d00", "#fbbf24", "AutoTrader"),
    "cars.com":        ("#052e16", "#86efac", "Cars.com"),
    "ebay motors":     ("#3f1f00", "#fb923c", "eBay"),
}
_AUCTION_SET = frozenset({"bring a trailer","bat","bringatrailer","pcarmarket","cars & bids","carsandbids","classic.com"})

def _badge(dealer: str) -> str:
    k = (dealer or "").lower().strip()
    bg, fg, label = _BADGE_CFG.get(k, ("#f3f4f6", "#374151", (dealer or "?")[:12]))
    return f'<span class="badge" style="background:{bg};color:{fg}">{_h(label)}</span>'

def _is_auction(dealer: str) -> bool:
    return (dealer or "").lower().strip() in _AUCTION_SET

def _delta_html(price, fmv_val, conf) -> str:
    """Compact % chip for non-auction cards."""
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

def _fmv_block(price, fmv_val, conf, comp_count, is_auction) -> str:
    """Full FMV line shown on every card. Auctions get the most detail."""
    if not fmv_val or conf == "NONE":
        return '<div class="fmv-line fmv-none">FMV: not enough comps yet</div>'
    try:
        pct = (float(price) - float(fmv_val)) / float(fmv_val) * 100 if price else None
    except:
        pct = None

    fmv_str = _p(fmv_val)
    comp_str = f"{comp_count} comp{'s' if comp_count != 1 else ''}"

    if pct is None:
        rel = ""
        cls = "fmv-neutral"
    elif abs(pct) < 2:
        rel = "at market"
        cls = "fmv-neutral"
    elif pct < -10:
        rel = f"<strong>{abs(pct):.0f}% below FMV</strong> 🔥"
        cls = "fmv-great"
    elif pct < 0:
        rel = f"{abs(pct):.0f}% below FMV"
        cls = "fmv-good"
    elif pct > 15:
        rel = f"<strong>{pct:.0f}% above FMV</strong>"
        cls = "fmv-high"
    else:
        rel = f"{pct:.0f}% above FMV"
        cls = "fmv-mid"

    conf_dot = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "⚪")

    if is_auction:
        # Full treatment for auctions: FMV figure prominent, relationship clear
        return (f'<div class="fmv-line {cls}">'
                f'{conf_dot} Est. FMV <span class="fmv-val">{fmv_str}</span>'
                f'{(" · " + rel) if rel else ""}'
                f' <span class="fmv-comps">({comp_str})</span>'
                f'</div>')
    else:
        # Compact for retail/dealer
        return (f'<div class="fmv-line {cls}">'
                f'{conf_dot} FMV ~{fmv_str}'
                f'{(" · " + rel) if rel else ""}'
                f'</div>')

# ── Generation helper ─────────────────────────────────────────────────────────

def _gen(year, model):
    if not year: return "Unknown"
    y = int(year); m = (model or "").lower()
    if "911" in m or m in ("911","930","964","993","996","997","991","992"):
        if y <= 1977: return "G-Series"
        if y <= 1989: return "G-Series" if y < 1989 else ("G-Series" if "carrera" in m else "964")
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
    created  = car.get("date_first_seen", "") or car.get("created_at", "")
    location = car.get("location", "") or ""
    trans    = car.get("transmission", "") or ""
    days     = car.get("days_on_site") or 0
    tier     = car.get("tier", "") or ""
    is_auc   = _is_auction(dealer)

    fmv_val    = fmv_score.get("fmv")
    conf       = fmv_score.get("confidence", "NONE")
    comp_count = fmv_score.get("comp_count", 0)
    delta      = _delta_html(price, fmv_val, conf)
    fmv_block  = _fmv_block(price, fmv_val, conf, comp_count, is_auc)

    age_str  = _age_label(created)

    # Price label
    if is_auc:
        price_lbl = "Current Bid"
        price_cls = "price-auction"
    else:
        price_lbl = "Asking"
        price_cls = "price-ask"

    # Image — onerror swaps to a styled SVG placeholder so no broken icons
    placeholder_svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='165'%3E%3Crect width='400' height='165' fill='%231e2530'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' font-family='sans-serif' font-size='13' fill='%234b5563'%3ENo photo%3C/text%3E%3C/svg%3E"
    # For PCA Mart images, fetch via JS with the correct Referer header
    # (hotlink protection blocks direct <img src> from file:// pages)
    is_pca_img = "mart.pca.org" in img
    if img and is_pca_img:
        img_id = f"pcaimg_{abs(hash(img)) % 999999}"
        img_html = (
            f'<div class="card-img-wrap">'
            f'<img id="{img_id}" src="{placeholder_svg}" alt="{_h(str(year)+" "+model)}" class="card-img" loading="lazy">'
            f'<script>(function(){{'
            f'var x=new XMLHttpRequest();x.open("GET","{_h(img)}",true);'
            f'x.setRequestHeader("Referer","https://mart.pca.org/");'
            f'x.responseType="blob";'
            f'x.onload=function(){{if(x.status==200){{var u=URL.createObjectURL(x.response);document.getElementById("{img_id}").src=u;}}}};'
            f'x.send();'
            f'}})();</script>'
            f'</div>'
        )
    else:
        img_html = (
            f'<div class="card-img-wrap">'
            f'<img src="{_h(img)}" alt="{_h(str(year)+" "+model)}" class="card-img" loading="lazy" '
            f'onerror="this.src=\'{placeholder_svg}\';this.classList.add(\'img-fallback\')">'
            f'</div>'
            if img else
            f'<div class="card-img-wrap">'
            f'<img src="{placeholder_svg}" alt="No photo" class="card-img img-fallback">'
            f'</div>'
        )

    # Tier badge
    tier_html = ""
    if tier == "TIER1":
        tier_html = '<span class="tier-badge">GT / Collector</span>'

    # Meta chips
    chips = []
    if trans:    chips.append(_h(trans))
    if mileage:  chips.append(f"{_m(mileage)} mi")
    if location: chips.append(f"📍 {_h(location[:22])}")
    chips_html = " · ".join(chips)

    # Days sitting — flag if stale
    days_html = ""
    if days and int(days) >= 30:
        days_html = f' · <span class="days-stale">⏱ {days}d listed</span>'

    return f"""<div class="card" data-dealer="{_h(dealer)}" data-year="{year}" data-model="{_h(model)}" data-gen="{_h(_gen(year,model))}" data-tier="{_h(tier)}" data-price="{price or 0}" data-source-type="{'auction' if is_auc else 'retail'}" onclick="window.open('{_h(url)}','_blank')">
  {img_html}
  <div class="card-body">
    <div class="card-top-row">
      {_badge(dealer)}
      <span class="card-age">{age_str}</span>
    </div>
    <div class="card-title">{year} Porsche {_h(model)}{(' ' + _h(trim)) if trim else ''}</div>
    {tier_html}
    <div class="card-price-row">
      <span class="price-label">{price_lbl}</span>
      <span class="{price_cls}">{_p(price)}</span>
      {delta if not is_auc else ""}
    </div>
    {fmv_block}
    <div class="card-meta">{chips_html}{days_html}</div>
  </div>
</div>"""

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

    return f"""<tr class="comp-row" data-gen="{_h(gen)}" data-year="{year}" data-model="{_h(model)}">
  <td>{_badge(source)}</td>
  <td>{year}</td>
  <td class="td-model">{_h(model)} {_h(trim)}</td>
  <td>{_h(gen)}</td>
  <td>{_h(trans) or '—'}</td>
  <td>{_m(mileage)}</td>
  <td class="td-price">{_p(price)}</td>
  <td>{sold_dt or '—'}</td>
  <td><a href="{_h(url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="tbl-link">→</a></td>
</tr>"""

# ── Source health pills ───────────────────────────────────────────────────────

def _health_pills(health: list) -> str:
    out = []
    for s in health:
        cls = {"ok": "pill-ok", "stale": "pill-stale", "error": "pill-error"}.get(s["status"], "pill-unknown")
        out.append(f'<span class="health-pill {cls}" title="{_h(s["name"])}: {_h(s["age"])}">'
                   f'{_h(s["name"])} <span class="pill-age">{_h(s["age"])}</span></span>')
    return "\n".join(out)

# ── Main generate ─────────────────────────────────────────────────────────────

def generate() -> str:
    init_db()
    with get_conn() as conn:
        d = get_dashboard_data(conn)
        fmv_scored_list = fmv_engine.score_active_listings(conn)  # list of dicts with .fmv FMVResult

        # Build id → fmv lookup from scored list
        fmv_by_id = {}
        for row in fmv_scored_list:
            fmv_obj = row.get("fmv")  # FMVResult object or None
            if fmv_obj:
                fmv_by_id[row["id"]] = {
                    "fmv":        getattr(fmv_obj, "weighted_median", None),
                    "confidence": getattr(fmv_obj, "confidence", "NONE"),
                    "comp_count": getattr(fmv_obj, "comp_count", 0),
                }
            else:
                fmv_by_id[row["id"]] = {"fmv": None, "confidence": "NONE", "comp_count": 0}

        # All active listings with FMV scores attached
        active = d["active"]

        # Display filter — no Holt, no pre-1984
        def _keep(c):
            if (c.get("dealer") or "").lower() == "holt motorsports": return False
            if (c.get("year") or 9999) < 1984: return False
            return True
        active = [c for c in active if _keep(c)]

        # Attach FMV scores
        for c in active:
            c["_fmv"] = fmv_by_id.get(c["id"], {"fmv": None, "confidence": "NONE", "comp_count": 0})

        # Sort newest first (default view)
        active_sorted = sorted(active, key=lambda c: c.get("date_first_seen") or "", reverse=True)

        # Sold comps — last 24 months
        cutoff = (date.today() - timedelta(days=730)).isoformat()
        comp_rows = conn.execute("""
            SELECT * FROM sold_comps
            WHERE sold_date >= ? AND sold_price IS NOT NULL
            ORDER BY sold_date DESC
        """, (cutoff,)).fetchall()
        comps = [dict(r) for r in comp_rows]

        # Active auctions only
        auctions = [c for c in active if _is_auction(c.get("dealer", ""))]

        # Stats
        today = d["today"]
        new_today   = [c for c in d["new_today"]   if _keep(c)]
        sold_today  = [c for c in d["sold_today"]  if _keep(c)]
        sitting_30  = [c for c in active if (c.get("days_on_site") or 0) >= 30]
        n_active    = len(active)
        n_new       = len(new_today)
        n_auctions  = len(auctions)
        n_comps     = len(comps)
        n_deals     = sum(1 for c in active if (
                          c["_fmv"].get("fmv") and c.get("price") and
                          c["_fmv"]["confidence"] != "NONE" and
                          float(c["price"]) < float(c["_fmv"]["fmv"]) * 0.95))

        # Health
        health = _source_health()
        health_html = _health_pills(health)

        # Build card HTML for all listings (JS will filter/show/hide)
        all_cards = "\n".join(_card(c, c["_fmv"]) for c in active_sorted)

        # Build auction cards
        auction_cards = "\n".join(_card(c, c["_fmv"]) for c in sorted(
            auctions, key=lambda c: c.get("date_first_seen") or "", reverse=True))

        # Build comp rows HTML
        comp_rows_html = "\n".join(_comp_row(c) for c in comps)

        # Collect unique values for filter dropdowns (from JS data)
        generations = sorted(set(_gen(c.get("year"), c.get("model")) for c in active if c.get("year")))
        models      = sorted(set(c.get("model", "") for c in active if c.get("model")))
        sources     = sorted(set(c.get("dealer", "") for c in active if c.get("dealer")))

        gen_opts     = "\n".join(f'<option value="{_h(g)}">{_h(g)}</option>' for g in generations)
        model_opts   = "\n".join(f'<option value="{_h(m)}">{_h(m)}</option>' for m in models)
        source_opts  = "\n".join(f'<option value="{_h(s)}">{_h(s)}</option>' for s in sources)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="180">
<title>Porsche Tracker</title>
<style>
/* ── Reset & Base ── */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;background:#0f1117;color:#e2e8f0}}
a{{color:inherit;text-decoration:none}}
button{{cursor:pointer;border:none;background:none;font:inherit;color:inherit}}

/* ── Layout ── */
.app{{display:flex;flex-direction:column;height:100vh;overflow:hidden}}
.topbar{{
  height:52px;min-height:52px;
  background:#161b27;border-bottom:1px solid #2d3748;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;gap:16px;z-index:50;
}}
.topbar-left{{display:flex;align-items:center;gap:20px}}
.logo{{font-size:1.05em;font-weight:700;color:#f1f5f9;letter-spacing:-0.3px;white-space:nowrap}}
.logo span{{color:#ef4444}}
.nav-tabs{{display:flex;gap:2px}}
.nav-tab{{
  padding:6px 14px;border-radius:6px;font-size:0.88em;font-weight:500;
  color:#94a3b8;transition:all .15s;cursor:pointer;
}}
.nav-tab:hover{{background:#2d3748;color:#f1f5f9}}
.nav-tab.active{{background:#3b82f6;color:#fff}}
.topbar-right{{display:flex;align-items:center;gap:12px;font-size:0.78em;color:#475569;white-space:nowrap}}
.health-pills{{display:flex;gap:6px;flex-wrap:nowrap}}
.health-pill{{
  padding:3px 8px;border-radius:10px;font-size:0.78em;font-weight:500;
  display:inline-flex;align-items:center;gap:5px;white-space:nowrap;
}}
.pill-ok     {{background:#14532d;color:#4ade80}}
.pill-stale  {{background:#713f12;color:#fbbf24}}
.pill-error  {{background:#7f1d1d;color:#fca5a5}}
.pill-unknown{{background:#1e293b;color:#64748b}}
.pill-age{{font-weight:400;opacity:0.75}}

.body-area{{display:flex;flex:1;overflow:hidden}}

/* ── Sidebar ── */
.sidebar{{
  width:220px;min-width:220px;
  background:#161b27;border-right:1px solid #2d3748;
  display:flex;flex-direction:column;overflow-y:auto;
  padding:16px 12px;gap:0;
}}
.sidebar-section{{margin-bottom:20px}}
.sidebar-label{{
  font-size:0.7em;font-weight:700;text-transform:uppercase;
  letter-spacing:.8px;color:#475569;margin-bottom:8px;
}}
.filter-group{{margin-bottom:12px}}
.filter-group label{{font-size:0.8em;color:#94a3b8;font-weight:500;display:block;margin-bottom:4px}}
.filter-group select{{
  width:100%;padding:6px 8px;border:1px solid #2d3748;border-radius:6px;
  font-size:0.82em;color:#e2e8f0;background:#1e2535;outline:none;
}}
.filter-group select:focus{{border-color:#3b82f6}}
.filter-range{{display:flex;gap:6px}}
.filter-range input{{
  width:100%;padding:5px 7px;border:1px solid #2d3748;border-radius:6px;
  font-size:0.82em;color:#e2e8f0;background:#1e2535;outline:none;
}}
.filter-range input:focus{{border-color:#3b82f6}}
.filter-range input::placeholder{{color:#475569}}
.filter-checkboxes{{display:flex;flex-direction:column;gap:5px}}
.filter-checkboxes label{{
  display:flex;align-items:center;gap:7px;font-size:0.82em;
  color:#94a3b8;cursor:pointer;
}}
.filter-checkboxes input[type=checkbox]{{width:14px;height:14px;cursor:pointer;accent-color:#3b82f6}}
.reset-btn{{
  width:100%;padding:7px;border-radius:6px;
  background:#1e2535;color:#94a3b8;font-size:0.82em;font-weight:500;
  border:1px solid #2d3748;transition:all .15s;
}}
.reset-btn:hover{{background:#2d3748;color:#f1f5f9}}

/* ── Main content ── */
.main{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
.main-header{{
  padding:14px 20px 12px;background:#161b27;border-bottom:1px solid #2d3748;
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
}}
.stats-row{{display:flex;gap:20px;flex-wrap:wrap}}
.stat-item{{display:flex;flex-direction:column;gap:1px}}
.stat-val{{font-size:1.5em;font-weight:700;line-height:1;color:#f1f5f9}}
.stat-val.green{{color:#4ade80}}
.stat-val.red  {{color:#f87171}}
.stat-val.blue {{color:#60a5fa}}
.stat-lbl{{font-size:0.72em;color:#475569;text-transform:uppercase;letter-spacing:.5px}}
.search-wrap{{position:relative}}
.search-input{{
  padding:7px 12px 7px 32px;border:1px solid #2d3748;border-radius:8px;
  font-size:0.88em;width:220px;outline:none;background:#1e2535;color:#e2e8f0;
}}
.search-input::placeholder{{color:#475569}}
.search-input:focus{{border-color:#3b82f6;background:#1e2535}}
.search-icon{{position:absolute;left:9px;top:50%;transform:translateY(-50%);color:#475569;font-size:1em}}
.results-count{{font-size:0.82em;color:#475569}}

.content-area{{flex:1;overflow-y:auto;padding:16px 20px;background:#0f1117}}

/* ── Cards grid ── */
.cards-grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(270px,1fr));
  gap:14px;
}}
.card{{
  background:#161b27;border:1px solid #2d3748;border-radius:10px;
  overflow:hidden;cursor:pointer;transition:box-shadow .15s,transform .15s,border-color .15s;
}}
.card:hover{{box-shadow:0 4px 24px rgba(0,0,0,.4);transform:translateY(-2px);border-color:#3b82f6}}
.card-img-wrap{{width:100%;height:165px;overflow:hidden;background:#1e2535}}
.card-img{{width:100%;height:165px;object-fit:cover;display:block;transition:transform .2s}}
.card:hover .card-img{{transform:scale(1.02)}}
.img-fallback{{opacity:0.6}}
.card-body{{padding:11px 13px 13px}}
.card-top-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
.card-age{{font-size:0.72em;color:#475569;white-space:nowrap}}
.card-title{{font-size:0.92em;font-weight:600;color:#f1f5f9;margin-bottom:4px;line-height:1.3}}
.tier-badge{{
  display:inline-block;font-size:0.68em;font-weight:700;
  background:#451a03;color:#fbbf24;padding:2px 7px;border-radius:4px;
  margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;
  border:1px solid #78350f;
}}
.card-price-row{{display:flex;align-items:baseline;gap:6px;margin-bottom:5px;flex-wrap:wrap}}
.price-label{{font-size:0.72em;color:#64748b}}
.price-ask    {{font-size:1.2em;font-weight:700;color:#f1f5f9}}
.price-auction{{font-size:1.2em;font-weight:700;color:#a78bfa}}
.card-meta{{font-size:0.75em;color:#475569;margin-top:4px}}
.days-stale{{color:#f87171}}

/* ── FMV line ── */
.fmv-line{{
  font-size:0.78em;padding:5px 8px;border-radius:5px;margin-bottom:5px;
  line-height:1.4;
}}
.fmv-val{{font-weight:700;font-size:1.05em}}
.fmv-comps{{opacity:0.65}}
.fmv-none  {{background:#1e2535;color:#475569}}
.fmv-neutral{{background:#1e2535;color:#94a3b8}}
.fmv-great {{background:#14532d;color:#86efac}}
.fmv-good  {{background:#14532d;color:#4ade80}}
.fmv-mid   {{background:#431407;color:#fdba74}}
.fmv-high  {{background:#450a0a;color:#fca5a5}}

/* ── Delta badges ── */
.delta{{
  font-size:0.72em;font-weight:700;padding:2px 6px;border-radius:5px;
  white-space:nowrap;
}}
.delta-great{{background:#14532d;color:#4ade80}}
.delta-good {{background:#14532d;color:#86efac}}
.delta-flat {{background:#1e2535;color:#64748b}}
.delta-mid  {{background:#431407;color:#fdba74}}
.delta-high {{background:#450a0a;color:#fca5a5}}

/* ── Source badge ── */
.badge{{
  font-size:0.72em;font-weight:600;padding:2px 7px;border-radius:8px;
  display:inline-block;white-space:nowrap;
}}

/* ── Table view (comps) ── */
.tbl-wrap{{overflow-x:auto;background:#161b27;border:1px solid #2d3748;border-radius:10px}}
.tbl{{width:100%;border-collapse:collapse;font-size:0.84em}}
.tbl thead tr{{background:#1e2535;border-bottom:2px solid #2d3748}}
.tbl th{{
  padding:9px 10px;text-align:left;font-weight:600;
  color:#475569;font-size:0.78em;text-transform:uppercase;
  letter-spacing:.5px;white-space:nowrap;cursor:pointer;user-select:none;
}}
.tbl th:hover{{color:#e2e8f0}}
.tbl td{{padding:8px 10px;border-bottom:1px solid #1e2535;vertical-align:middle;color:#cbd5e1}}
.tbl tbody tr:hover{{background:#1e2535}}
.tbl tbody tr:last-child td{{border-bottom:none}}
.td-price{{font-weight:600;text-align:right;color:#f1f5f9}}
.td-model{{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tbl-link{{color:#475569;font-size:1.1em;padding:2px 6px;border-radius:4px;transition:all .15s}}
.tbl-link:hover{{color:#60a5fa;background:#1e2535}}

/* ── Empty state ── */
.empty{{grid-column:1/-1;text-align:center;padding:80px 20px;color:#475569}}
.empty-icon{{font-size:3em;margin-bottom:12px}}
.empty-text{{font-size:1em;font-weight:500;color:#64748b}}
.empty-sub{{font-size:0.85em;margin-top:6px;color:#334155}}

/* ── View panels ── */
.view{{display:none}}
.view.active{{display:block}}

/* ── Section header ── */
.section-header{{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:14px;flex-wrap:wrap;gap:8px;
}}
.section-title{{font-size:1em;font-weight:700;color:#f1f5f9}}
.section-sub{{font-size:0.8em;color:#475569;margin-top:2px}}

/* ── Scrollbar ── */
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:#0f1117}}
::-webkit-scrollbar-thumb{{background:#2d3748;border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:#3b4a5e}}

/* ── Responsive ── */
@media(max-width:768px){{
  .sidebar{{display:none}}
  .topbar-right{{display:none}}
  .stats-row{{gap:12px}}
  .search-input{{width:160px}}
}}
</style>
</head>
<body>
<div class="app">

<!-- ── Top bar ── -->
<header class="topbar">
  <div class="topbar-left">
    <div class="logo">🏎 Porsche <span>Tracker</span></div>
    <nav class="nav-tabs">
      <button class="nav-tab active" onclick="switchView('listings')">New Listings</button>
      <button class="nav-tab" onclick="switchView('auctions')">Auctions</button>
      <button class="nav-tab" onclick="switchView('comps')">Sold Comps</button>
      <button class="nav-tab" onclick="switchView('market')">Market Reports</button>
    </nav>
  </div>
  <div class="topbar-right">
    <div class="health-pills">{health_html}</div>
    <span>{now_str}</span>
  </div>
</header>

<div class="body-area">

<!-- ── Sidebar ── -->
<aside class="sidebar">
  <div class="sidebar-section">
    <div class="sidebar-label">Filters</div>

    <div class="filter-group">
      <label>Generation</label>
      <select id="f-gen" onchange="applyFilters()">
        <option value="">All Generations</option>
        {gen_opts}
      </select>
    </div>

    <div class="filter-group">
      <label>Model</label>
      <select id="f-model" onchange="applyFilters()">
        <option value="">All Models</option>
        {model_opts}
      </select>
    </div>

    <div class="filter-group">
      <label>Year Range</label>
      <div class="filter-range">
        <input type="number" id="f-year-min" placeholder="From" min="1984" max="2025" onchange="applyFilters()">
        <input type="number" id="f-year-max" placeholder="To"   min="1984" max="2025" onchange="applyFilters()">
      </div>
    </div>

    <div class="filter-group">
      <label>Price Range ($)</label>
      <div class="filter-range">
        <input type="number" id="f-price-min" placeholder="Min" onchange="applyFilters()">
        <input type="number" id="f-price-max" placeholder="Max" onchange="applyFilters()">
      </div>
    </div>

    <div class="filter-group">
      <label>Source</label>
      <select id="f-source" onchange="applyFilters()">
        <option value="">All Sources</option>
        {source_opts}
      </select>
    </div>

    <div class="filter-group">
      <label>Type</label>
      <div class="filter-checkboxes">
        <label><input type="checkbox" id="f-live" checked onchange="applyFilters()"> Live Feed only</label>
        <label><input type="checkbox" id="f-deals" onchange="applyFilters()"> Deals only (↓5%+ FMV)</label>
        <label><input type="checkbox" id="f-tier1" onchange="applyFilters()"> GT/Collector only</label>
      </div>
    </div>

    <button class="reset-btn" onclick="resetFilters()">↺ Reset Filters</button>
  </div>

  <div class="sidebar-section">
    <div class="sidebar-label">Quick Links</div>
    <div style="display:flex;flex-direction:column;gap:6px">
      <a href="dashboard.html" style="font-size:0.82em;color:#64748b;padding:4px 6px;border-radius:5px;transition:all .15s" onmouseover="this.style.background='#1e2535';this.style.color='#e2e8f0'" onmouseout="this.style.background='';this.style.color='#64748b'">← Classic Dashboard</a>
      <a href="../data/live_feed.html" style="font-size:0.82em;color:#64748b;padding:4px 6px;border-radius:5px;transition:all .15s" onmouseover="this.style.background='#1e2535';this.style.color='#e2e8f0'" onmouseout="this.style.background='';this.style.color='#64748b'">Live Feed</a>
      <a href="market_report.html" style="font-size:0.82em;color:#64748b;padding:4px 6px;border-radius:5px;transition:all .15s" onmouseover="this.style.background='#1e2535';this.style.color='#e2e8f0'" onmouseout="this.style.background='';this.style.color='#64748b'">Market Report</a>
      <a href="daily_report.html" style="font-size:0.82em;color:#64748b;padding:4px 6px;border-radius:5px;transition:all .15s" onmouseover="this.style.background='#1e2535';this.style.color='#e2e8f0'" onmouseout="this.style.background='';this.style.color='#64748b'">Daily Auctions</a>
    </div>
  </div>
</aside>

<!-- ── Main ── -->
<main class="main">
  <div class="main-header">
    <div class="stats-row">
      <div class="stat-item">
        <span class="stat-val">{n_active}</span>
        <span class="stat-lbl">Active</span>
      </div>
      <div class="stat-item">
        <span class="stat-val green">{n_new}</span>
        <span class="stat-lbl">New Today</span>
      </div>
      <div class="stat-item">
        <span class="stat-val blue">{n_auctions}</span>
        <span class="stat-lbl">Auctions</span>
      </div>
      <div class="stat-item">
        <span class="stat-val">{n_comps:,}</span>
        <span class="stat-lbl">Sold Comps</span>
      </div>
      <div class="stat-item">
        <span class="stat-val green">{n_deals}</span>
        <span class="stat-lbl">Deals</span>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input class="search-input" type="text" id="search-box" placeholder="Search year, model, trim…" oninput="applyFilters()">
      </div>
      <span class="results-count" id="results-count"></span>
    </div>
  </div>

  <div class="content-area">

    <!-- ── View: New Listings ── -->
    <div class="view active" id="view-listings">
      <div class="section-header">
        <div>
          <div class="section-title">All Active Listings</div>
          <div class="section-sub">Newest first · All sources blended · Filters apply</div>
        </div>
      </div>
      <div class="cards-grid" id="cards-grid">
        {all_cards if all_cards else '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">No listings found</div></div>'}
      </div>
    </div>

    <!-- ── View: Auctions ── -->
    <div class="view" id="view-auctions">
      <div class="section-header">
        <div>
          <div class="section-title">Active Auctions</div>
          <div class="section-sub">BaT · pcarmarket · Cars &amp; Bids · classic.com — live bidding</div>
        </div>
      </div>
      <div class="cards-grid" id="auction-grid">
        {auction_cards if auction_cards else '<div class="empty"><div class="empty-icon">🔨</div><div class="empty-text">No active auctions</div><div class="empty-sub">Check back soon — BaT and pcarmarket are scraped every 45 min</div></div>'}
      </div>
    </div>

    <!-- ── View: Sold Comps ── -->
    <div class="view" id="view-comps">
      <div class="section-header">
        <div>
          <div class="section-title">Sold Comps</div>
          <div class="section-sub">24-month rolling · BaT, pcarmarket, Cars &amp; Bids, classic.com · {n_comps:,} records</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="text" id="comp-search" placeholder="Filter comps…"
            style="padding:6px 10px;border:1px solid #2d3748;border-radius:6px;font-size:0.82em;outline:none;width:180px;background:#1e2535;color:#e2e8f0"
            oninput="filterComps()">
          <select id="comp-gen-filter"
            style="padding:6px 8px;border:1px solid #2d3748;border-radius:6px;font-size:0.82em;outline:none;background:#1e2535;color:#e2e8f0"
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
              <th onclick="sortComps('year')">Year ↕</th>
              <th>Model</th>
              <th onclick="sortComps('gen')">Gen ↕</th>
              <th>Trans</th>
              <th onclick="sortComps('mileage')">Miles ↕</th>
              <th onclick="sortComps('price')" style="text-align:right">Sold $ ↕</th>
              <th onclick="sortComps('date')">Date ↕</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="comps-body">
            {comp_rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── View: Market Reports ── -->
    <div class="view" id="view-market">
      <div class="section-header">
        <div class="section-title">Market Reports</div>
        <div class="section-sub">Generated reports — open in browser</div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px">
        <a href="market_report.html" class="report-card" style="background:#161b27;border:1px solid #2d3748;border-radius:10px;padding:20px;display:block;transition:border-color .15s;color:#e2e8f0" onmouseover="this.style.borderColor='#3b82f6'" onmouseout="this.style.borderColor='#2d3748'">
          <div style="font-size:1.5em;margin-bottom:8px">📊</div>
          <div style="font-weight:600;margin-bottom:4px">Market Analysis Report</div>
          <div style="font-size:0.82em;color:#475569">Full price analysis, FMV distribution, segment breakdown</div>
        </a>
        <a href="daily_report.html" class="report-card" style="background:#161b27;border:1px solid #2d3748;border-radius:10px;padding:20px;display:block;transition:border-color .15s;color:#e2e8f0" onmouseover="this.style.borderColor='#3b82f6'" onmouseout="this.style.borderColor='#2d3748'">
          <div style="font-size:1.5em;margin-bottom:8px">🔨</div>
          <div style="font-weight:600;margin-bottom:4px">Daily Auction Report</div>
          <div style="font-size:0.82em;color:#475569">Today's BaT/pcarmarket activity and ending auctions</div>
        </a>
        <a href="weekly_report.html" class="report-card" style="background:#161b27;border:1px solid #2d3748;border-radius:10px;padding:20px;display:block;transition:border-color .15s;color:#e2e8f0" onmouseover="this.style.borderColor='#3b82f6'" onmouseout="this.style.borderColor='#2d3748'">
          <div style="font-size:1.5em;margin-bottom:8px">📅</div>
          <div style="font-weight:600;margin-bottom:4px">Weekly Report</div>
          <div style="font-size:0.82em;color:#475569">Week-over-week trends, new listings, price movements</div>
        </a>
        <a href="monthly_report.html" class="report-card" style="background:#161b27;border:1px solid #2d3748;border-radius:10px;padding:20px;display:block;transition:border-color .15s;color:#e2e8f0" onmouseover="this.style.borderColor='#3b82f6'" onmouseout="this.style.borderColor='#2d3748'">
          <div style="font-size:1.5em;margin-bottom:8px">📈</div>
          <div style="font-weight:600;margin-bottom:4px">Monthly Report</div>
          <div style="font-size:0.82em;color:#475569">Monthly macro trends, comp volume, market direction</div>
        </a>
        <a href="../data/live_feed.html" class="report-card" style="background:#161b27;border:1px solid #2d3748;border-radius:10px;padding:20px;display:block;transition:border-color .15s;color:#e2e8f0" onmouseover="this.style.borderColor='#3b82f6'" onmouseout="this.style.borderColor='#2d3748'">
          <div style="font-size:1.5em;margin-bottom:8px">⚡</div>
          <div style="font-weight:600;margin-bottom:4px">Live Feed</div>
          <div style="font-size:0.82em;color:#475569">Newest listings only — BaT, Rennlist, PCA Mart, pcarmarket</div>
        </a>
      </div>
    </div>

  </div><!-- /content-area -->
</main>
</div><!-- /body-area -->
</div><!-- /app -->

<script>
// ── View switching ────────────────────────────────────────────────────────────
function switchView(name) {{
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  event.target.classList.add('active');
  updateCount();
}}

// ── Filtering (main listings grid) ───────────────────────────────────────────
function applyFilters() {{
  const gen      = document.getElementById('f-gen').value.toLowerCase();
  const model    = document.getElementById('f-model').value.toLowerCase();
  const src      = document.getElementById('f-source').value.toLowerCase();
  const yearMin  = parseInt(document.getElementById('f-year-min').value) || 0;
  const yearMax  = parseInt(document.getElementById('f-year-max').value) || 9999;
  const priceMin = parseInt(document.getElementById('f-price-min').value) || 0;
  const priceMax = parseInt(document.getElementById('f-price-max').value) || 99999999;
  const liveOnly = document.getElementById('f-live').checked;
  const dealsOnly= document.getElementById('f-deals').checked;
  const tier1Only= document.getElementById('f-tier1').checked;
  const q        = document.getElementById('search-box').value.toLowerCase();

  const cards = document.querySelectorAll('#cards-grid .card');
  let shown = 0;
  cards.forEach(card => {{
    const cardGen   = (card.dataset.gen   || '').toLowerCase();
    const cardModel = (card.dataset.model || '').toLowerCase();
    const cardSrc   = (card.dataset.dealer|| '').toLowerCase();
    const cardYear  = parseInt(card.dataset.year) || 0;
    const cardPrice = parseInt(card.dataset.price) || 0;
    const cardTier  = (card.dataset.tier  || '').toUpperCase();
    const cardFeed  = (card.dataset.feedType || '').toLowerCase();
    const cardType  = (card.dataset.sourceType || '');
    const cardText  = card.textContent.toLowerCase();

    let show = true;
    if (gen      && cardGen   !== gen)           show = false;
    if (model    && cardModel !== model)          show = false;
    if (src      && cardSrc   !== src)            show = false;
    if (cardYear < yearMin || cardYear > yearMax) show = false;
    if (cardPrice > 0 && (cardPrice < priceMin || cardPrice > priceMax)) show = false;
    if (liveOnly && cardType !== 'auction' && cardFeed !== 'live') show = false;
    if (tier1Only&& cardTier  !== 'TIER1')        show = false;
    if (q && !cardText.includes(q))              show = false;

    card.style.display = show ? '' : 'none';
    if (show) shown++;
  }});
  updateCount(shown);
}}

function resetFilters() {{
  ['f-gen','f-model','f-source'].forEach(id => document.getElementById(id).value = '');
  ['f-year-min','f-year-max','f-price-min','f-price-max'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('f-live').checked = false;
  document.getElementById('f-deals').checked = false;
  document.getElementById('f-tier1').checked = false;
  document.getElementById('search-box').value = '';
  applyFilters();
}}

function updateCount(n) {{
  const el = document.getElementById('results-count');
  if (el) {{
    const total = document.querySelectorAll('#cards-grid .card').length;
    el.textContent = n !== undefined ? n + ' of ' + total : total + ' listings';
  }}
}}

// ── Comps table filtering & sorting ──────────────────────────────────────────
function filterComps() {{
  const q   = document.getElementById('comp-search').value.toLowerCase();
  const gen = document.getElementById('comp-gen-filter').value.toLowerCase();
  document.querySelectorAll('#comps-body .comp-row').forEach(row => {{
    const text = row.textContent.toLowerCase();
    const rowGen = (row.dataset.gen || '').toLowerCase();
    let show = true;
    if (q   && !text.includes(q))       show = false;
    if (gen && rowGen !== gen)           show = false;
    row.style.display = show ? '' : 'none';
  }});
}}

let _compSortDir = {{}};
function sortComps(col) {{
  const tbody = document.getElementById('comps-body');
  const rows  = Array.from(tbody.querySelectorAll('.comp-row'));
  const dir   = (_compSortDir[col] = !_compSortDir[col]);
  const colMap = {{year:1, gen:3, mileage:5, price:6, date:7}};
  const idx = colMap[col];
  rows.sort((a, b) => {{
    const av = a.cells[idx]?.textContent.replace(/[$,]/g,'').trim() || '';
    const bv = b.cells[idx]?.textContent.replace(/[$,]/g,'').trim() || '';
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = isNaN(an) ? av.localeCompare(bv) : an - bn;
    return dir ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {{
  updateCount();
  // Mark feed-type on cards from data attributes for live-only filter
  // (cards have data-source-type='auction' for auction sources, data needs feed_type too)
}});
</script>
</body>
</html>"""

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    return str(OUT_PATH)


if __name__ == "__main__":
    path = generate()
    print(f"New dashboard: file://{path}")

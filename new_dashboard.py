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
OUT_PATH  = BASE_DIR / "docs" / "dashboard.html"
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

# Normalize legacy underscore gen values → dot format used by filter chips
_NORM_GEN = {
    "991_1": "991.1", "991_2": "991.2",
    "997_1": "997.1", "997_2": "997.2",
    "718_cayman": "718", "718_boxster": "718",
}

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
    if conf in ("HIGH", "MEDIUM") and price_low and price_high and comp_count >= 6:
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

        # Read pre-computed FMV values from DB (written by fmv.score_and_persist()
        # each scrape cycle). Falls back to live recompute if columns are empty.
        fmv_rows = conn.execute(
            """SELECT id, fmv_value, fmv_confidence, fmv_comp_count,
                      fmv_low, fmv_high, fmv_pct
               FROM listings WHERE status='active'"""
        ).fetchall()

        fmv_by_id = {}
        needs_recompute = []
        for row in fmv_rows:
            lid, fv, fc, fcc, fl, fh, fp = row
            if fv is not None and fc and fc != "NONE":
                fmv_by_id[lid] = {
                    "fmv":        fv,
                    "confidence": fc,
                    "comp_count": fcc or 0,
                    "price_low":  fl,
                    "price_high": fh,
                }
            else:
                needs_recompute.append(lid)
                fmv_by_id[lid] = {"fmv": None, "confidence": "NONE", "comp_count": 0, "price_low": None, "price_high": None}

        # If more than 10% of listings have no FMV yet (e.g. fresh install),
        # fall back to live compute for this build only.
        if len(needs_recompute) > len(fmv_rows) * 0.10:
            log.info("FMV fallback: %d/%d listings missing DB FMV — recomputing live",
                     len(needs_recompute), len(fmv_rows))
            fmv_scored_list = fmv_engine.score_active_listings(conn)
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

        # ── VIN lifecycle: find active listings whose VIN also appears in sold_comps ──
        relisted_by_vin = {}
        try:
            relist_rows = conn.execute("""
                SELECT l.vin, sc.sold_price, sc.sold_date, sc.source
                FROM listings l
                JOIN sold_comps sc ON UPPER(l.vin) = UPPER(sc.vin)
                WHERE l.status='active' AND l.vin IS NOT NULL AND l.vin != ''
                  AND sc.sold_price IS NOT NULL AND sc.sold_price > 0
                ORDER BY sc.sold_date DESC
            """).fetchall()
            for vin, sold_price, sold_date, source in relist_rows:
                key = (vin or "").upper()
                if key not in relisted_by_vin:
                    relisted_by_vin[key] = {"sold_price": sold_price, "sold_date": sold_date, "source": source}
        except Exception as _e:
            log.warning("Relisted VIN query failed: %s", _e)

        cutoff = (date.today() - timedelta(days=730)).isoformat()
        comp_rows = conn.execute(
            "SELECT * FROM sold_comps WHERE sold_date >= ? AND sold_price IS NOT NULL ORDER BY sold_date DESC",
            (cutoff,)
        ).fetchall()
        comps = [dict(r) for r in comp_rows]

        auctions = [c for c in active if _is_auction(c.get("dealer", ""))]

        new_today  = [c for c in d["new_today"]  if _keep(c)]
        new_today_ids = set(c["id"] for c in new_today)
        n_active   = len(active)
        n_new      = len(new_today)
        # Query auction count directly from DB to match the auction page exactly.
        # Using the filtered 'active' list would miss any auction listings that
        # fail _keep() or were added between dashboard builds.
        n_auctions = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE status='active' AND source_category='AUCTION'"
        ).fetchone()[0]
        n_comps    = len(comps)
        n_deals    = sum(1 for c in active if (
                         c["_fmv"].get("fmv") and c.get("price") and
                         c["_fmv"]["confidence"] != "NONE" and
                         float(c["price"]) < float(c["_fmv"]["fmv"]) * 0.90))

        health     = _source_health()
        health_html = _health_pills(health)
        import json as _json
        card_items = []
        for c in active_sorted:
            fmv_s = c["_fmv"]
            pct = _fmv_pct(c.get("price"), fmv_s.get("fmv")) if fmv_s.get("confidence","NONE") != "NONE" else None
            dealer_k = (c.get("dealer") or "").lower().strip()
            badge_cfg = _BADGE_CFG.get(dealer_k, (None, None, (c.get("dealer") or "")[:12]))
            card_items.append({
                # --- filter/sort keys ---
                "id":   c["id"],
                "yr":   int(c.get("year") or 0),
                "pr":   int(c.get("price") or 0),
                "gen":  _NORM_GEN.get(c.get("generation") or "", c.get("generation")) or _gen(c.get("year"), c.get("model")),
                "src":  badge_cfg[2],
                "tier": c.get("tier") or "",
                "deal": pct is not None and pct <= -10,
                "nt":   c["id"] in new_today_ids,
                "cool": ("air" if (int(c.get("year") or 0) <= 1998 and "911" in (c.get("model") or "").lower())
                         else ("water" if (int(c.get("year") or 0) >= 1999 and "911" in (c.get("model") or "").lower())
                         else None)),
                "bs":   c.get("body_style") or "",
                "dom":  c.get("days_on_market") or 0,
                "txt":  ((str(c.get("year") or "") + " " + (c.get("model") or "") + " " +
                          (c.get("dealer") or "") + " " + (c.get("generation") or _gen(c.get("year"), c.get("model"))))).lower(),
                # --- raw data for client-side rendering ---
                "url":  c.get("listing_url") or "#",
                "img":  c.get("image_url") or "",
                "model": c.get("model") or "",
                "trim":  c.get("trim") or "",
                "dlr":  c.get("dealer") or "",
                "badge_label": badge_cfg[2] or "",
                "badge_bg":    badge_cfg[0] or "",
                "badge_fg":    badge_cfg[1] or "",
                "created": c.get("created_at") or c.get("date_first_seen") or "",
                "loc":   c.get("location") or "",
                "trans": c.get("transmission") or "",
                "mi":    int(c.get("mileage") or 0),
                "is_auc": _is_auction(c.get("dealer") or ""),
                "ends":  c.get("auction_ends_at") or "",
                "fmv":   int(fmv_s.get("fmv") or 0),
                "fmv_conf": fmv_s.get("confidence") or "NONE",
                "fmv_cc": int(fmv_s.get("comp_count") or 0),
                "fmv_lo": int(fmv_s.get("price_low") or 0),
                "fmv_hi": int(fmv_s.get("price_high") or 0),
                "fmv_pct": int(pct) if pct is not None else None,
                "relisted": (c.get("vin") or "").upper() in relisted_by_vin,
                "sold_prev": relisted_by_vin.get((c.get("vin") or "").upper(), {}).get("sold_price"),
                "sold_date": relisted_by_vin.get((c.get("vin") or "").upper(), {}).get("sold_date", "")[:10],
            })
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
<script>
// Apply saved theme before CSS renders — prevents flash of unstyled content
(function(){{var t=localStorage.getItem('ptox_theme');if(t)document.documentElement.dataset.theme=t;}})();
</script>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Rennmarkt">
<meta name="theme-color" content="#0A0A0C">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icons/icon-192.png">
<title>RennMarkt &mdash; Porsche Market Intelligence</title>
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
html,body {{ height:100%; background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.5; }}
a {{ color:inherit; text-decoration:none; }}
button {{ cursor:pointer; border:none; background:none; font:inherit; color:inherit; }}

/* ── Layout ── */
.app {{ display:flex; flex-direction:column; height:100vh; overflow:hidden; }}

/* ── Topbar / Nav ── */
.topbar {{
  height:68px; min-height:68px;
  background:#141414; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  padding:0 24px; gap:16px; z-index:50;
}}
.logo {{
  display:flex; align-items:center; flex-shrink:0; text-decoration:none; line-height:0;
}}
.logo svg {{ height:56px; width:auto; }}
.logo span {{ color:#c0392b; }}
.stats-bar {{ display:flex; gap:1px; margin:0 12px 8px; background:#2a2a2a; border-radius:14px; overflow:hidden; border:1px solid #2a2a2a; }}
.stat-cell {{ flex:1; padding:12px 8px 10px; text-align:center; background:#141414; cursor:pointer; transition:background 0.15s; position:relative; }}
.stat-cell:first-child {{ border-radius:13px 0 0 13px; }}
.stat-cell:last-child {{ border-radius:0 13px 13px 0; }}
.stat-cell:hover {{ background:#1c1c1c; }}
.stat-cell.active {{ background:#1e1e1e; }}
.stat-cell.active::after {{ content:''; position:absolute; bottom:0; left:0; right:0; height:2px; background:#c0392b; }}
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
.dd-theme-row {{ padding:8px 12px 6px; }}
.dd-theme-label {{ font-size:10px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:#666; display:block; margin-bottom:8px; }}
.dd-swatches {{ display:flex; gap:8px; align-items:center; }}
.swatch {{ width:22px; height:22px; border-radius:50%; cursor:pointer; padding:0; transition:transform 0.15s, box-shadow 0.15s; }}
.swatch:hover {{ transform:scale(1.2); box-shadow:0 0 0 3px rgba(255,255,255,0.2); }}
.swatch.active {{ transform:scale(1.15); box-shadow:0 0 0 3px rgba(255,255,255,0.5); }}
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
.img-gen-badge.admin-editable {{ cursor:pointer; }}
.img-gen-badge.admin-editable:hover {{ color:#D85A30; border:1px solid #D85A30; }}
.gen-edit-dropdown {{
  position:absolute; top:28px; left:8px; z-index:200;
  background:#1a1a1a; border:1px solid #444; border-radius:6px;
  padding:4px 0; min-width:110px; box-shadow:0 4px 16px rgba(0,0,0,0.7);
}}
.gen-edit-dropdown select {{
  background:#1a1a1a; color:#e8e4df; border:none; outline:none;
  font-family:'DM Mono',monospace; font-size:11px; padding:4px 8px;
  width:100%; cursor:pointer;
}}
.gen-edit-dropdown .gen-save-btn {{
  display:block; width:100%; margin-top:4px;
  background:#D85A30; border:none; color:#fff;
  font-family:'DM Mono',monospace; font-size:10px;
  padding:4px 8px; cursor:pointer; border-radius:0 0 5px 5px;
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
.fmv-wrap {{ cursor:pointer; position:relative; border-radius:4px; transition:background 0.15s; }}
.fmv-wrap:hover {{ background:rgba(255,255,255,0.03); }}
.fmv-comps-link {{ color:var(--muted); cursor:pointer; border-bottom:1px dotted var(--muted); transition:color 0.15s, border-color 0.15s; }}
.fmv-comps-link:hover {{ color:var(--text); border-color:var(--text); }}
.fmv-val-edit {{ color:#A0A0B4; cursor:pointer; border-bottom:1px dotted transparent; transition:color 0.15s, border-color 0.15s; }}
.fmv-val-edit:hover {{ color:var(--text); border-color:var(--muted); }}
.fmv-wrap {{ position:relative; border-radius:4px; }}
.fmv-listing-header {{ display:flex; gap:12px; padding:16px 20px; border-bottom:1px solid var(--border); }}
.fmv-listing-thumb {{ width:100px; height:68px; object-fit:cover; border-radius:6px; flex-shrink:0; }}
.fmv-listing-info {{ flex:1; min-width:0; }}
.fmv-listing-title {{ font-size:13px; font-weight:700; color:#fff; margin-bottom:4px; }}
.fmv-listing-meta {{ font-size:11px; color:var(--muted); font-family:'DM Mono',monospace; margin-bottom:6px; }}
.fmv-listing-prices {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }}
.fmv-listing-ask {{ font-size:12px; color:var(--muted); }}
.fmv-listing-ask b {{ color:var(--text); }}
.fmv-listing-fmv {{ font-size:12px; color:var(--muted); }}
.fmv-listing-fmv b {{ color:#22C55E; }}
.fmv-inline-pct {{ font-size:10px; margin-left:3px; }}
.fmv-listing-link {{ font-size:11px; color:var(--muted); text-decoration:none; border-bottom:1px dotted var(--muted); }}
.fmv-listing-link:hover {{ color:var(--text); }}
.fmv-comps-section-header {{ padding:10px 20px 8px; font-size:10px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:var(--muted); border-bottom:1px solid var(--border); }}
.fmv-modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:500; align-items:center; justify-content:center; padding:16px; }}
.fmv-modal-overlay.open {{ display:flex; }}
.fmv-modal {{ background:var(--bg2); border:1px solid var(--border); border-radius:12px; width:100%; max-width:480px; max-height:80vh; overflow:hidden; display:flex; flex-direction:column; }}
.fmv-modal-header {{ padding:16px 20px 12px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }}
.fmv-modal-title {{ font-size:13px; font-weight:700; color:#fff; }}
.fmv-modal-close {{ background:none; border:none; color:var(--muted); font-size:18px; cursor:pointer; padding:0 4px; line-height:1; }}
.fmv-modal-body {{ overflow-y:auto; padding:0; flex:1; }}
.fmv-comp-row {{ display:flex; align-items:center; justify-content:space-between; padding:10px 20px; border-bottom:1px solid var(--border); gap:8px; }}
.fmv-comp-row:last-child {{ border-bottom:none; }}
.fmv-comp-info {{ flex:1; min-width:0; }}
.fmv-comp-name {{ font-size:12px; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.fmv-comp-meta {{ font-size:10px; color:var(--muted); font-family:'DM Mono',monospace; margin-top:2px; }}
.fmv-comp-price {{ font-family:'DM Mono',monospace; font-size:13px; font-weight:700; color:#fff; white-space:nowrap; }}
.fmv-comp-link {{ font-size:11px; color:var(--muted); text-decoration:none; margin-left:6px; }}
.fmv-comp-link:hover {{ color:var(--text); }}
.fmv-modal-loading {{ padding:32px; text-align:center; color:var(--muted); font-size:12px; font-family:'DM Mono',monospace; }}
.fmv-override-input {{ display:none; width:140px; background:var(--bg3); border:1px solid var(--red); border-radius:4px; color:var(--text); font-family:'DM Mono',monospace; font-size:12px; padding:4px 8px; margin-top:4px; outline:none; }}
.fmv-override-input.visible {{ display:block; }}
.fmv-user-label {{ font-size:9px; color:#60A5FA; font-family:'DM Mono',monospace; letter-spacing:0.5px; text-transform:uppercase; margin-bottom:2px; }}
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
  cursor:pointer; transition:color 0.15s;
}}
.fmv-none:hover {{ color:#9898B0; }}
.fmv-none-dot {{ width:5px; height:5px; border-radius:50%; background:var(--border); flex-shrink:0; }}

.auction-ends {{
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); margin-bottom:5px;
}}
.countdown {{ color:var(--red); font-weight:500; }}
.card-meta {{ font-family:'DM Mono',monospace; font-size:10px; color:#8A8A9E; }}
.star-btn {{ background:none; border:none; cursor:pointer; font-size:14px; padding:0 0 0 4px; color:#444; line-height:1; flex-shrink:0; transition:color 0.15s; }}
.relisted-badge {{ display:inline-block; font-family:'DM Mono',monospace; font-size:8px; font-weight:600; background:#1a0a1a; color:#c084fc; padding:2px 6px; border-radius:3px; border:1px solid #3a1a3a; letter-spacing:0.5px; text-transform:uppercase; margin-bottom:4px; }}
.star-btn:hover {{ color:#f59e0b; }}
.star-btn.starred {{ color:#f59e0b; }}
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
  .fmv-wrap {{ min-width:0; }}
  .fmv-bar-wrap {{ min-width:0; }}
  .fmv-bottom-row {{ min-width:0; }}
  .fmv-val-txt {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .fmv-comps-txt {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
}}
</style>
</head>
<body>
<div class="app">

<!-- ── Nav ── -->
<header class="topbar">
  <a class="logo" href="index.html"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 280 64"><g transform="translate(8,6)"><circle cx="26" cy="26" r="22" fill="none" stroke="#242424" stroke-width="2.5"/><path d="M6,38 A22,22 0 0,1 43.5,8.5" fill="none" stroke="#D85A30" stroke-width="2.5" stroke-linecap="round"/><g stroke="#333" stroke-width="1.2" stroke-linecap="round"><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-80,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-55,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-30,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(-5,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(20,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(45,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(70,26,26)"/><line x1="4" y1="26" x2="8.5" y2="26" transform="rotate(95,26,26)"/></g><line x1="26" y1="26" x2="43.5" y2="8.5" stroke="white" stroke-width="1.8" stroke-linecap="round"/><circle cx="26" cy="26" r="4" fill="#D85A30"/><circle cx="26" cy="26" r="1.6" fill="#0d0d0d"/><text x="62" y="34" font-family="'Helvetica Neue',Arial,sans-serif" font-size="32" letter-spacing="-0.5"><tspan font-weight="800" fill="white">Renn</tspan><tspan font-weight="300" fill="#D85A30">Markt</tspan></text></g></svg></a>
  <button class="more-btn" onclick="toggleDropdown()">More &#x25BE;</button>
</header>
<div class="stats-bar">
  <div class="stat-cell active" onclick="switchView('listings',this);resetFilters()">
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
    <a class="dd-item" href="calculator.html"><span class="dd-icon">&#x1F4B0;</span> FMV Calculator</a>
    <a class="dd-item" href="market_report.html"><span class="dd-icon">&#x1F4CA;</span> Market Reports</a>
    <a class="dd-item" href="notify.html"><span class="dd-icon">&#x1F514;</span> Notifications</a>
    <div class="dd-divider"></div>
    <div class="dd-theme-row">
      <span class="dd-theme-label">Theme</span>
      <div class="dd-swatches">
        <button class="swatch" data-theme=""      title="Default (Dark)"  style="background:#0d0d0d;border:2px solid #c0392b"></button>
        <button class="swatch" data-theme="racing" title="Racing Red"      style="background:#0c0809;border:2px solid #e53e3e"></button>
        <button class="swatch" data-theme="gulf"   title="Gulf Blue"       style="background:#08100c;border:2px solid #2563eb"></button>
        <button class="swatch" data-theme="olive"  title="Olive Drab"      style="background:#0a0c08;border:2px solid #65a30d"></button>
        <button class="swatch" data-theme="purple" title="Midnight Purple" style="background:#09080d;border:2px solid #7c3aed"></button>
        <button class="swatch" data-theme="light"  title="Light"           style="background:#f5f4f2;border:2px solid #c0392b"></button>
      </div>
    </div>
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
    <span class="filter-group-label">Body Style</span>
    <div class="chip-row">
      <button class="chip" data-val="Coupe"     onclick="toggleBody(this,&apos;Coupe&apos;)">Coupe</button>
      <button class="chip" data-val="Cabriolet" onclick="toggleBody(this,&apos;Cabriolet&apos;)">Cabriolet</button>
      <button class="chip" data-val="Targa"     onclick="toggleBody(this,&apos;Targa&apos;)">Targa</button>
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
      <label><input type="checkbox" id="f-deals" onchange="applyFilters()"> Deals only (&darr;10%+ FMV)</label>
      <label><input type="checkbox" id="f-tier1" onchange="applyFilters()"> GT / Collector</label>
      <label style="cursor:pointer" onclick="filterStarred()"><span id="filter-starred-btn" style="font-size:13px;margin-right:3px">&#x2606;</span> Saved cars</label>
    </div>
  </div>

  <button class="reset-btn" onclick="resetFilters()">&#x21BA; Reset Filters</button>
</aside>

<!-- ── Main ── -->
<main class="main">

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

<!-- FMV comp drill-down modal -->
<div class="fmv-modal-overlay" id="fmv-modal-overlay" onclick="if(event.target===this)closeFmvModal()">
  <div class="fmv-modal">
    <div class="fmv-modal-header">
      <span class="fmv-modal-title" id="fmv-modal-title">Sold Comps</span>
      <button class="fmv-modal-close" onclick="closeFmvModal()">&#x2715;</button>
    </div>
    <div class="fmv-modal-body" id="fmv-modal-body">
      <div class="fmv-modal-loading">Loading comps…</div>
    </div>
  </div>
</div>
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
    <div class="drawer-section" style="grid-column:1/-1">
      <span class="drawer-section-label">Body Style</span>
      <div class="drawer-chips">
        <button class="chip" data-val="Coupe"     onclick="toggleBody(this,&apos;Coupe&apos;)">Coupe</button>
        <button class="chip" data-val="Cabriolet" onclick="toggleBody(this,&apos;Cabriolet&apos;)">Cabriolet</button>
        <button class="chip" data-val="Targa"     onclick="toggleBody(this,&apos;Targa&apos;)">Targa</button>
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
        <label><input type="checkbox" id="d-deals" onchange="syncFromDrawer()"> Deals only (&darr;10%+ FMV)</label>
        <label><input type="checkbox" id="d-tier1" onchange="syncFromDrawer()"> GT / Collector</label>
        <label style="cursor:pointer" onclick="filterStarred();closeDrawer()"><span style="font-size:13px;margin-right:3px">&#x2606;</span> Saved cars</label>
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
var activeBody    = null;
var filterNewToday = false;
var filterStarredOnly = false;
var _starred = {{}};  // url → true

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

function toggleBody(btn, type) {{
  if (activeBody === type) {{
    activeBody = null;
    document.querySelectorAll('.chip[data-val="Coupe"],.chip[data-val="Cabriolet"],.chip[data-val="Targa"]').forEach(function(c) {{ c.classList.remove('active'); }});
  }} else {{
    document.querySelectorAll('.chip[data-val="Coupe"],.chip[data-val="Cabriolet"],.chip[data-val="Targa"]').forEach(function(c) {{ c.classList.remove('active'); }});
    activeBody = type;
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
    if (activeBody && d.bs !== activeBody) return false;
    if (filterNewToday && !d.nt) return false;
    if (filterStarredOnly && !_starred[d.url]) return false;
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

var PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27400%27 height=%27165%27%3E%3Crect width=%27400%27 height=%27165%27 fill=%27%2318181F%27/%3E%3Ctext x=%2750%25%27 y=%2750%25%27 dominant-baseline=%27middle%27 text-anchor=%27middle%27 font-family=%27monospace%27 font-size=%2712%27 fill=%27%2325252E%27%3ENo photo%3C/text%3E%3C/svg%3E";

function fmtPrice(n) {{
  if (!n) return '\u2014';
  if (n >= 1000000) return '$' + (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return '$' + Math.round(n/1000) + 'K';
  return '$' + n.toLocaleString();
}}
function fmtMiles(n) {{
  if (!n) return '';
  if (n >= 1000) return Math.round(n/1000) + 'K mi';
  return n + ' mi';
}}
function ageLabel(created) {{
  if (!created) return '';
  var d = new Date(created.replace(' ','T') + (created.includes('T') ? '' : 'Z'));
  if (isNaN(d)) return '';
  var diff = Math.floor((Date.now() - d) / 60000);
  if (diff < 2) return 'Just now';
  if (diff < 60) return diff + 'm ago';
  var h = Math.floor(diff/60);
  if (h < 24) return h + 'h ago';
  var days = Math.floor(h/24);
  if (days === 1) return 'Yesterday';
  if (days < 7) return days + 'd ago';
  return d.toLocaleDateString('en-US',{{month:'short',day:'numeric'}});
}}

function buildFmvBar(d) {{
  if (!d.fmv || d.fmv_conf === 'NONE') return '';
  var pct = d.fmv_pct;
  var pctStr = pct !== null ? (pct > 0 ? '+' : '') + pct + '%' : '';
  var cls = pct !== null ? (pct <= -10 ? 'fmv-deal' : pct <= 0 ? 'fmv-fair' : 'fmv-over') : '';
  var confLabel = d.fmv_conf === 'HIGH' ? 'HIGH' : d.fmv_conf === 'MEDIUM' ? 'MED' : 'LOW';
  var rangeStr = (d.fmv_lo && d.fmv_hi && d.fmv_cc >= 6)
    ? ' &middot; ' + fmtPrice(d.fmv_lo) + '\u2013' + fmtPrice(d.fmv_hi) : '';
  // Comp count: clickable dotted-underline link — opens listing+comps modal
  var ccStr = d.fmv_cc
    ? ' &middot; <span class="fmv-comps-link" data-has-comps="1" title="View ' + d.fmv_cc + ' sold comps">' + d.fmv_cc + ' comp' + (d.fmv_cc !== 1 ? 's' : '') + '</span>'
    : '';
  // FMV value itself: clickable to open the edit input
  return '<div class="fmv-bar-block">'
    + '<div class="fmv-label-row">'
    + '<span class="fmv-val-edit" data-edit-fmv="1" title="Click to set your own FMV">FMV ' + fmtPrice(d.fmv) + '</span>'
    + '<span class="fmv-conf ' + cls + '">' + (pctStr ? pctStr + ' &middot; ' : '') + confLabel + rangeStr + ccStr + '</span>'
    + '</div></div>';
}}

function renderCard(d) {{
  var isAuc = d.is_auc;
  var priceLbl = isAuc ? 'Bid' : 'Ask';
  var priceCls = isAuc ? 'price-auction' : 'price-ask';

  var genBadge = d.gen ? '<div class="img-gen-badge' + (!IS_PUBLIC ? ' admin-editable' : '') + '"'
    + (!IS_PUBLIC ? ' onclick="event.stopPropagation();openGenEditor(this,\\x27' + d.id + '\\x27,\\x27' + d.gen + '\\x27)"' : '')
    + ' title="' + (IS_PUBLIC ? d.gen : 'Click to correct generation') + '">' + d.gen + '</div>' : '';

  var dealBadge = '';
  if (d.fmv_pct !== null && d.fmv_pct <= -10) {{
    dealBadge = '<div class="img-deal-badge">\u2193' + Math.abs(d.fmv_pct) + '%</div>';
  }}

  var imgSrc = d.img || PLACEHOLDER;
  var imgHtml = '<div class="card-img-wrap">'
    + '<img src="' + imgSrc + '" alt="' + d.yr + ' ' + d.model + '" class="card-img" loading="lazy" onerror="this.src=PLACEHOLDER">'
    + genBadge + dealBadge + '</div>';

  var badgeHtml = '';
  if (d.badge_label) {{
    var bStyle = d.badge_bg ? 'background:' + d.badge_bg + ';color:' + (d.badge_fg||'#9898B0') + ';' : '';
    badgeHtml = '<span class="source-badge" style="' + bStyle + '">' + d.badge_label + '</span>';
    if (d.gen) badgeHtml += '<span class="gen-badge">' + d.gen + '</span>';
  }}

  var ageHtml = '<span class="card-age" data-created="' + (d.created||'') + '">' + ageLabel(d.created) + '</span>';

  var tierHtml = '';  // GT/Collector badge removed
  var relistHtml = '';
  if (d.relisted && d.sold_prev) {{
    var prevStr = '$' + Math.round(d.sold_prev/1000) + 'K';
    relistHtml = '<div class="relisted-badge">&#x21BA; Relisted &middot; prev ' + prevStr + (d.sold_date ? ' on ' + d.sold_date.slice(0,7) : '') + '</div>';
  }}

  var titleStr = d.trim ? d.model + ' ' + d.trim : d.model;
  titleStr = titleStr.replace(new RegExp('^' + d.model + '\\s+' + d.model + '\\s*', 'i'), d.model + ' ');
  var titleHtml = '<div class="card-title">' + d.yr + ' Porsche ' + titleStr + '</div>';

  var fmvHtml = '';
  if (isAuc && d.ends && d.fmv && d.fmv_conf !== 'NONE') {{
    var endsMs = new Date(d.ends.replace('Z','+00:00')).getTime();
    var leftMs = endsMs - Date.now();
    if (leftMs > 0) {{
      if (leftMs > 86400000) {{
        fmvHtml = '<div class="fmv-none"><span class="fmv-none-dot" style="background:#555"></span>Auction in progress</div>';
      }} else {{
        var bidPct = d.pr && d.fmv ? (d.pr / d.fmv * 100) : 0;
        if (bidPct >= 65) {{
          fmvHtml = buildFmvBar(d);
        }} else {{
          fmvHtml = '<div class="fmv-none"><span class="fmv-none-dot" style="background:#555"></span>Auction ending soon</div>';
        }}
      }}
    }} else {{
      fmvHtml = buildFmvBar(d);
    }}
  }} else if (d.fmv && d.fmv_conf !== 'NONE') {{
    fmvHtml = buildFmvBar(d);
  }} else {{
    fmvHtml = '<div class="fmv-none"><span class="fmv-none-dot"></span>'
      + (IS_PUBLIC ? 'No FMV \u2014 insufficient comps' : 'No FMV \u2014 click to set manually')
      + '</div>';
  }}

  var endsHtml = '';
  if (isAuc && d.ends) {{
    endsHtml = '<div class="auction-ends">Ends <span class="countdown" data-ends="' + d.ends + '">\u2026</span></div>';
  }}

  var chips = [];
  if (d.trans) chips.push(d.trans);
  if (d.mi)    chips.push(fmtMiles(d.mi));
  if (d.loc)   chips.push(d.loc.substring(0,22));
  if (d.dom > 0) chips.push('<span class="dom-chip">\u23f1' + d.dom + 'd</span>');
  var metaHtml = '<div class="card-meta">' + chips.join(' &middot; ') + '</div>';

  return '<div class="card"'
    + ' data-dealer="' + d.dlr + '"'
    + ' data-year="' + d.yr + '"'
    + ' data-model="' + d.model + '"'
    + ' data-gen="' + d.gen + '"'
    + ' data-id="' + d.id + '"'
    + ' data-tier="' + d.tier + '"'
    + ' data-price="' + (d.pr||0) + '"'
    + ' data-src-label="' + d.src + '"'
    + ' data-source-type="' + (isAuc?'auction':'retail') + '"'
    + ' data-url="' + d.url + '">'
    + imgHtml
    + '<div class="card-body">'
    + '<div class="card-top-row">' + badgeHtml + ageHtml
  + '<button class="star-btn" data-url="' + d.url + '" onclick="event.stopPropagation();toggleStar(this)" title="Save car">&#x2606;</button>'
  + '</div>'
    + titleHtml
    + tierHtml
    + relistHtml
    + '<div class="card-price-row"><span class="price-lbl">' + priceLbl + '</span>'
    + '<span class="' + priceCls + '">' + fmtPrice(d.pr) + '</span></div>'
    + '<div class="fmv-wrap" data-url="' + d.url + '" data-year="' + d.yr + '" data-model="' + (d.model||'').replace(/"/g,'&quot;') + '" data-trim="' + (d.trim||'').replace(/"/g,'&quot;') + '" data-price="' + (d.pr||0) + '">'
    + fmvHtml
    + (!IS_PUBLIC ? '<input class="fmv-override-input" type="text" placeholder="e.g. 187 or 187000" onclick="event.stopPropagation()" onblur="commitFmvInput(this)" onkeydown="fmvKeydown(event,this)" />' : '')
    + '</div>'
    + endsHtml
    + metaHtml
    + '</div></div>';
}}

// ── Personal FMV override ────────────────────────────────────────────────────
// IS_PUBLIC: disables FMV adjustment on public URL unless unlocked
if (location.hash === '#unlock=gt3rs') {{
  localStorage.setItem('ptox_unlock', 'gt3rs');
  location.hash = '';
}}
var IS_PUBLIC = (location.hostname !== 'admin.rennmarkt.net')
  && (localStorage.getItem('ptox_unlock') !== 'gt3rs');
// Triple-tap header to unlock FMV editing — uses in-page modal (iOS PWA safe)
(function() {{
  // Inject modal HTML + CSS once
  var modalHTML = '<div id="unlock-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99999;display:none;align-items:center;justify-content:center;flex-direction:column;gap:12px;">'
    + '<div style="background:#1a1a1a;border:1px solid #333;border-radius:10px;padding:24px 20px;width:260px;display:flex;flex-direction:column;gap:12px;align-items:center;">'
    + '<div style="font-family:monospace;font-size:13px;color:#888;">FMV Access</div>'
    + '<input id="unlock-input" type="password" placeholder="passphrase" autocomplete="off" style="background:#0d0d0d;border:1px solid #444;color:#fff;font-family:monospace;font-size:16px;padding:10px 14px;border-radius:6px;width:100%;box-sizing:border-box;outline:none;">'
    + '<div style="display:flex;gap:8px;width:100%;">'
    + '<button id="unlock-cancel" style="flex:1;background:#222;border:1px solid #333;color:#888;font-family:monospace;font-size:13px;padding:9px;border-radius:6px;cursor:pointer;">Cancel</button>'
    + '<button id="unlock-submit" style="flex:1;background:#c0392b;border:none;color:#fff;font-family:monospace;font-size:13px;padding:9px;border-radius:6px;cursor:pointer;">Unlock</button>'
    + '</div>'
    + '<div id="unlock-error" style="font-family:monospace;font-size:11px;color:#e74c3c;display:none;">incorrect passphrase</div>'
    + '</div></div>';
  document.addEventListener('DOMContentLoaded', function() {{
    document.body.insertAdjacentHTML('beforeend', modalHTML);
    document.getElementById('unlock-submit').addEventListener('click', doUnlock);
    document.getElementById('unlock-cancel').addEventListener('click', hideModal);
    document.getElementById('unlock-input').addEventListener('keydown', function(e) {{
      if (e.key === 'Enter') doUnlock();
      if (e.key === 'Escape') hideModal();
    }});
  }});
  function showModal() {{
    var overlay = document.getElementById('unlock-overlay');
    if (!overlay) return;
    overlay.style.display = 'flex';
    setTimeout(function() {{ document.getElementById('unlock-input').focus(); }}, 50);
  }}
  function hideModal() {{
    var overlay = document.getElementById('unlock-overlay');
    if (overlay) overlay.style.display = 'none';
    var inp = document.getElementById('unlock-input');
    if (inp) inp.value = '';
    var err = document.getElementById('unlock-error');
    if (err) err.style.display = 'none';
  }}
  function doUnlock() {{
    var pw = document.getElementById('unlock-input').value.trim();
    if (pw === 'gt3rs') {{
      localStorage.setItem('ptox_unlock', 'gt3rs');
      hideModal();
      location.reload();
    }} else {{
      document.getElementById('unlock-error').style.display = 'block';
      document.getElementById('unlock-input').value = '';
    }}
  }}
  var taps = 0, timer;
  document.addEventListener('click', function(e) {{
    if (!e.target.closest('header')) return;
    taps++;
    clearTimeout(timer);
    if (taps >= 3) {{
      taps = 0;
      if (localStorage.getItem('ptox_unlock') === 'gt3rs') {{
        localStorage.removeItem('ptox_unlock');
        location.reload();
      }} else {{
        showModal();
      }}
    }}
    timer = setTimeout(function() {{ taps = 0; }}, 700);
  }});
}})();
var PUSH_SERVER = 'https://rennmarkt-push.openclawx1.workers.dev';
var _GEN_OPTIONS = ['Classic','930','964','993','996','997_1','997_2','991_1','991_2','992','986','987','981','718_cayman','718_boxster','Carrera GT','918','944'];

function openGenEditor(badge, listingId, currentGen) {{
  // Close any other open gen editors
  document.querySelectorAll('.gen-edit-dropdown').forEach(function(d) {{ d.remove(); }});
  var opts = _GEN_OPTIONS.map(function(g) {{
    return '<option value="' + g + '"' + (g === currentGen ? ' selected' : '') + '>' + g + '</option>';
  }}).join('');
  var dropdown = document.createElement('div');
  dropdown.className = 'gen-edit-dropdown';
  dropdown.innerHTML = '<select id="gen-sel-' + listingId + '">' + opts + '</select>'
    + '<button class="gen-save-btn" onclick="saveGenOverride(' + listingId + ',this)">Save</button>';
  badge.parentNode.appendChild(dropdown);
  dropdown.querySelector('select').focus();
  // Click outside closes
  setTimeout(function() {{
    document.addEventListener('click', function _close(e) {{
      if (!dropdown.contains(e.target) && e.target !== badge) {{
        dropdown.remove();
        document.removeEventListener('click', _close);
      }}
    }});
  }}, 0);
}}

function saveGenOverride(listingId, btn) {{
  var sel = document.getElementById('gen-sel-' + listingId);
  if (!sel) return;
  var newGen = sel.value;
  btn.textContent = '...';
  btn.disabled = true;
  fetch(PUSH_SERVER + '/gen-override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'X-Admin-Token': 'gt3rs'}},
    body: JSON.stringify({{id: listingId, generation: newGen}})
  }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
    if (data.ok) {{
      // Update badge text in DOM
      var badge = document.querySelector('.img-gen-badge[onclick*="' + listingId + '"]');
      if (badge) badge.textContent = newGen;
      // Update card data
      var card = document.querySelector('[data-id="' + listingId + '"]');
      if (card) card.dataset.gen = newGen;
      btn.closest('.gen-edit-dropdown').remove();
    }} else {{
      btn.textContent = 'Error';
      btn.disabled = false;
    }}
  }}).catch(function() {{
    btn.textContent = 'Error';
    btn.disabled = false;
  }});
}}

function fmvKeydown(e, input) {{
  if (e.key === 'Enter') input.blur();
  if (e.key === 'Escape') input.classList.remove('visible');
}}
function openFmvInput(e, wrap) {{
  if (e.target.classList.contains('fmv-override-input')) return;
  e.stopPropagation();
  var input = wrap.querySelector('.fmv-override-input');
  var stored = localStorage.getItem('ptox_fmv:' + wrap.dataset.url);
  if (stored) input.value = Math.round(parseInt(stored) / 1000);
  input.classList.add('visible');
  input.focus();
  input.select();
}}

function commitFmvInput(input) {{
  var wrap = input.closest('.fmv-wrap');
  input.classList.remove('visible');
  var raw = (input.value || '').trim();
  if (!raw) {{ maybeClearFmvOverride(wrap); return; }}
  // Accept K shorthand: 187 → $187K, 187500 → $187.5K
  var num = parseFloat(raw);
  var val = num >= 5000 ? Math.round(num) : Math.round(num * 1000);
  if (!val || val < 5000 || val > 5000000) {{ maybeClearFmvOverride(wrap); return; }}

  localStorage.setItem('ptox_fmv:' + wrap.dataset.url, val);
  updateCardFmvDisplay(wrap, val);

  // POST to push server as a personal comp (feeds FMV Calculator)
  fetch(PUSH_SERVER + '/user-comp', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      url:   wrap.dataset.url,
      fmv:   val,
      year:  parseInt(wrap.dataset.year  || 0),
      model: wrap.dataset.model || '',
      trim:  wrap.dataset.trim  || '',
      price: parseInt(wrap.dataset.price || 0),
    }})
  }}).catch(function(){{}});
}}

function maybeClearFmvOverride(wrap) {{
  var url = wrap.dataset.url;
  if (!localStorage.getItem('ptox_fmv:' + url)) return;
  if (!confirm('Clear your custom FMV for this listing?')) return;
  localStorage.removeItem('ptox_fmv:' + url);
  fetch(PUSH_SERVER + '/user-comp', {{
    method: 'DELETE',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{url: url}})
  }}).catch(function(){{}});
  var d = CARD_DATA.find(function(x){{ return x.url === url; }});
  if (d) {{
    var tmp = document.createElement('div');
    tmp.innerHTML = renderCard(d);
    var oldCard = wrap.closest('.card');
    if (oldCard) oldCard.replaceWith(tmp.firstChild);
    applyStoredFmvOverrides();
  }}
}}

function updateCardFmvDisplay(wrap, fmv) {{
  var price = parseInt(wrap.dataset.price || 0);
  var pct   = price && fmv ? Math.round((price - fmv) / fmv * 100) : null;
  var cls   = pct !== null ? (pct <= -10 ? 'fmv-deal' : pct <= 0 ? 'fmv-fair' : 'fmv-over') : '';
  var pctStr = pct !== null ? (pct > 0 ? '+' : '') + pct + '%' : '';
  var barEl = wrap.querySelector('.fmv-bar-block, .fmv-none');
  if (barEl) {{
    // Preserve existing comps link before replacing content
    var existingCompsLink = barEl.querySelector('.fmv-comps-link');
    var compsLinkHtml = existingCompsLink ? existingCompsLink.outerHTML : '';
    barEl.className = 'fmv-bar-block';
    barEl.innerHTML = '<div class="fmv-label-row">'
      + '<span class="fmv-val-edit" data-edit-fmv="1" title="Click to update your FMV">FMV ' + fmtPrice(fmv) + ' <span class="fmv-user-label">✓</span></span>'
      + '<span class="fmv-conf ' + cls + '">'
      + (pctStr ? pctStr + (compsLinkHtml ? ' &middot; ' : '') : '')
      + compsLinkHtml
      + '</span>'
      + '</div>';
  }}
}}

function applyStoredFmvOverrides() {{
  document.querySelectorAll('.fmv-wrap[data-url]').forEach(function(wrap) {{
    var stored = parseInt(localStorage.getItem('ptox_fmv:' + (wrap.dataset.url||'')) || '0');
    if (stored > 0) updateCardFmvDisplay(wrap, stored);
  }});
}}

function renderCards() {{
  var grid = document.getElementById('cards-grid');
  if (!grid) return;
  var next = Math.min(renderedCount + PAGE, visibleCards.length);
  var html = '';
  for (var i = renderedCount; i < next; i++) {{
    html += renderCard(visibleCards[i]);
  }}
  if (renderedCount === 0) {{
    grid.innerHTML = html || '<div class="empty"><div class="empty-icon">&#x1F4ED;</div><div class="empty-text">No listings match</div></div>';
  }} else {{
    grid.insertAdjacentHTML('beforeend', html);
  }}
  renderedCount = next;
  // Restart countdowns for newly inserted auction cards
  startCountdowns();
  // Apply any stored personal FMV overrides to newly rendered cards
  applyStoredFmvOverrides();
  // Apply stored star state to newly rendered cards
  loadStarred();
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
  activeBody = null;
  filterNewToday = false;
  filterStarredOnly = false;
  var sb = document.getElementById('filter-starred-btn'); if (sb) sb.classList.remove('active');
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
  document.querySelectorAll('.stat-cell').forEach(function(b) {{ b.classList.remove('active'); }});
  var v = document.getElementById('view-' + name);
  if (v) v.classList.add('active');
  if (btn) btn.classList.add('active');
  _currentView = name;
  if (name === 'listings') startCountdowns();
}}

function filterToday() {{
  filterNewToday = true;
  var fd = document.getElementById('f-deals'); if (fd) fd.checked = false;
  var dd = document.getElementById('d-deals'); if (dd) dd.checked = false;
  applyFilters(); updateFabState();
}}
function filterDeals() {{
  filterNewToday = false;
  var fd = document.getElementById('f-deals'); if (fd) fd.checked = true;
  var dd = document.getElementById('d-deals'); if (dd) dd.checked = true;
  applyFilters(); updateFabState();
}}
// ── FMV comp drill-down modal ─────────────────────────────────────────────────
// Delegated card click handler — handles navigation + fmv-wrap interception
document.addEventListener('click', function(e) {{
  // fmv-wrap clicks: handle comps/edit, never navigate
  var wrap = e.target.closest('.fmv-wrap');
  if (wrap) {{
    var link = e.target.closest('.fmv-comps-link');
    if (link) {{ showFmvComps(wrap); }}
    var editTrigger = e.target.closest('.fmv-val-edit');
    var noneTrigger = e.target.closest('.fmv-none');
    if ((editTrigger || noneTrigger) && !IS_PUBLIC) {{ openFmvInput(e, wrap); }}
    return;  // stop — never navigate when clicking fmv-wrap
  }}
  // Card click → navigate to listing
  var card = e.target.closest('.card');
  if (card && card.dataset.url) {{
    openListing(card.dataset.url);
  }}
}});

function showFmvComps(wrap) {{
  var year  = parseInt(wrap.dataset.year  || 0);
  var model = wrap.dataset.model || '';
  var trim  = wrap.dataset.trim  || '';
  var price = parseInt(wrap.dataset.price || 0);
  var listingUrl = wrap.dataset.url || '';

  var overlay = document.getElementById('fmv-modal-overlay');
  var title   = document.getElementById('fmv-modal-title');
  var body    = document.getElementById('fmv-modal-body');
  var d = CARD_DATA ? CARD_DATA.find(function(x){{ return x.url === listingUrl; }}) : null;

  title.textContent = year + ' Porsche ' + model + (trim ? ' ' + trim : '');
  body.innerHTML = '<div class="fmv-modal-loading">Loading comps…</div>';
  overlay.classList.add('open');

  var apiUrl = PUSH_SERVER + '/fmv-comps?year=' + year
    + '&model=' + encodeURIComponent(model)
    + '&trim='  + encodeURIComponent(trim || '');

  fetch(apiUrl)
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var html = '';

      // ── Listing summary ──
      html += '<div class="fmv-listing-header">';
      if (d && d.img) html += '<img src="' + d.img + '" class="fmv-listing-thumb" />';
      html += '<div class="fmv-listing-info">';
      html += '<div class="fmv-listing-title">' + year + ' Porsche ' + model + (trim ? ' ' + trim : '') + '</div>';
      var meta = [];
      if (d && d.mi)    meta.push(Math.round(d.mi/1000) + 'K mi');
      if (d && d.trans) meta.push(d.trans);
      if (d && d.gen)   meta.push(d.gen);
      if (d && d.dlr)   meta.push(d.dlr);
      if (meta.length)  html += '<div class="fmv-listing-meta">' + meta.join(' &middot; ') + '</div>';
      html += '<div class="fmv-listing-prices">';
      if (price) html += '<span class="fmv-listing-ask">Ask <b>' + fmtPrice(price) + '</b></span>';
      if (data.fmv) {{
        var pct = price && data.fmv ? Math.round((price - data.fmv) / data.fmv * 100) : null;
        var pctCls = pct !== null ? (pct <= -10 ? 'fmv-deal' : pct <= 0 ? 'fmv-fair' : 'fmv-over') : '';
        var pctStr = pct !== null ? ' (' + (pct > 0 ? '+' : '') + pct + '%)' : '';
        html += '<span class="fmv-listing-fmv">FMV <b>' + fmtPrice(data.fmv) + '</b>'
          + (pctStr ? '<span class="fmv-conf ' + pctCls + '">' + pctStr + '</span>' : '') + '</span>';
      }}
      html += '</div>';
      if (listingUrl) html += '<a class="fmv-listing-link" href="' + listingUrl + '" target="_blank" rel="noopener">View listing &#x2197;</a>';
      html += '</div></div>';

      // ── Comps ──
      if (!data.comps || data.comps.length === 0) {{
        html += '<div class="fmv-modal-loading">No comps found.</div>';
      }} else {{
        html += '<div class="fmv-comps-section-header">Comparable Sales &mdash; '
          + data.comp_count + ' comp' + (data.comp_count !== 1 ? 's' : '')
          + ' &middot; ' + data.confidence + ' confidence</div>';
        data.comps.forEach(function(c) {{
          var mi2  = c.mileage ? Math.round(c.mileage/1000) + 'K mi' : '';
          var date = c.sold_date ? c.sold_date.substring(0,7) : '';
          var src  = c.source || '';
          var m2   = [mi2, src, date].filter(Boolean).join(' · ');
          var lnk  = c.listing_url
            ? ' <a class="fmv-comp-link" href="' + c.listing_url + '" target="_blank" rel="noopener">&#x2197;</a>'
            : '';
          html += '<div class="fmv-comp-row">'
            + '<div class="fmv-comp-info">'
            + '<div class="fmv-comp-name">' + (c.year||'') + ' Porsche ' + (c.model||'') + (c.trim ? ' ' + c.trim : '') + '</div>'
            + '<div class="fmv-comp-meta">' + m2 + '</div>'
            + '</div>'
            + '<div style="display:flex;align-items:center;gap:4px">'
            + '<span class="fmv-comp-price">$' + Math.round((c.sold_price||0)/1000) + 'K</span>'
            + lnk + '</div></div>';
        }});
      }}
      body.innerHTML = html;
    }})
    .catch(function() {{
      body.innerHTML = '<div class="fmv-modal-loading">Could not load comps. Push server may be offline.</div>';
    }});
}}


function closeFmvModal() {{
  document.getElementById('fmv-modal-overlay').classList.remove('open');
}}

// Close modal on Escape
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeFmvModal();
}});

function toggleDropdown() {{
  document.getElementById('dd-overlay').classList.toggle('show');
  _syncSwatches();
}}
function _syncSwatches() {{
  var cur = localStorage.getItem('ptox_theme') || '';
  document.querySelectorAll('.swatch').forEach(function(s) {{
    s.classList.toggle('active', s.dataset.theme === cur);
  }});
}}
function setTheme(t) {{
  if (t) {{ document.documentElement.dataset.theme = t; localStorage.setItem('ptox_theme', t); }}
  else   {{ delete document.documentElement.dataset.theme; localStorage.removeItem('ptox_theme'); }}
  _syncSwatches();
}}
document.querySelectorAll('.swatch').forEach(function(s) {{
  s.addEventListener('click', function() {{ setTheme(s.dataset.theme); }});
}});
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

// PWA visibility restore — when returning from an external app (eBay, BaT, etc.)
// iOS fires visibilitychange. Re-apply filters and scroll position so the
// PWA doesn't appear blank or jump.
document.addEventListener('visibilitychange', function() {{
  if (document.visibilityState === 'visible') {{
    // Re-render the active view with current filter state
    if (typeof applyFilters === 'function') applyFilters();
  }}
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


// ── Starred / saved cars ──────────────────────────────────────────────────────
function loadStarred() {{
  try {{
    var raw = localStorage.getItem('ptox_starred') || '{{}}';
    _starred = JSON.parse(raw);
  }} catch(e) {{ _starred = {{}}; }}
  // Apply starred state to any already-rendered star buttons
  document.querySelectorAll('.star-btn').forEach(function(btn) {{
    var url = btn.dataset.url || '';
    if (_starred[url]) {{ btn.textContent = '\u2605'; btn.classList.add('starred'); }}
    else {{ btn.textContent = '\u2606'; btn.classList.remove('starred'); }}
  }});
}}

function toggleStar(btn) {{
  var url = btn.dataset.url || '';
  if (!url) return;
  if (_starred[url]) {{
    delete _starred[url];
    btn.textContent = '\u2606';
    btn.classList.remove('starred');
  }} else {{
    _starred[url] = 1;
    btn.textContent = '\u2605';
    btn.classList.add('starred');
  }}
  try {{ localStorage.setItem('ptox_starred', JSON.stringify(_starred)); }} catch(e) {{}}
  // If starred-only filter is active, re-apply so card hides/shows immediately
  if (filterStarredOnly) applyFilters();
}}

function filterStarred() {{
  filterStarredOnly = !filterStarredOnly;
  filterNewToday = false;
  var btn = document.getElementById('filter-starred-btn');
  if (btn) btn.classList.toggle('active', filterStarredOnly);
  applyFilters(); updateFabState();
}}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function() {{
  if (window.location.hash === '#comps') {{
    var compsCell = document.querySelector('.stat-cell[onclick*="comps"]');
    switchView('comps', compsCell);
  }}
  loadStarred();
  applyFilters();
  startCountdowns();
  updateTimestamps();
  setInterval(updateTimestamps, 60000);
}});
</script>
</html>"""

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file then rename to avoid serving
    # empty/partial HTML if a scrape cycle interrupts mid-write.
    _tmp = OUT_PATH.with_suffix(".tmp")
    _tmp.write_text(html, encoding="utf-8")
    _tmp.replace(OUT_PATH)
    (BASE_DIR / "docs" / "stats.json").write_text(
        json.dumps({"n_active": n_active, "n_new": n_new, "n_auctions": n_auctions,
                    "n_comps": n_comps, "n_deals": n_deals}),
        encoding="utf-8"
    )
    return str(OUT_PATH)


if __name__ == "__main__":
    path = generate()
    print(f"Dashboard: file://{path}")

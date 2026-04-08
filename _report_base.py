"""Shared CSS, helpers, and generation-bucketing used by all three report tiers."""
import re
import statistics
from collections import defaultdict

# ---------------------------------------------------------------------------
# Shared dark-theme CSS
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f1117;color:#e8eaf0;font-size:13px;line-height:1.55}
a{color:#3b82f6;text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:20px;font-weight:700;color:#a855f7;padding:22px 24px 4px}
h2{font-size:14px;font-weight:600;color:#c0c4d8;padding:20px 24px 8px;
   border-top:1px solid #1e2235;margin-top:8px;text-transform:uppercase;
   letter-spacing:.06em}
h3{font-size:12px;font-weight:600;color:#8890b0;padding:10px 24px 6px;
   text-transform:uppercase;letter-spacing:.05em}
.meta{font-size:11px;color:#555d7a;padding:0 24px 18px}
.nav{background:#161923;border-bottom:1px solid #1e2235;
     padding:10px 24px;font-size:11px;color:#555d7a}
.nav a{color:#6b77a0;margin-right:16px}
.section{padding:0 24px 20px}
.empty{color:#555d7a;padding:20px 24px;font-style:italic}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{background:#161923;color:#6b77a0;text-align:left;
         padding:7px 10px;white-space:nowrap;border-bottom:1px solid #1e2235;
         font-weight:500}
tbody tr{border-bottom:1px solid #1a1d27}
tbody tr:hover{background:#161923}
td{padding:6px 10px;vertical-align:top}
.tbl-wrap{overflow-x:auto}
.stat-row{display:flex;gap:12px;padding:0 24px 18px;flex-wrap:wrap}
.stat{background:#161923;border:1px solid #1e2235;border-radius:6px;
      padding:12px 18px;min-width:130px}
.stat .v{font-size:26px;font-weight:700;color:#a855f7}
.stat .l{font-size:11px;color:#555d7a;margin-top:2px}
.badge{display:inline-block;padding:1px 6px;border-radius:3px;
       font-size:10px;font-weight:600;white-space:nowrap}
.sold{background:#1a3330;color:#34d399}
.nosell{background:#2d1f1a;color:#f97316}
.deal{background:#1a3a1a;color:#4caf50}
.over{background:#3a1a1a;color:#ef5350}
.new-badge{background:#1a2340;color:#60a5fa}
.record{background:#2d1a3a;color:#c084fc}
.up{color:#34d399;font-weight:600}
.down{color:#ef5350;font-weight:600}
.flat{color:#6b77a0}
.seg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
          gap:10px;padding:0 24px 18px}
.seg{background:#161923;border:1px solid #1e2235;border-radius:6px;padding:14px 16px}
.seg-name{font-size:12px;font-weight:600;color:#c0c4d8;margin-bottom:8px}
.seg-row{display:flex;justify-content:space-between;font-size:11px;
         color:#6b77a0;margin-top:3px}
.seg-val{color:#e8eaf0}
.pred-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
           gap:10px;padding:0 24px 18px}
.pred{background:#161923;border:1px solid #1e2235;border-radius:6px;padding:14px 16px}
.pred-seg{font-size:12px;font-weight:600;color:#c0c4d8;margin-bottom:6px}
.pred-dir{font-size:18px;font-weight:700;margin-bottom:4px}
.pred-conf{font-size:11px;color:#6b77a0;margin-bottom:6px}
.pred-reason{font-size:11px;color:#8890b0;line-height:1.5}
.archive-list{padding:0 24px 16px;display:flex;gap:8px;flex-wrap:wrap}
.archive-list a{background:#161923;border:1px solid #1e2235;border-radius:4px;
                padding:4px 10px;font-size:11px;color:#6b77a0}
.archive-list a:hover{color:#a855f7;text-decoration:none}
.cat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:0 24px 18px}
.cat-box{background:#161923;border:1px solid #1e2235;border-radius:6px;padding:14px 16px}
.cat-name{font-size:12px;font-weight:700;margin-bottom:8px}
.badge-DEALER{background:rgba(59,130,246,.18);color:#60a5fa}
.badge-AUCTION{background:rgba(168,85,247,.18);color:#c084fc}
.badge-RETAIL{background:rgba(34,197,94,.18);color:#4ade80}
"""


def _nav(active=""):
    links = [
        ("dashboard.html", "Dashboard"),
        ("market_report.html", "Market Report"),
        ("daily_report.html", "Daily"),
        ("weekly_report.html", "Weekly"),
        ("monthly_report.html", "Monthly"),
    ]
    parts = []
    for href, label in links:
        name = href.replace(".html", "")
        cls = ' style="color:#a855f7"' if name == active else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<div class="nav">' + "".join(parts) + "</div>"


def html_shell(title, body, active_nav=""):
    return (f'<!DOCTYPE html><html lang="en"><head>'
            f'<meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title>'
            f'<style>{CSS}</style>'
            f'</head><body>'
            f'{_nav(active_nav)}'
            f'{body}'
            f'</body></html>')


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def esc(s):
    if not s:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def fmt_price(p, fallback="—"):
    if p is None:
        return fallback
    try:
        return f"${int(p):,}"
    except (ValueError, TypeError):
        return fallback


def fmt_miles(m):
    if m is None:
        return "—"
    try:
        return f"{int(m):,}"
    except (ValueError, TypeError):
        return "—"


def pct_change(old, new):
    """Return formatted % change string with CSS class."""
    if not old or not new:
        return '<span class="flat">—</span>'
    chg = (new - old) / old * 100
    if abs(chg) < 0.5:
        return '<span class="flat">±0%</span>'
    cls = "up" if chg > 0 else "down"
    sign = "+" if chg > 0 else ""
    return f'<span class="{cls}">{sign}{chg:.1f}%</span>'


def safe_median(vals):
    filtered = [v for v in vals if v]
    return int(statistics.median(filtered)) if len(filtered) >= 2 else None


def safe_mean(vals):
    filtered = [v for v in vals if v]
    return int(statistics.mean(filtered)) if filtered else None


# ---------------------------------------------------------------------------
# Porsche generation bucketing (canonical, used across all reports)
# ---------------------------------------------------------------------------

_SEGMENTS = [
    # (label, test_fn)
    # air-cooled first
    ("356 (1950–1965)",           lambda y, m: y and y <= 1965 and "356" in (m or "")),
    ("Early 911 (1965–1973)",     lambda y, m: y and 1965 < y <= 1973 and "911" in (m or "")),
    ("930 Turbo (1975–1989)",     lambda y, m: y and 1975 <= y <= 1989 and "930" in (m or "")),
    ("3.2 Carrera (1984–1989)",   lambda y, m: y and 1984 <= y <= 1989),
    ("964 (1989–1994)",           lambda y, m: y and 1989 <= y <= 1994),
    ("993 (1995–1998)",           lambda y, m: y and 1995 <= y <= 1998),
    # water-cooled 911
    ("996 (1999–2004)",           lambda y, m: y and 1999 <= y <= 2004 and "boxster" not in (m or "").lower() and "cayman" not in (m or "").lower()),
    ("997 (2005–2012)",           lambda y, m: y and 2005 <= y <= 2012 and "boxster" not in (m or "").lower() and "cayman" not in (m or "").lower()),
    ("991 (2012–2019)",           lambda y, m: y and 2012 <= y <= 2019 and "boxster" not in (m or "").lower() and "cayman" not in (m or "").lower()),
    ("992 (2019+)",               lambda y, m: y and y >= 2019 and "boxster" not in (m or "").lower() and "cayman" not in (m or "").lower()),
    # Boxster / Cayman
    ("986 Boxster (1997–2004)",   lambda y, m: y and 1997 <= y <= 2004 and "boxster" in (m or "").lower()),
    ("987 Boxster/Cayman (2005–2012)", lambda y, m: y and 2005 <= y <= 2012 and ("boxster" in (m or "").lower() or "cayman" in (m or "").lower())),
    ("981 Boxster/Cayman (2012–2016)", lambda y, m: y and 2012 <= y <= 2016 and ("boxster" in (m or "").lower() or "cayman" in (m or "").lower())),
    ("718 Boxster/Cayman (2017+)",     lambda y, m: y and y >= 2017 and ("boxster" in (m or "").lower() or "cayman" in (m or "").lower() or "718" in (m or ""))),
]

_SEGMENT_LABELS = [s[0] for s in _SEGMENTS]
_SEGMENT_ORDER  = {s[0]: i for i, s in enumerate(_SEGMENTS)}


def generation(year, model):
    m = (model or "").lower()
    y = year
    for label, test in _SEGMENTS:
        try:
            if test(y, m):
                return label
        except Exception:
            pass
    # Fallback by year bands
    if y:
        if y < 1989:
            return "Air-cooled (pre-1989)"
        if y < 1999:
            return "993/964 era"
        if y < 2012:
            return "996/997 era"
        return "991/992 era"
    return "Other"


def group_by_generation(rows, year_key="year", model_key="model"):
    by_gen = defaultdict(list)
    for r in rows:
        g = generation(r.get(year_key), r.get(model_key))
        by_gen[g].append(r)
    # Return sorted by canonical order
    return sorted(by_gen.items(), key=lambda kv: _SEGMENT_ORDER.get(kv[0], 99))


# ---------------------------------------------------------------------------
# Simple linear trend (slope per period)
# ---------------------------------------------------------------------------

def linear_trend(values):
    """Given a list of (index, value) or just [value, value, ...],
    return (slope_pct_per_period, r_squared).
    slope_pct > 0 = rising, < 0 = falling."""
    if len(values) < 2:
        return 0.0, 0.0
    # Treat as y = [v0, v1, v2 ...], x = [0, 1, 2 ...]
    ys = [v for v in values if v is not None]
    if len(ys) < 2:
        return 0.0, 0.0
    xs = list(range(len(ys)))
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(xs[i] * ys[i] for i in range(n))
    denom = n * sxx - sx * sx
    if not denom:
        return 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    # R²
    mean_y = sy / n
    ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    # slope as % of mean
    if mean_y:
        slope_pct = slope / mean_y * 100
    else:
        slope_pct = 0.0
    return slope_pct, r2


# ---------------------------------------------------------------------------
# Category breakdown section (shared by weekly/monthly)
# ---------------------------------------------------------------------------

def section_category_breakdown(active_items, sold_items=None):
    """
    Return HTML showing DEALER / AUCTION / RETAIL breakdown.
    Items must have a 'source_category' field (or it defaults to DEALER).
    sold_items: list of sold_comps rows with 'sold_price' (optional).
    """
    cats = ["DEALER", "AUCTION", "RETAIL"]

    active_by_cat = defaultdict(list)
    for c in active_items:
        active_by_cat[c.get("source_category") or "DEALER"].append(c)

    sold_by_cat = defaultdict(list)
    if sold_items:
        for c in sold_items:
            sold_by_cat[c.get("source_category") or "DEALER"].append(c)

    html = '<div class="cat-grid">'
    for cat in cats:
        a = active_by_cat.get(cat, [])
        s = sold_by_cat.get(cat, [])

        prices = [c["price"] for c in a if c.get("price")]
        med_ask = fmt_price(safe_median(prices))

        days = [c["days_on_site"] for c in a if c.get("days_on_site")]
        avg_days = f"{int(statistics.mean(days))}d" if days else "—"

        html += f'''<div class="cat-box">
<div class="cat-name"><span class="badge badge-{cat}">{cat}</span></div>
<div class="seg-row"><span>Active listings</span><span class="seg-val">{len(a)}</span></div>
<div class="seg-row"><span>Median ask</span><span class="seg-val">{med_ask}</span></div>
<div class="seg-row"><span>Avg days listed</span><span class="seg-val">{avg_days}</span></div>'''

        if sold_items is not None:
            sold_ps = [c["sold_price"] for c in s if c.get("sold_price")]
            med_sold = fmt_price(safe_median(sold_ps)) if sold_ps else "—"
            html += (f'<div class="seg-row"><span>Sold comps</span>'
                     f'<span class="seg-val">{len(s)}</span></div>')
            html += (f'<div class="seg-row"><span>Median sold</span>'
                     f'<span class="seg-val">{med_sold}</span></div>')

        html += '</div>'
    html += '</div>'
    return html

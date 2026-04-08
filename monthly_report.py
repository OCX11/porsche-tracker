"""
Monthly market report (generated on 1st of each month).

Shows:
- Summary of last 30 days (new, sold, price changes)
- Trend lines per segment: air-cooled, 996/997, 991/992, Cayman/Boxster
- Accuracy check: last month's predictions vs actuals (if predictions were stored)
- Forward predictions: direction + confidence + reasoning per segment

Archives: static/monthly_YYYY-MM.html  (keeps last 12 months)
Output:   static/monthly_report.html   (always the latest)
"""
import json
import shutil
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import db
from _report_base import (
    html_shell, esc, fmt_price, fmt_miles, pct_change,
    generation, group_by_generation, safe_median, safe_mean,
    linear_trend, section_category_breakdown,
)

STATIC       = Path(__file__).parent / "static"
OUTPUT       = STATIC / "monthly_report.html"
PRED_STORE   = Path(__file__).parent / "data" / "predictions.json"
ARCHIVE_MAX  = 12


# ---------------------------------------------------------------------------
# Segment groupings for monthly overview
# ---------------------------------------------------------------------------

SEGMENT_GROUPS = {
    "Air-cooled": ["356 (1950–1965)", "Early 911 (1965–1973)",
                   "930 Turbo (1975–1989)", "3.2 Carrera (1984–1989)",
                   "964 (1989–1994)", "993 (1995–1998)"],
    "996 / 997":  ["996 (1999–2004)", "997 (2005–2012)"],
    "991 / 992":  ["991 (2012–2019)", "992 (2019+)"],
    "Cayman / Boxster": ["986 Boxster (1997–2004)",
                         "987 Boxster/Cayman (2005–2012)",
                         "981 Boxster/Cayman (2012–2016)",
                         "718 Boxster/Cayman (2017+)"],
}


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _load(conn, today):
    month_ago = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    six_months_ago = (date.fromisoformat(today) - timedelta(days=180)).isoformat()

    # New listings last 30 days
    new_month = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE date_first_seen>=? ORDER BY date_first_seen DESC",
        (month_ago,)
    ).fetchall()]

    # Sold last 30 days (from tracked inventory)
    sold_month = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status='sold' AND date_last_seen>=? ORDER BY days_on_site",
        (month_ago,)
    ).fetchall()]

    # Price drops last 30 days
    price_drops = [dict(r) for r in conn.execute(
        """SELECT l.year, l.model, l.trim, l.dealer, l.listing_url,
                  ph_new.price AS new_price, ph_old.price AS old_price,
                  ph_new.recorded_at
           FROM listings l
           JOIN price_history ph_new ON ph_new.listing_id=l.id AND ph_new.recorded_at>=?
           JOIN price_history ph_old ON ph_old.listing_id=l.id
               AND ph_old.recorded_at=(
                   SELECT MAX(recorded_at) FROM price_history
                   WHERE listing_id=l.id AND recorded_at<ph_new.recorded_at
               )
           WHERE ph_new.price < ph_old.price
           ORDER BY ph_new.recorded_at DESC""",
        (month_ago,)
    ).fetchall()]

    # All sold comps for trend analysis (last 6 months)
    all_comps = [dict(r) for r in conn.execute(
        """SELECT * FROM sold_comps
           WHERE sold_price IS NOT NULL AND scraped_at>=?
           ORDER BY scraped_at""",
        (six_months_ago,)
    ).fetchall()]

    # Monthly median prices from price_history (last 6 months) — for trend lines
    # Group price_history records by month + generation
    ph_trend = [dict(r) for r in conn.execute(
        """SELECT ph.price, ph.recorded_at, l.year, l.model, l.tier
           FROM price_history ph JOIN listings l ON l.id=ph.listing_id
           WHERE ph.recorded_at>=? AND ph.price IS NOT NULL
           ORDER BY ph.recorded_at""",
        (six_months_ago,)
    ).fetchall()]

    # Days-on-market by model (sold)
    days_stats = [dict(r) for r in conn.execute(
        """SELECT model, COUNT(*) cnt, AVG(days_on_site) avg_days,
                  MIN(days_on_site) min_days, MAX(days_on_site) max_days
           FROM listings WHERE status='sold' AND days_on_site IS NOT NULL
           GROUP BY model ORDER BY avg_days DESC"""
    ).fetchall()]

    # Active listings right now
    active = [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE status='active'"
    ).fetchall()]

    return new_month, sold_month, price_drops, all_comps, ph_trend, days_stats, active


# ---------------------------------------------------------------------------
# Trend line computation
# ---------------------------------------------------------------------------

def _build_monthly_medians(ph_trend, tier=None):
    """Group price_history by YYYY-MM and generation, compute monthly medians.

    If `tier` is provided, only include rows matching that tier.
    """
    by_month_gen = defaultdict(lambda: defaultdict(list))
    for r in ph_trend:
        if tier and r.get("tier") and r["tier"] != tier:
            continue
        month = r["recorded_at"][:7]  # YYYY-MM
        g     = generation(r.get("year"), r.get("model"))
        by_month_gen[month][g].append(r["price"])

    months  = sorted(by_month_gen.keys())
    gen_series = defaultdict(list)
    for m in months:
        for g, prices in by_month_gen[m].items():
            med = safe_median(prices)
            if med:
                gen_series[g].append((m, med))

    return gen_series


def _build_segment_trends(gen_series):
    """Roll individual generations up into the 4 high-level segments."""
    seg_series = defaultdict(lambda: defaultdict(list))
    for seg_name, gens in SEGMENT_GROUPS.items():
        for g in gens:
            for month, med in gen_series.get(g, []):
                seg_series[seg_name][month].append(med)

    # For each segment, produce list of (month, median_of_medians)
    result = {}
    for seg, month_prices in seg_series.items():
        series = [(m, safe_median(ps)) for m, ps in sorted(month_prices.items()) if safe_median(ps)]
        result[seg] = series
    return result


# ---------------------------------------------------------------------------
# Prediction generation
# ---------------------------------------------------------------------------

def _generate_prediction(seg_name, series, sold_comps):
    """Return dict {direction, pct, confidence, reasoning, predicted_median}."""
    if len(series) < 2:
        return {
            "direction": "—", "pct": None, "confidence": "low",
            "predicted_median": None,
            "reasoning": "Insufficient data — need at least 2 months of history.",
        }

    values = [v for _, v in series]
    slope_pct, r2 = linear_trend(values)

    # Confidence based on R² and sample size
    if len(values) >= 4 and r2 >= 0.7:
        conf = "high"
    elif len(values) >= 3 and r2 >= 0.4:
        conf = "medium"
    else:
        conf = "low"

    # Direction string
    if slope_pct > 1.5:
        direction = f"+{slope_pct:.1f}%"
        direction_word = "rising"
    elif slope_pct < -1.5:
        direction = f"{slope_pct:.1f}%"
        direction_word = "declining"
    else:
        direction = "Flat"
        direction_word = "flat"

    # Current median
    current_med = values[-1]
    predicted_med = int(current_med * (1 + slope_pct / 100)) if current_med else None

    # Reasoning
    reasons = []
    n_comps = len([c for c in sold_comps
                   if any(g.lower() in seg_name.lower()
                          for g in [c.get("model") or ""])])
    if n_comps:
        reasons.append(f"{n_comps} sold comps in this segment.")
    reasons.append(f"Trend slope: {slope_pct:+.1f}%/month over {len(values)} months (R²={r2:.2f}).")
    if slope_pct > 3:
        reasons.append("Strong upward momentum — demand likely exceeds supply.")
    elif slope_pct < -3:
        reasons.append("Sustained softness — consider waiting for stabilization.")
    elif abs(slope_pct) <= 1.5:
        reasons.append("Market appears balanced with minimal price pressure.")
    if conf == "low":
        reasons.append("Low confidence due to limited data — treat as directional only.")

    return {
        "direction": direction,
        "direction_word": direction_word,
        "pct": slope_pct,
        "confidence": conf,
        "predicted_median": predicted_med,
        "current_median": current_med,
        "reasoning": " ".join(reasons),
    }


# ---------------------------------------------------------------------------
# Prediction accuracy check
# ---------------------------------------------------------------------------

def _load_prior_predictions():
    """Load stored predictions from the previous month."""
    if not PRED_STORE.exists():
        return {}
    try:
        return json.loads(PRED_STORE.read_text())
    except Exception:
        return {}


def _save_predictions(preds, today):
    """Persist current predictions for next month's accuracy check."""
    PRED_STORE.parent.mkdir(exist_ok=True)
    data = {seg: {**p, "generated_on": today} for seg, p in preds.items()}
    PRED_STORE.write_text(json.dumps(data, indent=2))


def _accuracy_check(prior_preds, seg_trends):
    """Compare prior predictions to what actually happened."""
    if not prior_preds:
        return []
    results = []
    for seg, pred in prior_preds.items():
        series = seg_trends.get(seg, [])
        if not series:
            continue
        actual_med = series[-1][1] if series else None
        pred_med   = pred.get("predicted_median")
        if not pred_med or not actual_med:
            continue
        actual_pct = (actual_med - pred_med) / pred_med * 100
        correct_dir = (
            (pred.get("pct", 0) > 0 and actual_med > pred_med) or
            (pred.get("pct", 0) < 0 and actual_med < pred_med) or
            (abs(pred.get("pct", 0)) <= 1.5 and abs(actual_pct) <= 3)
        )
        results.append({
            "segment": seg,
            "predicted_median": pred_med,
            "actual_median": actual_med,
            "pct_error": actual_pct,
            "direction_correct": correct_dir,
            "generated_on": pred.get("generated_on", "?"),
        })
    return results


# ---------------------------------------------------------------------------
# HTML sections
# ---------------------------------------------------------------------------

def _section_summary(new_month, sold_month, price_drops, active):
    n_new    = len(new_month)
    n_sold   = len(sold_month)
    n_drops  = len(price_drops)
    n_active = len(active)
    n_t1     = sum(1 for c in active if c.get("tier") == "TIER1")

    priced = [c for c in active if c.get("price")]
    med_ask = safe_median([c["price"] for c in priced]) if priced else None

    days_list = [c["days_on_site"] for c in sold_month if c.get("days_on_site")]
    avg_days = int(statistics.mean(days_list)) if days_list else None

    html = f'''<div class="stat-row">
<div class="stat"><div class="v">{n_new}</div><div class="l">New Listings (30d)</div></div>
<div class="stat"><div class="v" style="color:#f97316">{n_sold}</div><div class="l">Sold (30d)</div></div>
<div class="stat"><div class="v" style="color:#ef5350">{n_drops}</div><div class="l">Price Drops (30d)</div></div>
<div class="stat"><div class="v">{n_active}</div><div class="l">Active Now</div></div>
<div class="stat"><div class="v" style="color:#d97706">{n_t1}</div><div class="l">GT/Collector Active</div></div>
<div class="stat"><div class="v">{fmt_price(med_ask)}</div><div class="l">Median Ask</div></div>
<div class="stat"><div class="v">{avg_days or "—"}d</div><div class="l">Avg Days to Sell</div></div>
</div>'''
    return html


def _section_segment_trends(seg_trends):
    if not seg_trends:
        return '<p class="empty">Trend data requires multiple months of price history.</p>'
    html = '<div class="seg-grid">'
    for seg_name, series in seg_trends.items():
        if not series:
            continue
        values = [v for _, v in series]
        slope_pct, _ = linear_trend(values)
        trend_cls = "up" if slope_pct > 1 else ("down" if slope_pct < -1 else "flat")
        trend_sym = "↑" if slope_pct > 1 else ("↓" if slope_pct < -1 else "→")
        cur = values[-1] if values else None
        prev = values[0] if len(values) > 1 else None
        html += f'''<div class="seg">
<div class="seg-name">{esc(seg_name)}</div>
<div class="seg-row"><span>Current median</span><span class="seg-val">{fmt_price(cur)}</span></div>
<div class="seg-row"><span>6-month start</span><span class="seg-val">{fmt_price(prev)}</span></div>
<div class="seg-row"><span>Trend</span>
  <span class="{trend_cls}">{trend_sym} {slope_pct:+.1f}%/mo</span></div>
<div class="seg-row"><span>Data points</span><span class="seg-val">{len(series)} months</span></div>
</div>'''
    html += "</div>"
    return html


def _section_predictions(predictions):
    if not predictions:
        return '<p class="empty">No prediction data available.</p>'
    html = '<div class="pred-grid">'
    conf_colors = {"high": "#34d399", "medium": "#f59e0b", "low": "#6b77a0"}
    for seg, pred in predictions.items():
        dir_word = pred.get("direction_word", "")
        dir_val  = pred.get("direction", "—")
        dir_cls  = "up" if "ris" in dir_word else ("down" if "declin" in dir_word else "flat")
        conf     = pred.get("confidence", "low")
        conf_col = conf_colors.get(conf, "#6b77a0")
        cur_med  = pred.get("current_median")
        pred_med = pred.get("predicted_median")
        html += f'''<div class="pred">
<div class="pred-seg">{esc(seg)}</div>
<div class="pred-dir {dir_cls}">{esc(dir_val)} next 30 days</div>
<div class="pred-conf">Confidence: <span style="color:{conf_col};font-weight:600">{conf.upper()}</span>
  &nbsp;·&nbsp; Current median: {fmt_price(cur_med)}
  &nbsp;·&nbsp; Forecast: {fmt_price(pred_med)}</div>
<div class="pred-reason">{esc(pred.get("reasoning",""))}</div>
</div>'''
    html += "</div>"
    return html


def _section_accuracy(accuracy):
    if not accuracy:
        return '<p class="empty">No prior predictions on record — accuracy tracking begins next month.</p>'
    html = '<div class="section"><div class="tbl-wrap"><table>'
    html += ('<thead><tr><th>Segment</th><th>Predicted Median</th>'
             '<th>Actual Median</th><th>Error</th><th>Direction</th></tr></thead><tbody>')
    for r in accuracy:
        err = r["pct_error"]
        err_cls = "up" if err > 0 else "down"
        correct = '✓' if r["direction_correct"] else '✗'
        corr_col = "#34d399" if r["direction_correct"] else "#ef5350"
        html += (f'<tr><td>{esc(r["segment"])}</td>'
                 f'<td>{fmt_price(r["predicted_median"])}</td>'
                 f'<td>{fmt_price(r["actual_median"])}</td>'
                 f'<td><span class="{err_cls}">{err:+.1f}%</span></td>'
                 f'<td style="color:{corr_col};font-weight:700">{correct}</td></tr>')
    html += "</tbody></table></div></div>"
    return html


def _archive_links():
    files = sorted(STATIC.glob("monthly_2*.html"), reverse=True)[:ARCHIVE_MAX]
    if not files:
        return ""
    links = "".join(f'<a href="{f.name}">{f.stem.replace("monthly_","")}</a>' for f in files)
    return f'<h3>Archive</h3><div class="archive-list">{links}</div>'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(today=None) -> Path:
    today  = today or date.today().isoformat()
    month  = today[:7]  # YYYY-MM
    month_start = date.fromisoformat(today).replace(day=1).isoformat()

    with db.get_conn() as conn:
        new_month, sold_month, price_drops, all_comps, ph_trend, days_stats, active = _load(conn, today)

    gen_series_t1 = _build_monthly_medians(ph_trend, tier="TIER1")
    gen_series_t2 = _build_monthly_medians(ph_trend, tier="TIER2")
    seg_trends_t1 = _build_segment_trends(gen_series_t1)
    seg_trends_t2 = _build_segment_trends(gen_series_t2)

    # Prior predictions for accuracy check
    prior_preds = _load_prior_predictions()
    accuracy_t1 = _accuracy_check(prior_preds.get("TIER1", {}), seg_trends_t1)
    accuracy_t2 = _accuracy_check(prior_preds.get("TIER2", {}), seg_trends_t2)

    # Generate new predictions per tier
    predictions_t1, predictions_t2 = {}, {}
    for seg_name in SEGMENT_GROUPS:
        predictions_t1[seg_name] = _generate_prediction(
            seg_name, seg_trends_t1.get(seg_name, []),
            [c for c in all_comps if c.get("tier") == "TIER1"]
        )
        predictions_t2[seg_name] = _generate_prediction(
            seg_name, seg_trends_t2.get(seg_name, []),
            [c for c in all_comps if c.get("tier") != "TIER1"]
        )

    # Persist for next month's accuracy check (keyed by tier)
    _save_predictions({"TIER1": predictions_t1, "TIER2": predictions_t2}, today)

    def tier_header(label, color):
        return f'<div style="border-left:4px solid {color};padding:10px 14px;margin:16px 0 0;background:#111"><strong style="color:{color}">{label}</strong></div>'

    body = (
        f'<h1>Monthly Market Report — {month}</h1>'
        f'<p class="meta">Generated {today} &nbsp;·&nbsp; '
        f'Covers {month_start} → {today}</p>'

        + _section_summary(new_month, sold_month, price_drops, active)

        + '<h2>Inventory by Source Category</h2>'
        + section_category_breakdown(active, all_comps)

        + '<h2>Segment Trend Lines (6-Month)</h2>'
        + tier_header("GT / Collector (Tier 1)", "#d97706")
        + _section_segment_trends(seg_trends_t1)
        + tier_header("Standard (Tier 2)", "#6b7280")
        + _section_segment_trends(seg_trends_t2)

        + '<h2>Forward Predictions — Next 30 Days</h2>'
        + tier_header("GT / Collector (Tier 1)", "#d97706")
        + _section_predictions(predictions_t1)
        + tier_header("Standard (Tier 2)", "#6b7280")
        + _section_predictions(predictions_t2)

        + '<h2>Prior Month Accuracy Check</h2>'
        + tier_header("GT / Collector (Tier 1)", "#d97706")
        + _section_accuracy(accuracy_t1)
        + tier_header("Standard (Tier 2)", "#6b7280")
        + _section_accuracy(accuracy_t2)

        + _archive_links()
    )

    html = html_shell(f"Monthly Report — {month}", body, active_nav="monthly_report")
    OUTPUT.write_text(html, encoding="utf-8")

    # Archive copy
    archive = STATIC / f"monthly_{month}.html"
    shutil.copy2(OUTPUT, archive)

    # Prune old archives
    old = sorted(STATIC.glob("monthly_2*.html"), reverse=True)[ARCHIVE_MAX:]
    for f in old:
        f.unlink(missing_ok=True)

    return OUTPUT


if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    path = generate()
    print(f"Monthly report: file://{path}")

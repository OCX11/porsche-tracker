"""
archive_search.py
-----------------
CLI search tool for the Porsche tracker inventory database.

Usage:
  python archive_search.py [options]

All filters are optional and combinable.
"""

import argparse
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
from db import get_conn, init_db


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(v):
    return f"${v:,}" if v else "—"

def _fmt_int(v):
    return f"{v:,}" if v else "—"

def _trunc(s, n):
    if not s:
        return "—"
    s = str(s)
    return s[:n-1] + "…" if len(s) > n else s

def _col(val, width, align="<"):
    s = str(val) if val is not None else "—"
    return f"{s:{align}{width}}"


# ── Query builder ─────────────────────────────────────────────────────────────

def build_query(args):
    wheres = []
    params = []

    # Status filter (default: all)
    if args.status == "active":
        wheres.append("status = 'active'")
    elif args.status == "sold":
        wheres.append("status = 'sold'")
    # else: all — no filter

    if args.year:
        wheres.append("year = ?")
        params.append(args.year)

    if args.year_range:
        wheres.append("year BETWEEN ? AND ?")
        params.extend(args.year_range)

    if args.model:
        wheres.append("LOWER(model) LIKE ?")
        params.append(f"%{args.model.lower()}%")

    if args.trim:
        wheres.append("LOWER(COALESCE(trim,'')) LIKE ?")
        params.append(f"%{args.trim.lower()}%")

    if args.color:
        wheres.append("LOWER(COALESCE(color,'')) LIKE ?")
        params.append(f"%{args.color.lower()}%")

    if args.transmission:
        wheres.append("LOWER(COALESCE(transmission,'')) LIKE ?")
        params.append(f"%{args.transmission.lower()}%")

    if args.vin:
        wheres.append("LOWER(COALESCE(vin,'')) LIKE ?")
        params.append(f"%{args.vin.lower()}%")

    if args.dealer:
        wheres.append("LOWER(dealer) LIKE ?")
        params.append(f"%{args.dealer.lower()}%")

    if args.price_min is not None:
        wheres.append("price >= ?")
        params.append(args.price_min)

    if args.price_max is not None:
        wheres.append("price <= ?")
        params.append(args.price_max)

    if args.mileage_max is not None:
        wheres.append("(mileage IS NULL OR mileage <= ?)")
        params.append(args.mileage_max)

    if args.days_min is not None:
        wheres.append("days_on_site >= ?")
        params.append(args.days_min)

    if args.tier:
        wheres.append("UPPER(COALESCE(tier,'')) = ?")
        params.append(args.tier.upper())

    if args.source:
        wheres.append("UPPER(COALESCE(source_category,'')) = ?")
        params.append(args.source.upper())

    if args.since:
        wheres.append("date_first_seen >= ?")
        params.append(args.since)

    if args.has_screenshot:
        wheres.append("screenshot_path IS NOT NULL AND screenshot_path != ''")

    if args.has_html:
        wheres.append("html_path IS NOT NULL AND html_path != '' AND html_path != 'FAILED'")

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    # Sort
    sort_map = {
        "price":   "price ASC NULLS LAST",
        "year":    "year DESC NULLS LAST",
        "mileage": "mileage ASC NULLS LAST",
        "days":    "days_on_site DESC NULLS LAST",
        "date":    "date_first_seen DESC",
    }
    order = sort_map.get(args.sort, "date_first_seen DESC")

    sql = f"""
        SELECT id, dealer, year, model, trim, color, transmission,
               price, mileage, days_on_site, status, screenshot_path, html_path,
               vin, source_category, tier, listing_url, date_first_seen, archived_at
        FROM listings
        {where_clause}
        ORDER BY {order}
        LIMIT ?
    """
    params.append(args.limit)
    return sql, params


# ── Output ────────────────────────────────────────────────────────────────────

# Column widths
_W = {
    "id":      5,
    "dealer":  22,
    "year":    4,
    "model":   6,
    "trim":    16,
    "color":   8,
    "trans":   6,
    "price":   10,
    "mileage": 8,
    "days":    5,
    "status":  6,
    "media":   2,
}

_HDR = (
    f"{'ID':>{_W['id']}}  "
    f"{'Dealer':<{_W['dealer']}}  "
    f"{'Year':>{_W['year']}}  "
    f"{'Model':<{_W['model']}}  "
    f"{'Trim':<{_W['trim']}}  "
    f"{'Color':<{_W['color']}}  "
    f"{'Trans':<{_W['trans']}}  "
    f"{'Price':>{_W['price']}}  "
    f"{'Mileage':>{_W['mileage']}}  "
    f"{'Days':>{_W['days']}}  "
    f"{'Status':<{_W['status']}}  "
    f"📷"
)
_SEP = "-" * len(_HDR)


def _media_icon(row):
    has_shot = row["screenshot_path"] and row["screenshot_path"] not in ("", "FAILED")
    has_html = row["html_path"] and row["html_path"] not in ("", "FAILED")
    if has_shot and has_html:
        return "✓✓"
    if has_shot:
        return "📷"
    if has_html:
        return "H"
    return ""


def print_table(rows):
    print(_HDR)
    print(_SEP)
    for r in rows:
        media = _media_icon(r)
        line = (
            f"{r['id']:>{_W['id']}}  "
            f"{_trunc(r['dealer'], _W['dealer']):<{_W['dealer']}}  "
            f"{r['year'] or '?':>{_W['year']}}  "
            f"{_trunc(r['model'], _W['model']):<{_W['model']}}  "
            f"{_trunc(r['trim'], _W['trim']):<{_W['trim']}}  "
            f"{_trunc(r['color'], _W['color']):<{_W['color']}}  "
            f"{_trunc(r['transmission'], _W['trans']):<{_W['trans']}}  "
            f"{_fmt_price(r['price']):>{_W['price']}}  "
            f"{_fmt_int(r['mileage']):>{_W['mileage']}}  "
            f"{r['days_on_site'] or '?':>{_W['days']}}  "
            f"{(r['status'] or ''):6}  "
            f"{media}"
        )
        print(line)


def print_summary(rows, total_count):
    active  = sum(1 for r in rows if r["status"] == "active")
    sold    = sum(1 for r in rows if r["status"] == "sold")
    prices  = [r["price"] for r in rows if r["price"]]
    with_ss = sum(1 for r in rows if r["screenshot_path"] and r["screenshot_path"] not in ("", "FAILED"))
    with_h  = sum(1 for r in rows if r["html_path"] and r["html_path"] not in ("", "FAILED", None))

    print(_SEP)
    print(f"Showing {len(rows)} of {total_count} matching row(s)  |  "
          f"active={active}  sold={sold}  "
          f"screenshots={with_ss}  html={with_h}")
    if prices:
        print(f"Price range: {_fmt_price(min(prices))} – {_fmt_price(max(prices))}  "
              f"avg: {_fmt_price(int(sum(prices)/len(prices)))}")


# ── Open action ───────────────────────────────────────────────────────────────

def open_listing(listing_id):
    init_db()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT screenshot_path, html_path, listing_url FROM listings WHERE id=?",
            (listing_id,)
        ).fetchone()

    if not row:
        print(f"No listing found with id={listing_id}")
        sys.exit(1)

    shot = row["screenshot_path"]
    html = row["html_path"]
    url  = row["listing_url"]

    # Prefer screenshot, then HTML, then URL
    if shot and shot not in ("", "FAILED"):
        path = BASE_DIR / shot
        if path.exists():
            print(f"Opening screenshot: {path}")
            subprocess.run(["open", str(path)])
            return
        print(f"Screenshot file missing: {path}")

    if html and html not in ("", "FAILED"):
        path = BASE_DIR / html
        if path.exists():
            print(f"Opening HTML: {path}")
            subprocess.run(["open", str(path)])
            return
        print(f"HTML file missing: {path}")

    if url:
        print(f"Opening URL: {url}")
        subprocess.run(["open", url])
        return

    print(f"No archive files or URL for listing {listing_id}")
    sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Search the Porsche tracker inventory database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Open shortcut
    p.add_argument("--open", type=int, metavar="ID",
                   help="Open the screenshot/html/url for listing ID in Preview")

    # Filters
    p.add_argument("--year",       type=int, metavar="YYYY")
    p.add_argument("--year-range", type=int, nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--model",      type=str)
    p.add_argument("--trim",       type=str)
    p.add_argument("--color",      type=str)
    p.add_argument("--transmission", type=str)
    p.add_argument("--vin",        type=str)
    p.add_argument("--dealer",     type=str)
    p.add_argument("--price-min",  type=int, metavar="N")
    p.add_argument("--price-max",  type=int, metavar="N")
    p.add_argument("--mileage-max", type=int, metavar="N")
    p.add_argument("--days-min",   type=int, metavar="N")
    p.add_argument("--status",     choices=["active", "sold", "all"], default="all")
    p.add_argument("--tier",       choices=["TIER1", "TIER2"])
    p.add_argument("--source",     choices=["AUCTION", "RETAIL", "DEALER"])
    p.add_argument("--since",      type=str, metavar="YYYY-MM-DD")
    p.add_argument("--has-screenshot", action="store_true")
    p.add_argument("--has-html",   action="store_true")

    # Output
    p.add_argument("--limit",  type=int, default=100)
    p.add_argument("--sort",   choices=["price", "year", "mileage", "days", "date"],
                   default="date")

    return p.parse_args()


def main():
    args = parse_args()

    # --open: just open and exit
    if args.open:
        open_listing(args.open)
        return

    init_db()
    sql, params = build_query(args)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

        # Count total matching (without LIMIT)
        count_sql = sql.split("ORDER BY")[0].replace(
            "SELECT id, dealer, year, model, trim, color, transmission,\n               price, mileage, days_on_site, status, screenshot_path, html_path,\n               vin, source_category, tier, listing_url, date_first_seen, archived_at",
            "SELECT COUNT(*)"
        )
        # Simpler: re-run count query
        where_start = sql.find("WHERE")
        where_end   = sql.find("ORDER BY")
        if where_start != -1:
            where_part = sql[where_start:where_end].strip()
            count_params = params[:-1]  # drop LIMIT param
        else:
            where_part = ""
            count_params = []
        total = conn.execute(
            f"SELECT COUNT(*) FROM listings {where_part}", count_params
        ).fetchone()[0]

    rows = [dict(r) for r in rows]

    if not rows:
        print("No listings found matching those filters.")
        return

    print_table(rows)
    print_summary(rows, total)


if __name__ == "__main__":
    main()

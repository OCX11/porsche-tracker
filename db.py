"""Database layer for Porsche competitor inventory tracker."""
import re
import sqlite3
import json
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "inventory.db"


def classify_tier(model: str, trim: str, year: int = None) -> str:
    """Return 'TIER1' (GT/Collector) or 'TIER2' (Standard) based on model, trim, and year."""
    m = (model or "").lower()
    t = (trim or "").lower()
    combined = f"{m} {t}"

    # Air-cooled era: pre-1998 911 (993/964/930 generations)
    if year and year <= 1998 and "911" in m:
        return "TIER1"

    # Explicit chassis codes in model field (e.g. when scraper stores "930", "964", "993")
    if any(code in m for code in ("930", "964", "993")):
        return "TIER1"

    # GT3, GT2, GT4 (any suffix: RS, Touring, etc.)
    if re.search(r"\bgt[234]\b", combined):
        return "TIER1"

    # Turbo S (not plain Turbo, not "Turbo Silver" or other color names)
    if re.search(r"\bturbo s\b", combined):
        return "TIER1"

    # Spyder (Boxster Spyder, 918 Spyder, etc.)
    if "spyder" in combined:
        return "TIER1"

    # Speedster
    if "speedster" in combined:
        return "TIER1"

    # Sport Classic
    if "sport classic" in combined:
        return "TIER1"

    # Cayman R (standalone "R" in trim only)
    if "cayman" in m and re.search(r"\br\b", t):
        return "TIER1"

    # 356 (classic)
    if "356" in m:
        return "TIER1"

    return "TIER2"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dealer          TEXT NOT NULL,
                vin             TEXT,
                year            INTEGER,
                make            TEXT,
                model           TEXT,
                trim            TEXT,
                mileage         INTEGER,
                price           INTEGER,
                listing_url     TEXT,
                image_url       TEXT,
                date_first_seen TEXT NOT NULL,
                date_last_seen  TEXT NOT NULL,
                days_on_site    INTEGER GENERATED ALWAYS AS (
                    CAST(julianday(date_last_seen) - julianday(date_first_seen) AS INTEGER) + 1
                ) STORED,
                status          TEXT DEFAULT 'active',  -- active | sold
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id  INTEGER NOT NULL REFERENCES listings(id),
                price       INTEGER,
                recorded_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                snap_date   TEXT NOT NULL,
                dealer      TEXT NOT NULL,
                listing_key TEXT NOT NULL,
                year        INTEGER,
                make        TEXT,
                model       TEXT,
                trim        TEXT,
                mileage     INTEGER,
                price       INTEGER,
                listing_url TEXT
            );

            CREATE TABLE IF NOT EXISTS sold_comps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                year        INTEGER,
                make        TEXT,
                model       TEXT,
                trim        TEXT,
                mileage     INTEGER,
                sold_price  INTEGER,
                sold_date   TEXT,
                listing_url TEXT,
                image_url   TEXT,
                title       TEXT,
                scraped_at  TEXT DEFAULT (date('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_listings_dealer_vin
                ON listings(dealer, vin) WHERE vin IS NOT NULL;

            CREATE INDEX IF NOT EXISTS ix_listings_status ON listings(status);
            CREATE INDEX IF NOT EXISTS ix_snapshots_date  ON snapshots(snap_date);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_day
                ON snapshots(snap_date, dealer, listing_key);
            CREATE TABLE IF NOT EXISTS bat_reserve_not_met (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT,
                year        INTEGER,
                model       TEXT,
                high_bid    INTEGER,
                auction_date TEXT,
                listing_url TEXT,
                bids        INTEGER,
                date_scraped TEXT DEFAULT (date('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bat_rnm_url
                ON bat_reserve_not_met(listing_url) WHERE listing_url IS NOT NULL;

            CREATE TABLE IF NOT EXISTS hagerty_valuations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                year            INTEGER NOT NULL,
                make            TEXT    DEFAULT 'Porsche',
                model           TEXT    NOT NULL,
                trim            TEXT,
                generation      TEXT,
                condition_good_price       INTEGER,
                condition_excellent_price  INTEGER,
                hagerty_url     TEXT,
                scraped_at      TEXT DEFAULT (date('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_hagerty_val
                ON hagerty_valuations(year, model, trim, scraped_at);

            -- NOTE: sold_comps_view is created/replaced AFTER all migrations below
            --       so that it can safely reference all columns including those added
            --       by ALTER TABLE. Do not create it here.
        """)

        # Deduplicate sold_comps before creating the unique index to avoid
        # IntegrityError when executescript's implicit COMMIT enforces constraints.
        # Keep the row with the highest id (most recently inserted) for each duplicate.
        idx_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_sold_comp_url'"
        ).fetchone()
        if not idx_exists:
            conn.execute("""
                DELETE FROM sold_comps
                WHERE listing_url IS NOT NULL
                  AND id NOT IN (
                      SELECT MAX(id) FROM sold_comps
                      WHERE listing_url IS NOT NULL
                      GROUP BY source, listing_url
                  )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_sold_comp_url
                    ON sold_comps(source, listing_url) WHERE listing_url IS NOT NULL
            """)
            conn.commit()

        # Migrations
        cols = [r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
        if "image_url" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN image_url TEXT")
        if "source_category" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN source_category TEXT")
            # Backfill existing rows
            conn.execute("""
                UPDATE listings SET source_category = CASE
                    WHEN LOWER(dealer) IN (
                        'holt motorsports','sloan motor cars','ryan friedman motor cars',
                        'velocity porsche','european collectables','lbi limited',
                        'road scholars','gaudin classic','udrive automobiles',
                        'motorcars of the main line','grand prix motors'
                    ) THEN 'DEALER'
                    WHEN LOWER(dealer) IN (
                        'bring a trailer','bat','pcarmarket'
                    ) THEN 'AUCTION'
                    ELSE 'RETAIL'
                END
                WHERE source_category IS NULL
            """)

        # Clean bad price_history records (NULL, zero, or implausibly large values)
        conn.execute("""
            DELETE FROM price_history
            WHERE price IS NULL OR price <= 0 OR price > 2000000
        """)

        sc_cols = [r[1] for r in conn.execute("PRAGMA table_info(sold_comps)").fetchall()]
        if "source_category" not in sc_cols:
            conn.execute("ALTER TABLE sold_comps ADD COLUMN source_category TEXT")
            conn.execute("""
                UPDATE sold_comps SET source_category = CASE
                    WHEN LOWER(source) IN ('bat','bring a trailer','carsandbids','cars & bids','classic.com','pcarmarket') THEN 'AUCTION'
                    WHEN LOWER(source) IN ('pca mart','rennlist','autotrader','cars.com','ebay') THEN 'RETAIL'
                    ELSE 'AUCTION'
                END
                WHERE source_category IS NULL
            """)

        if "transmission" not in sc_cols:
            conn.execute("ALTER TABLE sold_comps ADD COLUMN transmission TEXT")
        if "vin" not in sc_cols:
            conn.execute("ALTER TABLE sold_comps ADD COLUMN vin TEXT")
        if "color" not in sc_cols:
            conn.execute("ALTER TABLE sold_comps ADD COLUMN color TEXT")

        # tier column: listings
        if "tier" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN tier TEXT")
            rows = conn.execute(
                "SELECT id, year, model, trim FROM listings WHERE tier IS NULL"
            ).fetchall()
            for r in rows:
                t = classify_tier(r[2], r[3], r[1])
                conn.execute("UPDATE listings SET tier=? WHERE id=?", (t, r[0]))

        # tier column: sold_comps
        if "tier" not in sc_cols:
            conn.execute("ALTER TABLE sold_comps ADD COLUMN tier TEXT")
            sc_rows = conn.execute(
                "SELECT id, year, model, trim FROM sold_comps WHERE tier IS NULL"
            ).fetchall()
            for r in sc_rows:
                t = classify_tier(r[2], r[3], r[1])
                conn.execute("UPDATE sold_comps SET tier=? WHERE id=?", (t, r[0]))

        # Archive / capture columns: listings
        for col in ("color", "transmission", "condition", "body_style", "location",
                    "seller_type", "notes", "archived_at", "archive_reason",
                    "html_path", "screenshot_path"):
            if col not in cols:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")

        # feed_type: 'live' = actionable marketplace sources (BaT, pcarmarket,
        #            Rennlist, PCA Mart, classic.com, Cars & Bids) — surfaces in
        #            the Live Feed dashboard.
        #            'market' = dealer inventory + eBay/AutoTrader/cars.com —
        #            goes straight to historical/analytical data only.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
        if "feed_type" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN feed_type TEXT DEFAULT 'market'")
            # Backfill existing rows based on dealer name
            conn.execute("""
                UPDATE listings SET feed_type = CASE
                    WHEN LOWER(dealer) IN (
                        'bring a trailer','bat','bringatrailer',
                        'pcarmarket','cars & bids','carsandbids',
                        'classic.com','rennlist','pca mart'
                    ) THEN 'live'
                    ELSE 'market'
                END
            """)

        # Extended sold_comps columns (added by enrich_bat_vins.py)
        sc_cols_now = [r[1] for r in conn.execute("PRAGMA table_info(sold_comps)").fetchall()]
        for col in ("generation", "engine", "drivetrain", "options"):
            if col not in sc_cols_now:
                conn.execute(f"ALTER TABLE sold_comps ADD COLUMN {col} TEXT")

        # Sanitise sold_date: some scrapers wrote the asking price into sold_date
        # (e.g. "89990", "117000") instead of a proper YYYY-MM-DD string.
        # NULL these out so they don't corrupt date-filtered FMV queries.
        conn.execute("""
            UPDATE sold_comps
            SET sold_date = NULL
            WHERE sold_date IS NOT NULL
              AND sold_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        """)

        # Sanitise mileage: some scrapers stored model/trim text in the mileage
        # column (e.g. "Carrera Cabriolet 54K", "Turbo", "S 6-Speed").
        # SQLite stores these as TEXT; passing them to fmt_price/fmt_miles causes
        # "Cannot specify ',' with 's'" in the market report.  NULL them out.
        conn.execute("""
            UPDATE sold_comps
            SET mileage = NULL
            WHERE mileage IS NOT NULL
              AND typeof(mileage) = 'text'
        """)

        # Rebuild sold_comps_view after all migrations so it sees every column
        conn.execute("DROP VIEW IF EXISTS sold_comps_view")
        conn.execute("""
            CREATE VIEW sold_comps_view AS
                SELECT
                    id,
                    source,
                    year,
                    make,
                    model,
                    trim,
                    mileage,
                    sold_price  AS sale_price,
                    sold_date   AS sale_date,
                    listing_url,
                    image_url,
                    title,
                    scraped_at,
                    source_category,
                    tier,
                    transmission,
                    vin,
                    color,
                    generation,
                    engine,
                    drivetrain,
                    options
                FROM sold_comps
        """)


_DEALER_NAMES = frozenset({
    "holt motorsports", "sloan motor cars", "ryan friedman motor cars",
    "velocity porsche", "european collectables", "lbi limited", "road scholars",
    "gaudin classic", "udrive automobiles", "motorcars of the main line", "grand prix motors",
    "built for backroads",                        # Distill-fed dealer
})
_AUCTION_NAMES = frozenset({
    "bring a trailer", "bat", "bringatrailer", "pcarmarket",
    "cars & bids", "carsandbids", "classic.com",
})
_RETAIL_NAMES = frozenset({
    "pca mart", "rennlist", "autotrader", "cars.com", "ebay",
    # rennlist already present; cars.com now also Distill-fed
})


def source_category(name: str) -> str:
    """Return 'DEALER', 'AUCTION', or 'RETAIL' for a dealer/source name."""
    n = (name or "").lower().strip()
    if n in _DEALER_NAMES or any(d in n for d in _DEALER_NAMES):
        return "DEALER"
    if n in _AUCTION_NAMES or any(a in n for a in _AUCTION_NAMES):
        return "AUCTION"
    if n in _RETAIL_NAMES or any(r in n for r in _RETAIL_NAMES):
        return "RETAIL"
    # Fallback: anything with a URL-like or source-like name that isn't a dealer
    return "DEALER"


# Sources whose new listings surface in the Live Feed (actionable within minutes).
# Everything else is market/historical context only.
_LIVE_SOURCES = frozenset({
    "bring a trailer", "bat", "bringatrailer",
    "pcarmarket",
    "cars & bids", "carsandbids",
    "classic.com",
    "rennlist",
    "pca mart",
})

def feed_type_for(dealer: str) -> str:
    """Return 'live' for actionable marketplace sources, 'market' for dealer/retail context."""
    return "live" if (dealer or "").lower().strip() in _LIVE_SOURCES else "market"


def upsert_listing(conn, dealer, year, make, model, trim, mileage, price, vin, url, today,
                   image_url=None, color=None, transmission=None, location=None,
                   condition=None, body_style=None, seller_type=None, feed_type=None,
                   date_first_seen=None):
    """Insert or update a listing. Returns (listing_id, is_new, price_changed)."""
    # Auto-derive feed_type from dealer name if not explicitly supplied
    if feed_type is None:
        feed_type = feed_type_for(dealer)

    row = conn.execute(
        "SELECT id, price, status FROM listings WHERE dealer=? AND vin=?",
        (dealer, vin)
    ).fetchone() if vin else conn.execute(
        """SELECT id, price, status FROM listings
           WHERE dealer=? AND year=? AND make=? AND model=? AND vin IS NULL
           LIMIT 1""",
        (dealer, year, make, model)
    ).fetchone()

    tier = classify_tier(model, trim, year)

    if row:
        listing_id = row["id"]
        old_price = row["price"]
        price_changed = (old_price != price) and price is not None and old_price is not None

        conn.execute(
            """UPDATE listings
               SET date_last_seen=?, price=?, trim=?, mileage=?, listing_url=?,
                   image_url=COALESCE(?,image_url), status='active',
                   source_category=COALESCE(source_category,?), tier=?,
                   color=COALESCE(?,color), transmission=COALESCE(?,transmission),
                   location=COALESCE(?,location), condition=COALESCE(?,condition),
                   body_style=COALESCE(?,body_style), seller_type=COALESCE(?,seller_type),
                   feed_type=COALESCE(feed_type,?)
               WHERE id=?""",
            (today, price, trim, mileage, url, image_url, source_category(dealer), tier,
             color, transmission, location, condition, body_style, seller_type,
             feed_type, listing_id)
        )
        if price_changed and price and price > 0 and price < 2_000_000:
            conn.execute(
                "INSERT INTO price_history(listing_id, price, recorded_at) VALUES(?,?,datetime('now'))",
                (listing_id, price)
            )
        return listing_id, False, price_changed
    else:
        cur = conn.execute(
            """INSERT INTO listings(dealer, vin, year, make, model, trim, mileage, price,
                                    listing_url, image_url, date_first_seen, date_last_seen,
                                    source_category, tier, color, transmission, location,
                                    condition, body_style, seller_type, feed_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dealer, vin, year, make, model, trim, mileage, price, url, image_url,
             date_first_seen or today, today,
             source_category(dealer), tier, color, transmission, location,
             condition, body_style, seller_type, feed_type)
        )
        listing_id = cur.lastrowid
        if price and price > 0 and price < 2_000_000:
            conn.execute(
                "INSERT INTO price_history(listing_id, price, recorded_at) VALUES(?,?,datetime('now'))",
                (listing_id, price)
            )
        return listing_id, True, False


def archive_listing(conn, listing_id, reason="sold"):
    """Mark a listing as sold/archived with a timestamp."""
    conn.execute(
        """UPDATE listings
           SET status='sold', archived_at=datetime('now'), archive_reason=?
           WHERE id=?""",
        (reason, listing_id)
    )


def update_listing_paths(conn, listing_id, html_path=None, screenshot_path=None):
    """Store capture file paths after archiving a listing page."""
    if html_path is not None:
        conn.execute("UPDATE listings SET html_path=? WHERE id=?", (html_path, listing_id))
    if screenshot_path is not None:
        conn.execute("UPDATE listings SET screenshot_path=? WHERE id=?", (screenshot_path, listing_id))


def insert_bat_reserve_not_met(conn, title, year, model, high_bid, auction_date,
                               listing_url, bids=None):
    """Insert a BaT reserve-not-met record; ignore if listing_url already exists."""
    try:
        conn.execute(
            """INSERT OR IGNORE INTO bat_reserve_not_met
               (title, year, model, high_bid, auction_date, listing_url, bids)
               VALUES (?,?,?,?,?,?,?)""",
            (title, year, model, high_bid, auction_date, listing_url, bids)
        )
    except Exception:
        pass


def upsert_sold_comp(conn, source, year, make, model, trim, mileage, sold_price,
                     sold_date, listing_url, image_url=None, title=None,
                     transmission=None, vin=None, color=None):
    """Insert a sold comp; ignore if URL already exists."""
    cat = source_category(source)
    tier = classify_tier(model, trim, year)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO sold_comps
               (source, year, make, model, trim, mileage, sold_price, sold_date,
                listing_url, image_url, title, source_category, tier,
                transmission, vin, color)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, year, make, model, trim, mileage, sold_price, sold_date,
             listing_url, image_url, title, cat, tier,
             transmission, vin, color)
        )
    except Exception:
        pass


def upsert_hagerty_valuation(conn, year, model, trim, generation,
                              condition_good, condition_excellent, url):
    today = date.today().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO hagerty_valuations
           (year, model, trim, generation, condition_good_price, condition_excellent_price,
            hagerty_url, scraped_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (year, model, trim, generation, condition_good, condition_excellent, url, today)
    )


def get_hagerty_valuations(conn):
    """Latest Hagerty valuation per year/model/trim combination."""
    return [dict(r) for r in conn.execute("""
        SELECT h.* FROM hagerty_valuations h
        WHERE h.scraped_at = (
            SELECT MAX(scraped_at) FROM hagerty_valuations h2
            WHERE h2.year=h.year AND h2.model=h.model
              AND COALESCE(h2.trim,'')=COALESCE(h.trim,'')
        )
        ORDER BY h.generation, h.year, h.trim
    """).fetchall()]


def mark_sold(conn, dealer, active_keys, today):
    """Mark listings not seen today as sold/archived."""
    if not active_keys:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM listings WHERE dealer=? AND status='active'", (dealer,)
        ).fetchall()]
        conn.execute(
            """UPDATE listings SET status='sold', date_last_seen=?,
               archived_at=COALESCE(archived_at, datetime('now')), archive_reason=COALESCE(archive_reason,'sold')
               WHERE dealer=? AND status='active'""",
            (today, dealer)
        )
        return

    placeholders = ",".join("?" * len(active_keys))
    conn.execute(
        f"""UPDATE listings
            SET status='sold', date_last_seen=?,
                archived_at=COALESCE(archived_at, datetime('now')),
                archive_reason=COALESCE(archive_reason,'sold')
            WHERE dealer=? AND status='active'
            AND COALESCE(vin, year||'|'||make||'|'||model||'|'||mileage||'|'||price)
            NOT IN ({placeholders})""",
        [today, dealer] + list(active_keys)
    )


def save_snapshot(conn, snap_date, dealer, cars):
    """Save today's raw snapshot for diff purposes."""
    for c in cars:
        key = c.get("vin") or f"{c.get('year')}|{c.get('make')}|{c.get('model')}|{c.get('mileage')}|{c.get('price')}"
        conn.execute(
            """INSERT OR REPLACE INTO snapshots
               (snap_date, dealer, listing_key, year, make, model, trim, mileage, price, listing_url)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (snap_date, dealer, key,
             c.get("year"), c.get("make"), c.get("model"), c.get("trim"),
             c.get("mileage"), c.get("price"), c.get("url"))
        )


def clean_nonconforming(conn):
    """Delete listings that don't match the Porsche 911/Cayman/Boxster/718 1965-2027 filter."""
    bad_ids = conn.execute("""
        SELECT id FROM listings WHERE
            LOWER(COALESCE(make,'')) NOT IN ('porsche','')
            OR COALESCE(year,2000) < 1965
            OR COALESCE(year,2000) > 2027
            OR (
                LOWER(COALESCE(model,'')) NOT LIKE '%911%'
                AND LOWER(COALESCE(model,'')) NOT LIKE '%cayman%'
                AND LOWER(COALESCE(model,'')) NOT LIKE '%boxster%'
                AND LOWER(COALESCE(model,'')) NOT LIKE '%718%'
            )
    """).fetchall()
    if not bad_ids:
        return 0
    ids = [r[0] for r in bad_ids]
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM price_history WHERE listing_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM listings WHERE id IN ({placeholders})", ids)
    return len(ids)


def get_price_history(conn, listing_id):
    rows = conn.execute(
        "SELECT price, recorded_at FROM price_history WHERE listing_id=? ORDER BY recorded_at",
        (listing_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_dashboard_data(conn):
    today = date.today().isoformat()

    active = conn.execute(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM price_history ph WHERE ph.listing_id=l.id) AS price_changes
           FROM listings l WHERE l.status='active'
           ORDER BY l.date_first_seen DESC"""
    ).fetchall()

    new_today = conn.execute(
        "SELECT * FROM listings WHERE date_first_seen=? ORDER BY dealer",
        (today,)
    ).fetchall()

    sold_today = conn.execute(
        "SELECT * FROM listings WHERE date_last_seen=? AND status='sold' ORDER BY dealer",
        (today,)
    ).fetchall()

    # Active auctions with current bid and previous bid for progression display
    active_auctions = conn.execute(
        """WITH ranked AS (
               SELECT listing_id, price, recorded_at,
                      ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY recorded_at DESC) AS rn
               FROM price_history
               WHERE price > 0 AND price < 2000000
           )
           SELECT l.*,
                  COALESCE(ph_cur.price, l.price) AS current_bid,
                  ph_prev.price AS prev_bid
           FROM listings l
           LEFT JOIN ranked ph_cur  ON ph_cur.listing_id=l.id  AND ph_cur.rn=1
           LEFT JOIN ranked ph_prev ON ph_prev.listing_id=l.id AND ph_prev.rn=2
           WHERE l.status='active'
             AND l.source_category='AUCTION'
           ORDER BY l.date_first_seen DESC"""
    ).fetchall()

    dealer_counts = conn.execute(
        """SELECT dealer, COUNT(*) AS cnt
           FROM listings WHERE status='active'
           GROUP BY dealer ORDER BY cnt DESC"""
    ).fetchall()

    # Check if bat_reserve_not_met exists before querying (may not exist on older DBs)
    rnm_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bat_reserve_not_met'"
    ).fetchone()
    reserve_not_met = []
    if rnm_exists:
        reserve_not_met = conn.execute(
            """SELECT * FROM bat_reserve_not_met
               ORDER BY auction_date DESC, id DESC
               LIMIT 10"""
        ).fetchall()

    return {
        "active": [dict(r) for r in active],
        "new_today": [dict(r) for r in new_today],
        "sold_today": [dict(r) for r in sold_today],
        "active_auctions": [dict(r) for r in active_auctions],
        "dealer_counts": [dict(r) for r in dealer_counts],
        "reserve_not_met": [dict(r) for r in reserve_not_met],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
    }


def get_market_data(conn):
    """Data for the market analysis report."""
    active = conn.execute(
        "SELECT * FROM listings WHERE status='active' ORDER BY year DESC, model, price"
    ).fetchall()

    sold_comps = conn.execute(
        """SELECT * FROM sold_comps
           ORDER BY sold_date DESC, year DESC"""
    ).fetchall()

    price_history = conn.execute(
        """SELECT l.year, l.model, l.trim, l.dealer,
                  ph.price, ph.recorded_at
           FROM price_history ph
           JOIN listings l ON l.id = ph.listing_id
           ORDER BY ph.recorded_at DESC
           LIMIT 2000"""
    ).fetchall()

    days_stats = conn.execute(
        """SELECT model,
                  COUNT(*) as cnt,
                  AVG(days_on_site) as avg_days,
                  MIN(days_on_site) as min_days,
                  MAX(days_on_site) as max_days
           FROM listings
           WHERE status='sold' AND days_on_site IS NOT NULL
           GROUP BY model
           ORDER BY avg_days DESC"""
    ).fetchall()

    hagerty = get_hagerty_valuations(conn)

    return {
        "active": [dict(r) for r in active],
        "sold_comps": [dict(r) for r in sold_comps],
        "price_history": [dict(r) for r in price_history],
        "days_stats": [dict(r) for r in days_stats],
        "hagerty": hagerty,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

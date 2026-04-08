"""
distill_poller.py
-----------------
Polls Distill's local SQLite database every 60 seconds for newly triggered
sieve_data rows, parses the extracted text, and upserts Porsche listings
into inventory.db.

Replaces the webhook/watcher approach — no webhook needed, no file drops,
no cloud routing. Just a direct read from the same SQLite file Distill
writes locally on every check run.

Source routing:
  classic.com          → dealer="classic.com"          category=AUCTION
  cars.com             → dealer="cars.com"             category=RETAIL
  rennlist.com         → dealer="Rennlist"             category=RETAIL
  builtforbackroads.com→ dealer="Built for Backroads"  category=DEALER
  mart.pca.org / ebay  → SKIP (active scraper owns these)

Text format per site (what Distill extracts into sieve_data.text):
  classic.com   : blocks delimited by "bookmark_border", each has year/model/mileage/price
  cars.com      : blocks delimited by "Gallery", each has "Used YEAR Porsche MODEL", price, mi
  rennlist.com  : blocks delimited by "For Sale" header lines, each has year/model/price
  builtforbackroads.com: blocks delimited by "New listing", each has price and "Xk miles"

Run permanently via launchd (com.porschetracker.distill-poller.plist).
"""

import json
import logging
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_FILE   = BASE_DIR / "logs" / "distill_poller.log"
STATE_FILE = BASE_DIR / "distill_poller_state.json"
DISTILL_DB = Path.home() / "Library/Application Support/Distill Web Monitor/distill-sah.sqlite.db"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
from db import get_conn, init_db, upsert_listing, source_category

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 60    # seconds between poll cycles
DB_RETRY_WAIT  = 5     # seconds to wait if Distill DB is locked
LOOKBACK_HOURS = 24    # hours to look back on first run (no state file)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [poller] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Source routing ────────────────────────────────────────────────────────────

_SOURCE_MAP: List[Tuple[str, str, str, bool]] = [
    # DISABLED — classic.com: dedup key collision with BaT, API access pending (insight@classic.com).
    # ("classic.com",           "classic.com",        "AUCTION", False),
    ("cars.com",              "cars.com",            "RETAIL",  True),   # handled by scraper_carscom.py
    ("autotrader.com",        "AutoTrader",          "RETAIL",  True),   # handled by scraper_autotrader.py
    ("rennlist.com",          "Rennlist",            "RETAIL",  True),   # handled by scraper_rennlist.py
    ("builtforbackroads.com", "Built for Backroads", "RETAIL",  False),
    ("mart.pca.org",          "PCA Mart",            "RETAIL",  True),
    ("pca.org",               "PCA Mart",            "RETAIL",  True),
    # DISABLED — eBay: owned by scraper.py (eBay Browse API)
    # ("ebay.com",              "eBay",                "RETAIL",  True),
    ("ebay.com",              "eBay Motors",         "RETAIL",  True),   # handled by scraper_ebay.py
]


def _resolve_source(uri: str) -> Tuple[str, str, bool]:
    """Return (dealer_name, category, skip) for a sieve URI."""
    uri_lower = (uri or "").lower()
    for fragment, dealer, category, skip in _SOURCE_MAP:
        if fragment in uri_lower:
            return dealer, category, skip
    try:
        from urllib.parse import urlparse
        host = urlparse(uri).netloc.replace("www.", "")
        return host or "distill-unknown", "RETAIL", False
    except Exception:
        return "distill-unknown", "RETAIL", False


# ── Parsing helpers ───────────────────────────────────────────────────────────

_YEAR_RE  = re.compile(r"\b(19[6-9]\d|20[0-2]\d)\b")
_PRICE_RE = re.compile(r"\$(\d[\d,]+)|\b(\d{1,3}(?:,\d{3})+)\b")

_MODEL_TOKENS = [
    "911", "GT3", "GT2", "GT4", "Turbo S", "Turbo", "Carrera", "Targa",
    "Cayman", "Boxster", "Speedster", "Spyder", "Sport Classic",
    "930", "964", "993", "996", "997", "991", "992", "718",
]

# Pre-compiled keyword pattern for finding title lines
_PORSCHE_KW_RE = re.compile(
    r"\bPorsche\b|" + "|".join(rf"\b{re.escape(t)}\b" for t in _MODEL_TOKENS),
    re.I,
)


def _clean_price(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if 5_000 < v < 5_000_000 else None
    s = re.sub(r"[^\d]", "", str(raw))
    if not s:
        return None
    v = int(s)
    return v if 5_000 < v < 5_000_000 else None


def _parse_title(title: str) -> Dict:
    """Extract year/model/trim from a listing title line."""
    result: Dict = {"year": None, "make": "Porsche", "model": None, "trim": None}
    if not title:
        return result

    m = _YEAR_RE.search(title)
    if m:
        result["year"] = int(m.group(1))

    clean = re.sub(r"(?i)^(used|new|cpo|certified|pre-owned)\s+", "", title).strip()

    for tok in sorted(_MODEL_TOKENS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(tok)}\b", clean, re.I):
            # Map token to normalized model name
            if tok.isdigit() or len(tok) <= 4:
                model = tok                   # "911", "GT3", "718", "993", etc.
            elif tok.lower() in ("cayman", "boxster"):
                model = tok.capitalize()      # preserve Cayman / Boxster distinctly
            else:
                model = "911"                 # Carrera/Targa/Turbo/Speedster = 911 variants

            result["model"] = model

            after = re.split(rf"\b{re.escape(tok)}\b", clean, maxsplit=1, flags=re.I)[-1]
            trim = re.sub(r"^\s*[-–—,]\s*", "", after).strip()
            # Drop everything from em-dash, pipe, bare price, or "mileage" onwards
            trim = re.split(r"[—–|\$]|\d{1,3}(,\d{3})+\s*mi", trim)[0].strip()
            trim = re.sub(r"\s+\d{4,}(?:\s*mi(?:les?)?)?\s*$", "", trim, flags=re.I).strip()
            trim = re.sub(r"\s+\d{4,}(?:\s*mi(?:les?)?)?\s*$", "", trim, flags=re.I).strip()
            # Drop description bleed like "SShowing..." (BfB concatenates title+desc)
            trim = re.sub(r"([A-Z])\b[a-z]{3,}.*$", r"\1", trim).strip()
            if trim:
                result["trim"] = trim
            break

    return result


def _extract_price(text: str) -> Optional[int]:
    """Find first plausible listing price in free text (≥ $10k)."""
    if not text:
        return None
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        c = _clean_price(raw)
        if c and c >= 10_000:
            return c
    return None


def _extract_autotrader_price(block: str) -> Optional[int]:
    """
    AutoTrader cards concatenate price and label with no space: '107,350See payment'.
    The standard _PRICE_RE fails because there is no word boundary between the
    digits and the letter 'S'.  Try the AutoTrader-specific pattern first, then
    fall back to the generic extractor.
    """
    m = re.search(r"(\d[\d,]+)See\s+payment", block, re.I)
    if m:
        return _clean_price(m.group(1))
    return _extract_price(block)


def _extract_listing_url(block: str, sieve_uri: str) -> Optional[str]:
    """
    Return the canonical per-listing URL for an AutoTrader block, or None.

    AutoTrader text blocks produced by Distill contain no href attributes —
    Distill strips markup before storing in sieve_data.text.  Per-listing URLs
    (e.g. /cars-for-sale/listing/…) are therefore not recoverable from the
    extracted text.  This function is a placeholder for when a Source-mode
    capture (raw HTML) becomes available; until then it always returns None.
    """
    return None


def _extract_mileage(text: str) -> Optional[int]:
    """
    Extract mileage from free text. Handles:
      "1,750 mi."  "8,200 miles"  "47k mi"  "39k miles"  "less than 20k miles"
    Returns None if no mileage found.
    """
    if not text:
        return None

    # "less than Xk miles" — use upper bound as approximation
    m = re.search(r"less\s+than\s+(\d+)\s*k\s*(?:mi(?:les?)?)?", text, re.I)
    if m:
        v = int(m.group(1)) * 1000
        return v if 0 <= v < 500_000 else None

    # "Xk mi" or "Xk miles" (no space before k)
    m = re.search(r"\b(\d+)\s*k\s*(?:mi(?:les?)?)?(?=[\s,.\n]|$)", text, re.I)
    if m:
        v = int(m.group(1)) * 1000
        return v if 0 <= v < 500_000 else None

    # "X,XXX mi" or "X,XXX miles" or "X,XXX mi."
    # Exclude "X mi away" (distance to dealer) by rejecting when followed by "away"
    m = re.search(r"([\d,]+)\s*mi(?:les?)?\.?(?!\s*(?:per|away)\b)", text, re.I)
    if m:
        s = re.sub(r"[^\d]", "", m.group(1))
        if s:
            v = int(s)
            return v if 0 <= v < 500_000 else None

    return None


# ── Text splitting ────────────────────────────────────────────────────────────

def _split_blocks(text: str, uri: str) -> List[str]:
    """
    Split a sieve's text field into per-listing blocks using site-specific delimiters.
    Only returns blocks that contain at least one year (1960-2029).
    """
    uri_lower = (uri or "").lower()

    if "classic.com" in uri_lower:
        # Each listing is prefixed with "bookmark_border"
        parts = re.split(r"\bbookmark_border\b", text, flags=re.I)
        return [p.strip() for p in parts if p.strip() and _YEAR_RE.search(p)]

    if "cars.com" in uri_lower:
        # Each card starts with "Gallery" (photo carousel header)
        parts = re.split(r"\bGallery\b", text)
        return [p.strip() for p in parts if p.strip() and _YEAR_RE.search(p)]

    if "rennlist.com" in uri_lower:
        # HTML mode (new) — Distill dataAttr="html", captured text is raw HTML
        if text.strip().startswith("<"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            blocks = []
            for item in soup.select(".shelf-item"):
                link = item.select_one("a[href*='/forums/market/']")
                img  = item.select_one("img")
                text_content = item.get_text(separator="\n", strip=True)
                if not _YEAR_RE.search(text_content):
                    continue
                # Inject URL and image as parseable sentinel lines
                extra = ""
                if link and link.get("href"):
                    href = link["href"]
                    if not href.startswith("http"):
                        href = "https://rennlist.com" + href
                    extra += f"\nLISTING_URL: {href}"
                if img and img.get("src") and "ibsrv.net" in img.get("src", ""):
                    extra += f"\nIMAGE_URL: {img['src']}"
                blocks.append(text_content + extra)
            return blocks
        # Text mode fallback (legacy — dataAttr was "text")
        parts = re.split(r"\n\s*For Sale(?:\s*\|\s*\S+)?\s*\n", "\n" + text)
        return [p.strip() for p in parts if p.strip() and _YEAR_RE.search(p)]

    if "builtforbackroads.com" in uri_lower:
        if text.strip().startswith("<"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            # Multiple <a> tags share the same listing href — group them
            from collections import defaultdict
            grouped = defaultdict(list)
            img_map = {}
            for a in soup.select("a[href*='/listing/']"):
                href = a.get("href","")
                if not href: continue
                grouped[href].append(a.get_text(separator=" ", strip=True))
                if href not in img_map:
                    img = a.select_one("img[src]")
                    if img: img_map[href] = img["src"]
            blocks = []
            for href, texts in grouped.items():
                combined = "\n".join(t for t in texts if t and t != "New listing")
                if not _YEAR_RE.search(combined): continue
                combined += f"\nLISTING_URL: {href}"
                if href in img_map:
                    combined += f"\nIMAGE_URL: {img_map[href]}"
                blocks.append(combined)
            return blocks
        # Text fallback
        parts = re.split(r"\bNew listing\b", text, flags=re.I)
        blocks = [p.strip() for p in parts if p.strip() and _YEAR_RE.search(p)]
        return blocks if blocks else ([text] if _YEAR_RE.search(text) else [])

    if "autotrader.com" in uri_lower:
        # Distill extracts plain text (not raw HTML) from AutoTrader.
        # Each listing card starts with "Newly Listed" or "Sponsored".
        # If innerHTML is also tracked, the diff text may contain raw HTML — drop those blocks.
        if "<div" in text and text.count("<") > 20:
            # Raw HTML captured — strip tags and try to recover plain text blocks
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text.replace("<ins>", "").replace("</ins>", ""), "html.parser")
            text = soup.get_text(separator="\n")
        parts = re.split(r"\b(?:Newly Listed|Sponsored)\b", text, flags=re.I)
        return [p.strip() for p in parts if p.strip() and _YEAR_RE.search(p)]

    if "ebay.com" in uri_lower:
        if not text.strip().startswith("<"):
            return []  # non-HTML eBay — skip
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        blocks = []
        for item in soup.select("div.su-card-container"):
            title_el = item.select_one("span.su-styled-text.primary.default")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not _YEAR_RE.search(title):
                continue
            link     = item.select_one("a[href*='/itm/']")
            img      = item.select_one("img[src*='ebayimg']")
            price_el = item.select_one("span[class*='price']")
            block = title
            if price_el:
                block += f"\n{price_el.get_text(strip=True)}"
            if link and link.get("href"):
                block += f"\nLISTING_URL: {link['href'].split('?')[0]}"
            if img and img.get("src"):
                block += f"\nIMAGE_URL: {img['src']}"
            blocks.append(block)
        return blocks

    # Unknown site — treat full text as one block
    return [text] if _YEAR_RE.search(text) else []


def _best_title_line(block: str) -> str:
    """
    Return the most informative title line from a block:
    prefers lines that contain both a year AND a Porsche model keyword.
    Falls back to the first line that has any year.
    """
    for line in block.split("\n"):
        line = line.strip()
        if _YEAR_RE.search(line) and _PORSCHE_KW_RE.search(line):
            return line
    for line in block.split("\n"):
        line = line.strip()
        if _YEAR_RE.search(line):
            return line
    return block[:200]


def _block_to_listing(block: str, sieve_uri: str) -> Optional[Dict]:
    """Convert one text block to a listing dict. Returns None if unrecognisable."""
    if not _YEAR_RE.search(block):
        return None

    title  = _best_title_line(block)
    parsed = _parse_title(title)

    if not parsed["year"]:
        m = _YEAR_RE.search(block)
        if m:
            parsed["year"] = int(m.group(1))

    uri_lower = (sieve_uri or "").lower()
    if "autotrader.com" in uri_lower:
        price = _extract_autotrader_price(block)
        url   = _extract_listing_url(block, sieve_uri)  # None — not in Distill text
        # AutoTrader puts trim on the line immediately after the year/make/model line.
        # _parse_title only sees the title line, so recover trim from the block here.
        if not parsed["trim"]:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                if _YEAR_RE.search(line) and _PORSCHE_KW_RE.search(line) and i + 1 < len(lines):
                    candidate = lines[i + 1]
                    # Skip lines that look like mileage, price, badges, distance, or HTML
                    if not re.search(
                        r"\d+[Kk]?\s*mi|See payment|\d{3,}|\baway\b|"
                        r"No Accidents|Great Price|Make Offer|Request Info|"
                        r"Certified|Private Seller|Buy Online|Clean title",
                        candidate, re.I
                    ) and "<" not in candidate:
                        parsed["trim"] = candidate
                    break
    else:
        price = _extract_price(block)
        url   = sieve_uri   # search-page URL; best available for non-AutoTrader sources

    # Extract injected sentinel lines (Rennlist HTML mode)
    url_m = re.search(r"^LISTING_URL: (https://\S+)", block, re.MULTILINE)
    img_m = re.search(r"^IMAGE_URL: (https://\S+)",   block, re.MULTILINE)
    if url_m:
        url = url_m.group(1)

    mileage = _extract_mileage(block)

    if not parsed["year"] and not price:
        return None

    result = {
        "year":    parsed["year"],
        "make":    "Porsche",
        "model":   parsed["model"],
        "trim":    parsed["trim"],
        "price":   price,
        "mileage": mileage,
        "url":     url,
        "vin":     None,
    }
    if img_m:
        result["image_url"] = img_m.group(1)
    return result


def _extract_listings(text: str, sieve_uri: str) -> List[Dict]:
    """
    Full pipeline: split text → parse blocks → deduplicate.
    Deduplication key is (year, model, price) — classic.com in particular
    renders the same car twice (once with price, once with only auction dates).
    """
    blocks   = _split_blocks(text, sieve_uri)
    seen: set = set()
    result   = []

    for block in blocks:
        lst = _block_to_listing(block, sieve_uri)
        if not lst:
            continue
        key = (lst["year"], lst["model"], lst["price"])
        if key in seen:
            continue
        seen.add(key)
        result.append(lst)

    return result


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> Dict[str, int]:
    """Load {sieve_id: last_processed_ts_ms} from state file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning("Could not load state file: %s", e)
    return {}


def _save_state(state: Dict[str, int]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error("Could not save state file: %s", e)


def _default_cutoff_ms() -> int:
    """Timestamp (ms) for LOOKBACK_HOURS ago."""
    return int(time.time() * 1000) - LOOKBACK_HOURS * 3_600_000


# ── Distill DB query ──────────────────────────────────────────────────────────

def _fetch_triggered_rows(state: Dict[str, int]) -> List[sqlite3.Row]:
    """
    Query Distill's local DB for triggered sieve_data rows newer than what
    we've already processed.  Uses the minimum known ts as a coarse filter,
    then filters per-sieve in Python.
    """
    default_cutoff = _default_cutoff_ms()
    # Coarse cutoff: oldest last-seen ts across all sieves (or 24h ago if no state)
    coarse_cutoff = min(list(state.values()) + [default_cutoff])

    dconn = sqlite3.connect(str(DISTILL_DB), timeout=5)
    dconn.row_factory = sqlite3.Row
    try:
        rows = dconn.execute("""
            SELECT sd.id,
                   sd.sieve_id,
                   sd.text,
                   sd.ts,
                   sd.text_hash,
                   s.name AS sieve_name,
                   s.uri  AS sieve_uri
            FROM   sieve_data sd
            JOIN   sieves     s ON s.id = sd.sieve_id
            WHERE  sd.triggered = 1
              AND  sd.ts > ?
              AND  sd.text IS NOT NULL
              AND  sd.text != ''
            ORDER BY sd.ts ASC
        """, (coarse_cutoff,)).fetchall()
    finally:
        dconn.close()

    return rows


# ── Poll cycle ────────────────────────────────────────────────────────────────

def poll_once(state: Dict[str, int]) -> Dict[str, int]:
    """
    Run one poll cycle against Distill's DB, upsert any new listings into
    inventory.db, and return the updated state dict.
    """
    try:
        rows = _fetch_triggered_rows(state)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "unable to open" in msg:
            log.warning("Distill DB unavailable (%s) — retrying in %ds", e, DB_RETRY_WAIT)
            time.sleep(DB_RETRY_WAIT)
        else:
            log.error("Distill DB error: %s", e)
        return state
    except Exception as e:
        log.error("Unexpected error reading Distill DB: %s", e)
        return state

    if not rows:
        log.debug("No new triggered rows")
        return state

    today            = date.today().isoformat()
    total_inserted   = 0
    total_updated    = 0
    total_skipped    = 0

    with get_conn() as inv_conn:
        for row in rows:
            sieve_id   = row["sieve_id"]
            ts         = row["ts"]
            text       = row["text"]
            sieve_uri  = row["sieve_uri"]
            sieve_name = row["sieve_name"]

            # Per-sieve ts guard (coarse query may over-fetch)
            if ts <= state.get(sieve_id, 0):
                continue

            dealer, category, skip = _resolve_source(sieve_uri)

            if skip:
                log.debug("SKIP '%s' — active scraper owns '%s'", sieve_name, dealer)
                state[sieve_id] = max(state.get(sieve_id, 0), ts)
                continue

            log.info("Processing '%s'  dealer=%s  ts=%d", sieve_name, dealer, ts)

            listings = _extract_listings(text, sieve_uri)
            if not listings:
                log.warning("  No listings extracted from '%s' — check text format", sieve_name)
                log.debug("  text preview: %s", text[:400].replace("\n", " | "))
                state[sieve_id] = max(state.get(sieve_id, 0), ts)
                continue

            # For sources where Distill returns a full-page snapshot (Rennlist),
            # expire any active listings not present in this trigger.
            # Key: (year, model, price) — same dedup key used at insert time.
            FULL_SNAPSHOT_DEALERS = {"Rennlist", "eBay Motors"}
            preserved_created_at = {}  # (year,model,price) -> created_at for continuity
            if dealer in FULL_SNAPSHOT_DEALERS:
                current_keys = {
                    (lst["year"], lst["model"] or "911", lst["price"])
                    for lst in listings
                }
                expired = inv_conn.execute(
                    """SELECT id, year, model, price, created_at FROM listings
                       WHERE dealer=? AND status='active'""",
                    (dealer,)
                ).fetchall()
                # Preserve created_at so re-upserted listings keep their original age
                for r in expired:
                    key = (r["year"], r["model"], r["price"])
                    if key not in preserved_created_at and r["created_at"]:
                        preserved_created_at[key] = r["created_at"]
                expire_ids = [
                    r["id"] for r in expired
                    if (r["year"], r["model"], r["price"]) not in current_keys
                ]
                if expire_ids:
                    inv_conn.execute(
                        f"""UPDATE listings SET status='sold',
                            archived_at=datetime('now'),
                            archive_reason='not_in_distill_snapshot'
                            WHERE id IN ({','.join('?'*len(expire_ids))})""",
                        expire_ids
                    )
                    log.info("  EXPIRED %d stale %s listings not in snapshot", len(expire_ids), dealer)

            for lst in listings:
                year      = lst["year"]
                model     = lst["model"] or "911"
                trim      = lst["trim"]  or ""
                price     = lst["price"]
                mileage   = lst["mileage"]
                url       = lst["url"]
                vin       = lst["vin"]
                image_url = lst.get("image_url")

                if not year and not price and not url:
                    total_skipped += 1
                    continue

                # Drop obvious garbage listings (parts, salvage, misparse)
                # Real Porsches in our price range start well above $5k.
                # Exempts AUCTION sources — BaT/pcarmarket show current bid, not buy-now.
                if price and price < 5_000 and source_category(dealer) != "AUCTION":
                    log.debug("  SKIP price floor [%s] %s %s $%s", dealer, year, model, price)
                    total_skipped += 1
                    continue

                try:
                    # For full-snapshot dealers, pass original created_at so
                    # reply-bump re-upserts don't reset the listing's age
                    orig_created = preserved_created_at.get((year, model, price))
                    listing_id, is_new, price_changed = upsert_listing(
                        inv_conn,
                        dealer=dealer,
                        year=year,
                        make="Porsche",
                        model=model,
                        trim=trim,
                        mileage=mileage,
                        price=price,
                        vin=vin,
                        url=url,
                        today=today,
                        image_url=image_url,
                        date_first_seen=orig_created[:10] if orig_created else None,
                    )
                    if is_new:
                        total_inserted += 1
                        log.info(
                            "  NEW  [%s] %s %s %s  $%s  %s mi  id=%s",
                            dealer,
                            year or "?", model, trim,
                            f"{price:,}" if price else "?",
                            f"{mileage:,}" if mileage else "?",
                            listing_id,
                        )
                    elif price_changed:
                        total_updated += 1
                        log.info(
                            "  PRICE  [%s] id=%s  %s %s → $%s",
                            dealer, listing_id, year, model,
                            f"{price:,}" if price else "?",
                        )

                except Exception as exc:
                    log.exception("  Error upserting listing (dealer=%s year=%s): %s",
                                  dealer, year, exc)
                    total_skipped += 1

            state[sieve_id] = max(state.get(sieve_id, 0), ts)

    if total_inserted or total_updated:
        log.info("Cycle done — inserted=%d  updated=%d  skipped=%d",
                 total_inserted, total_updated, total_skipped)
    else:
        log.debug("Cycle done — no changes (skipped=%d)", total_skipped)

    return state


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Distill poller starting")
    log.info("Distill DB : %s", DISTILL_DB)
    log.info("Inventory  : %s", BASE_DIR / 'data/inventory.db')
    log.info("Poll interval: %ds  Lookback: %dh", POLL_INTERVAL, LOOKBACK_HOURS)

    if not DISTILL_DB.exists():
        log.error("Distill DB not found at %s — is Distill running?", DISTILL_DB)
        sys.exit(1)

    init_db()

    state = _load_state()
    if not state:
        log.info("No state file — will process last %dh of triggered rows", LOOKBACK_HOURS)
    else:
        log.info("Loaded state for %d sieve(s)", len(state))

    while True:
        try:
            state = poll_once(state)
            _save_state(state)
        except Exception as exc:
            log.exception("Unexpected error in poll cycle: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

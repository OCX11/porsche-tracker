#!/usr/bin/env python3
"""
notify_imessage.py — iMessage deal alerts for Porsche inventory tracker.

Drop-in replacement for notify_gunther.py that sends via iMessage instead of
Telegram. No nanobot, no Telegram bot token, no third-party service — just
the Mac Mini's Messages.app sending directly to your iPhone.

Delivery mechanism: AppleScript → Messages.app → iMessage to RECIPIENT_NUMBER.

Alert thresholds (from WATCHLIST.md):
  TIER1 (GT/Collector): alert on DEAL (10%+ below FMV) or WATCH (5-10% below)
  TIER2 (Standard):     alert only on DEAL (10%+ below FMV)

Dedup: data/seen_alerts_imessage.json — tracks evaluated URLs so we don't
re-alert unless price drops or flag improves since last evaluation.

Run manually:    python3 notify_imessage.py
Run via launchd: add to existing scrape cycle in main.py or as a separate plist

Schedule: called at end of each main.py scrape run (every 12 min daytime).
"""
import json
import logging
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_DIR    = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
SEEN_FILE  = SCRIPT_DIR / "data" / "seen_alerts_imessage.json"
CONFIG_FILE = SCRIPT_DIR / "data" / "imessage_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "imessage_alerts.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Toggle ─────────────────────────────────────────────────────────────────────
# Flip to True once you've confirmed the test message came through
NOTIFICATIONS_ENABLED = True

sys.path.insert(0, str(SCRIPT_DIR))
import db as database


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load recipient phone number from data/imessage_config.json.

    File format:
        { "recipient": "+15551234567" }

    Create it once:
        echo '{"recipient": "+1XXXXXXXXXX"}' > ~/porsche-tracker/data/imessage_config.json
    """
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception as e:
            log.error("Could not read imessage_config.json: %s", e)
    return {}


# ── Dedup store ────────────────────────────────────────────────────────────────

def _load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_seen(seen: dict):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def _listing_key(s: dict) -> str:
    url = s.get("listing_url") or ""
    if url:
        return url
    return f"{s.get('dealer')}|{s.get('year')}|{s.get('model')}|{s.get('trim')}|{s.get('mileage')}"


# ── iMessage delivery ──────────────────────────────────────────────────────────

def _send_imessage(recipient: str, text: str) -> bool:
    """Send an iMessage via AppleScript → Messages.app.

    Works as long as:
    - The Mac Mini is logged in with an Apple ID
    - Messages.app has iMessage enabled
    - The recipient is reachable via iMessage (iPhone / Apple ID)
    """
    # Escape backslashes and double-quotes for AppleScript string literal
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Messages"
    activate
    delay 0.5
    set targetService to first service whose service type is iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{safe_text}" to targetBuddy
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.error("AppleScript error: %s", result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("iMessage send timed out")
        return False
    except Exception as e:
        log.error("iMessage send failed: %s", e)
        return False


def _send_imessage_image(recipient: str, image_url: str) -> bool:
    """Download image_url to a temp file and send it via iMessage.

    AppleScript can send files by POSIX path — we download to /tmp,
    send, then clean up. Falls back silently if image unavailable.
    """
    if not image_url:
        return False
    # Skip relative/local paths (e.g. PCA Mart cached images served locally)
    if not image_url.startswith("http"):
        log.debug("Skipping non-http image_url: %s", image_url[:60])
        return False
    try:
        suffix = Path(image_url.split("?")[0]).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            Path(tmp_path).write_bytes(resp.read())
    except Exception as e:
        log.debug("Image download failed (%s): %s", image_url[:60], e)
        return False

    script = f'''
tell application "Messages"
    set targetService to first service whose service type is iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send POSIX file "{tmp_path}" to targetBuddy
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.debug("AppleScript image send error: %s", result.stderr.strip())
            return False
        return True
    except Exception as e:
        log.debug("Image send failed: %s", e)
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


# ── Alert formatting ───────────────────────────────────────────────────────────

def _clean_url(url: str) -> str:
    """Strip tracking params from eBay URLs — keep just the item URL."""
    if not url:
        return url
    if "ebay.com/itm/" in url:
        import re
        m = re.search(r"(https://www\.ebay\.com/itm/\d+)", url)
        if m:
            return m.group(1)
    return url


def _short_trim(trim: str, maxlen: int = 50) -> str:
    """Cap trim at maxlen chars, breaking on word boundary."""
    if not trim or len(trim) <= maxlen:
        return trim
    return trim[:maxlen].rsplit(" ", 1)[0].strip()



# Short source labels for iMessage (keep messages compact)
_SOURCE_LABELS = {
    "bring a trailer":    "BaT",
    "cars and bids":      "C&B",
    "pcarmarket":         "pcarmarket",
    "pca mart":           "PCA Mart",
    "ebay motors":        "eBay",
    "autotrader":         "AutoTrader",
    "cars.com":           "Cars.com",
    "rennlist":           "Rennlist",
    "built for backroads":"BfB",
    "dupont registry":    "DuPont",
}
_AUCTION_SOURCES = frozenset({"bring a trailer", "cars and bids", "pcarmarket"})


def _format_new_listing(s):
    """Format a new-listing alert — consistent across all 10 sources."""
    year      = s.get("year", "?")
    model     = s.get("model", "") or ""
    trim      = s.get("trim") or ""
    price     = s.get("price")
    mileage   = s.get("mileage")
    dealer    = s.get("dealer", "?")
    url       = s.get("listing_url", "")
    tier      = s.get("tier", "TIER2")

    # Remove model name prefix from trim if it duplicates
    # e.g. model="Cayman", trim="Cayman GT4" → trim_disp="GT4"
    import re as _re
    trim_clean = trim
    if trim and model:
        trim_clean = _re.sub(r"^" + _re.escape(model) + r"\s+", "", trim, flags=_re.I).strip()
    trim_disp = _short_trim(trim_clean)

    # Title: "2022 Porsche 911 GT3" — never double model name
    title_parts = [str(year), "Porsche", model]
    if trim_disp and trim_disp.lower() != model.lower():
        title_parts.append(trim_disp)
    title = " ".join(p for p in title_parts if p)

    tier_label = "GT/Collector 🔥" if tier == "TIER1" else "Standard"
    price_str  = f"${price:,}" if price else "No Price Listed"
    miles_str  = f"{mileage:,} mi" if mileage else "mileage TBD"
    url_clean  = _clean_url(url)

    # Source label — short and clean
    src_key   = dealer.lower().strip()
    src_label = _SOURCE_LABELS.get(src_key, dealer)
    is_auction = src_key in _AUCTION_SOURCES
    src_type  = "AUCTION" if is_auction else "RETAIL"

    lines = [
        f"🆕 {title}",
        f"💰 {price_str}",
        f"🛣️  {miles_str}",
        f"📍 {src_label} · {src_type} · {tier_label}",
        f"🔗 {url_clean}",
    ]
    return "\n".join(lines)


def notify_new_listings(conn, new_listing_ids):
    """Send one iMessage per new listing. No FMV scoring required.

    Called from main.py immediately after run_snapshot(), before deal-scoring
    alerts. Uses "new:{listing_url}" keys in seen_alerts_imessage.json to
    prevent re-alerting across scrape cycles.
    """
    if not NOTIFICATIONS_ENABLED:
        log.info("Notifications disabled — skipping new-listing alerts.")
        return

    if not new_listing_ids:
        return

    cfg = _load_config()
    recipient = cfg.get("recipient", "")
    if not recipient:
        log.error("No recipient configured. Create data/imessage_config.json "
                  "with {\"recipient\": \"+1XXXXXXXXXX\"}")
        return

    placeholders = ",".join("?" * len(new_listing_ids))
    rows = conn.execute(
        f"""SELECT id, year, make, model, trim, price, mileage, dealer,
                   listing_url, source_category, tier, image_url, image_url_cdn
            FROM listings WHERE id IN ({placeholders})""",
        new_listing_ids
    ).fetchall()

    seen = _load_seen()
    sent = 0

    for row in rows:
        s = dict(row)
        url      = s.get("listing_url") or ""
        seen_key = f"new:{url}" if url else f"new:id:{s.get('id')}"

        if seen_key in seen:
            log.debug("Skip new-listing alert (already sent): %s", seen_key[:80])
            continue

        log.info("NEW LISTING: %s %s %s  ask=%s",
                 s.get("year"), s.get("model"), s.get("trim") or "",
                 f"${s['price']:,}" if s.get("price") else "no price")

        msg = _format_new_listing(s)
        ok  = _send_imessage(recipient, msg)

        seen[seen_key] = {
            "alerted_at": datetime.now().isoformat(),
            "alerted":    ok,
        }
        _save_seen(seen)  # save after each so a crash mid-run doesn't re-send

        if ok:
            sent += 1
            log.info("  → iMessage sent to %s", recipient)
            import time as _time
            _time.sleep(1.0)  # let Messages settle before sending image
            # Prefer CDN URL for PCA Mart (image_url is local /static/img_cache/ path)
            img_url = s.get("image_url") or ""
            if img_url.startswith("/static/"):
                img_url = s.get("image_url_cdn") or ""
            if img_url and img_url.startswith("http"):
                img_ok = _send_imessage_image(recipient, img_url)
                if img_ok:
                    log.info("  → image sent")
                else:
                    log.debug("  → image skipped (download failed)")
        else:
            log.error("  → iMessage delivery failed")

    log.info("New-listing alerts: %d sent of %d new IDs", sent, len(new_listing_ids))


def notify_auction_ending(conn):
    """Send iMessage alerts for auctions ending soon.

    TIER1: alert when auction_ends_at is within 3 hours.
    TIER2: alert only when auction_ends_at is within 1 hour (last-minute only).
    Dedup key: 'ending:{listing_id}' — fires ONCE per listing.
    """
    if not NOTIFICATIONS_ENABLED:
        return

    cfg = _load_config()
    recipient = cfg.get("recipient", "")
    if not recipient:
        return

    now = datetime.utcnow()
    window_3h = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_1h = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute("""
        SELECT id, year, make, model, trim, price, mileage, dealer,
               listing_url, source_category, tier, image_url, image_url_cdn,
               auction_ends_at
        FROM listings
        WHERE status = 'active'
          AND source_category = 'AUCTION'
          AND auction_ends_at IS NOT NULL
          AND auction_ends_at > ?
          AND auction_ends_at <= ?
    """, (now_str, window_3h)).fetchall()

    seen  = _load_seen()
    sent  = 0

    for row in rows:
        s    = dict(row)
        tier = s.get("tier", "TIER2")
        lid  = s.get("id")
        ends = s.get("auction_ends_at", "")

        # TIER2: only alert in last hour
        if tier != "TIER1" and ends > window_1h:
            continue

        seen_key = f"ending:{lid}"
        if seen_key in seen:
            continue

        # Compute time remaining
        try:
            ends_dt = datetime.strptime(ends, "%Y-%m-%dT%H:%M:%SZ")
            delta   = ends_dt - now
            total_s = max(0, int(delta.total_seconds()))
            rem_h   = total_s // 3600
            rem_m   = (total_s % 3600) // 60
        except Exception:
            rem_h = rem_m = 0

        tier_label = "GT/Collector" if tier == "TIER1" else "Standard"
        price      = s.get("price")
        price_str  = f"${price:,}" if price else "No Price"
        trim_disp  = _short_trim(s.get("trim") or "")
        url_clean  = _clean_url(s.get("listing_url") or "")

        lines = [
            (f"⏰ ENDING SOON: {s.get('year','?')} Porsche {s.get('model','')} {trim_disp}").rstrip(),
            f"💰 Current Bid: {price_str}",
            f"⏱  Ends in {rem_h}h {rem_m}m",
            f"📍 {s.get('dealer','?')}  [{tier_label}]",
            f"🔗 {url_clean}",
        ]
        msg = "\n".join(lines)

        log.info("ENDING SOON: %s %s %s — %dh %dm remaining",
                 s.get("year"), s.get("model"), s.get("trim") or "", rem_h, rem_m)

        seen[seen_key] = {"alerted_at": datetime.now().isoformat(), "alerted": False}
        ok = _send_imessage(recipient, msg)
        if ok:
            seen[seen_key]["alerted"] = True
            sent += 1
            log.info("  → ending-soon iMessage sent to %s", recipient)
            img_url = s.get("image_url") or ""
            if img_url.startswith("/static/"):
                img_url = s.get("image_url_cdn") or ""
            if img_url and img_url.startswith("http"):
                _send_imessage_image(recipient, img_url)
        else:
            log.error("  → ending-soon iMessage delivery failed")
        _save_seen(seen)

    log.info("Ending-soon alerts: %d sent", sent)


# ── Main ───────────────────────────────────────────────────────────────────────

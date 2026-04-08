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
from datetime import datetime
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
import fmv as fmv_engine


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

def _format_alert(s: dict) -> str:
    """Format a deal alert as a clean iMessage-friendly string.
    iMessage doesn't support Markdown — plain text with emoji."""
    ds       = s["deal_score"]
    year     = s.get("year", "?")
    model    = s.get("model", "")
    trim     = s.get("trim") or ""
    price    = s.get("price")
    mileage  = s.get("mileage")
    dealer   = s.get("dealer", "?")
    url      = s.get("listing_url", "")
    tier     = s.get("tier", "TIER2")
    flag     = ds["deal_flag"]
    pct      = ds["pct_vs_fmv"]
    fmv      = ds["fmv"]
    conf     = ds["confidence"]
    comp_cnt = ds["comp_count"]
    src_cat  = s.get("source_category", "")

    flag_emoji = "🔥" if flag == "DEAL" else "👀"
    tier_label = "GT/Collector" if tier == "TIER1" else "Standard"
    src_label  = f" [{src_cat}]" if src_cat else ""

    price_str = f"${price:,}" if price else "No Price"
    miles_str = f"{mileage:,} mi" if mileage else "mileage unknown"
    pct_str   = f"{pct:+.0%} vs FMV (${fmv:,})"

    conf_note = ""
    if conf == "LOW":
        conf_note = f"\n⚠️ Limited comp data ({comp_cnt} comp{'s' if comp_cnt != 1 else ''}) — verify manually"
    elif conf == "MEDIUM":
        conf_note = f"\n({comp_cnt} comps)"

    lines = [
        f"{flag_emoji} {flag}: {year} Porsche {model} {trim}",
        f"💰 {price_str}  {pct_str}",
        f"🛣️  {miles_str}",
        f"📍 {dealer}{src_label}  [{tier_label}]",
        f"🔗 {url}",
    ]
    if conf_note:
        lines.append(conf_note)

    return "\n".join(lines)


# ── New-listing alerts ────────────────────────────────────────────────────────

def _format_new_listing(s):
    """Format a new-listing alert — simpler than deal alert, no FMV."""
    year      = s.get("year", "?")
    model     = s.get("model", "")
    trim      = s.get("trim") or ""
    price     = s.get("price")
    mileage   = s.get("mileage")
    dealer    = s.get("dealer", "?")
    url       = s.get("listing_url", "")
    tier      = s.get("tier", "TIER2")
    src_cat   = s.get("source_category", "")

    tier_label = "GT/Collector" if tier == "TIER1" else "Standard"
    src_label  = f" [{src_cat}]" if src_cat else ""
    price_str  = f"${price:,}" if price else "No Price"
    miles_str  = f"{mileage:,} mi" if mileage else "mileage unknown"

    lines = [
        (f"🆕 NEW: {year} Porsche {model} {trim}").rstrip(),
        f"💰 {price_str}",
        f"🛣️  {miles_str}",
        f"📍 {dealer}{src_label}  [{tier_label}]",
        f"🔗 {url}",
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
                   listing_url, source_category, tier, image_url
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

        log.info("NEW LISTING: %s %s %s  ask=$%s",
                 s.get("year"), s.get("model"), s.get("trim") or "",
                 f"{s['price']:,}" if s.get("price") else "?")

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
        else:
            log.error("  → iMessage delivery failed")

    log.info("New-listing alerts: %d sent of %d new IDs", sent, len(new_listing_ids))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not NOTIFICATIONS_ENABLED:
        log.info("Notifications disabled (NOTIFICATIONS_ENABLED=False) — skipping.")
        return

    cfg = _load_config()
    recipient = cfg.get("recipient", "")
    if not recipient:
        log.error("No recipient configured. Create data/imessage_config.json with {\"recipient\": \"+1XXXXXXXXXX\"}")
        return

    database.init_db()
    with database.get_conn() as conn:
        scored = fmv_engine.score_active_listings(conn)

    seen         = _load_seen()
    alerts_sent  = 0
    evaluated    = 0

    for s in scored:
        ds   = s.get("deal_score")
        tier = s.get("tier", "TIER2")
        if not ds:
            continue

        flag = ds["deal_flag"]
        conf = ds["confidence"]

        # Skip if no comp data
        if conf == "NONE":
            continue

        # Tier-aware alert thresholds (from WATCHLIST.md)
        # TIER1: alert on DEAL or WATCH (any GT/Collector pricing signal is notable)
        # TIER2: alert only on DEAL (10%+ below FMV — cuts noise on standard cars)
        if tier == "TIER1":
            should_alert = flag in ("DEAL", "WATCH")
        else:
            should_alert = flag == "DEAL"

        if not should_alert:
            continue

        key        = _listing_key(s)
        price      = s.get("price")
        last_entry = seen.get(key, {})
        last_price = last_entry.get("last_price")
        last_flag  = last_entry.get("last_flag")

        # Alert if: new listing, price dropped, or flag upgraded WATCH→DEAL
        price_dropped = last_price is not None and price and price < last_price
        flag_improved = last_flag == "WATCH" and flag == "DEAL"
        is_new        = key not in seen

        if not (is_new or price_dropped or flag_improved):
            log.debug("Skip (already alerted, no change): %s %s %s",
                      s.get("year"), s.get("model"), s.get("trim") or "")
            continue

        evaluated += 1
        log.info("ALERT: %s %s %s  ask=$%s  %+.0f%% vs FMV  [%s]  conf=%s",
                 s.get("year"), s.get("model"), s.get("trim") or "",
                 f"{price:,}" if price else "?",
                 ds["pct_vs_fmv"] * 100, flag, conf)

        seen[key] = {
            "evaluated_at": datetime.now().isoformat(),
            "last_price":   price,
            "last_flag":    flag,
            "alerted":      False,
        }

        msg = _format_alert(s)
        ok  = _send_imessage(recipient, msg)
        if ok:
            seen[key]["alerted"] = True
            alerts_sent += 1
            log.info("  → iMessage sent to %s", recipient)
        else:
            log.error("  → iMessage delivery failed")

        _save_seen(seen)  # save after each so a crash mid-run doesn't re-evaluate

    log.info("Done — %d alert(s) sent, %d evaluated, %d total scored listings",
             alerts_sent, evaluated, len(scored))


if __name__ == "__main__":
    main()

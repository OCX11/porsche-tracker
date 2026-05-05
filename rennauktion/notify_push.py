#!/usr/bin/env python3
"""
rennauktion/notify_push.py — Push alerts for RennAuktion (auction listings).

Handles: auction-ending alerts.
"""
import json
import logging
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# Project root is one level above this file
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR  = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

SEEN_FILE = DATA_DIR / "seen_alerts_push.json"

PUSH_SERVER_URL = "http://127.0.0.1:5055"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "push_alerts.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

NOTIFICATIONS_ENABLED = True


# ── Dedup store ────────────────────────────────────────────────────────────────

def _load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
        except Exception:
            return {}
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        pruned = {k: v for k, v in data.items()
                  if v.get("alerted_at", "") >= cutoff}
        if len(pruned) < len(data):
            SEEN_FILE.parent.mkdir(exist_ok=True)
            SEEN_FILE.write_text(json.dumps(pruned, indent=2))
        return pruned
    return {}


def _save_seen(seen: dict):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


# ── Push delivery ──────────────────────────────────────────────────────────────

def _send_push(payload: dict) -> bool:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{PUSH_SERVER_URL}/send-push",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            sent = result.get("sent", 0)
            if sent > 0:
                log.info("Push delivered to %d subscriber(s)", sent)
                return True
            log.info("Push server: no subscribers yet")
            return False
    except urllib.error.URLError as e:
        log.error("Push server unreachable: %s — is push_server.py running?", e)
        return False
    except Exception as e:
        log.error("Push delivery failed: %s", e)
        return False


# ── Formatting helpers ─────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "bring a trailer":    "BaT",
    "cars and bids":      "C&B",
    "pcarmarket":         "pcarmarket",
}


def _clean_url(url: str) -> str:
    return url or ""


# ── Public API ─────────────────────────────────────────────────────────────────

def notify_auction_ending(conn):
    """Send push alerts for auctions ending soon.
    TIER1: within 3 hours. TIER2: within 1 hour.
    """
    if not NOTIFICATIONS_ENABLED:
        return

    now = datetime.utcnow()
    window_3h = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_1h = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute("""
        SELECT id, year, make, model, trim, price, mileage, dealer,
               listing_url, source_category, tier, auction_ends_at
        FROM listings
        WHERE status = 'active'
          AND source_category = 'AUCTION'
          AND auction_ends_at IS NOT NULL
          AND auction_ends_at > ?
          AND auction_ends_at <= ?
    """, (now_str, window_3h)).fetchall()

    seen = _load_seen()
    sent = 0

    for row in rows:
        s    = dict(row)
        tier = s.get("tier", "TIER2")
        lid  = s.get("id")
        ends = s.get("auction_ends_at", "")

        if tier != "TIER1" and ends > window_1h:
            continue

        seen_key = f"ending:{lid}"
        if seen_key in seen:
            continue

        try:
            ends_dt = datetime.strptime(ends, "%Y-%m-%dT%H:%M:%SZ")
            delta   = ends_dt - now
            total_s = max(0, int(delta.total_seconds()))
            rem_h   = total_s // 3600
            rem_m   = (total_s % 3600) // 60
        except Exception:
            rem_h = rem_m = 0

        model  = s.get("model", "")
        trim   = s.get("trim") or ""
        price  = s.get("price")
        dealer = s.get("dealer", "?")
        url    = _clean_url(s.get("listing_url") or "")

        src_key   = dealer.lower().strip()
        src_label = _SOURCE_LABELS.get(src_key, dealer)
        price_str = f"${price:,}" if price else "No Price"

        payload = {
            "title": f"⏰ ENDING: {s.get('year','?')} Porsche {model} {trim}".rstrip(),
            "body":  f"{price_str}  ·  {rem_h}h {rem_m}m left  ·  {src_label}",
            "url":   url,
        }

        ok = _send_push(payload)
        if ok:
            seen[seen_key] = {"alerted_at": datetime.now().isoformat(), "alerted": True}
            _save_seen(seen)
            sent += 1

    log.info("Ending-soon push alerts: %d sent", sent)

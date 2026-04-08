#!/usr/bin/env python3
"""
enrich_ebay_mileage.py — Backfill mileage and VIN on eBay Motors active listings.

The eBay Browse API item_summary/search endpoint does not return mileage or VIN.
The per-item detail endpoint does. This script:
  1. Finds all active eBay Motors listings with mileage IS NULL
  2. Extracts the item_id from the listing_url
  3. Calls GET /buy/browse/v1/item/{item_id} to get localizedAspects
  4. Patches mileage and vin directly in the DB (targeted UPDATE, not re-upsert)

Progress saved to data/ebay_enrich_progress.json — safe to Ctrl+C and resume.
Rate-limited to 0.3s between calls. On 429, sleeps 30s and retries once.

Run:  python3 enrich_ebay_mileage.py
"""
import base64
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR    = Path(__file__).parent
PROGRESS_FILE = SCRIPT_DIR / "data" / "ebay_enrich_progress.json"
LOG_DIR       = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "ebay_enrich.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(SCRIPT_DIR))
import db

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------
_CFG_FILE = SCRIPT_DIR / "data" / "ebay_api_config.json"

_OAUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
_ITEM_URL   = "https://api.ebay.com/buy/browse/v1/item/{}"
_SCOPE      = "https://api.ebay.com/oauth/api_scope"

# ---------------------------------------------------------------------------
# In-memory token cache (verbatim from scraper_ebay.py)
# ---------------------------------------------------------------------------
_token_cache = {"token": None, "expires_at": 0}

# ---------------------------------------------------------------------------
# Proxy config (verbatim from scraper_ebay.py — optional for official API)
# ---------------------------------------------------------------------------
_PROXY_CFG = {}
_PROXY_URL = ""


def _load_proxy():
    global _PROXY_CFG, _PROXY_URL
    p = SCRIPT_DIR
    for _ in range(6):
        cand = p / "data" / "proxy_config.json"
        try:
            with open(cand) as f:
                cfg = json.load(f)
            if cfg.get("enabled") and cfg.get("proxy_url"):
                _PROXY_CFG = cfg
                _PROXY_URL = cfg["proxy_url"]
                log.info("Proxy loaded: %s:%s", cfg.get("host"), cfg.get("port"))
                return
        except Exception:
            pass
        p = p.parent
    log.info("Proxy not configured — continuing without proxy (OK for official API)")


_load_proxy()


def _get_proxies():
    if _PROXY_URL:
        return {"http": _PROXY_URL, "https": _PROXY_URL}
    return None


# ---------------------------------------------------------------------------
# OAuth — verbatim from scraper_ebay.py
# ---------------------------------------------------------------------------
def _load_api_config():
    try:
        with open(_CFG_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load API config from %s: %s", _CFG_FILE, e)
        return {}


def _get_token(app_id, cert_id):
    """
    Fetch OAuth token using Client Credentials flow.
    Caches in _token_cache (in-memory only). Returns token string or None.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    credentials = base64.b64encode(
        "{}:{}".format(app_id, cert_id).encode()
    ).decode()

    headers = {
        "Authorization": "Basic {}".format(credentials),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = "grant_type=client_credentials&scope={}".format(_SCOPE)

    proxies = _get_proxies()
    try:
        r = requests.post(
            _OAUTH_URL,
            headers=headers,
            data=data,
            timeout=20,
            proxies=proxies,
        )
        if r.status_code != 200:
            log.error("OAuth failed: HTTP %d — %s", r.status_code, r.text[:500])
            return None
        body = r.json()
        token = body.get("access_token")
        expires_in = body.get("expires_in", 7200)
        if not token:
            log.error("OAuth: no access_token in response — keys: %s", list(body.keys()))
            return None
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in
        log.info("OAuth token obtained (expires in %ds)", expires_in)
        return token
    except Exception as e:
        log.error("OAuth request error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Item ID extraction
# ---------------------------------------------------------------------------
def _extract_item_id(url):
    """
    Extract eBay item_id from a listing URL.
    Handles:
      https://www.ebay.com/itm/123456789012
      https://www.ebay.com/itm/some-title-here/123456789012
      https://www.ebay.com/itm/123456789012?_skw=porsche&hash=...
    Returns item_id string or None.
    """
    if not url:
        return None
    m = re.search(r'/(\d{10,13})(?:[?/]|$)', url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Aspect extraction
# ---------------------------------------------------------------------------
def _extract_from_aspects(aspects):
    """
    Extract mileage and VIN from localizedAspects list.
    Each element: {"name": "Mileage", "value": "34,000"} or {"name": "VIN", "value": "WP0..."}
    Returns (mileage_int_or_None, vin_str_or_None).
    """
    mileage = None
    vin = None
    for aspect in (aspects or []):
        name = aspect.get("name", "").lower()
        val  = str(aspect.get("value", "")).strip()
        if name == "mileage" and mileage is None:
            digits = re.sub(r"[^\d]", "", val)
            mileage = int(digits) if digits else None
        if name == "vin" and vin is None:
            vin = val if val else None
    return mileage, vin


# ---------------------------------------------------------------------------
# eBay item detail fetch
# ---------------------------------------------------------------------------
def _fetch_item(item_id, token):
    """
    Call GET /buy/browse/v1/item/{item_id}.
    Returns (mileage, vin) tuple, or (None, None) on skip-worthy errors.
    Returns None on fatal/unrecoverable error (caller marks as failed).
    """
    url = _ITEM_URL.format(item_id)
    headers = {
        "Authorization": "Bearer {}".format(token),
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }
    proxies = _get_proxies()

    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=20, proxies=proxies)

            if r.status_code == 429:
                log.warning("429 rate-limited on item %s — sleeping 30s (attempt %d/2)",
                            item_id, attempt + 1)
                time.sleep(30)
                continue

            if r.status_code == 404:
                log.debug("404 — item %s no longer available", item_id)
                return None, None

            if r.status_code != 200:
                log.error("HTTP %d on item %s — %s", r.status_code, item_id, r.text[:300])
                return None, None

            body = r.json()
            aspects = body.get("localizedAspects")
            mileage, vin = _extract_from_aspects(aspects)
            return mileage, vin

        except Exception as e:
            if attempt == 0:
                log.warning("Request error on item %s (attempt 1/2): %s", item_id, e)
                time.sleep(5)
            else:
                log.error("Request failed after 2 attempts for item %s: %s", item_id, e)
                return None, None

    return None, None


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------
def _load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {"done_ids": [], "failed_ids": [], "updated": 0}


def _save_progress(prog):
    PROGRESS_FILE.parent.mkdir(exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    prog["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp.write_text(json.dumps(prog, indent=2))
    tmp.replace(PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    db.init_db()
    conn = db.get_conn()

    rows = conn.execute("""
        SELECT id, listing_url
        FROM listings
        WHERE dealer = 'eBay Motors'
          AND status = 'active'
          AND mileage IS NULL
          AND listing_url IS NOT NULL
        ORDER BY id
    """).fetchall()

    total_missing = len(rows)
    log.info("Found %d active eBay Motors listings with mileage NULL", total_missing)

    if not total_missing:
        log.info("Nothing to do — all active eBay listings have mileage.")
        conn.close()
        return

    cfg = _load_api_config()
    app_id  = cfg.get("app_id")
    cert_id = cfg.get("cert_id")
    if not app_id or not cert_id:
        log.error("Missing app_id or cert_id in %s — aborting", _CFG_FILE)
        conn.close()
        return

    token = _get_token(app_id, cert_id)
    if not token:
        log.error("Could not obtain OAuth token — aborting")
        conn.close()
        return

    prog       = _load_progress()
    done_ids   = set(prog.get("done_ids", []))
    failed_ids = set(prog.get("failed_ids", []))
    updated    = prog.get("updated", 0)

    pending = [r for r in rows if r["id"] not in done_ids and r["id"] not in failed_ids]
    log.info("%d pending  |  already done: %d  |  failed: %d",
             len(pending), len(done_ids), len(failed_ids))

    mileage_added = 0
    vin_added     = 0

    for i, row in enumerate(pending, 1):
        listing_id  = row["id"]
        listing_url = row["listing_url"]

        # Refresh token if near expiry
        token = _get_token(app_id, cert_id)
        if not token:
            log.error("Token refresh failed — stopping early")
            break

        item_id = _extract_item_id(listing_url)
        if not item_id:
            log.warning("[%d/%d] id=%d — could not extract item_id from URL: %s",
                        i, len(pending), listing_id, listing_url)
            failed_ids.add(listing_id)
            continue

        mileage, vin = _fetch_item(item_id, token)

        if mileage is not None or vin is not None:
            conn.execute(
                "UPDATE listings SET mileage=?, vin=COALESCE(vin,?) WHERE id=?",
                (mileage, vin, listing_id),
            )
            conn.commit()
            updated += 1
            if mileage is not None:
                mileage_added += 1
            if vin is not None:
                vin_added += 1
            log.info("[%d/%d] item=%-14s  mileage=%-8s  vin=%s",
                     i, len(pending), item_id,
                     "{:,}".format(mileage) if mileage is not None else "—",
                     vin or "—")
        else:
            log.info("[%d/%d] item=%-14s  no mileage/VIN in aspects", i, len(pending), item_id)

        done_ids.add(listing_id)

        # Save progress every 25 listings
        if i % 25 == 0:
            prog["done_ids"]   = list(done_ids)
            prog["failed_ids"] = list(failed_ids)
            prog["updated"]    = updated
            _save_progress(prog)
            log.info("--- checkpoint: %d mileages  %d VINs added so far ---",
                     mileage_added, vin_added)

        if i < len(pending):
            time.sleep(0.3)

    # Final save
    prog["done_ids"]     = list(done_ids)
    prog["failed_ids"]   = list(failed_ids)
    prog["updated"]      = updated
    prog["completed_at"] = datetime.now().isoformat(timespec="seconds")
    _save_progress(prog)

    conn.close()
    log.info(
        "Done — %d mileages added, %d VINs added, %d failed",
        mileage_added, vin_added, len(failed_ids),
    )


if __name__ == "__main__":
    run()

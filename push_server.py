#!/usr/bin/env python3
"""
push_server.py — PTOX11 Web Push notification server.

Runs as a Flask app on localhost:5055. Exposed to the internet via
Cloudflare Tunnel so the PWA on any device can subscribe.

Endpoints:
  GET  /vapid-public-key        → returns VAPID public key for frontend
  POST /subscribe               → save a push subscription
  POST /unsubscribe             → remove a push subscription
  GET  /subscribers             → list active subscriber count (internal)
  POST /send-push               → send a push to all subscribers (internal, localhost only)
  POST /user-comp               → save a personal FMV comp (url, fmv, year, model, trim)
  DELETE /user-comp             → remove a personal FMV comp by url
  GET  /user-comps              → return all personal FMV comps
  POST /waitlist                → save a waitlist email to Google Sheets

Push payloads are sent by notify_push.py (replaces notify_imessage.py).
This server just holds subscriptions and relays pushes to APNs/FCM via VAPID.
"""
import json
import os
import logging
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, abort

SCRIPT_DIR = Path(__file__).parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "push_server.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

VAPID_KEY_FILE = SCRIPT_DIR / "data" / "vapid_keys.json"
SUBS_FILE = SCRIPT_DIR / "data" / "push_subscriptions.json"

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


# ── VAPID keys ─────────────────────────────────────────────────────────────────

def _load_vapid():
    if not VAPID_KEY_FILE.exists():
        log.error("vapid_keys.json not found — run keygen first")
        sys.exit(1)
    return json.loads(VAPID_KEY_FILE.read_text())


VAPID = _load_vapid()
VAPID_PUBLIC_KEY = VAPID["public_key"]
VAPID_PRIVATE_KEY_PEM = VAPID["private_key_pem"]

# Load private key object once at startup for manual JWT signing
from cryptography.hazmat.primitives.serialization import load_pem_private_key as _load_pem
_PRIVKEY = _load_pem(VAPID_PRIVATE_KEY_PEM.encode(), password=None)


# ── Subscription store ─────────────────────────────────────────────────────────

def _load_subs() -> dict:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_subs(subs: dict):
    SUBS_FILE.parent.mkdir(exist_ok=True)
    SUBS_FILE.write_text(json.dumps(subs, indent=2))


def _sub_key(sub: dict) -> str:
    """Unique key for a subscription — use the endpoint URL."""
    return sub.get("endpoint", "")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/vapid-public-key")
def vapid_public_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/subscribe", methods=["POST"])
def subscribe():
    sub = request.get_json(silent=True)
    if not sub or not sub.get("endpoint"):
        abort(400, "Invalid subscription object")

    key = _sub_key(sub)
    subs = _load_subs()

    label = request.args.get("label", "")
    subs[key] = {
        "subscription": sub,
        "label": label,
        "subscribed_at": datetime.now().isoformat(),
    }
    _save_subs(subs)
    log.info("New subscription: %s  label=%s  total=%d", key[:50], label or "(none)", len(subs))
    return jsonify({"status": "subscribed", "total": len(subs)}), 201


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    body = request.get_json(silent=True) or {}
    endpoint = body.get("endpoint") or (body.get("subscription") or {}).get("endpoint")
    if not endpoint:
        abort(400, "Missing endpoint")

    subs = _load_subs()
    if endpoint in subs:
        del subs[endpoint]
        _save_subs(subs)
        log.info("Unsubscribed: %s  remaining=%d", endpoint[:50], len(subs))
        return jsonify({"status": "unsubscribed", "remaining": len(subs)})
    return jsonify({"status": "not_found"}), 404


@app.route("/subscribers")
def subscribers():
    """Internal status — total subscriber count."""
    subs = _load_subs()
    entries = [{"label": v.get("label", ""), "subscribed_at": v.get("subscribed_at", "")}
               for v in subs.values()]
    return jsonify({"count": len(subs), "subscribers": entries})


@app.route("/send-push", methods=["POST"])
def send_push():
    """Internal endpoint — called by notify_push.py on the same machine.
    Rejects requests from non-localhost to prevent abuse.
    """
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1", "localhost"):
        log.warning("Rejected /send-push from non-localhost: %s", remote)
        abort(403, "Internal endpoint")

    payload = request.get_json(silent=True)
    if not payload:
        abort(400, "Missing payload")

    title   = payload.get("title", "PTOX11")
    body    = payload.get("body", "")
    url     = payload.get("url", "")
    icon    = payload.get("icon", "/icons/icon-192.png")
    badge   = payload.get("badge", "/icons/icon-192.png")

    notification = {
        "title": title,
        "body": body,
        "url": url,
        "icon": icon,
        "badge": badge,
        "timestamp": int(datetime.now().timestamp() * 1000),
    }

    subs = _load_subs()
    if not subs:
        log.info("send-push: no subscribers")
        return jsonify({"sent": 0, "failed": 0, "removed": 0})

    results = _send_to_all(subs, notification)
    return jsonify(results)


# ── Push delivery ──────────────────────────────────────────────────────────────

def _make_vapid_jwt(endpoint: str) -> str:
    """Build a VAPID JWT for the given push endpoint.
    Apple Web Push requires compact JSON (no spaces) and aud = scheme://host.
    """
    import base64
    import time as _time
    from urllib.parse import urlparse
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"

    # Compact JSON — NO spaces (Apple rejects pretty-printed JWT)
    import json as _json
    header  = b64url(_json.dumps({"typ":"JWT","alg":"ES256"}, separators=(",",":")).encode())
    payload = b64url(_json.dumps({
        "aud": aud,
        "exp": int(_time.time()) + 86400,
        "sub": "https://www.rennmarkt.net/",
    }, separators=(",",":")).encode())

    signing_input = f"{header}.{payload}".encode()
    sig_der = _PRIVKEY.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig_der)
    sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{payload}.{b64url(sig_bytes)}"


def _send_to_all(subs: dict, notification: dict) -> dict:
    import requests
    from pywebpush import WebPusher

    sent = 0
    failed = 0
    expired = []

    for endpoint, entry in list(subs.items()):
        sub = entry["subscription"]
        try:
            # Encrypt payload
            pusher = WebPusher(sub)
            encoded = pusher.encode(json.dumps(notification), content_encoding="aes128gcm")
            body = encoded.get("body") or encoded.get("Body")

            # Build VAPID JWT with correct aud for this endpoint
            token = _make_vapid_jwt(endpoint)
            auth_header = f"vapid t={token},k={VAPID_PUBLIC_KEY}"

            resp = requests.post(
                endpoint,
                data=body,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/octet-stream",
                    "TTL": "86400",
                    "Content-Encoding": "aes128gcm",
                },
                timeout=15,
            )
            if resp.status_code in (200, 201, 202, 204):
                sent += 1
                log.debug("Push sent: %s", endpoint[:50])
            elif resp.status_code in (404, 410):
                log.info("Subscription expired (HTTP %s), removing: %s", resp.status_code, endpoint[:50])
                expired.append(endpoint)
            else:
                log.warning("Push failed (HTTP %s): %s — %s", resp.status_code, endpoint[:50], resp.text[:120])
                failed += 1
        except Exception as e:
            log.warning("Push error: %s — %s", endpoint[:50], str(e)[:120])
            failed += 1

    # Remove expired subscriptions
    if expired:
        for ep in expired:
            del subs[ep]
        _save_subs(subs)

    log.info("Push delivery: sent=%d  failed=%d  removed=%d  remaining=%d",
             sent, failed, len(expired), len(subs))
    return {"sent": sent, "failed": failed, "removed": len(expired)}


# ── FMV comp drill-down ───────────────────────────────────────────────────────

@app.route("/fmv-comps", methods=["GET"])
def fmv_comps():
    """Return the sold comps that drove the FMV estimate for a given car.
    Query params: year, model, trim
    """
    try:
        year  = int(request.args.get("year", 0))
        model = request.args.get("model", "").strip()
        trim  = request.args.get("trim", "").strip()
    except (ValueError, TypeError):
        abort(400, "year must be integer")

    if not year or not model:
        abort(400, "year and model are required")

    try:
        import sys
        sys.path.insert(0, str(SCRIPT_DIR))
        import db as _db
        import fmv as _fmv

        _db.init_db()
        with _db.get_conn() as conn:
            result = _fmv.get_fmv(conn, year=year, model=model, trim=trim or None)

        comp_list = []
        for c in (result.comps or []):
            comp_list.append({
                "year":        c.year,
                "model":       c.model,
                "trim":        c.trim,
                "sold_price":  c.sold_price,
                "sold_date":   c.sold_date,
                "mileage":     c.mileage,
                "source":      c.source,
                "listing_url": c.listing_url,
            })

        return jsonify({
            "year":       year,
            "model":      model,
            "trim":       trim,
            "fmv":        result.weighted_median,
            "confidence": result.confidence,
            "comp_count": result.comp_count,
            "comps":      comp_list,
        })
    except Exception as e:
        log.error("fmv-comps error: %s", e)
        abort(500, str(e))


# ── Personal FMV comps ───────────────────────────────────────────────────────────

USER_COMPS_FILE = SCRIPT_DIR / "data" / "user_comps.json"

def _load_user_comps():
    if USER_COMPS_FILE.exists():
        try:
            return json.loads(USER_COMPS_FILE.read_text())
        except Exception:
            return []
    return []

def _save_user_comps(comps):
    USER_COMPS_FILE.write_text(json.dumps(comps, indent=2))

@app.route("/user-comp", methods=["POST"])
def save_user_comp():
    """Save a personal FMV comp — stored and used by FMV Calculator."""
    data = request.get_json(silent=True) or {}
    listing_url = (data.get("url") or "").strip()
    fmv_value   = data.get("fmv")
    year        = data.get("year")
    model       = data.get("model", "")
    trim        = data.get("trim", "")
    price       = data.get("price")

    if not listing_url or not fmv_value or not year:
        abort(400, "Missing required fields: url, fmv, year")
    try:
        fmv_value = int(fmv_value)
        year      = int(year)
    except (ValueError, TypeError):
        abort(400, "fmv and year must be integers")

    comps = _load_user_comps()
    comps = [c for c in comps if c.get("url") != listing_url]
    comps.append({
        "url":      listing_url,
        "fmv":      fmv_value,
        "year":     year,
        "model":    model,
        "trim":     trim,
        "price":    price,
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "source":   "user",
    })
    _save_user_comps(comps)
    log.info("User comp saved: %s %s %s → FMV $%d", year, model, trim, fmv_value)
    return jsonify({"ok": True, "total": len(comps)})

@app.route("/user-comp", methods=["DELETE"])
def delete_user_comp():
    """Remove a personal FMV comp by listing URL."""
    data = request.get_json(silent=True) or {}
    listing_url = (data.get("url") or "").strip()
    if not listing_url:
        abort(400, "Missing url")
    comps = _load_user_comps()
    before = len(comps)
    comps = [c for c in comps if c.get("url") != listing_url]
    _save_user_comps(comps)
    log.info("User comp removed: %s (had %d, now %d)", listing_url[-40:], before, len(comps))
    return jsonify({"ok": True, "removed": before - len(comps)})

@app.route("/user-comps", methods=["GET"])
def get_user_comps():
    """Return all personal FMV comps for use by the calculator."""
    comps = _load_user_comps()
    return jsonify({"comps": comps, "count": len(comps)})


# ── Generation override ──────────────────────────────────────────────────────

_DB_PATH = Path(__file__).parent / "data" / "inventory.db"

@app.route("/gen-override", methods=["POST", "OPTIONS"])
def gen_override():
    """Correct the generation for a listing. Admin only (checked server-side)."""
    if request.method == "OPTIONS":
        resp = jsonify({"ok": True})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
        return resp

    # Simple token check — same passphrase as dashboard unlock
    token = request.headers.get("X-Admin-Token", "")
    if token != "gt3rs":
        abort(403, "Unauthorized")

    data = request.get_json(silent=True) or {}
    listing_id  = data.get("id")
    new_gen     = (data.get("generation") or "").strip()

    if not listing_id or not new_gen:
        abort(400, "Missing id or generation")

    valid_gens = {
        "Classic","930","964","993","996",
        "997_1","997_2","991_1","991_2","992",
        "986","987","981","718_cayman","718_boxster",
        "Carrera GT","918","944","928","914","912","356"
    }
    if new_gen not in valid_gens:
        abort(400, f"Invalid generation: {new_gen}")

    import sqlite3
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "UPDATE listings SET generation=? WHERE id=?",
            (new_gen, int(listing_id))
        )
        conn.commit()
        conn.close()
        log.info("Gen override: listing %s → %s", listing_id, new_gen)
        return jsonify({"ok": True, "id": listing_id, "generation": new_gen})
    except Exception as e:
        log.error("Gen override failed: %s", e)
        abort(500, str(e))


# ── Waitlist ─────────────────────────────────────────────────────────────────────
# Google Apps Script Web App URL — set via env var or paste directly:
SHEETS_WAITLIST_URL = os.environ.get("SHEETS_WAITLIST_URL", "https://script.google.com/macros/s/AKfycbyQp-NrzGlpJnuaaTClhNme_eLxAFHok82-QDZhgFqnfBxRjFZkqgTLnnexpBDk0Wqu/exec")

@app.route("/waitlist", methods=["POST", "OPTIONS"])
def waitlist():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "invalid email"}), 400
    payload = {"email": email, "source": "rennmarkt.net"}
    if SHEETS_WAITLIST_URL:
        try:
            import urllib.request as _ur, json as _json
            req = _ur.Request(
                SHEETS_WAITLIST_URL,
                data=_json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            _ur.urlopen(req, timeout=8)
        except Exception as e:
            log.warning("Sheets waitlist write failed: %s", e)
            return jsonify({"ok": False, "error": "sheets error"}), 500
    else:
        log.warning("SHEETS_WAITLIST_URL not set — email not saved: %s", email)
    log.info("Waitlist signup: %s", email)
    return jsonify({"ok": True})


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("PTOX11 Push Server starting on port 5055")
    log.info("VAPID public key: %s", VAPID_PUBLIC_KEY[:30] + "...")
    subs = _load_subs()
    log.info("Loaded %d existing subscriptions", len(subs))
    app.run(host="127.0.0.1", port=5055, debug=False)

"""
distill_receiver.py
-------------------
Flask webhook receiver for Distill.io change notifications.

Distill fires a POST to http://localhost:5000/distill-change whenever a
monitored page changes (new listing, price update, mileage update, etc.).

This script receives the JSON payload, timestamps it, and saves it as a
JSON file in ~/porsche-tracker/distill_drops/ for the watcher to process.

Run permanently via launchd (see com.porschetracker.distill-receiver.plist).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DROP_DIR = BASE_DIR / "distill_drops"
LOG_FILE = BASE_DIR / "logs" / "distill_receiver.log"
PORT     = 5000

DROP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [receiver] %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/distill-change", methods=["POST"])
def distill_change():
    """Receive a Distill webhook payload and save it as a timestamped JSON file."""
    try:
        # Accept both application/json and form-encoded payloads
        if request.is_json:
            payload = request.get_json(force=True)
        else:
            raw = request.get_data(as_text=True)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}

        # Stamp with receipt time so the watcher knows when it arrived
        payload["_received_at"] = datetime.utcnow().isoformat() + "Z"

        ts   = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        path = DROP_DIR / f"distill_{ts}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

        log.info("Saved webhook → %s", path.name)
        return jsonify({"status": "ok", "file": path.name}), 200

    except Exception as exc:
        log.exception("Error handling webhook: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "drop_dir": str(DROP_DIR)}), 200


if __name__ == "__main__":
    log.info("Distill receiver starting on port %s  →  drops: %s", PORT, DROP_DIR)
    # use_reloader=False is critical for launchd/background process stability
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)

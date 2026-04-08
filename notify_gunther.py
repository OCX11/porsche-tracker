#!/usr/bin/env python3
"""
notify_gunther.py — FMV-based Porsche deal alerts via Telegram.

Alert thresholds (from WATCHLIST.md):
  TIER1 (GT/Collector): alert when listing is DEAL (10%+ below FMV) or WATCH (5-10% below)
  TIER2 (Standard):     alert only on DEAL (10%+ below FMV)

Dedup: data/seen_alerts.json tracks evaluated URLs so we don't re-alert
unless price drops since last evaluation.

Confidence gating: skips alerts with confidence=NONE (no comp data).
LOW confidence alerts are sent with a caveat.
"""
import json
import logging
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
SEEN_FILE = SCRIPT_DIR / "data" / "seen_alerts.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "gunther.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Set to False to silence all Telegram alerts without changing any other logic.
NOTIFICATIONS_ENABLED = False

NANOBOT_CONFIG = Path.home() / ".nanobot" / "config.json"

sys.path.insert(0, str(SCRIPT_DIR))
import db as database
import fmv as fmv_engine


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


# ── Telegram ───────────────────────────────────────────────────────────────────

def _load_telegram_creds():
    try:
        cfg = json.loads(NANOBOT_CONFIG.read_text())
        tg = cfg.get("channels", {}).get("telegram", {})
        token = tg.get("token", "")
        allow_from = tg.get("allowFrom", [])
        return token, allow_from[0] if allow_from else ""
    except Exception as e:
        log.warning("Could not read Telegram creds: %s", e)
        return "", ""


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload,
                                   headers={"Content-Type": "application/json"}),
            timeout=15
        ) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.error("Telegram sendMessage error: %s", result)
                return False
            return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


# ── Alert formatting ───────────────────────────────────────────────────────────

def _format_alert(s: dict) -> str:
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

    flag_emoji = "🔥" if flag == "DEAL" else "👀"
    tier_label = "GT/Collector" if tier == "TIER1" else "Standard"

    price_str = f"${price:,}" if price else "No Price"
    miles_str = f"{mileage:,} mi" if mileage else "mileage unknown"
    pct_str   = f"{pct:+.0%}"
    fmv_str   = f"${fmv:,}"

    conf_note = ""
    if conf == "LOW":
        conf_note = f"\n⚠️ _FMV based on limited data ({comp_cnt} comp{'s' if comp_cnt != 1 else ''}) — verify manually_"
    elif conf == "MEDIUM":
        conf_note = f"\n_({comp_cnt} comps)_"

    return (
        f"{flag_emoji} *{flag}: {year} Porsche {model} {trim}*\n"
        f"💰 {price_str} ({pct_str} vs FMV of {fmv_str})\n"
        f"🛣️ {miles_str}\n"
        f"📍 {dealer}  [{tier_label}]\n"
        f"🔗 {url}"
        f"{conf_note}"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not NOTIFICATIONS_ENABLED:
        log.info("Notifications disabled (NOTIFICATIONS_ENABLED=False) — skipping.")
        return

    database.init_db()

    with database.get_conn() as conn:
        scored = fmv_engine.score_active_listings(conn)

    seen = _load_seen()
    token, chat_id = _load_telegram_creds()
    alerts_sent = 0
    evaluated   = 0

    for s in scored:
        ds   = s.get("deal_score")
        tier = s.get("tier", "TIER2")
        if not ds:
            continue

        flag = ds["deal_flag"]
        conf = ds["confidence"]

        # Gate on confidence — NONE means no comp data at all
        if conf == "NONE":
            continue

        # Apply tier-specific alert thresholds
        # TIER1: alert on DEAL or WATCH (5%+ below FMV is notable for a GT car)
        # TIER2: alert only on DEAL (10%+ below — standard cars have more price noise)
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

        # Only alert if:
        # - Never seen before, OR
        # - Price has dropped since last evaluation, OR
        # - Flag improved (e.g. was WATCH, now DEAL)
        price_changed = last_price is not None and price and price != last_price
        flag_improved = last_flag and flag == "DEAL" and last_flag == "WATCH"
        is_new        = key not in seen

        if not (is_new or price_changed or flag_improved):
            log.debug("Skip (already alerted, no change): %s %s %s",
                      s.get("year"), s.get("model"), s.get("trim") or "")
            continue

        evaluated += 1
        log.info("ALERT candidate: %s %s %s  ask=$%s  %+.0f%% vs FMV  [%s]  conf=%s",
                 s.get("year"), s.get("model"), s.get("trim") or "",
                 f"{price:,}" if price else "?",
                 (ds["pct_vs_fmv"] * 100), flag, conf)

        seen[key] = {
            "evaluated_at": datetime.now().isoformat(),
            "last_price":   price,
            "last_flag":    flag,
            "alerted":      False,
        }

        if token and chat_id:
            msg = _format_alert(s)
            ok  = _send_telegram(token, chat_id, msg)
            if ok:
                seen[key]["alerted"] = True
                alerts_sent += 1
                log.info("  → Telegram sent")
            else:
                log.error("  → Telegram delivery failed")
        else:
            log.warning("  → No Telegram creds configured, alert not delivered")

        _save_seen(seen)  # save after each so a crash mid-run doesn't re-evaluate

    log.info(
        "Done — %d alert(s) sent, %d evaluated, %d total scored listings",
        alerts_sent, evaluated, len(scored)
    )


if __name__ == "__main__":
    main()

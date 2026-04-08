"""
health_monitor.py — scraper health checks with iMessage alerts.

Checks:
  1. For each active source in DEALERS: if the source returned 0 listings in
     ALL of the last 3 consecutive scrape runs → send one iMessage alert.
  2. If today's scrape log hasn't been updated in over 30 minutes → alert
     (scheduler may be stuck).

Dedup: data/health_monitor_seen.json — one alert per source per calendar day.
Called at the end of each main.py scrape cycle.
"""
import json
import logging
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR  = Path(__file__).parent
DATA_DIR    = SCRIPT_DIR / "data"
LOG_DIR     = SCRIPT_DIR / "data" / "logs"
SEEN_FILE   = DATA_DIR / "health_monitor_seen.json"
CONFIG_FILE = DATA_DIR / "imessage_config.json"

# How many consecutive zero-result runs before alerting
ZERO_RUN_THRESHOLD = 3
# Minutes since last log write before "scheduler stuck" alert fires
STALE_LOG_MINUTES  = 30


# ---------------------------------------------------------------------------
# iMessage delivery (same pattern as notify_imessage.py)
# ---------------------------------------------------------------------------

def _load_recipient():
    """Return the iMessage recipient from imessage_config.json, or None."""
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("recipient")
    except Exception as e:
        log.warning("health_monitor: could not load imessage_config.json: %s", e)
        return None


def _send_imessage(recipient, text):
    """Send an iMessage via AppleScript → Messages.app. Returns True on success."""
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
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.error("health_monitor: AppleScript error: %s", result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("health_monitor: iMessage send timed out")
        return False
    except Exception as e:
        log.error("health_monitor: iMessage send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Dedup store
# ---------------------------------------------------------------------------

def _load_seen():
    """Return {alert_key: date_str} dedup dict."""
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def _already_alerted(seen, key):
    """True if we already sent this alert today."""
    return seen.get(key) == date.today().isoformat()


def _mark_alerted(seen, key):
    seen[key] = date.today().isoformat()


# ---------------------------------------------------------------------------
# Scrape log parsing
# ---------------------------------------------------------------------------

def _today_log_path():
    return LOG_DIR / f"scrape_{date.today().isoformat()}.log"


def _parse_scrape_blocks(log_path):
    """
    Parse scrape_YYYY-MM-DD.log into a list of run dicts.

    Each run dict: {"timestamp": datetime, "counts": {"Source Name": int, ...}}
    Returns runs in file order (oldest first).
    """
    try:
        text = log_path.read_text()
    except Exception:
        return []

    runs = []
    current_ts = None
    current_counts = {}

    for line in text.splitlines():
        # Header line: === Scrape 2026-04-01 12:34:56 ===
        m = re.match(r"=== Scrape (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ===", line.strip())
        if m:
            if current_ts is not None:
                runs.append({"timestamp": current_ts, "counts": current_counts})
            current_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            current_counts = {}
            continue

        # Source line: "  Bring a Trailer                       47"
        # Skip separator and TOTAL lines
        if current_ts is None:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or stripped.startswith("TOTAL"):
            continue
        # Last token is the count (integer); everything before is the source name
        parts = stripped.rsplit(None, 1)
        if len(parts) == 2:
            source_name = parts[0].strip()
            try:
                count = int(parts[1])
                current_counts[source_name] = count
            except ValueError:
                pass

    # Flush last block
    if current_ts is not None:
        runs.append({"timestamp": current_ts, "counts": current_counts})

    return runs


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _check_zero_runs(runs, active_sources, seen, recipient):
    """Alert for any source with 0 listings in the last N consecutive runs."""
    if len(runs) < ZERO_RUN_THRESHOLD:
        return  # Not enough history yet

    recent = runs[-ZERO_RUN_THRESHOLD:]

    for source in active_sources:
        # Only alert for sources that appear in at least one of the recent runs
        # (sources added later won't have history in earlier runs)
        appeared_in = [r for r in recent if source in r["counts"]]
        if not appeared_in:
            continue

        all_zero = all(r["counts"].get(source, 0) == 0 for r in appeared_in)
        # Require the source to appear in all N recent runs and be zero each time
        if len(appeared_in) < ZERO_RUN_THRESHOLD:
            continue
        if not all_zero:
            continue

        key = f"zero:{source}"
        if _already_alerted(seen, key):
            log.info("health_monitor: %s zero-run alert already sent today", source)
            continue

        msg = (
            f"\u26a0\ufe0f {source} has returned 0 listings for "
            f"{ZERO_RUN_THRESHOLD} consecutive runs \u2014 may be blocked"
        )
        log.warning("health_monitor: %s", msg)
        if recipient and _send_imessage(recipient, msg):
            log.info("health_monitor: alert sent for %s", source)
            _mark_alerted(seen, key)
        else:
            log.error("health_monitor: failed to send alert for %s", source)


def _check_stale_log(log_path, seen, recipient):
    """Alert if the scrape log hasn't been written to in STALE_LOG_MINUTES."""
    try:
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
    except Exception:
        # Log doesn't exist yet — could be a new day, not an error
        return

    age_minutes = (datetime.now() - mtime).total_seconds() / 60
    if age_minutes <= STALE_LOG_MINUTES:
        return

    key = "stale_log"
    if _already_alerted(seen, key):
        return

    msg = (
        f"\u26a0\ufe0f Porsche tracker scrape log hasn't updated in "
        f"{int(age_minutes)} minutes \u2014 scheduler may be stuck"
    )
    log.warning("health_monitor: %s", msg)
    if recipient and _send_imessage(recipient, msg):
        log.info("health_monitor: stale-log alert sent")
        _mark_alerted(seen, key)
    else:
        log.error("health_monitor: failed to send stale-log alert")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main():
    """Run all health checks. Called at end of each main.py scrape cycle."""
    # Import DEALERS at call time to avoid circular imports at module load
    try:
        from scraper import DEALERS as _DEALERS
        active_sources = [d["name"] for d in _DEALERS]
    except Exception as e:
        log.warning("health_monitor: could not import DEALERS: %s", e)
        active_sources = []

    log_path   = _today_log_path()
    runs       = _parse_scrape_blocks(log_path)
    seen       = _load_seen()
    recipient  = _load_recipient()

    # Purge stale dedup entries from previous days
    today = date.today().isoformat()
    seen  = {k: v for k, v in seen.items() if v == today}

    _check_zero_runs(runs, active_sources, seen, recipient)
    _check_stale_log(log_path, seen, recipient)

    _save_seen(seen)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    main()

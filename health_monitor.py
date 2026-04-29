"""
health_monitor.py — scraper health checks with push notification alerts.

Checks:
  1. For each active source in DEALERS: if the source returned 0 listings in
     ALL of the last 3 consecutive scrape runs → send one push alert.
  2. If today's scrape log hasn't been updated in over 30 minutes → alert
     (scheduler may be stuck).

Dedup: data/health_monitor_seen.json — one alert per source per calendar day.
Called at the end of each main.py scrape cycle.
"""
import json
import logging
import re
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR  = Path(__file__).parent
DATA_DIR    = SCRIPT_DIR / "data"
LOG_DIR     = SCRIPT_DIR / "data" / "logs"
SEEN_FILE   = DATA_DIR / "health_monitor_seen.json"

PUSH_SERVER = "http://127.0.0.1:5055/send-push"
DASHBOARD_URL = "https://www.rennmarkt.net/"

# How many consecutive zero-result runs before alerting
ZERO_RUN_THRESHOLD = 3
# Minutes since last log write before "scheduler stuck" alert fires
STALE_LOG_MINUTES  = 30


# ---------------------------------------------------------------------------
# Push delivery
# ---------------------------------------------------------------------------

def _send_push(title, body):
    """Send a push notification via local push server. Returns True on success."""
    try:
        resp = requests.post(
            PUSH_SERVER,
            json={"title": title, "body": body, "url": DASHBOARD_URL},
            timeout=10,
        )
        result = resp.json()
        if result.get("sent", 0) > 0:
            return True
        log.warning("health_monitor: push sent=0 (no subscribers or delivery failed)")
        return False
    except Exception as e:
        log.error("health_monitor: push failed: %s", e)
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
    current_run = None

    for line in text.splitlines():
        # Detect start of a new scrape cycle
        run_start = re.match(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO\s+={10,}",
            line,
        )
        if run_start:
            if current_run:
                runs.append(current_run)
            ts = datetime.strptime(run_start.group(1), "%Y-%m-%d %H:%M:%S")
            current_run = {"timestamp": ts, "counts": {}}
            continue

        # Detect per-source result lines, e.g. "  [BaT] 12 listings"
        if current_run:
            m = re.search(r"\[([^\]]+)\]\s+(\d+)\s+listing", line)
            if m:
                source, count = m.group(1), int(m.group(2))
                current_run["counts"][source] = count

    if current_run:
        runs.append(current_run)

    return runs


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _check_zero_runs(runs, active_sources, seen):
    if len(runs) < ZERO_RUN_THRESHOLD:
        return

    recent_runs = runs[-ZERO_RUN_THRESHOLD:]

    for source in active_sources:
        appeared_in = [r for r in recent_runs if source in r["counts"]]
        all_zero = all(r["counts"].get(source, 0) == 0 for r in appeared_in)
        if len(appeared_in) < ZERO_RUN_THRESHOLD:
            continue
        if not all_zero:
            continue

        key = f"zero:{source}"
        if _already_alerted(seen, key):
            log.info("health_monitor: %s zero-run alert already sent today", source)
            continue

        msg = f"{source} returned 0 listings for {ZERO_RUN_THRESHOLD} consecutive runs — may be blocked"
        log.warning("health_monitor: %s", msg)
        if _send_push(f"⚠️ Scraper Down: {source}", msg):
            log.info("health_monitor: push alert sent for %s", source)
            _mark_alerted(seen, key)
        else:
            log.error("health_monitor: failed to send push alert for %s", source)


def _check_stale_log(log_path, seen):
    """Alert if the scrape log hasn't been written to in STALE_LOG_MINUTES."""
    try:
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
    except Exception:
        return

    age_minutes = (datetime.now() - mtime).total_seconds() / 60
    if age_minutes <= STALE_LOG_MINUTES:
        return

    key = "stale_log"
    if _already_alerted(seen, key):
        return

    msg = f"Scrape log hasn't updated in {int(age_minutes)} min — scheduler may be stuck"
    log.warning("health_monitor: %s", msg)
    if _send_push("⚠️ PTOX11 Scheduler Stuck", msg):
        log.info("health_monitor: stale-log push alert sent")
        _mark_alerted(seen, key)
    else:
        log.error("health_monitor: failed to send stale-log push alert")



# ---------------------------------------------------------------------------
# Service health checks
# ---------------------------------------------------------------------------

_LAUNCHD_SERVICES = {
    'com.porschetracker.scrape': 'run_daily.sh',
    'com.porschetracker.archive-capture': 'archive_capture.py',
    'com.porschetracker.distill-poller': 'distill_poller.py',
    'com.porschetracker.distill-receiver': 'distill_receiver.py',
    'com.porschetracker.distill-watcher': 'distill_watcher.py',
}


def _check_services(seen):
    """Check if key launchd services are running. Alert + attempt restart if not."""
    import subprocess
    today = date.today().isoformat()

    try:
        result = subprocess.run(['launchctl', 'list'], capture_output=True, text=True, timeout=10)
        running_labels = result.stdout
    except Exception as e:
        log.warning("health_monitor: launchctl list failed: %s", e)
        return

    for label, desc in _LAUNCHD_SERVICES.items():
        if label not in running_labels:
            key = "service_down:" + label
            if seen.get(key) == today:
                continue

            log.warning("health_monitor: service %s (%s) not running", label, desc)

            # Attempt restart
            try:
                plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
                if plist.exists():
                    subprocess.run(['launchctl', 'load', str(plist)], timeout=10)
                    log.info("health_monitor: attempted restart of %s", label)
            except Exception as e:
                log.warning("health_monitor: restart failed for %s: %s", label, e)

            msg = "%s is not running (attempted restart)" % desc
            if _send_push("⚠️ Service Down", msg):
                _mark_alerted(seen, key)


def _check_proxy(seen):
    """Check DataImpulse proxy health with a simple request."""
    today = date.today().isoformat()
    key = "proxy_down"
    if seen.get(key) == today:
        return

    try:
        proxy_cfg_path = DATA_DIR / "proxy_config.json"
        if not proxy_cfg_path.exists():
            return
        with open(proxy_cfg_path) as f:
            cfg = json.load(f)
        if not cfg.get("enabled"):
            return

        proxy_url = "http://%s:%s@%s:%s" % (
            cfg.get("username", ""), cfg.get("password", ""),
            cfg.get("host", ""), cfg.get("port", ""))
        proxies = {"http": proxy_url, "https": proxy_url}

        resp = requests.get("https://api.ipify.org", proxies=proxies, timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            log.info("health_monitor: proxy OK (exit IP: %s)", resp.text.strip())
        else:
            raise Exception("HTTP %d" % resp.status_code)
    except Exception as e:
        log.warning("health_monitor: proxy check failed: %s", e)
        msg = "DataImpulse proxy health check failed: %s" % str(e)[:100]
        if _send_push("⚠️ Proxy Down", msg):
            _mark_alerted(seen, key)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main():
    """Run all health checks. Called at end of each main.py scrape cycle."""
    try:
        from scraper import DEALERS as _DEALERS
        active_sources = [d["name"] for d in _DEALERS]
    except Exception as e:
        log.warning("health_monitor: could not import DEALERS: %s", e)
        active_sources = []

    log_path = _today_log_path()
    runs     = _parse_scrape_blocks(log_path)
    seen     = _load_seen()

    # Purge stale dedup entries from previous days
    today = date.today().isoformat()
    seen  = {k: v for k, v in seen.items() if v == today}

    _check_zero_runs(runs, active_sources, seen)
    _check_stale_log(log_path, seen)
    _check_services(seen)
    _check_proxy(seen)

    _save_seen(seen)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    main()

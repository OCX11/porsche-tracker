#!/usr/bin/env python3
"""
PTOX11 iMessage Bot Daemon
Polls chat.db every 30s for messages from owner, routes to Claude API or
built-in commands, sends response via iMessage.
"""

import json
import logging
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
OWNER_PHONE    = "+16108361111"
POLL_INTERVAL  = 30  # seconds
MAX_RESP_CHARS = 280

BASE_DIR   = Path(__file__).parent.resolve()
DATA_DIR   = BASE_DIR / "data"
LOGS_DIR   = BASE_DIR / "logs"
STATE_FILE = DATA_DIR / "imessage_bot_state.json"
CHAT_DB    = Path.home() / "Library/Messages/chat.db"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("imessage_bot")

# ── Claude ────────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic()

SYSTEM_PROMPT = (
    "You are a concise assistant for PTOX11, a Porsche market intelligence platform. "
    "The owner is texting you ideas, tasks, and questions via iMessage. "
    "Keep responses SHORT — max ~250 chars. "
    "For PTOX11 status questions, note you don't have live DB access but can give general guidance. "
    "Max 3 sentences unless asked for more. Never use markdown formatting."
)

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning("State load failed: %s", e)
    return {"last_message_date": 0}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("State save failed: %s", e)


# ── Read messages ─────────────────────────────────────────────────────────────
def read_new_messages(last_date):
    """Return list of {text, date} dicts from owner newer than last_date.

    Uses the sqlite3 CLI via subprocess so it benefits from any Full Disk
    Access grant on /usr/bin/sqlite3 or the parent process.  Falls back to
    the Python sqlite3 module if the CLI is unavailable.
    """
    messages = []
    sql = (
        "SELECT m.text, m.date "
        "FROM message m "
        "JOIN handle h ON m.handle_id = h.ROWID "
        "WHERE h.id = '{}' "
        "  AND m.is_from_me = 0 "
        "  AND m.text IS NOT NULL "
        "  AND m.text != '' "
        "  AND m.date > {} "
        "ORDER BY m.date ASC;"
    ).format(OWNER_PHONE, int(last_date))

    # Try sqlite3 CLI first (gets FDA from parent process on macOS)
    try:
        r = subprocess.run(
            ["sqlite3", "-separator", "\x1f", str(CHAT_DB), sql],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                if not line:
                    continue
                parts = line.split("\x1f", 1)
                if len(parts) == 2:
                    messages.append({"text": parts[0], "date": int(parts[1])})
            return messages
        else:
            log.warning("sqlite3 CLI error: %s", r.stderr.strip())
    except Exception as e:
        log.warning("sqlite3 CLI unavailable: %s", e)

    # Fallback: Python sqlite3 module (needs FDA for python3 binary)
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(CHAT_DB), uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT m.text, m.date "
            "FROM message m "
            "JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE h.id = ? AND m.is_from_me = 0 "
            "  AND m.text IS NOT NULL AND m.text != '' AND m.date > ? "
            "ORDER BY m.date ASC",
            (OWNER_PHONE, last_date),
        )
        for row in cur.fetchall():
            messages.append({"text": row["text"], "date": row["date"]})
        conn.close()
    except Exception as e:
        log.error(
            "chat.db read error: %s "
            "(grant Full Disk Access to /usr/bin/python3 in System Settings → "
            "Privacy & Security → Full Disk Access)",
            e,
        )
    return messages


# ── Send iMessage ─────────────────────────────────────────────────────────────
def send_imessage(phone, message):
    # Escape backslashes then double-quotes for embedding in AppleScript string
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '  set targetService to first service whose service type = iMessage\n'
        '  set targetBuddy to buddy "{phone}" of targetService\n'
        '  send "{msg}" to targetBuddy\n'
        'end tell'
    ).format(phone=phone, msg=safe)
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.error("osascript send error: %s", result.stderr.strip())
        else:
            log.info("Sent to %s: %s", phone, message[:60])
    except Exception as e:
        log.error("send_imessage exception: %s", e)


# ── Special commands ──────────────────────────────────────────────────────────
def cmd_status():
    """Active listing count + last scrape time."""
    count = "?"
    try:
        r = subprocess.run(
            ["sqlite3", str(DATA_DIR / "inventory.db"),
             "SELECT COUNT(*) FROM listings WHERE status='active'"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            count = r.stdout.strip()
    except Exception as e:
        log.error("cmd_status db error: %s", e)

    last_scrape = "unknown"
    log_path = LOGS_DIR / "scraper.log"
    if log_path.exists():
        try:
            mod_time = datetime.fromtimestamp(log_path.stat().st_mtime)
            mins_ago = int((datetime.now() - mod_time).total_seconds() / 60)
            last_scrape = "{}m ago".format(mins_ago)
        except Exception:
            pass

    return "PTOX11: {} active listings. Last scrape: {}.".format(count, last_scrape)


def cmd_add_to_note(note_name, text):
    """Append text to an Apple Notes note. Returns True on success."""
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    safe_note = note_name.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Notes"\n'
        '  set theNote to first note whose name = "{}"\n'
        '  set body of theNote to (body of theNote) & "<br>" & "{}"\n'
        'end tell'
    ).format(safe_note, safe_text)
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log.error("Notes AppleScript error: %s", r.stderr.strip())
            return False
        return True
    except Exception as e:
        log.error("cmd_add_to_note exception: %s", e)
        return False


def cmd_push_test():
    """Send a test push notification via local push server."""
    payload = json.dumps({
        "title": "PTOX11 Bot Test",
        "body": "Push test from iMessage bot \u2713",
        "url": "https://ocx11.github.io/PTOX11/",
    }).encode()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:5055/send-push",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "Push test sent \u2713 (HTTP {})".format(resp.status)
    except Exception as e:
        return "Push test failed: {}".format(e)


# ── Claude API ────────────────────────────────────────────────────────────────
def call_claude(text):
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        reply = response.content[0].text.strip()
        if len(reply) > MAX_RESP_CHARS:
            reply = reply[:MAX_RESP_CHARS - 1] + "\u2026"
        return reply
    except Exception as e:
        log.error("Claude API error: %s", e)
        return "[API error: {}]".format(e)


# ── Dispatch ──────────────────────────────────────────────────────────────────
def handle_message(text):
    stripped = text.strip()
    lower = stripped.lower()

    if lower == "status":
        return cmd_status()

    if lower.startswith("add idea:"):
        idea = stripped[9:].strip()
        ok = cmd_add_to_note("Observations for PTOX11", idea)
        return "Added to Observations \u2713" if ok else "Failed to add to Notes."

    if lower.startswith("add task:"):
        task = stripped[9:].strip()
        ok = cmd_add_to_note("\U0001f3ce PTOX11 \u2014 Task Board", task)
        return "Added to Task Board \u2713" if ok else "Failed to add to Notes."

    if lower == "push test":
        return cmd_push_test()

    return call_claude(stripped)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("PTOX11 iMessage bot starting. Owner: %s  Poll: %ds", OWNER_PHONE, POLL_INTERVAL)
    state = load_state()
    log.info("Last processed message date: %s", state["last_message_date"])

    while True:
        try:
            messages = read_new_messages(state["last_message_date"])
            for msg in messages:
                text = msg["text"]
                date = msg["date"]
                log.info("Incoming [date=%s]: %s", date, text[:80])
                try:
                    response = handle_message(text)
                except Exception as e:
                    log.error("handle_message error: %s", e)
                    response = "[Bot error — check logs]"
                log.info("Response: %s", response[:80])
                send_imessage(OWNER_PHONE, response)
                # Advance state even if send failed, to avoid reprocessing loops
                state["last_message_date"] = date
                save_state(state)
        except Exception as e:
            log.error("Poll loop error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

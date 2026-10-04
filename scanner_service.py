"""
Bid Scout — local scan worker service (self-hosted Docker edition).

Replaces the GitHub Actions workflow_dispatch plumbing that api/index.py used
to drive scraper.py. This is a small, internal-only Flask app (not reachable
through Traefik — only the app container talks to it, over the Docker
network, at SCANNER_URL) with three jobs:

  1. Run scraper.py as a subprocess on demand ("Run Now"), one at a time.
  2. Kill that subprocess on demand ("Cancel scan") — a direct OS-level
     kill, which is simpler and more reliable than GitHub Actions' cancel
     API (no run-id guessing, no 404 diagnosis needed).
  3. Run scraper.py with no division argument once a day at
     DISPLAY_SCHEDULE_UTC, scanning every division — replacing
     .github/workflows/daily-scan.yml's cron trigger.

Routes:
  GET  /health            liveness probe for the compose healthcheck
  POST /run-now            body: {"division_id": "<id>"} or {} for all
  POST /cancel              stop whatever scan is currently running

Environment variables: same SUPABASE_URL / SUPABASE_SERVICE_KEY /
AI_PROVIDER / ANTHROPIC_API_KEY / GEMINI_API_KEY / FIRECRAWL_API_KEY /
CREDENTIALS_KEY as scraper.py always needed, plus DISPLAY_SCHEDULE_UTC
(reused — same value the dashboard already shows) and SCANNER_PORT
(default 9100).
"""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

_lock = threading.Lock()
_state = {"proc": None, "division_id": None}  # division_id None = "all divisions"


def _schedule_hm(text):
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mn < 60:
        return h, mn
    return None


SCHEDULE_HM = _schedule_hm(os.environ.get("DISPLAY_SCHEDULE_UTC", "06:41 UTC")) or (6, 41)


def _is_running():
    proc = _state["proc"]
    return proc is not None and proc.poll() is None


def _start(division_id):
    with _lock:
        if _is_running():
            return False
        args = [sys.executable, "scraper.py", division_id or ""]
        _state["proc"] = subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(__file__)))
        _state["division_id"] = division_id
        return True


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "running": _is_running()})


@app.route("/run-now", methods=["POST"])
def run_now():
    body = request.get_json(silent=True) or {}
    division_id = (body.get("division_id") or "").strip() or None
    if not _start(division_id):
        return jsonify({"error": "a scan is already running"}), 409
    return jsonify({"ok": True})


@app.route("/cancel", methods=["POST"])
def cancel():
    with _lock:
        proc = _state["proc"]
        if proc is None or proc.poll() is not None:
            return jsonify({"ok": True, "was_running": False})
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return jsonify({"ok": True, "was_running": True})


def _run_daily_scan():
    print(f"[scheduler] starting scheduled scan of every division", flush=True)
    if not _start(None):
        print("[scheduler] skipped — a scan was already running", flush=True)


def _seconds_until_next(hour, minute):
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _scheduler_loop():
    hour, minute = SCHEDULE_HM
    while True:
        wait_s = _seconds_until_next(hour, minute)
        time.sleep(wait_s)
        try:
            _run_daily_scan()
        except Exception as e:
            print(f"[scheduler] FATAL: {e}", file=sys.stderr, flush=True)
        # Sleep past the trigger minute so the same tick can't fire twice.
        time.sleep(60)


threading.Thread(target=_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("SCANNER_PORT", "9100"))
    app.run(host="0.0.0.0", port=port)

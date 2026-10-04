"""
Bid Scout — local scan worker service (self-hosted Docker edition).

Replaces the GitHub Actions workflow_dispatch plumbing that api/index.py used
to drive scraper.py. This is a small, internal-only Flask app (not reachable
through Traefik — only the app container talks to it, over the Docker
network, at SCANNER_URL) with four jobs:

  1. Run scraper.py as a subprocess on demand ("Run Now"), one at a time.
  2. Kill that subprocess on demand ("Cancel scan") — a direct OS-level
     kill, which is simpler and more reliable than GitHub Actions' cancel
     API (no run-id guessing, no 404 diagnosis needed).
  3. Run scraper.py with no division argument once a day at
     DISPLAY_SCHEDULE_UTC, scanning every division — replacing
     .github/workflows/daily-scan.yml's cron trigger.
  4. Re-fetch a single ConstructConnect document live, on demand, when its
     cached copy in Storage has expired (see the retention loop below and
     api/index.py's api_proxy_pdf) — and once a day, delete cached copies
     older than CACHED_DOCUMENT_RETENTION_DAYS.

Routes:
  GET  /health              liveness probe for the compose healthcheck
  POST /run-now              body: {"division_id": "<id>"} or {} for all
  POST /cancel                stop whatever scan is currently running
  POST /refetch-document      body: {"division_id", "document_url"} — only
                              works for documents whose adapter captures
                              bytes directly (currently just ConstructConnect)

Environment variables: same SUPABASE_URL / SUPABASE_SERVICE_KEY /
AI_PROVIDER / ANTHROPIC_API_KEY / GEMINI_API_KEY / FIRECRAWL_API_KEY /
CREDENTIALS_KEY as scraper.py always needed, plus DISPLAY_SCHEDULE_UTC
(reused — same value the dashboard already shows), SCANNER_PORT (default
9100), and CACHED_DOCUMENT_RETENTION_DAYS (default 30).
"""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

import adapters
import credentials
import supabase_store

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


def _site_login(site):
    """Same decrypt-the-saved-password logic as scraper.py's _site_login —
    reimplemented here (rather than importing scraper.py, which has a lot
    of its own top-level setup meant for running as __main__) since it's
    this small."""
    username = (site.get("login_username") or "").strip()
    enc = site.get("login_password_enc")
    if not username or not enc:
        return None
    try:
        password = credentials.decrypt_password(enc)
    except Exception as e:
        print(f"  ! Could not decrypt login credentials for '{site.get('name')}': {e}")
        return None
    return {
        "username": username,
        "password": password,
        "url": (site.get("login_url") or "").strip() or site.get("url"),
    }


@app.route("/refetch-document", methods=["POST"])
def refetch_document():
    """Live re-fetch for a ConstructConnect document whose cached copy has
    expired (storage_path NULL but is_cached_document True — see
    api/index.py's api_proxy_pdf, the only caller of this route). Opens a
    fresh logged-in browser session, re-downloads the whole project (that's
    the only granularity ConstructConnect offers), and re-uploads every
    file it gets back — not just the one requested — so sibling files in
    the same project are refreshed too while a session is already open."""
    body = request.get_json(silent=True) or {}
    division_id = (body.get("division_id") or "").strip()
    document_url = body.get("document_url") or ""
    if not division_id or not document_url:
        return jsonify({"ok": False, "error": "division_id and document_url required"}), 400

    doc = supabase_store.scanned_document_lookup(division_id).get(document_url)
    if doc is None:
        return jsonify({"ok": False, "error": "no such document"}), 404
    if not doc.get("is_cached_document"):
        return jsonify({"ok": False, "error": "this document was never browser-captured"}), 422
    if not doc.get("source_url"):
        return jsonify({"ok": False, "error": "no source_url on record — can't re-fetch"}), 422

    site_name, project_label = supabase_store._split_site(doc.get("site") or "")
    site = next(
        (s for s in supabase_store.load_sites(division_id)
         if s.get("name") == site_name and s.get("adapter") == "constructconnect"),
        None,
    )
    if site is None:
        return jsonify({"ok": False, "error": f"no ConstructConnect site named '{site_name}'"}), 404
    login = _site_login(site)
    if not login:
        return jsonify({"ok": False, "error": "site has no usable login configured"}), 422

    results = adapters.ConstructConnectAdapter().refetch_project_documents(
        login, doc["source_url"], project_label
    )
    if not results:
        return jsonify({"ok": False, "error": "re-fetch found no documents"}), 502

    refreshed = 0
    for _lbl, url, fn, content, _src, _bid, _bid_time in results:
        if content is None:
            continue
        doc_key = url or f"{site.get('url', '')}#{fn}"
        storage_path = supabase_store.upload_document_bytes(division_id, doc_key, content)
        if storage_path:
            supabase_store.set_storage_path(division_id, doc_key, storage_path)
            refreshed += 1

    if refreshed == 0:
        return jsonify({"ok": False, "error": "re-fetched but couldn't store any files"}), 502
    return jsonify({"ok": True, "refreshed": refreshed})


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


CACHED_DOCUMENT_RETENTION_DAYS = int(os.environ.get("CACHED_DOCUMENT_RETENTION_DAYS", "30") or "30")


def _run_retention_sweep():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHED_DOCUMENT_RETENTION_DAYS)).isoformat()
    expired = supabase_store.list_expired_cached_documents(cutoff)
    if not expired:
        print("[retention] nothing older than the retention window", flush=True)
        return
    cleared = 0
    for row in expired:
        supabase_store.delete_document_bytes(row["storage_path"])
        supabase_store.clear_storage_path(row["division_id"], row["document_url"])
        cleared += 1
    print(f"[retention] cleared {cleared} cached document(s) older than "
          f"{CACHED_DOCUMENT_RETENTION_DAYS} day(s)", flush=True)


def _retention_loop():
    # Run once shortly after startup, then every 24h — independent of the
    # daily-scan schedule above, since this just needs to happen roughly
    # once a day, not at any particular time.
    time.sleep(60)
    while True:
        try:
            _run_retention_sweep()
        except Exception as e:
            print(f"[retention] FATAL: {e}", file=sys.stderr, flush=True)
        time.sleep(24 * 60 * 60)


threading.Thread(target=_retention_loop, daemon=True).start()


# Local testing only — the Docker image runs this under gunicorn instead
# (see Dockerfile.scanner), which imports `app` directly and never hits
# __main__. The scheduler thread above starts either way, since it's at
# module level, not inside this block.
if __name__ == "__main__":
    port = int(os.environ.get("SCANNER_PORT", "9100"))
    app.run(host="0.0.0.0", port=port)

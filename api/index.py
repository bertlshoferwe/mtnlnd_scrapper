"""
Web dashboard for Bid Scout — self-hosted Docker + Supabase edition.

This Flask app itself does NOT run the scan or schedule anything — that
lives in scanner_service.py, a separate always-on container reachable at
SCANNER_URL. This app only reads/writes Supabase and, for "Run Now"/
"Cancel scan", asks the scanner service to start/stop scraper.py.

Routes:
  GET  /                                    the dashboard page
  GET  /vendor/<path>                       bundled front-end assets (pdf.js)
  GET  /api/adapters                        list available portal adapters
  GET  /api/divisions                       list divisions
  POST /api/divisions                       create a division
  PATCH /api/divisions/<division_id>        rename a division
  DEL  /api/divisions/<division_id>          delete a division (cascades in Supabase)
  GET  /api/<division_id>/sites              list sites
  POST /api/<division_id>/sites              add a site
  PATCH /api/<division_id>/sites/<site_id>   edit a site (name, url, adapter, selector/pattern)
  POST /api/<division_id>/sites/<site_id>/active   toggle whether the site is scanned
  POST /api/<division_id>/sites/reorder      persist a drag-to-reorder (also the scan order)
  DEL  /api/<division_id>/sites/<site_id>     remove a site
  GET  /api/<division_id>/keywords           list keywords
  POST /api/<division_id>/keywords           add a keyword
  POST /api/<division_id>/keywords/upload     bulk-add keywords from an uploaded .pdf/.docx (one per line)
  DEL  /api/<division_id>/keywords            remove one keyword (?keyword=…) or all of them
  POST /api/<division_id>/keywords/cluster    suggest merge groups for near-duplicate keywords (review only)
  POST /api/<division_id>/keywords/merge      apply a set of approved merge groups from /keywords/cluster
  GET  /api/<division_id>/status             latest run status
  POST /api/<division_id>/cancel-scan        cancel the running scan
  POST /api/<division_id>/run-now            tell the local scan worker to start now
  GET  /api/<division_id>/results-info       stats + latest run_date, for the Results card
  GET  /api/<division_id>/results-grouped    results collapsed to one entry per project, files nested (search, status, site, keyword, bid_window, sort, include_closed, updated, new, view=review|jira|not_fit, project=<key>)
  POST /api/<division_id>/projects/decision  move a project to Jira / Not a fit / back to review
  POST /api/<division_id>/projects/jira      file a project as a Jira issue (JIRA_* env vars), and as a bid on the workflow board
  POST /api/<division_id>/projects/jira/verify   check the filed issue still exists in Jira; clears jira_key if it was deleted
  GET  /api/<division_id>/projects/bid-info  a project's site, bid time, files and plan holders, for the bid board's bid window
  GET  /api/<division_id>/bids/<bid_id>/zip    one tab of a bid board bid (?tab=docs|att) as a zip
  GET  /api/<division_id>/results             paginated/filterable rows (search, status, page, page_size) for the Results table
  GET  /download/<division_id>/results        build and stream an .xlsx on the fly from Supabase rows
  GET  /workflow/                             the bid workflow board (workflow/index.html) + its config.js

Every route that touches Supabase or the scan worker does one or two quick
network calls and returns almost immediately — the actual scanning work
(which can easily take minutes) happens in scanner_service.py's own process,
not here.
"""

import os
import io
import difflib
import json
import re
import requests
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# A scan_runs row can be left showing status='running' forever if the scanner
# container's scraper.py subprocess is killed without warning (OOM, host
# restart, docker kill, a Watchtower image update landing mid-scan — this
# last one bit a real run 2026-10-04) — _scan_division's finish_run() never
# executes. Any "running" row older than this is definitely dead, not slow;
# the status endpoint reports those as a failed run instead of a live one so
# the dashboard stops showing a phantom "Checking …" indefinitely. Must stay
# comfortably above scraper.py's SCAN_DIVISION_BUDGET_S (raised to 8hrs
# 2026-10-04 for ConstructConnect's own much bigger per-run budget) or this
# would flag a run as stale while it's still legitimately working.
STALE_RUN_AFTER = timedelta(minutes=600)

from flask import Flask, jsonify, request, render_template, send_file, send_from_directory, abort, Response, redirect

# openpyxl / pypdf / python-docx are only needed by the xlsx download and
# keyword-upload routes, so they're imported lazily inside those functions
# instead of here — every other request (including the dashboard's frequent
# status polling) would otherwise pay to load all three on every cold start.

import supabase_store
import adapters
import credentials

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(_REPO_ROOT, "templates")
VENDOR_DIR = os.path.join(_REPO_ROOT, "vendor")
WORKFLOW_DIR = os.path.join(_REPO_ROOT, "workflow")
app = Flask(__name__, template_folder=TEMPLATE_DIR)

COLUMN_HEADERS = [
    "Date", "Site", "Bid Opening", "Filename",
    "Matched Keywords", "Match Count", "AI Notes",
]

# This is the actual schedule the scanner container runs on (see
# scanner_service.py) — also shown in the dashboard so it's not a mystery
# where/when scans run. Set via self-host/.env; the app and scanner read the
# same env var, so there's only one place to change it.
DISPLAY_SCHEDULE_UTC = os.environ.get("DISPLAY_SCHEDULE_UTC") or "06:41 UTC"


def _schedule_hm(text):
    """Pull an "HH:MM" (24h, UTC) out of DISPLAY_SCHEDULE_UTC so the page can
    re-render it in the visitor's local time. Returns None if there's no
    parseable time, in which case the page just shows the raw string."""
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mn < 60:
        return f"{h:02d}:{mn:02d}"
    return None


SCHEDULE_UTC_HM = _schedule_hm(DISPLAY_SCHEDULE_UTC)

# "Add to Jira" button — files a project as an issue via the Jira Cloud REST
# API. JIRA_BASE_URL is the site root (e.g. https://yourteam.atlassian.net),
# JIRA_EMAIL + JIRA_API_TOKEN are Basic-auth credentials for a Jira Cloud API
# token (https://id.atlassian.com/manage-profile/security/api-tokens), and
# JIRA_PROJECT_KEY is the default target project's short key (e.g. "GEO") —
# a division can file somewhere else via its jira_project_key setting. New
# issues land wherever the project's workflow puts a freshly-created issue
# (its board's leftmost column) — nothing here sets status explicitly.
# Stripped (and the key upper-cased) so stray spaces or "geo" in the env
# file still work.
JIRA_BASE_URL = (os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/")
JIRA_EMAIL = (os.environ.get("JIRA_EMAIL") or "").strip()
JIRA_API_TOKEN = (os.environ.get("JIRA_API_TOKEN") or "").strip()
JIRA_PROJECT_KEY = (os.environ.get("JIRA_PROJECT_KEY") or "").strip().upper()
JIRA_ISSUE_TYPE = (os.environ.get("JIRA_ISSUE_TYPE") or "").strip() or "Task"

# The bid workflow board (workflow/index.html), served at /workflow/. Unlike
# the dashboard it talks to Supabase straight from the browser, so it needs
# the address browsers use for Supabase (not the internal http://kong:8000)
# and the anon key. Unset = the board shows a "not configured" message.
WORKFLOW_SUPABASE_URL = (os.environ.get("WORKFLOW_SUPABASE_URL") or "").rstrip("/")
WORKFLOW_SUPABASE_ANON_KEY = os.environ.get("WORKFLOW_SUPABASE_ANON_KEY") or ""


def _require_division(division_id):
    division = supabase_store.get_division(division_id)
    if division is None:
        return None, (jsonify({"error": f"no such division '{division_id}'"}), 404)
    return division, None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        display_schedule=DISPLAY_SCHEDULE_UTC,
        schedule_utc_hm=SCHEDULE_UTC_HM or "",
    )


@app.route("/vendor/<path:filename>")
def vendor_asset(filename):
    """Serve third-party front-end assets (pdf.js) bundled with the repo.
    Done through Flask rather than Vercel's static handling so it works
    regardless of how the project routes the root path. .mjs needs an explicit
    JS mimetype or browsers refuse to load it as a module."""
    resp = send_from_directory(VENDOR_DIR, filename, max_age=31536000)
    if filename.endswith(".mjs"):
        resp.headers["Content-Type"] = "text/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------------------
# Workflow board
# ---------------------------------------------------------------------------

@app.route("/workflow")
def workflow_redirect():
    # The page loads config.js and vendor/supabase.js by relative path, so it
    # needs the trailing slash. Keep ?bid= / ?division= across the redirect.
    qs = request.query_string.decode()
    return redirect("/workflow/" + ("?" + qs if qs else ""), code=301)


@app.route("/workflow/")
def workflow_index():
    # The config goes straight into the page rather than through config.js:
    # Cloudflare caches .js files, and once cached a 404 from the proxy while
    # the container was restarting for an update, leaving the board saying
    # "The app isn't configured" for hours. HTML isn't cached.
    with open(os.path.join(WORKFLOW_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    page = page.replace('<script src="config.js"></script>',
                        "<script>" + _workflow_config_js() + "</script>", 1)
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _workflow_config_js():
    cfg = {
        "SUPABASE_URL": WORKFLOW_SUPABASE_URL,
        "SUPABASE_ANON_KEY": WORKFLOW_SUPABASE_ANON_KEY,
        # A view over `divisions` (id, name only) — see workflow/sql/002_bidscout.sql.
        "DIVISIONS_TABLE": "wf_divisions",
        "DIVISION_ID_COLUMN": "id",
        "DIVISION_NAME_COLUMN": "name",
    }
    # </ can't appear inside an inline <script>
    return "window.APP_CONFIG = " + json.dumps(cfg).replace("</", "<\\/") + ";\n"


@app.route("/workflow/config.js")
def workflow_config():
    # Kept for anything still asking for it; the page itself has the config inline.
    resp = Response(_workflow_config_js(), mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/workflow/vendor/<path:filename>")
def workflow_vendor(filename):
    return send_from_directory(os.path.join(WORKFLOW_DIR, "vendor"), filename, max_age=86400)


# ---------------------------------------------------------------------------
# Divisions
# ---------------------------------------------------------------------------

@app.route("/api/divisions", methods=["GET"])
def api_get_divisions():
    return jsonify(supabase_store.load_divisions())


@app.route("/api/divisions", methods=["POST"])
def api_create_division():
    data = request.get_json(force=True, silent=True) or {}
    try:
        division = supabase_store.create_division(data.get("name", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "division": division})


@app.route("/api/divisions/<division_id>", methods=["PATCH"])
def api_rename_division(division_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        division = supabase_store.rename_division(division_id, data.get("name", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), (404 if "not found" in str(e) else 400)
    return jsonify({"ok": True, "division": division})


@app.route("/api/divisions/<division_id>", methods=["DELETE"])
def api_delete_division(division_id):
    try:
        supabase_store.delete_division(division_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

@app.route("/api/<division_id>/sites", methods=["GET"])
def api_get_sites(division_id):
    _, err = _require_division(division_id)
    if err:
        return err
    return jsonify([_public_site(s) for s in supabase_store.load_sites(division_id)])


def _public_site(site):
    """Strip a site row of its encrypted login password before it goes to
    the browser — the dashboard only needs to know a login is configured
    (and show the username/login URL back in the edit form), never the
    encrypted secret itself."""
    site = dict(site)
    site.pop("login_password_enc", None)
    site["has_login"] = bool(site.get("login_username"))
    return site


def _parse_site_payload(data):
    """Turn the add/edit form's JSON into (name, url, listing, adapter,
    login) or raise ValueError with a user-facing message. Same rules for
    both routes so a site can be edited into any strategy it could be
    created in.

    `login` is None (no login / login removed) or {"username", "password",
    "url"} with the password in plaintext — encrypting it and deciding
    whether a blank password means "unchanged" (edit) or is an error
    (create) is the caller's job, since that differs by route."""
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    adapter = (data.get("adapter") or "").strip()
    if adapter and adapter not in adapters.ADAPTERS:
        raise ValueError(f"unknown adapter '{adapter}'")
    if not name:
        raise ValueError("name is required")
    if not url and not adapter:
        raise ValueError("url is required (unless a portal adapter is selected)")

    # A URL that matches a known portal's host gets that adapter attached
    # automatically — no need to hunt it down in the dropdown. An explicit
    # choice (including deliberately picking "None") always wins over this.
    if not adapter and url:
        matched = adapters.match_adapter_class(url)
        if matched:
            adapter = matched.key

    # Adapter set -> the adapter knows where to look, listing/pattern ignored.
    # No adapter and no manual selector/pattern -> the generic browser crawler
    # discovers the plan links itself (the "Let AI find the plan links" default).
    # A selector/pattern switches to the targeted listing crawl instead.
    listing = None
    if not adapter and (data.get("link_selector") or data.get("link_pattern")):
        listing = {}
        if data.get("link_selector"):
            listing["link_selector"] = data["link_selector"].strip()
        if data.get("link_pattern"):
            listing["link_pattern"] = data["link_pattern"].strip()

    login_username = (data.get("login_username") or "").strip()
    login = None
    if login_username:
        login = {
            "username": login_username,
            "password": data.get("login_password") or "",
            "url": (data.get("login_url") or "").strip() or None,
        }

    return name, url, listing, (adapter or None), login


@app.route("/api/<division_id>/sites", methods=["POST"])
def api_add_site(division_id):
    _, err = _require_division(division_id)
    if err:
        return err

    try:
        name, url, listing, adapter, login = _parse_site_payload(
            request.get_json(force=True, silent=True) or {}
        )
        if login and not login["password"]:
            raise ValueError("a password is required when a login username is given")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    login_row = None
    if login:
        login_row = {
            "username": login["username"],
            "password_enc": credentials.encrypt_password(login["password"]),
            "url": login["url"],
        }

    site = supabase_store.add_site(
        division_id, name, url, listing=listing, adapter=adapter, login=login_row
    )
    return jsonify({"ok": True, "site": _public_site(site)})


@app.route("/api/<division_id>/sites/<int:site_id>", methods=["PATCH"])
def api_update_site(division_id, site_id):
    _, err = _require_division(division_id)
    if err:
        return err

    try:
        name, url, listing, adapter, login = _parse_site_payload(
            request.get_json(force=True, silent=True) or {}
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # No username -> login removed entirely. A username with a blank
    # password means "keep the saved password" (the edit form never gets it
    # back to re-submit), so login_password_enc is left out of the patch.
    # Either way, a changed/removed credential invalidates whatever the last
    # scan found — clear it rather than show a stale result for the old
    # password.
    if login is None:
        login_patch = {
            "login_username": None, "login_password_enc": None, "login_url": None,
            "login_last_ok": None, "login_last_checked_at": None, "login_last_error": None,
        }
    else:
        login_patch = {"login_username": login["username"], "login_url": login["url"]}
        if login["password"]:
            login_patch["login_password_enc"] = credentials.encrypt_password(login["password"])
            login_patch["login_last_ok"] = None
            login_patch["login_last_checked_at"] = None
            login_patch["login_last_error"] = None

    try:
        site = supabase_store.update_site(
            division_id, site_id, name, url, listing=listing, adapter=adapter,
            login_patch=login_patch,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True, "site": _public_site(site)})


@app.route("/api/<division_id>/sites/<int:site_id>/active", methods=["POST"])
def api_set_site_active(division_id, site_id):
    """Toggle whether a site is scanned. Body: {"active": true|false}."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    active = bool(data.get("active"))
    try:
        site = supabase_store.set_site_active(division_id, site_id, active)
    except ValueError as e:
        msg = str(e)
        return jsonify({"error": msg}), (404 if msg == "site not found" else 400)
    return jsonify({"ok": True, "site": _public_site(site)})


@app.route("/api/<division_id>/sites/reorder", methods=["POST"])
def api_reorder_sites(division_id):
    """Persists a drag-to-reorder from the dashboard — also the order sites
    are scanned in (see scraper.py). Body: {"order": [site_id, ...]},
    every site id for this division, top to bottom."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order is required"}), 400
    try:
        supabase_store.reorder_sites(division_id, order)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/adapters", methods=["GET"])
def api_list_adapters():
    return jsonify(adapters.list_adapters())


@app.route("/api/<division_id>/sites/<int:site_id>", methods=["DELETE"])
def api_delete_site(division_id, site_id):
    _, err = _require_division(division_id)
    if err:
        return err
    try:
        supabase_store.delete_site(division_id, site_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

@app.route("/api/<division_id>/keywords", methods=["GET"])
def api_get_keywords(division_id):
    _, err = _require_division(division_id)
    if err:
        return err
    return jsonify(supabase_store.load_keywords(division_id))


@app.route("/api/<division_id>/keywords", methods=["POST"])
def api_add_keyword(division_id):
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    kw = (data.get("keyword") or "").strip()
    if not kw:
        return jsonify({"error": "keyword is required"}), 400
    try:
        keywords = supabase_store.add_keyword(division_id, kw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "keywords": keywords})


MAX_KEYWORD_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_KEYWORD_LENGTH = 120  # skip lines longer than this — likely a sentence/paragraph, not a keyword


def _extract_text_for_keywords(file_bytes, filename):
    """Return the plain text of an uploaded .pdf or .docx, or None for
    anything else. Uses pypdf (not pdfplumber) since this only needs plain
    text, not per-page layout — keeps the Vercel function bundle smaller."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    elif lower.endswith(".docx"):
        from docx import Document as DocxDocument
        doc = DocxDocument(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)
    return None


def _lines_to_keywords(text):
    """Turn extracted text into candidate keywords: one per line, trimmed of
    bullets/numbering, blank lines skipped, and anything too long to
    plausibly be a keyword/phrase (rather than a full sentence) dropped."""
    candidates = []
    for line in text.splitlines():
        line = supabase_store.demojibake(line)
        line = line.strip(" \t\u2022\u2023\u25e6\u2043\u2219-–—.").strip()
        line = re.sub(r"^\d+[\.\)]\s*", "", line)  # strip leading "1. " / "2) " numbering
        line = line.strip("*_`").strip()           # strip Markdown emphasis / code ticks
        line = line.rstrip(":").strip()            # strip a trailing "Label:" colon
        if not line or len(line) > MAX_KEYWORD_LENGTH:
            continue
        if supabase_store.looks_like_section_heading(line) or _looks_like_prose(line):
            continue
        candidates.append(line)
    return candidates


def _looks_like_prose(line):
    """Best-effort filter for the explanatory sentences that sit between the
    lists in a structured term document ('These phrases appear in plan
    notes...', 'The most widely specified chamber system...'). Errs toward
    keeping: anything with a brand mark or a parenthetical is left alone."""
    low = line.lower()
    if low.startswith((
        "these ", "the most ", "phrases ", "short strings", "short phrases",
        "compiled for ", "panels are ", "vegetated mse", "sources:",
    )):
        return True
    if "®" in line or "™" in line or "(" in line:  # (R), TM
        return False
    words = line.split()
    return len(words) >= 8 and (
        line.endswith(".") or ". " in line or ";" in line or line.count(",") >= 2
    )


@app.route("/api/<division_id>/keywords/upload", methods=["POST"])
def api_upload_keywords(division_id):
    """Add keywords in bulk from an uploaded .pdf or .docx — one keyword per
    line in the document. Meant for a document that's just a list of terms
    (e.g. a compliance checklist), not for extracting keywords out of prose."""
    _, err = _require_division(division_id)
    if err:
        return err

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "choose a .pdf or .docx file first"}), 400

    upload = request.files["file"]
    filename = upload.filename
    file_bytes = upload.read()
    if len(file_bytes) > MAX_KEYWORD_UPLOAD_BYTES:
        return jsonify({"error": "file is too large (10 MB max)"}), 400

    if not filename.lower().endswith((".pdf", ".docx")):
        return jsonify({"error": "only .pdf and .docx files are supported"}), 400

    try:
        text = _extract_text_for_keywords(file_bytes, filename)
    except Exception as e:
        return jsonify({"error": f"couldn't read that file: {e}"}), 400

    if not text or not text.strip():
        return jsonify({"error": "no readable text found in that file"}), 400

    candidates = _lines_to_keywords(text)
    if not candidates:
        return jsonify({"error": "no usable keyword lines found — each keyword should be on its own line"}), 400

    keywords, added, skipped = supabase_store.add_keywords_bulk(division_id, candidates)
    return jsonify({
        "ok": True,
        "keywords": keywords,
        "added": added,
        "skipped_present": skipped["already_present"],
        "skipped_repeat": skipped["repeated_in_file"],
    })


@app.route("/api/<division_id>/keywords", methods=["DELETE"])
def api_delete_keywords(division_id):
    """?keyword=<term> removes that one keyword; with no query param, clears
    every keyword for the division. The term goes in the query string, not
    the path, so slashes / punctuation / ® ™ in keywords survive the round
    trip (a <path:> segment mangles those on some hosts)."""
    _, err = _require_division(division_id)
    if err:
        return err
    keyword = request.args.get("keyword")
    if keyword is not None:
        keywords = supabase_store.delete_keyword(division_id, keyword)
    else:
        keywords = supabase_store.clear_keywords(division_id)
    return jsonify({"ok": True, "keywords": keywords})


@app.route("/api/<division_id>/keywords/cluster", methods=["POST"])
def api_cluster_keywords(division_id):
    """Review-only: groups likely-redundant keywords and suggests a
    canonical phrase for each group, for the dashboard's "Clean up with
    AI" tool. Does the actual embedding + AI work via the scanner
    container (scanner_service.py's /cluster-keywords — this app has none
    of the AI/embedding deps installed, same reason run-now proxies to it).
    Nothing is changed by this call; see /keywords/merge for that."""
    _, err = _require_division(division_id)
    if err:
        return err
    base, cfg_err = _scanner_url()
    if cfg_err:
        return cfg_err
    try:
        resp = requests.post(f"{base}/cluster-keywords", json={"division_id": division_id}, timeout=200)
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to reach the scan worker: {e}"}), 424
    if resp.status_code >= 300:
        detail = (resp.json().get("error")
                  if resp.headers.get("content-type", "").startswith("application/json")
                  else resp.text)
        return jsonify({"error": f"Scan worker error: {detail}"}), 424
    return jsonify(resp.json())


@app.route("/api/<division_id>/keywords/merge", methods=["POST"])
def api_merge_keywords(division_id):
    """Applies a set of user-approved groups from /keywords/cluster — adds
    each group's (possibly user-edited) canonical phrase, then removes the
    keywords it replaces. Pure reuse of the existing add/delete keyword
    functions; no new storage logic."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        return jsonify({"error": "groups is required"}), 400

    merged, removed = 0, 0
    keywords = supabase_store.load_keywords(division_id)
    for group in groups:
        if not isinstance(group, dict):
            continue
        keep = (group.get("keep") or "").strip()
        remove = [k for k in (group.get("remove") or []) if isinstance(k, str)]
        if not keep or not remove:
            continue
        try:
            keywords = supabase_store.add_keyword(division_id, keep)
        except ValueError:
            pass  # looked like a section heading or was already blank — skip, still remove the rest
        merged += 1
        for kw in remove:
            if kw.lower() == keep.lower():
                continue  # the kept phrase might be one of the originals — don't delete what we just kept
            keywords = supabase_store.delete_keyword(division_id, kw)
            removed += 1

    return jsonify({"ok": True, "merged": merged, "removed": removed, "keywords": keywords})


# ---------------------------------------------------------------------------
# Status + Run Now (via GitHub Actions workflow_dispatch)
# ---------------------------------------------------------------------------

def _run_is_stale(run):
    """True if a still-'running' scan_runs row is old enough that its
    scanner-container process cannot still be alive (see STALE_RUN_AFTER)."""
    started = run.get("started_at")
    if not started:
        return False
    try:
        ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts > STALE_RUN_AFTER


@app.route("/api/<division_id>/status", methods=["GET"])
def api_status(division_id):
    _, err = _require_division(division_id)
    if err:
        return err
    run = supabase_store.get_latest_run(division_id)
    if run is None:
        return jsonify({"running": False, "last_started": None, "last_finished": None, "last_result": None})

    running = run["status"] == "running"
    if running and _run_is_stale(run):
        # The owning job is long gone — surface it as a failed run and mark the
        # row so it doesn't keep tripping this check (and so "Run now" isn't
        # blocked by a scan that will never finish).
        running = False
        supabase_store.finish_run(run["id"], "error: timed out")
        run = {**run, "status": "error: timed out",
               "finished_at": datetime.now(timezone.utc).isoformat()}

    return jsonify({
        "running": running,
        "last_started": run["started_at"],
        "last_finished": run["finished_at"],
        "last_result": None if running else run["status"],
        "progress_done": run.get("progress_done") or 0,
        "progress_total": run.get("progress_total") or 0,
        "progress_label": run.get("progress_label") or "",
        "progress_site_i": run.get("progress_site_i") or 0,
        "progress_site_n": run.get("progress_site_n") or 0,
        "progress_overall": run.get("progress_overall") or 0,
    })


def _scanner_url():
    url = os.environ.get("SCANNER_URL")
    if not url:
        return None, (jsonify({
            "error": "SCANNER_URL is not configured — can't reach the local scan worker. See README.md."
        }), 500)
    return url.rstrip("/"), None


@app.route("/api/<division_id>/run-now", methods=["POST"])
def api_run_now(division_id):
    _, err = _require_division(division_id)
    if err:
        return err

    base, cfg_err = _scanner_url()
    if cfg_err:
        return cfg_err
    try:
        resp = requests.post(f"{base}/run-now", json={"division_id": division_id}, timeout=8)
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to reach the scan worker: {e}"}), 424

    if resp.status_code < 300:
        return jsonify({"ok": True})
    if resp.status_code == 409:
        return jsonify({"error": "A scan is already running."}), 409
    return jsonify({"error": f"Scan worker error {resp.status_code}: {resp.text}"}), 424


@app.route("/api/<division_id>/cancel-scan", methods=["POST"])
def api_cancel_scan(division_id):
    _, err = _require_division(division_id)
    if err:
        return err

    # Mark this division's running scan as cancelled so the dashboard clears
    # even if the scan worker call below fails or the process is already gone.
    run = supabase_store.get_latest_run(division_id)
    if run and run.get("status") == "running":
        supabase_store.finish_run(run["id"], "cancelled")

    base, cfg_err = _scanner_url()
    if cfg_err:
        return cfg_err
    try:
        requests.post(f"{base}/cancel", timeout=8)
    except requests.RequestException as e:
        return jsonify({"ok": True,
                        "warning": f"marked cancelled, but reaching the scan worker failed: {e}"})

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@app.route("/api/<division_id>/results-info", methods=["GET"])
def api_results_info(division_id):
    _, err = _require_division(division_id)
    if err:
        return err
    rows = supabase_store.get_scan_results(division_id, limit=1)
    if not rows:
        return jsonify({"exists": False})
    stats = supabase_store.get_stats(division_id)
    return jsonify({
        "exists": True,
        "latest_run_date": rows[0]["run_date"],
        "stats": stats,
    })


@app.route("/api/<division_id>/results", methods=["GET"])
def api_get_results(division_id):
    """Paginated, filterable rows for the dashboard's Results table.
    Query params: search, status, page (1-based), page_size (max 100)."""
    _, err = _require_division(division_id)
    if err:
        return err

    search = (request.args.get("search") or "").strip()
    status = (request.args.get("status") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, min(100, int(request.args.get("page_size", 20))))
    except ValueError:
        return jsonify({"error": "page and page_size must be integers"}), 400

    rows, total = supabase_store.get_scan_results_page(
        division_id, search=search or None, status=status or None,
        page=page, page_size=page_size,
    )
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})


@app.route("/api/<division_id>/results-grouped", methods=["GET"])
def api_get_results_grouped(division_id):
    """Results collapsed to one entry per project, files nested. Query params:
    search, status, page (1-based), page_size."""
    _, err = _require_division(division_id)
    if err:
        return err
    search = (request.args.get("search") or "").strip()
    status = (request.args.get("status") or "").strip()
    site = (request.args.get("site") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    bid_window = (request.args.get("bid_window") or "").strip()
    sort = (request.args.get("sort") or "").strip()
    include_closed = request.args.get("include_closed") in ("1", "true")
    updated_only = request.args.get("updated") in ("1", "true")
    new_only = request.args.get("new") in ("1", "true")
    # One project by key — for a ?project= link back from the workflow board.
    project_key = (request.args.get("project") or "").strip()
    # The dashboard's folder tabs: "review" (no decision yet, or "Not a fit"
    # with a newer matching document), "jira", "not_fit". Omitted = all.
    # The old "active"/"done" values still map across for a stale page.
    view = (request.args.get("view") or "").strip()
    view = {"active": "review", "done": "not_fit"}.get(view, view)
    if view not in ("review", "jira", "not_fit"):
        view = None
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, min(100, int(request.args.get("page_size", 15))))
    except ValueError:
        return jsonify({"error": "page and page_size must be integers"}), 400

    projects, total, sites, updated_total, new_total, view_counts = supabase_store.get_results_grouped(
        division_id, search=search or None, status=status or None, site=site or None,
        keyword=keyword or None, bid_window=bid_window or None, sort=sort or None,
        include_closed=include_closed, updated_only=updated_only, new_only=new_only,
        view=view, project_key=project_key or None, page=page, page_size=page_size,
    )
    return jsonify({"projects": projects, "total": total, "sites": sites,
                    "updated_total": updated_total, "new_total": new_total,
                    "view_counts": view_counts,
                    "not_fit_reasons": list(supabase_store.NOT_FIT_REASONS),
                    "fit_training": supabase_store.fit_training_stats(division_id),
                    "page": page, "page_size": page_size})


@app.route("/api/<division_id>/projects/decision", methods=["POST"])
def api_set_project_decision(division_id):
    """Move a project between tabs. Body: {"project_key", "decision":
    "not_fit" | "review" | "jira" | "duplicate", "reason", "note",
    "duplicate_of"}. "not_fit" needs one of the one-click reasons; "jira" is
    only for a project that already has a Jira issue (filing a new one goes
    through /projects/jira, which sets this itself); "duplicate" needs
    duplicate_of, the original project's key; "review" clears the decision."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("project_key") or "").strip()
    decision = (data.get("decision") or "").strip()
    reason = (data.get("reason") or "").strip() or None
    note = (data.get("note") or "").strip()[:1000] or None
    if not key:
        return jsonify({"error": "project_key is required"}), 400
    duplicate_of = (data.get("duplicate_of") or "").strip() or None
    if decision not in ("not_fit", "review", "jira", "duplicate"):
        return jsonify({"error": "decision must be not_fit, review, jira or duplicate"}), 400
    if decision == "duplicate" and (not duplicate_of or duplicate_of == key):
        return jsonify({"error": "pick the project this one duplicates"}), 400
    if decision == "not_fit" and reason not in supabase_store.NOT_FIT_REASONS:
        return jsonify({"error": "pick a reason"}), 400
    if (decision == "jira" and not supabase_store.load_project_jira_keys(division_id).get(key)
            and not supabase_store.board_links(division_id).get(key)):
        return jsonify({"error": "this project hasn't been added to Jira"}), 400
    try:
        supabase_store.set_project_decision(
            division_id, key, None if decision == "review" else decision, reason, note,
            duplicate_of=duplicate_of)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/<division_id>/projects/duplicate-candidates", methods=["GET"])
def api_duplicate_candidates(division_id):
    """Projects that ?key= might be a duplicate of, most similar name first:
    {"projects": [{"key", "project", "source_prefix", "bid_date", "bucket"}]}
    — every one, small, so the picker searches them in the browser. Skips
    the project itself and other duplicates."""
    _, err = _require_division(division_id)
    if err:
        return err
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    projects = supabase_store.get_results_grouped(division_id, page_size=None)[0]
    _, name = supabase_store._split_site(key)
    name = name.lower()
    out = []
    for p in projects:
        if p["key"] == key or p.get("duplicate_of"):
            continue
        score = difflib.SequenceMatcher(None, name, p["project"].lower()).ratio()
        out.append((score, p))
    out.sort(key=lambda x: -x[0])
    return jsonify({"projects": [
        {"key": p["key"], "project": p["project"], "source_prefix": p["source_prefix"],
         "bid_date": p["bid_date"], "bucket": p["bucket"]}
        for _, p in out]})


@app.route("/api/<division_id>/settings", methods=["GET"])
def api_get_settings(division_id):
    """The division's settings (hidden settings window), with defaults filled in."""
    _, err = _require_division(division_id)
    if err:
        return err
    return jsonify({"settings": supabase_store.get_division_settings(division_id),
                    "fit_training": supabase_store.fit_training_stats(division_id),
                    "jira_default_project": JIRA_PROJECT_KEY})


@app.route("/api/<division_id>/settings", methods=["PATCH"])
def api_save_settings(division_id):
    """Body: any of {"fit_enabled", "fit_min_decisions", "fit_sure_at",
    "jira_project_key"}."""
    _, err = _require_division(division_id)
    if err:
        return err
    try:
        saved = supabase_store.save_division_settings(
            division_id, request.get_json(force=True, silent=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"settings": saved, "fit_training": supabase_store.fit_training_stats(division_id)})


@app.route("/api/<division_id>/projects/bid-info", methods=["GET"])
def api_project_bid_info(division_id):
    """What the bid board's bid window shows from the Bid Scout project it
    came from. ?key=<project_key>. Returns {"site", "bid_date", "due_time"
    ("HH:MM" or null), "team" (team name or null), "files": [...], "holders",
    "holders_refreshed_at"} —
    files open through /api/<division_id>/pdf, holders as in plan-holders."""
    _, err = _require_division(division_id)
    if err:
        return err
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    site, _ = supabase_store._split_site(key)
    holders, refreshed_at = supabase_store.load_project_plan_holders(division_id, key)
    return jsonify({
        "site": site,
        "bid_date": supabase_store.load_project_bid_dates(division_id).get(key),
        "due_time": supabase_store.bid_time_hm(supabase_store.load_project_bid_times(division_id).get(key)),
        "team": supabase_store.project_team_name(division_id, key),
        "files": supabase_store.load_project_files(division_id, key),
        "holders": holders, "holders_refreshed_at": refreshed_at,
    })


@app.route("/api/<division_id>/projects/plan-holders", methods=["GET"])
def api_project_plan_holders(division_id):
    """The project window's Plan Holders tab. ?key=<project_key>. Returns
    {"holders": [...] or null, "refreshed_at": iso or null} — null holders
    means this project's site doesn't publish a list (or it hasn't been
    fetched yet), [] means the list exists but is empty."""
    _, err = _require_division(division_id)
    if err:
        return err
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    holders, refreshed_at = supabase_store.load_project_plan_holders(division_id, key)
    return jsonify({"holders": holders, "refreshed_at": refreshed_at})


@app.route("/api/<division_id>/projects/jira", methods=["POST"])
def api_add_project_to_jira(division_id):
    """File a project as a Jira issue. Body: {"project_key", "summary",
    "due_date"}. project_key is the internal scan_results.site value (used
    to remember the created issue); summary/due_date become the issue's
    summary/due date. Returns the created issue's key + browse URL."""
    _, err = _require_division(division_id)
    if err:
        return err
    if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN):
        return jsonify({"error": "Jira isn't configured — set JIRA_BASE_URL, "
                                  "JIRA_EMAIL and JIRA_API_TOKEN."}), 400
    jira_project = (supabase_store.get_division_settings(division_id).get("jira_project_key")
                    or JIRA_PROJECT_KEY)
    if not jira_project:
        return jsonify({"error": "No Jira project is picked for this tab yet — "
                                  "set one in the settings window."}), 400
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("project_key") or "").strip()
    summary = (data.get("summary") or "").strip()
    if not key or not summary:
        return jsonify({"error": "project_key and summary are required"}), 400
    fields = {
        "project": {"key": jira_project},
        "summary": summary,
        "issuetype": {"name": JIRA_ISSUE_TYPE},
    }
    due_date = (data.get("due_date") or "").strip()
    if due_date:
        fields["duedate"] = due_date
    # Already filed once (its board bid was deleted since, or it predates the
    # board): reuse that issue rather than file a second one in Jira.
    existing = supabase_store.load_project_jira_keys(division_id).get(key)
    if existing:
        try:
            supabase_store.set_project_decision(division_id, key, "jira")
        except Exception as e:
            print(f"  ! couldn't record the Jira decision for {key} ({e})")
        return _add_to_board(division_id, key, summary, due_date, existing)
    try:
        resp = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue",
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            json={"fields": fields},
            timeout=15,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"couldn't reach Jira: {e}"}), 424
    if not resp.ok:
        detail = resp.text
        try:
            body = resp.json()
            detail = "; ".join(body.get("errorMessages") or []) or str(body.get("errors") or body)
        except Exception:
            pass
        # 424, not 502: Cloudflare swaps a 502 for its own page, dropping
        # this message — the dashboard would only say "Request failed (502)".
        print(f"  ! Jira refused {key} ({resp.status_code}): {detail}")
        return jsonify({"error": f"Jira error: {detail}"}), 424
    issue_key = resp.json().get("key")
    try:
        supabase_store.set_project_jira_key(division_id, key, issue_key)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Filing it is the decision — moves it to the Jira tab.
    try:
        supabase_store.set_project_decision(division_id, key, "jira")
    except ValueError as e:
        print(f"  ! couldn't record the Jira decision for {key} ({e})")
    return _add_to_board(division_id, key, summary, due_date, issue_key)


def _add_to_board(division_id, key, summary, due_date, issue_key):
    out = {"ok": True, "jira_key": issue_key, "jira_url": f"{JIRA_BASE_URL}/browse/{issue_key}"}
    # Running side by side with Jira for now: the same project also becomes a
    # bid on the workflow board. Never blocks the Jira filing — a failure just
    # comes back as workflow_error for the dashboard to mention. Skipped until
    # the board is configured (WORKFLOW_SUPABASE_URL), i.e. its SQL has been run.
    if not WORKFLOW_SUPABASE_URL:
        return jsonify(out)
    try:
        back = request.host_url.rstrip("/") + "/?" + urlencode({"division": division_id, "project": key})
        bid_id = supabase_store.create_workflow_bid(
            division_id, key, summary, due=due_date or None, source_url=back)
        out["workflow_url"] = f"/workflow/?bid={bid_id}"
        out["board_bid_id"] = bid_id
    except Exception as e:
        print(f"  ! couldn't add {key} to the workflow board ({e})")
        out["workflow_error"] = str(e)
    return jsonify(out)


@app.route("/api/<division_id>/projects/board-candidates", methods=["GET"])
def api_board_candidates(division_id):
    """Bids typed in by hand on the board that ?key= might already be, most
    similar title first: {"bids": [{"id", "key", "title", "due", "score"}]}
    (score 0-1). Every unlinked bid is listed, so the picker can search."""
    _, err = _require_division(division_id)
    if err:
        return err
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    if not WORKFLOW_SUPABASE_URL:
        return jsonify({"bids": []})
    name = supabase_store._split_site(key)[1].lower()
    try:
        bids = supabase_store.unlinked_workflow_bids(division_id)
    except Exception as e:
        return jsonify({"error": f"couldn't read the bid board: {e}"}), 424
    for b in bids:
        b["score"] = round(difflib.SequenceMatcher(None, name, (b["title"] or "").lower()).ratio(), 3)
    bids.sort(key=lambda b: -b["score"])
    return jsonify({"bids": bids})


@app.route("/api/<division_id>/projects/board-link", methods=["POST"])
def api_board_link(division_id):
    """Body {"project_key", "bid_id"}: connect a project to a bid already on
    the board (typed in by hand) instead of adding a new one, and update that
    bid from Bid Scout. No Jira issue is filed. The project moves to the
    Jira tab, like Add to Board."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("project_key") or "").strip()
    bid_id = (data.get("bid_id") or "").strip()
    if not key or not bid_id:
        return jsonify({"error": "project_key and bid_id are required"}), 400
    back = request.host_url.rstrip("/") + "/?" + urlencode({"division": division_id, "project": key})
    try:
        supabase_store.link_workflow_bid(division_id, bid_id, key, source_url=back)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"couldn't update the bid board: {e}"}), 424
    try:
        supabase_store.set_project_decision(division_id, key, "jira")
    except Exception as e:
        print(f"  ! couldn't record the board decision for {key} ({e})")
    return jsonify({"ok": True, "bid_id": bid_id})


@app.route("/api/<division_id>/projects/jira/verify", methods=["POST"])
def api_verify_project_jira(division_id):
    """Check whether a previously-filed Jira issue still exists (it may have
    been deleted on the Jira side since). Body: {"project_key", "jira_key"}.
    If Jira reports it gone (404), clears the stored jira_key so the
    dashboard's button reverts to "Add to Jira". Any other outcome (Jira
    unreachable, not configured, auth error) leaves the stored key alone —
    a transient failure here shouldn't make a real link disappear."""
    _, err = _require_division(division_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("project_key") or "").strip()
    jira_key = (data.get("jira_key") or "").strip()
    if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN) or not key or not jira_key:
        return jsonify({"exists": True})
    try:
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{jira_key}",
            params={"fields": "summary"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=10,
        )
    except requests.RequestException:
        return jsonify({"exists": True})
    if resp.status_code == 404:
        try:
            supabase_store.set_project_jira_key(division_id, key, None)
        except ValueError:
            pass
        # The issue is gone, so it's no longer "in Jira" — back to review.
        decision = (supabase_store.load_project_decisions(division_id).get(key) or {}).get("decision")
        if decision == "jira":
            try:
                supabase_store.set_project_decision(division_id, key, None, source="jira_deleted")
            except ValueError:
                pass
        return jsonify({"exists": False})
    return jsonify({"exists": True})


_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _site_group(site):
    """The site a result belongs to, for grouping into per-site sheets.
    scan_results.site is either "<site name> — <project>" or just the site
    name; either way the part before " — " (or the whole value) is the site."""
    parts = (site or "").split(" — ", 1)
    name = parts[0] if len(parts) == 2 else (site or "")
    return name.strip() or "Other"


def _unique_sheet_name(name, used):
    """A workbook-legal, <=31 char, unique sheet title."""
    base = _BAD_SHEET_CHARS.sub(" ", name).strip()[:31] or "Other"
    candidate, n = base, 2
    while candidate.lower() in used:
        tag = f" ({n})"
        candidate = base[:31 - len(tag)] + tag
        n += 1
    used.add(candidate.lower())
    return candidate


DATA_FONT_SIZE = 10
# Columns short enough that wrapping would only ever waste vertical space —
# everything else (Site, Filename, Matched Keywords, AI Notes) can genuinely
# run long and needs it. Keeps rows only as tall as their longest column
# actually forces.
NO_WRAP_COLUMNS = {1, 3, 6}  # Date, Bid Opening, Match Count


def _run_date_only(value):
    """'2026-09-19T11:30:15.043417+00:00' -> date(2026, 9, 19). A bare date
    (no time-of-day) reads far cleaner in a sheet meant to be skimmed, and
    matches how Bid Opening is already shown. Falls back to the raw string
    if it's not parseable ISO — never blanks a real value over a format
    quirk."""
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return value


def _write_matches_sheet(ws, rows, bid_dates):
    from openpyxl.styles import Font, Alignment, PatternFill
    job_band_fill = PatternFill("solid", fgColor="F2F2F2")
    header_fill = PatternFill("solid", fgColor="D9DEE8")

    ws.append(COLUMN_HEADERS)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True, size=11)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 20
    # Matched Keywords gets the lion's share of the extra room — it's the
    # column doing the most wrapping — and Match Count barely needs any.
    widths = [13, 26, 13, 32, 42, 12, 50]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    # Group every document under its job (the "Site" value is really
    # "<site> — <project>", i.e. the job) so the several documents a job can
    # have land on consecutive rows instead of scattered throughout the
    # sheet in scan order. Filename order within a job keeps that stable.
    rows = sorted(rows, key=lambda r: ((r.get("site") or "").lower(),
                                        (r.get("filename") or "").lower()))
    for r in rows:
        ws.append([
            _run_date_only(r["run_date"]), r["site"], bid_dates.get(r.get("site"), ""),
            r["filename"], r["matched_keywords"], r["match_count"], r["ai_notes"],
        ])
        row = ws[ws.max_row]
        for col, cell in enumerate(row, start=1):
            cell.font = Font(name="Arial", size=DATA_FONT_SIZE)
            cell.alignment = Alignment(wrap_text=col not in NO_WRAP_COLUMNS, vertical="top")
        row[0].number_format = "yyyy-mm-dd"

    if not rows:
        return

    # Merge the Site/Bid Opening columns down each job's block of rows (so a
    # job with several documents reads as one group instead of repeating its
    # name and bid date on every line) and shade every other job's block so
    # the boundary between jobs is obvious at a glance.
    job_start = 2
    band = False
    for row_idx in range(3, ws.max_row + 2):  # +1: a sentinel past the last row
        job = ws.cell(row=row_idx, column=2).value if row_idx <= ws.max_row else object()
        if job != ws.cell(row=job_start, column=2).value:
            if row_idx - 1 > job_start:
                for col in (2, 3):
                    ws.merge_cells(start_row=job_start, start_column=col,
                                    end_row=row_idx - 1, end_column=col)
                    ws.cell(row=job_start, column=col).alignment = \
                        Alignment(wrap_text=True, vertical="center")
            if band:
                for r2 in range(job_start, row_idx):
                    for c2 in range(1, len(COLUMN_HEADERS) + 1):
                        ws.cell(row=r2, column=c2).fill = job_band_fill
            band = not band
            job_start = row_idx


def _build_results_workbook(division_id, division_name):
    from openpyxl import Workbook

    # The download is a worklist — only documents that hit a keyword, and only
    # for projects the source site still lists. "No match" / "Download failed"
    # / closed projects stay visible in the dashboard but aren't exported.
    # One sheet per site.
    rows = supabase_store.get_scan_results(division_id, matches_only=True)
    closed = supabase_store.closed_project_keys(division_id)
    rows = [r for r in rows if r.get("site") not in closed]
    bid_dates = supabase_store.load_project_bid_dates(division_id)

    groups = {}
    for r in rows:
        groups.setdefault(_site_group(r.get("site")), []).append(r)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet

    if not groups:
        _write_matches_sheet(wb.create_sheet("Keyword Matches"), [], bid_dates)
    else:
        used = set()
        for name in sorted(groups, key=str.lower):
            _write_matches_sheet(wb.create_sheet(_unique_sheet_name(name, used)),
                                 groups[name], bid_dates)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


PROXY_MAX_BYTES = 50 * 1024 * 1024


@app.route("/api/<division_id>/pdf", methods=["GET"])
def api_proxy_pdf(division_id):
    """Stream a scanned document through this origin so the dashboard can show
    it in an <iframe> (cross-origin PDFs and X-Frame-Options make direct
    framing unreliable). `?download=1` forces a save instead of inline view.

    SSRF guard: only proxies a URL this division has actually logged in
    scan_results — never an arbitrary URL.

    Three sources of bytes: a stored document (storage_path set — browser-
    captured content with no real per-document URL, e.g. ConstructConnect;
    `url` here is just the synthetic dedup key used to look the row up, not
    something we ever fetch) is read from Supabase Storage; one whose cached
    copy expired (is_cached_document True, storage_path cleared by the
    scanner's retention sweep) triggers a live re-fetch through the scanner
    container first — slow (a real login + browser session), see
    scanner_service.py; everything else is fetched live from `url` itself,
    same as always.
    """
    _, err = _require_division(division_id)
    if err:
        return err

    target = request.args.get("url", "")
    if not target.startswith(("http://", "https://")):
        abort(400, description="bad url")
    doc = supabase_store.scanned_document_lookup(division_id).get(target)
    if doc is None:
        abort(403, description="not a document from this division's scans")

    disp = "attachment" if request.args.get("download") else "inline"

    if not doc["storage_path"] and doc.get("is_cached_document"):
        base, cfg_err = _scanner_url()
        if cfg_err:
            return cfg_err
        try:
            resp = requests.post(f"{base}/refetch-document",
                                 json={"division_id": division_id, "document_url": target},
                                 timeout=200)
        except requests.RequestException as e:
            abort(502, description=f"couldn't reach the scan worker to re-fetch this file: {e}")
        if resp.status_code >= 300:
            detail = (resp.json().get("error") if resp.headers.get("content-type", "").startswith("application/json") else resp.text)
            abort(502, description=f"couldn't re-fetch this file: {detail}")
        doc = supabase_store.scanned_document_lookup(division_id).get(target)
        if doc is None or not doc["storage_path"]:
            abort(502, description="re-fetched, but the file still isn't in storage")

    if doc["storage_path"]:
        data = supabase_store.download_document_bytes(doc["storage_path"])
        if data is None:
            abort(502, description="couldn't fetch the stored file")
        filename = doc["filename"].rsplit("/", 1)[-1] or "document.pdf"
        return Response(data, mimetype="application/pdf", headers={
            "Content-Disposition": f'{disp}; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
        })

    try:
        upstream = requests.get(target, timeout=20, stream=True,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; BidScoutBot/1.0)"})
        upstream.raise_for_status()
    except requests.RequestException as e:
        abort(502, description=f"couldn't fetch the file: {e}")

    content_length = upstream.headers.get("Content-Length")
    if content_length and int(content_length) > PROXY_MAX_BYTES:
        upstream.close()
        abort(502, description=f"file is too large to preview "
              f"({int(content_length) // (1024 * 1024)}MB, limit {PROXY_MAX_BYTES // (1024 * 1024)}MB)")

    filename = _filename_from(target)

    def stream():
        # No (or an under-reported) Content-Length means we only find out
        # it's oversized mid-stream — raising here (instead of a clean
        # `break`) drops the connection so the client sees a failed fetch,
        # not a truncated file that looks like a short-but-valid PDF.
        total = 0
        for chunk in upstream.iter_content(65536):
            total += len(chunk)
            if total > PROXY_MAX_BYTES:
                raise RuntimeError(f"file exceeded {PROXY_MAX_BYTES} bytes mid-stream")
            yield chunk

    return Response(stream(), mimetype="application/pdf", headers={
        "Content-Disposition": f'{disp}; filename="{filename}"',
        "Cache-Control": "private, max-age=300",
    })


def _document_bytes(division_id, doc, url):
    """A scanned document's bytes for a zip download, or None: the stored copy
    when there is one, otherwise a live fetch of `url`. An expired stored copy
    (needs a slow logged-in re-fetch through the scanner) is skipped — it's
    listed in the zip's note instead, and still opens one at a time."""
    if doc and doc.get("storage_path"):
        return supabase_store.download_document_bytes(doc["storage_path"])
    if not doc or doc.get("is_cached_document"):
        return None
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (compatible; BidScoutBot/1.0)"})
        r.raise_for_status()
    except requests.RequestException:
        return None
    return r.content if len(r.content) <= PROXY_MAX_BYTES else None


@app.route("/api/<division_id>/bids/<bid_id>/zip", methods=["GET"])
def api_bid_zip(division_id, bid_id):
    """Every file on one tab of a bid board bid, as a zip. ?tab=docs (the Bid
    Scout project's documents, in their folders, plus documents uploaded on
    the Bid Docs tab) or ?tab=att (the Attachments tab's uploads)."""
    _, err = _require_division(division_id)
    if err:
        return err
    tab = request.args.get("tab") or "att"
    bid = supabase_store.load_workflow_bid(bid_id)
    if not bid or str(bid["division_id"]) != division_id:
        abort(404, description="bid not found")
    uploads = [a for a in (bid.get("attachments") or []) if (a.get("kind") == "doc") == (tab == "docs")]
    jobs = []   # (path in zip, fetch -> bytes or None)
    if tab == "docs" and bid.get("source_project_id"):
        lookup = supabase_store.scanned_document_lookup(division_id)
        for f in supabase_store.load_project_files(division_id, bid["source_project_id"]):
            jobs.append((f["filename"], lambda f=f: _document_bytes(division_id, lookup.get(f["url"]), f["url"])))
    for a in uploads:
        jobs.append(((("Uploaded/" if tab == "docs" else "") + (a.get("name") or "file")),
                     lambda a=a: supabase_store.download_bid_file(a.get("path"))))
    if not jobs:
        abort(404, description="nothing to download on this tab")

    from concurrent.futures import ThreadPoolExecutor
    import tempfile
    import zipfile
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: (j[0], j[1]()), jobs))
    out = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    missing, seen = [], set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in results:
            name = name.strip("/") or "file"
            base, n = name, 2
            while name in seen:   # two uploads with the same name
                stem, dot, ext = base.rpartition(".")
                name = f"{stem} ({n}).{ext}" if dot else f"{base} ({n})"
                n += 1
            seen.add(name)
            if data is None:
                missing.append(name)
            else:
                z.writestr(name, data)
        if missing:
            z.writestr("Files not included.txt",
                       "These files couldn't be added to the download. Open them one at a time "
                       "from the bid instead:\n\n" + "\n".join(missing) + "\n")
    out.seek(0)
    title = re.sub(r'[\\/:*?"<>|]+', "-", bid.get("title") or "Bid").strip() or "Bid"
    return send_file(out, as_attachment=True, mimetype="application/zip",
                     download_name=f"{title} - {'Bid Docs' if tab == 'docs' else 'Attachments'}.zip")


def _filename_from(url):
    from urllib.parse import unquote, urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    if "file_name" in q:
        return unquote(q["file_name"][0])
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    return tail or "document.pdf"


@app.route("/download/<division_id>/results", methods=["GET"])
def download_results(division_id):
    division, err = _require_division(division_id)
    if err:
        return err

    rows = supabase_store.get_scan_results(division_id, limit=1, matches_only=True)
    if not rows:
        abort(404, description="No keyword matches yet — nothing to download.")

    buf = _build_results_workbook(division_id, division["name"])
    download_name = f"{division['name']}-scan-results.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=download_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# Local testing only — the Docker image runs this under gunicorn instead,
# which imports `app` directly and never hits __main__.
if __name__ == "__main__":
    app.run(debug=True, port=5000)

"""
Supabase read/write layer. Used by both api/index.py (the Vercel dashboard)
and scraper.py (the GitHub Actions scan job) — one shared module so the two
never disagree about schema or field names.

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables. The
service_role key is used deliberately (not the anon/public key): both
callers are trusted server-side code (a Vercel function, a GitHub Actions
runner), never the browser, so bypassing Row Level Security here is correct,
not a shortcut. See schema.sql for the RLS setup that protects against the
anon key ever being used by mistake.
"""

import hashlib
import os
import re
import uuid
import requests
from datetime import date, datetime, timedelta, timezone
from supabase import create_client

_client = None

# A project whose every non-failed document has missed this many consecutive
# site reconciles is treated as "no longer listed" (see reconcile_seen).
CLOSED_AFTER_MISSES = int(os.environ.get("CLOSED_AFTER_MISSES") or "2")

# How long a matched document shows the "New" badge (see get_results_grouped)
# before it ages out on its own, even if nobody hits "Mark done".
NEW_BADGE_HOURS = int(os.environ.get("NEW_BADGE_HOURS") or "48")

# Holds the raw bytes of browser-captured documents (no real per-document URL
# to re-fetch from later, e.g. ConstructConnect) — see upload_document_bytes.
DOCS_BUCKET = "scanned-documents"

# How long a newly-discovered matched project shows the "New" pill. Extended
# to 120h when the project was first seen on a Friday so it survives the
# weekend and is still flagged Monday morning — the base 72h window would
# otherwise lapse Sunday/Monday before anyone's back to look at it.
NEW_PROJECT_WINDOW_HOURS = 72
NEW_PROJECT_FRIDAY_WINDOW_HOURS = 120


def _is_new_project(first_seen_iso, now=None):
    """True if a matched project's first_seen falls within its "New" window.
    Mirrors the client-side isFirstScan() in templates/index.html — kept here
    too so the Results header's "N new projects" count (over every visible
    project, not just the current page) agrees with the per-row pill."""
    if not first_seen_iso:
        return False
    try:
        seen = datetime.fromisoformat(first_seen_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    window_hours = (
        NEW_PROJECT_FRIDAY_WINDOW_HOURS if seen.weekday() == 4
        else NEW_PROJECT_WINDOW_HOURS
    )
    return (now - seen) < timedelta(hours=window_hours)


def get_client():
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "division"


# ---------------------------------------------------------------------------
# Divisions
# ---------------------------------------------------------------------------

def load_divisions():
    res = get_client().table("divisions").select("*").order("created_at").execute()
    return res.data


def get_division(division_id):
    res = get_client().table("divisions").select("*").eq("id", division_id).limit(1).execute()
    return res.data[0] if res.data else None


# Per-division settings kept in divisions.settings (jsonb), edited from the
# dashboard's hidden settings window. Anything unset falls back to these.
DEFAULT_SETTINGS = {
    "fit_enabled": True,
    "fit_min_decisions": int(os.environ.get("FIT_MIN_DECISIONS") or "50"),
    "fit_sure_at": 60,
    # Jira project "Add to Jira" files into for this division (its short key,
    # e.g. "GEO"). Blank = the JIRA_PROJECT_KEY env var.
    "jira_project_key": "",
    # Set once the division's Jira project has been imported onto the bid
    # board; the scanner then pulls Jira changes every 15 minutes (jira_sync.py).
    "jira_sync": False,
}
_SETTING_LIMITS = {"fit_min_decisions": (5, 1000), "fit_sure_at": (0, 100)}


def get_division_settings(division_id):
    """DEFAULT_SETTINGS overlaid with whatever the division has saved. Just
    the defaults if the column isn't there yet."""
    out = dict(DEFAULT_SETTINGS)
    try:
        res = (
            get_client().table("divisions").select("settings")
            .eq("id", division_id).limit(1).execute()
        )
        if res.data and isinstance(res.data[0].get("settings"), dict):
            out.update({k: v for k, v in res.data[0]["settings"].items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return out


def save_division_settings(division_id, patch):
    """Validate and merge `patch` into the division's settings. Returns the
    full settings. Raises ValueError on a bad value or a missing column."""
    clean = {}
    for k, v in (patch or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        if isinstance(DEFAULT_SETTINGS[k], bool):
            clean[k] = bool(v)
        elif isinstance(DEFAULT_SETTINGS[k], str):
            v = str(v or "").strip().upper()
            if v and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,9}", v):
                raise ValueError(f"{v} doesn't look like a Jira project key (like GEO)")
            clean[k] = v
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number")
            lo, hi = _SETTING_LIMITS.get(k, (None, None))
            if lo is not None and not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            clean[k] = v
    current = get_division_settings(division_id)
    current.update(clean)
    saved = {k: current[k] for k in DEFAULT_SETTINGS}
    try:
        get_client().table("divisions").update({"settings": saved}).eq("id", division_id).execute()
    except Exception as e:
        raise ValueError("couldn't save — add the settings column from schema.sql") from e
    _fit_stats_cache.pop(division_id, None)
    return saved


def create_division(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")

    existing_ids = {d["id"] for d in load_divisions()}
    base_slug = _slugify(name)
    slug, n = base_slug, 2
    while slug in existing_ids:
        slug = f"{base_slug}-{n}"
        n += 1

    row = {"id": slug, "name": name}
    get_client().table("divisions").insert(row).execute()
    return row


def rename_division(division_id, name):
    """Change a division's display name. The id/slug stays fixed so existing
    URLs and the foreign keys on sites/keywords/results keep working."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    res = get_client().table("divisions").update({"name": name}).eq("id", division_id).execute()
    if not res.data:
        raise ValueError("division not found")
    return res.data[0]


def delete_division(division_id):
    """Wipe a division completely: every site, keyword, scan result, daily
    summary, and run-history row, then the division itself. The schema's
    ON DELETE CASCADE would handle the children, but we delete them
    explicitly too so it still works on a database whose foreign keys were
    set up differently. A delete here is final — the dashboard confirms
    first."""
    client = get_client()
    for table in ("scan_results", "daily_summaries", "scan_runs", "sites", "keywords", "project_flags"):
        try:
            client.table(table).delete().eq("division_id", division_id).execute()
        except Exception:
            pass  # e.g. project_flags on a DB that hasn't run that migration
    res = client.table("divisions").delete().eq("id", division_id).execute()
    if not res.data:
        raise ValueError("division not found")


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

def load_sites(division_id):
    # Also the order the scan worker visits sites in (scraper.py just
    # iterates whatever this returns) — falls back to insertion order on a
    # DB that hasn't run the sort_order migration yet, rather than erroring
    # the whole sites list out.
    try:
        res = (
            get_client().table("sites").select("*")
            .eq("division_id", division_id).order("sort_order").execute()
        )
    except Exception:
        res = (
            get_client().table("sites").select("*")
            .eq("division_id", division_id).order("id").execute()
        )
    # `active`/`sort_order` may be absent on a DB that skipped a migration —
    # default them so both the dashboard and the scan worker see sane values.
    for row in res.data:
        row.setdefault("active", True)
        if row.get("active") is None:
            row["active"] = True
        if row.get("sort_order") is None:
            row["sort_order"] = row["id"]
    return res.data


def reorder_sites(division_id, ordered_ids):
    """Persists a drag-to-reorder from the dashboard — ordered_ids is every
    site id for this division, top to bottom. One update per site; this
    list is always small (a handful to a few dozen sites). Raises
    ValueError with a migration hint if the column is missing."""
    try:
        for i, site_id in enumerate(ordered_ids):
            get_client().table("sites").update({"sort_order": i}) \
                .eq("division_id", division_id).eq("id", site_id).execute()
    except Exception as e:
        raise ValueError(
            "couldn't update 'sort_order' — run the migration in schema.sql: "
            "alter table sites add column if not exists sort_order bigint"
        ) from e


def set_site_active(division_id, site_id, active):
    """Flip a site's active flag. Inactive sites stay in the list but no scan
    touches them. Raises ValueError with a migration hint if the column is
    missing."""
    try:
        res = (
            get_client().table("sites").update({"active": bool(active)})
            .eq("division_id", division_id).eq("id", site_id).execute()
        )
    except Exception as e:
        raise ValueError(
            "couldn't update 'active' — run the migration in schema.sql: "
            "alter table sites add column if not exists active boolean not null default true"
        ) from e
    if not res.data:
        raise ValueError("site not found")
    return res.data[0]


def record_login_result(division_id, site_id, ok, message=None):
    """Record the outcome of a site's most recent login attempt (set by the
    scan worker right after it tries to log in), so the dashboard can show
    whether the saved credentials are still working. Best-effort — a site
    predating this column shouldn't fail the whole scan over it."""
    try:
        get_client().table("sites").update({
            "login_last_ok": bool(ok),
            "login_last_checked_at": datetime.now(timezone.utc).isoformat(),
            "login_last_error": None if ok else (message or "login failed"),
        }).eq("division_id", division_id).eq("id", site_id).execute()
    except Exception as e:
        print(f"  ! Could not record login result: {e}")


def add_site(division_id, name, url, listing=None, tabs=None, adapter=None, login=None):
    """`login`, when given, is {"username", "password_enc", "url"} — the
    password already encrypted by the caller (api/index.py), since this
    module has no opinion on how secrets are handled."""
    existing = load_sites(division_id)
    next_order = (max(s["sort_order"] for s in existing) + 1) if existing else 0
    row = {"division_id": division_id, "name": name, "url": url, "sort_order": next_order}
    if listing is not None:
        row["listing"] = listing
    if tabs is not None:
        row["tabs"] = tabs
    if adapter:
        row["adapter"] = adapter
    if login is not None:
        row["login_username"] = login["username"]
        row["login_password_enc"] = login["password_enc"]
        row["login_url"] = login["url"]
    try:
        res = get_client().table("sites").insert(row).execute()
    except Exception:
        # sort_order column not migrated yet on this DB — fall back to the
        # old insertion-order behavior rather than failing "add site" outright.
        row.pop("sort_order", None)
        res = get_client().table("sites").insert(row).execute()
    return res.data[0]


def update_site(division_id, site_id, name, url, listing=None, adapter=None, login_patch=None):
    """Overwrite an existing site's config. `listing` and `adapter` are
    written as given (including None, to clear a previously-set value) so the
    dashboard's edit form can move a site between strategies. `tabs` is left
    untouched — it has no dashboard UI. `login_patch` is merged in as given
    (the caller decides whether to touch login_password_enc, so leaving a
    password field blank in the edit form doesn't overwrite a saved one)."""
    patch = {
        "name": name,
        "url": url,
        "listing": listing,
        "adapter": adapter,
    }
    if login_patch:
        patch.update(login_patch)
    res = (
        get_client().table("sites").update(patch)
        .eq("division_id", division_id).eq("id", site_id).execute()
    )
    if not res.data:
        raise ValueError("site not found")
    return res.data[0]


def delete_site(division_id, site_id):
    res = (
        get_client().table("sites").delete()
        .eq("division_id", division_id).eq("id", site_id).execute()
    )
    if not res.data:
        raise ValueError("site not found")


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

def load_keywords(division_id):
    res = (
        get_client().table("keywords").select("*")
        .eq("division_id", division_id).order("id").execute()
    )
    return [row["keyword"] for row in res.data]


_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â„", "Å")


def demojibake(text):
    """Repair double-encoded text (UTF-8 read as cp1252 then re-encoded),
    e.g. 'FlexterraÂ® HP-FGMÂ®' -> 'Flexterra® HP-FGM®', 'TensarTechâ„¢' ->
    'TensarTech™'. Only touched when tell-tale markers are present, and only
    kept if the round trip actually removes them."""
    if not text or not any(m in text for m in _MOJIBAKE_MARKERS):
        return text
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not any(m in fixed for m in _MOJIBAKE_MARKERS):
            return fixed
    return text


def _clean_keyword(kw):
    """Trim, repair mojibake, drop Markdown emphasis/heading marks, and
    collapse internal whitespace so 'soil   sampling', '**soil sampling**'
    and 'soil sampling' are stored and compared as the same keyword."""
    kw = demojibake((kw or "").strip())
    kw = re.sub(r"^#+\s*", "", kw)          # "## Geotextiles" -> "Geotextiles"
    kw = kw.replace("**", "").replace("__", "")  # "HydroChain®** (…)" -> "HydroChain® (…)"
    kw = kw.strip("*_`").strip()            # "**Filter fabric**" -> "Filter fabric"
    kw = kw.rstrip(":").strip()             # "Erosion control:" -> "Erosion control"
    return re.sub(r"\s+", " ", kw)


def looks_like_section_heading(text):
    """True for lines that are document structure, not keywords: anything
    that started with '#', or an ALL-CAPS label of 3+ words like
    'PRODUCT TYPE / MATERIAL CATEGORY TERMS'. Lines with digits or
    parentheses are spared (spec refs like 'AASHTO M288', brands like
    'GSE HD (HDPE)'), as are short acronyms (GCL, HDPE). Used for both
    single adds and file uploads so a pasted outline never becomes a
    keyword."""
    if not text:
        return False
    if text.lstrip().startswith("#"):
        return True
    if any(ch.isdigit() for ch in text) or "(" in text or ")" in text:
        return False
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and len(text.split()) >= 3 and all(c.isupper() for c in letters)


def add_keyword(division_id, keyword):
    raw = keyword
    keyword = _clean_keyword(keyword)
    if not keyword:
        raise ValueError("keyword is required")
    if looks_like_section_heading(raw) or looks_like_section_heading(keyword):
        raise ValueError("that looks like a section heading, not a keyword")
    existing = load_keywords(division_id)
    if keyword.lower() in (k.lower() for k in existing):
        return existing
    get_client().table("keywords").insert({"division_id": division_id, "keyword": keyword}).execute()
    return load_keywords(division_id)


def add_keywords_bulk(division_id, keywords):
    """
    Add multiple keywords in one round trip (used by the "add from a file"
    upload). Skips any already in the DB and any repeated within the batch,
    comparing case- and whitespace-insensitively.

    Returns (all_keywords, added_list, skipped) where skipped is
    {"already_present": int, "repeated_in_file": int}.
    """
    existing_lower = {k.lower() for k in load_keywords(division_id)}
    seen_in_batch = set()
    new_rows, added = [], []
    already_present = repeated_in_file = 0
    for kw in keywords:
        if looks_like_section_heading(kw):
            continue
        kw = _clean_keyword(kw)
        if not kw or looks_like_section_heading(kw):
            continue
        key = kw.lower()
        if key in existing_lower:
            already_present += 1
        elif key in seen_in_batch:
            repeated_in_file += 1
        else:
            seen_in_batch.add(key)
            new_rows.append({"division_id": division_id, "keyword": kw})
            added.append(kw)
    if new_rows:
        get_client().table("keywords").insert(new_rows).execute()
    skipped = {"already_present": already_present, "repeated_in_file": repeated_in_file}
    return load_keywords(division_id), added, skipped


def delete_keyword(division_id, keyword):
    # ilike gives case-insensitive matching; escape LIKE metacharacters so a
    # keyword containing % or _ deletes only itself, not everything.
    pattern = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    get_client().table("keywords").delete() \
        .eq("division_id", division_id).ilike("keyword", pattern).execute()
    return load_keywords(division_id)


def clear_keywords(division_id):
    """Remove every keyword for a division (the dashboard's 'Clear all')."""
    get_client().table("keywords").delete().eq("division_id", division_id).execute()
    return []


def load_keyword_rows(division_id):
    """Full keyword rows (id, keyword, embedding) — used by the scan worker's
    semantic pre-filter. `embedding` is a list[float] or None."""
    res = (
        get_client().table("keywords").select("id,keyword,embedding")
        .eq("division_id", division_id).order("id").execute()
    )
    return res.data


def save_keyword_embeddings(division_id, embedded_rows):
    """Persist freshly-computed embedding vectors. `embedded_rows` is a list
    of {"id", "keyword", "embedding"} dicts. Uses upsert so it's one round
    trip for the whole batch (the first scan after a big upload embeds
    hundreds at once)."""
    rows = [
        {"id": r["id"], "division_id": division_id,
         "keyword": r["keyword"], "embedding": r["embedding"]}
        for r in embedded_rows
    ]
    if rows:
        get_client().table("keywords").upsert(rows).execute()


# ---------------------------------------------------------------------------
# Scan runs (status tracking)
# ---------------------------------------------------------------------------

def start_run(division_id):
    row = {"division_id": division_id, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running"}
    res = get_client().table("scan_runs").insert(row).execute()
    return res.data[0]["id"]


def finish_run(run_id, status):
    get_client().table("scan_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }).eq("id", run_id).execute()


def update_run_progress(run_id, done=None, total=None, label=None,
                        site_i=None, site_n=None, overall=None):
    """Lightweight progress ping for the dashboard's live status. `done`/
    `total` are per the CURRENT site; `overall` is documents scanned across
    all sites so far; `site_i`/`site_n` are the current/total site count.
    Any field may be omitted."""
    core = {}  # columns that have existed since the first progress release
    if done is not None:
        core["progress_done"] = done
    if total is not None:
        core["progress_total"] = total
    if label is not None:
        core["progress_label"] = label
    extra = {}  # added later — a DB that skipped the migration won't have these
    if site_i is not None:
        extra["progress_site_i"] = site_i
    if site_n is not None:
        extra["progress_site_n"] = site_n
    if overall is not None:
        extra["progress_overall"] = overall

    if not core and not extra:
        return
    # Try the full patch; if the newer columns don't exist, fall back to the
    # core ones so per-site 0/0 resets still land (no stale "298 of 298").
    attempts = [{**core, **extra}] + ([core] if extra and core else [])
    err = None
    for patch in attempts:
        if not patch:
            continue
        try:
            get_client().table("scan_runs").update(patch).eq("id", run_id).execute()
            return
        except Exception as e:
            err = e
    print(f"  ! progress update failed (non-fatal): {err}")


def get_latest_run(division_id):
    res = (
        get_client().table("scan_runs").select("*")
        .eq("division_id", division_id).order("started_at", desc=True).limit(1).execute()
    )
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# Scan results
# ---------------------------------------------------------------------------

def log_scan_row(division_id, run_date, site, document_url, filename,
                  matched_keywords, match_count, keyword_locations, status, ai_notes,
                  source_url=None, storage_path=None):
    get_client().table("scan_results").insert({
        "division_id": division_id,
        "run_date": run_date,
        "site": site,
        "document_url": document_url,
        "source_url": source_url,
        "storage_path": storage_path,
        "is_cached_document": bool(storage_path),
        "filename": filename,
        "matched_keywords": matched_keywords,
        "match_count": match_count,
        "keyword_locations": keyword_locations,
        "status": status,
        "ai_notes": ai_notes,
    }).execute()


def ensure_docs_bucket():
    """Creates the private Storage bucket that holds browser-captured
    documents' raw bytes (idempotent — no-ops if it already exists). Called
    once per scan run; if this fails (storage not set up, permissions, etc.)
    uploads will just keep failing gracefully — see upload_document_bytes —
    not break the scan itself."""
    try:
        get_client().storage.create_bucket(DOCS_BUCKET, options={"public": False})
    except Exception:
        pass


def _storage_path_for(division_id, doc_key):
    """A stable, filesystem/URL-safe Storage path for a document — hashed
    since doc_key (the "already scanned" dedup key) can contain arbitrary
    characters (slashes, unicode, a URL fragment) that aren't valid object
    key segments."""
    return f"{division_id}/{hashlib.sha256(doc_key.encode()).hexdigest()}.pdf"


def upload_document_bytes(division_id, doc_key, data):
    """Persists a browser-captured document's raw bytes to Supabase Storage
    so it can be viewed/downloaded later — needed for adapters whose
    documents never had a real per-document URL to live-fetch from (they
    came from a JS-triggered browser download during the scan itself, e.g.
    ConstructConnect's "Zipped PDFs"). Returns the storage path on success,
    None on failure (a scan shouldn't fail just because storage is
    unavailable — the document is still scanned and logged either way,
    just not viewable afterward)."""
    path = _storage_path_for(division_id, doc_key)
    try:
        get_client().storage.from_(DOCS_BUCKET).upload(
            path, data, file_options={"content-type": "application/pdf", "upsert": "true"},
        )
        return path
    except Exception as e:
        print(f"  ! couldn't store document bytes ({e})")
        return None


def download_document_bytes(storage_path):
    """Bytes previously saved by upload_document_bytes, or None if missing/
    unavailable."""
    try:
        return get_client().storage.from_(DOCS_BUCKET).download(storage_path)
    except Exception as e:
        print(f"  ! couldn't fetch stored document ({e})")
        return None


def delete_document_bytes(storage_path):
    """Removes a previously-uploaded document from Storage (used by the
    retention job). No-ops quietly on failure — a missed delete just means
    the next run tries again, not a reason to fail the cleanup pass."""
    try:
        get_client().storage.from_(DOCS_BUCKET).remove([storage_path])
    except Exception as e:
        print(f"  ! couldn't delete stored document ({e})")


def clear_storage_path(division_id, document_url):
    """Nulls storage_path after its Storage object has been deleted by the
    retention job. is_cached_document is left True — that's the permanent
    "this one needs a live re-fetch, not a plain URL fetch" signal."""
    (get_client().table("scan_results").update({"storage_path": None})
     .eq("division_id", division_id).eq("document_url", document_url).execute())


def list_expired_cached_documents(cutoff_iso):
    """Every (division_id, document_url, storage_path) still holding a
    Storage object older than cutoff_iso (compared against run_date) —
    feeds the retention job. Only ever matches browser-captured documents,
    since storage_path is only set for those in the first place."""
    res = (
        get_client().table("scan_results")
        .select("division_id,document_url,storage_path,run_date")
        .eq("is_cached_document", True)
        .not_.is_("storage_path", "null")
        .lt("run_date", cutoff_iso)
        .execute()
    )
    return res.data


def list_cached_documents_for_overdue_projects():
    """[{"division_id", "document_url", "storage_path"}, ...] for every
    stored document belonging to a project whose bid_date has passed
    without being sent to Jira — feeds the retention job, same as
    list_expired_cached_documents, just a different trigger (the project
    itself is moot, not just old) than a fixed day count. A later view
    still works via the normal live re-fetch path (see api_proxy_pdf) since
    is_cached_document stays True."""
    today = date.today().isoformat()
    # Anything not sent to Jira — undecided or "Not a fit" — has no use for
    # its cached files once the bid has opened.
    try:
        res = (
            get_client().table("project_flags")
            .select("division_id,project_key")
            .or_("decision.is.null,decision.neq.jira")
            .not_.is_("bid_date", "null")
            .lt("bid_date", today)
            .execute()
        )
    except Exception:
        # Decision columns not added yet — the old "not done" rule.
        res = (
            get_client().table("project_flags")
            .select("division_id,project_key")
            .eq("done", False)
            .not_.is_("bid_date", "null")
            .lt("bid_date", today)
            .execute()
        )
    out = []
    for row in res.data:
        docs = (
            get_client().table("scan_results")
            .select("division_id,document_url,storage_path")
            .eq("division_id", row["division_id"]).eq("site", row["project_key"])
            .eq("is_cached_document", True)
            .not_.is_("storage_path", "null")
            .execute()
        )
        out.extend(docs.data)
    return out


def already_scanned_urls(division_id):
    """{document_url: source_url_or_None} for every document this division has
    already attempted — including past download failures — so a re-run
    (especially an adapter that re-lists every advertised project each day)
    skips them instead of re-downloading and re-running the AI pass. A file
    that failed once (oversized, unreachable, etc.) is not retried; delete
    its scan_results row if you want it picked up again."""
    res = (
        get_client().table("scan_results").select("document_url,source_url")
        .eq("division_id", division_id).execute()
    )
    return {
        r["document_url"]: r.get("source_url")
        for r in res.data
        if r.get("document_url")
    }


def scanned_document_lookup(division_id):
    """{document_url: {"storage_path", "filename", "site", "source_url",
    "is_cached_document"}} for every document this division has ever logged
    (any status). Used by the PDF proxy both to gate access (can't be
    pointed at an arbitrary URL) and to know whether a document's bytes are
    in Supabase Storage (browser-captured content with no real external URL
    to re-fetch from, e.g. ConstructConnect) rather than live-fetchable —
    storage_path is None for the latter, or once its cached copy has
    expired (see is_cached_document, which stays True either way so the
    proxy knows to live re-fetch instead of trying a plain URL GET)."""
    rows = _fetch_all_scan_results(
        division_id,
        columns="document_url,storage_path,filename,site,source_url,is_cached_document",
    )
    return {
        r["document_url"]: {
            "storage_path": r.get("storage_path"),
            "filename": r.get("filename") or "",
            "site": r.get("site") or "",
            "source_url": r.get("source_url"),
            "is_cached_document": bool(r.get("is_cached_document")),
        }
        for r in rows if r.get("document_url")
    }


def set_storage_path(division_id, document_url, storage_path):
    """Writes a freshly re-fetched document's new Storage path back onto its
    existing scan_results row (the inverse of clear_storage_path) — used
    after a live re-fetch re-uploads bytes for a document whose cached copy
    had expired."""
    (get_client().table("scan_results").update({"storage_path": storage_path})
     .eq("division_id", division_id).eq("document_url", document_url).execute())


def backfill_source_url(division_id, document_url, source_url):
    """One-off: attach a source_url to already-logged rows that predate it."""
    get_client().table("scan_results").update({"source_url": source_url}) \
        .eq("division_id", division_id).eq("document_url", document_url).execute()


def get_scan_results(division_id, limit=2000, matches_only=False):
    """Most recent rows first, for building the downloadable spreadsheet.
    matches_only=True returns only documents that hit a keyword (match_count > 0),
    so the filter happens in the DB rather than after a truncated fetch."""
    query = (
        get_client().table("scan_results").select("*")
        .eq("division_id", division_id).order("run_date", desc=True).limit(limit)
    )
    if matches_only:
        query = query.gt("match_count", 0)
    return query.execute().data


def get_scan_results_page(division_id, search=None, status=None, page=1, page_size=20):
    """
    Paginated, filterable results for the dashboard's Results table.
    Returns (rows, total_count) — total_count reflects the filtered set, for
    correct pagination controls, not the division's all-time row count.
    """
    query = (
        get_client().table("scan_results")
        .select("*", count="exact")
        .eq("division_id", division_id)
    )
    if status:
        # Prefix match so filtering by "Matched" also catches
        # "Matched (approx. location)" without needing two dropdown entries.
        query = query.ilike("status", f"{status}%")
    if search:
        like = f"%{search}%"
        query = query.or_(f"site.ilike.{like},filename.ilike.{like}")

    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    start = (page - 1) * page_size
    end = start + page_size - 1

    res = query.order("run_date", desc=True).range(start, end).execute()
    return res.data, (res.count or 0)


def _split_site(site):
    """'UDOT — US-40; MP 52 to Currant Creek' -> ('UDOT', 'US-40; ...').
    Falls back to ('', site) when there's no ' — ' separator."""
    parts = (site or "").split(" — ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", (site or "").strip()


# ---------------------------------------------------------------------------
# "Still advertised?" tracking
# ---------------------------------------------------------------------------

def reconcile_seen(division_id, advertised_by_site, run_date):
    """After a scan, refresh the 'still advertised' state on scan_results.

    advertised_by_site: {site_name: {doc_key, ...}} — one entry per site whose
    discovery *succeeded and returned at least one document* this run (a site
    that errored or came back empty is left out, so a broken adapter can't
    mark projects closed). For those sites: rows whose document_url is still
    in the listing get last_seen_at=run_date, misses=0; rows that aren't get
    misses += 1. No-ops if the columns aren't there yet."""
    if not advertised_by_site:
        return
    client = get_client()
    try:
        rows = (
            client.table("scan_results").select("id,site,document_url,misses,status")
            .eq("division_id", division_id).execute()
        ).data
    except Exception as e:
        print(f"  ! seen-tracking skipped ({e})")
        return

    def owning_site(site_val):
        for name in advertised_by_site:
            if site_val == name or (site_val or "").startswith(name + " — "):
                return name
        return None

    seen_ids, missed = [], []
    for r in rows:
        name = owning_site(r.get("site"))
        if name is None:
            continue
        du = r.get("document_url")
        if du and du in advertised_by_site[name]:
            seen_ids.append(r["id"])
        elif r.get("status") != "Download failed":
            missed.append(r)

    try:
        for i in range(0, len(seen_ids), 200):
            client.table("scan_results").update(
                {"last_seen_at": run_date, "misses": 0}
            ).in_("id", seen_ids[i:i + 200]).execute()
        for r in missed:
            client.table("scan_results").update(
                {"misses": (r.get("misses") or 0) + 1}
            ).eq("id", r["id"]).execute()
    except Exception as e:
        print(f"  ! seen-tracking write failed ({e})")
        return
    print(f"  Seen-tracking: {len(seen_ids)} still listed, {len(missed)} absent this run")


def closed_project_keys(division_id):
    """Set of scan_results.site values whose every non-failed document has
    misses >= CLOSED_AFTER_MISSES — the source site no longer lists them.
    Empty set if the misses column isn't there yet."""
    try:
        rows = (
            get_client().table("scan_results").select("site,filename,misses,status")
            .eq("division_id", division_id).execute()
        ).data
    except Exception:
        return set()
    by_project = {}
    for r in rows:
        site = r.get("site")
        if not site or not r.get("filename") or r.get("status") == "Download failed":
            continue
        by_project.setdefault(site, []).append(r.get("misses") or 0)
    return {
        s for s, ms in by_project.items()
        if ms and all(m >= CLOSED_AFTER_MISSES for m in ms)
    }


# ---------------------------------------------------------------------------
# Project decisions (user-set): Jira / Not a fit / back to review
# ---------------------------------------------------------------------------

# One-click reasons for "Not a fit" — what the AI will eventually learn from,
# so keep them stable (rename the label, not the meaning). "Unknown (from
# before)" is only ever set by the migration for projects that were "done"
# before decisions existed; it's excluded from anything the AI learns from.
NOT_FIT_REASONS = (
    "Not our products",
    "Information only / nothing specified",
    "Too small",
    "Outside our area",
    "Bid date too soon",
    "Other",
)
LEGACY_REASON = "Unknown (from before)"
DECISIONS = ("jira", "not_fit")
# "duplicate" is a third saved decision: filed under Not a Fit (reason
# DUPLICATE_REASON) with duplicate_of = the original project's key. It's left
# out of the fit suggestions' examples — a duplicate says nothing about fit.
DUPLICATE_REASON = "Duplicate"


def load_project_decisions(division_id):
    """{project_key: {"decision", "reason", "note", "decided_at"}} for every
    project with a decision ("jira" / "not_fit"). Before the decision columns
    exist, falls back to the old flags: a Jira key -> "jira", done -> "not_fit"
    (the same mapping the migration applies), so nothing jumps tabs in
    between deploying and running the SQL."""
    for cols in ("project_key,decision,decision_reason,decision_note,decided_at,duplicate_of",
                 "project_key,decision,decision_reason,decision_note,decided_at"):
        try:
            res = (
                get_client().table("project_flags").select(cols)
                .eq("division_id", division_id).not_.is_("decision", "null").execute()
            )
            return {r["project_key"]: {"decision": r["decision"], "reason": r.get("decision_reason"),
                                       "note": r.get("decision_note"), "decided_at": r.get("decided_at"),
                                       "duplicate_of": r.get("duplicate_of")}
                    for r in res.data}
        except Exception:
            pass
    try:
        res = (
            get_client().table("project_flags").select("project_key,done,jira_key")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    out = {}
    for r in res.data:
        if r.get("jira_key"):
            out[r["project_key"]] = {"decision": "jira", "reason": None, "note": None, "decided_at": None}
        elif r.get("done"):
            out[r["project_key"]] = {"decision": "not_fit", "reason": LEGACY_REASON,
                                     "note": None, "decided_at": None}
    return out


def set_project_decision(division_id, project_key, decision, reason=None, note=None,
                         source="user", duplicate_of=None):
    """Record a decision on a project: "jira", "not_fit", or None (back to
    review). Deciding also acknowledges every document seen so far
    (docs_ack_through = now) — a "Not a fit" project only comes back to
    review when a newer matching document turns up after this moment.

    Also appends to project_decisions with a snapshot of what the project
    looked like right now (see build_project_snapshot) — the history the AI
    will learn from, kept even after scan data and cached files age out.
    That log is best-effort; the decision itself raises ValueError with a
    migration hint if the columns are missing."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "division_id": division_id,
        "project_key": project_key,
        "decision": decision,
        "decision_reason": (reason if decision == "not_fit"
                            else DUPLICATE_REASON if decision == "duplicate" else None),
        "decision_note": (note or None) if decision else None,
        "decided_at": now if decision else None,
        "duplicate_of": duplicate_of if decision == "duplicate" else None,
        # Kept in step for anything still reading the old flag.
        "done": decision is not None,
        "updated_at": now,
    }
    if decision:
        payload["docs_ack_through"] = now
    try:
        get_client().table("project_flags").upsert(payload).execute()
    except Exception as e:
        if decision == "duplicate":
            raise ValueError(
                "couldn't save — add the duplicate_of column from schema.sql"
            ) from e
        # Before the duplicate_of column exists every other decision still saves.
        payload.pop("duplicate_of")
        try:
            get_client().table("project_flags").upsert(payload).execute()
        except Exception as e2:
            raise ValueError(
                "couldn't save — add the decision columns from schema.sql"
            ) from e2
    try:
        get_client().table("project_decisions").insert({
            "division_id": division_id,
            "project_key": project_key,
            "decision": decision or "review",
            "reason": payload["decision_reason"],
            "note": payload["decision_note"],
            "source": source,
            "snapshot": build_project_snapshot(division_id, project_key),
            "decided_at": now,
        }).execute()
    except Exception as e:
        print(f"  ! couldn't log decision for {project_key} ({e})")


def build_project_snapshot(division_id, project_key):
    """What the project looked like at the moment of a decision: site, bid
    timing, which keywords matched in which files (and on what pages), the
    AI's notes on matched files, and who's holding plans. Compact on
    purpose — a ConstructConnect project can have 900+ files, so unmatched
    files are only counted (by top-level folder), not listed."""
    res = (
        get_client().table("scan_results")
        .select("filename,status,matched_keywords,match_count,keyword_locations,ai_notes,run_date")
        .eq("division_id", division_id).eq("site", project_key)
        .order("run_date", desc=True).execute()
    )
    seen, files = set(), []
    for r in res.data:
        fn = r.get("filename") or ""
        if not fn or fn in seen:
            continue
        seen.add(fn)
        files.append(r)

    prefix, project = _split_site(project_key)
    matched, unmatched_by_folder, kw_files = [], {}, {}
    for r in files:
        kws = [k.strip() for k in (r.get("matched_keywords") or "").split(",") if k.strip()]
        if kws and (r.get("status") or "").startswith("Matched"):
            for k in kws:
                kw_files[k] = kw_files.get(k, 0) + 1
            matched.append({
                "file": r["filename"], "keywords": kws, "match_count": r.get("match_count") or 0,
                "pages": (r.get("keyword_locations") or "")[:400],
                "ai_notes": (r.get("ai_notes") or "")[:400],
            })
        else:
            rel = r["filename"].split(" - ", 1)[-1]
            folder = rel.split("/", 1)[0] if "/" in rel else "(top level)"
            unmatched_by_folder[folder] = unmatched_by_folder.get(folder, 0) + 1

    flags = {}
    try:
        fr = (
            get_client().table("project_flags").select("bid_date,bid_time,plan_holders")
            .eq("division_id", division_id).eq("project_key", project_key).limit(1).execute()
        )
        flags = fr.data[0] if fr.data else {}
    except Exception:
        pass
    # The fit suggestion showing when the decision was made (if any) — lets
    # the dashboard report how often the suggestions agreed with the user.
    prediction = None
    try:
        pr = (
            get_client().table("project_flags")
            .select("fit_verdict,fit_reason,fit_confidence,fit_predicted_at")
            .eq("division_id", division_id).eq("project_key", project_key).limit(1).execute()
        )
        if pr.data and pr.data[0].get("fit_verdict"):
            prediction = {"verdict": pr.data[0]["fit_verdict"], "reason": pr.data[0].get("fit_reason"),
                          "confidence": pr.data[0].get("fit_confidence"),
                          "predicted_at": pr.data[0].get("fit_predicted_at")}
    except Exception:
        pass
    bid_date = flags.get("bid_date")
    days_to_bid = None
    if bid_date:
        try:
            days_to_bid = (date.fromisoformat(bid_date) - date.today()).days
        except ValueError:
            pass
    holders = flags.get("plan_holders") or []
    roles = {}
    for h in holders:
        r = h.get("role") or "Unknown"
        roles[r] = roles.get(r, 0) + 1

    return {
        "site": prefix, "project": project,
        "bid_date": bid_date, "bid_time": flags.get("bid_time"), "days_to_bid": days_to_bid,
        "file_count": len(files), "matched_file_count": len(matched),
        "keywords": kw_files,
        "matched_files": matched[:200],
        "unmatched_by_folder": unmatched_by_folder,
        "plan_holders": {
            "count": len(holders), "roles": roles,
            "companies": [{"company": h.get("company"), "role": h.get("role")} for h in holders][:60],
        } if holders else None,
        "prediction": prediction,
    }


# ---------------------------------------------------------------------------
# Fit suggestions (see fit_predictor.py)
# ---------------------------------------------------------------------------

def load_decision_log(division_id):
    """Every project_decisions row for the division, newest first (includes
    the snapshot and any cached embedding). Empty if the table isn't there."""
    out, start = [], 0
    while True:
        try:
            res = (
                get_client().table("project_decisions")
                .select("id,project_key,decision,reason,note,source,snapshot,embedding,decided_at")
                .eq("division_id", division_id).order("decided_at", desc=True)
                .range(start, start + 499).execute()
            )
        except Exception:
            return out
        out.extend(res.data)
        if len(res.data) < 500:
            return out
        start += 500


def save_decision_embeddings(mapping):
    """{project_decisions.id: vector} — cache so each past decision is only
    embedded once."""
    for row_id, vec in mapping.items():
        try:
            get_client().table("project_decisions").update({"embedding": vec}).eq("id", row_id).execute()
        except Exception as e:
            print(f"  ! couldn't cache a decision embedding ({e})")
            return


def list_review_projects(division_id):
    """Every matched project currently in To Review, as get_results_grouped
    returns them (newest documents first)."""
    out, page = [], 1
    while True:
        projects, total, *_ = get_results_grouped(
            division_id, status="Matched", view="review", sort="recent",
            page=page, page_size=50)
        out.extend(projects)
        if page * 50 >= total or not projects:
            return out
        page += 1


def load_project_fit(division_id):
    """{project_key: {"verdict", "reason", "confidence", "explanation",
    "predicted_at", "basis_count"}} — the latest suggestion per project.
    Empty if the columns aren't there yet."""
    try:
        res = (
            get_client().table("project_flags")
            .select("project_key,fit_verdict,fit_reason,fit_confidence,fit_explanation,"
                    "fit_predicted_at,fit_basis_count")
            .eq("division_id", division_id).not_.is_("fit_verdict", "null").execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: {
        "verdict": r["fit_verdict"], "reason": r.get("fit_reason"),
        "confidence": r.get("fit_confidence"), "explanation": r.get("fit_explanation"),
        "predicted_at": r.get("fit_predicted_at"), "basis_count": r.get("fit_basis_count"),
    } for r in res.data}


def save_project_fit(division_id, project_key, result):
    try:
        get_client().table("project_flags").upsert({
            "division_id": division_id, "project_key": project_key,
            "fit_verdict": result["verdict"], "fit_reason": result.get("reason"),
            "fit_confidence": result.get("confidence"),
            "fit_explanation": result.get("explanation"),
            "fit_predicted_at": result.get("predicted_at"),
            "fit_basis_count": result.get("basis_count"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"  ! couldn't save fit suggestion for {project_key} ({e})")


_fit_stats_cache = {}
_FIT_STATS_TTL_S = 60


def _workflow_space(division_id):
    """The division's workflow space on the bid board, where Add to Jira files
    bids: its oldest top-level space (e.g. "Bid Workflow"). Made as
    "<Division> Workflow", following the "Bid pipeline" template, if the
    division has none yet — the board makes the same one when it loads."""
    sb = get_client()
    spaces = (
        sb.table("wf_spaces").select("id,key,statuses,next")
        .eq("division_id", division_id).is_("parent_id", "null")
        .order("created_at").execute().data
    )
    # "BS" was the old "Bid Scout inbox" space; bids go to the workflow space now.
    own = [sp for sp in spaces if sp["key"] != "BS"]
    if own:
        return own[0]
    div = sb.table("divisions").select("name").eq("id", division_id).limit(1).execute().data
    name = f"{div[0]['name'] if div else division_id} Workflow"
    # workflow templates only — a field setup (kind "fields") has no columns
    tpls = [t for t in sb.table("wf_templates").select("id,name,statuses").order("created_at").execute().data
            if t.get("statuses")]
    tpl = next((t for t in tpls if "bid pipeline" in (t.get("name") or "").lower()), tpls[0] if tpls else None)
    src = (tpl or {}).get("statuses") or [{"name": "To do", "cat": "todo"},
                                          {"name": "In progress", "cat": "doing"},
                                          {"name": "Done", "cat": "done"}]
    statuses = []
    for st in src:
        row = {"id": str(uuid.uuid4()), "name": st.get("name"), "cat": st.get("cat")}
        if tpl and st.get("id"):
            row.update({"tpl": tpl["id"], "tc": st["id"]})
        statuses.append(row)
    used = {sp["key"] for sp in spaces}
    key, n = "WF", 2
    while key in used:
        key, n = f"WF{n}", n + 1
    return sb.table("wf_spaces").insert({
        "division_id": division_id, "name": name, "key": key, "statuses": statuses,
    }).execute().data[0]


def create_workflow_bid(division_id, project_key, title, due=None, source_url=None, jira_key=None):
    """Add a project to its division's workflow space on the bid board, in
    the space's first column. Returns the bid id; calling it again for the
    same project returns the same bid instead of a duplicate (also enforced
    by wf_bids_source_uidx)."""
    sb = get_client()

    def existing():
        r = (sb.table("wf_bids").select("id").eq("division_id", division_id)
             .eq("source_project_id", project_key).limit(1).execute().data)
        return r[0]["id"] if r else None

    bid_id = existing()
    if bid_id:
        _set_bid_jira_key(bid_id, jira_key)
        return bid_id
    space = _workflow_space(division_id)
    bids = sb.table("wf_bids").select("num,rank").eq("space_id", space["id"]).execute().data
    num = max([b["num"] for b in bids] + [0]) + 1
    num = max(num, space.get("next") or 1)
    rank = max([b.get("rank") or 0 for b in bids] + [0]) + 1000
    sb.table("wf_spaces").update({"next": num + 1}).eq("id", space["id"]).execute()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        bid_id = sb.table("wf_bids").insert({
            "space_id": space["id"], "division_id": division_id, "num": num, "rank": rank,
            "type": "Task", "title": title.strip(), "descr": "",
            "status": (space.get("statuses") or [{}])[0].get("id"),
            "priority": "Medium", "reporter": "Bid Scout", "due": due,
            "source_project_id": project_key, "source_url": source_url,
            "history": [{"by": "Bid Scout", "at": now_ms, "field": "created"}],
        }).execute().data[0]["id"]
    except Exception:
        # Lost a race with another click on the same project: return the winner.
        bid_id = existing()
        if not bid_id:
            raise
    _set_bid_jira_key(bid_id, jira_key)
    try:
        _fill_workflow_bid(division_id, bid_id, project_key, load_project_bid_times(division_id).get(project_key))
    except Exception as e:
        print(f"  ! couldn't fill in time/site on bid for {project_key} ({e})")
    return bid_id


def _set_bid_jira_key(bid_id, jira_key):
    """Remember the Jira issue a board bid goes with (so the Jira sync updates it
    rather than adding a copy). Skipped quietly before workflow/sql/010_jira_sync.sql."""
    if not jira_key:
        return
    try:
        get_client().table("wf_bids").update({"jira_key": jira_key}).eq("id", bid_id).execute()
    except Exception as e:
        print(f"  ! couldn't note Jira key {jira_key} on bid {bid_id} ({e})")


def bid_time_hm(text):
    """'10:00am MT' / '2:00 PM' / '14:00' -> 'HH:MM' (24-hour), else None —
    the board's due-time box takes a plain time."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*(?:([ap])\.?\s*m\b\.?)?", text or "", re.I)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mn:02d}" if h < 24 and mn < 60 else None


def _space_fields(space_id):
    """The custom fields a board space shows: its field setup (wf_spaces.field_set,
    else the oldest kind="fields" template — the default), or before field setups
    existed, the fields of the workflow template it follows."""
    sb = get_client()
    try:
        space = sb.table("wf_spaces").select("statuses,field_set").eq("id", space_id).limit(1).execute().data
        sets = (sb.table("wf_templates").select("id,fields").eq("kind", "fields")
                .order("created_at").execute().data)
    except Exception:
        space = sb.table("wf_spaces").select("statuses").eq("id", space_id).limit(1).execute().data
        sets = []
    if not space:
        return []
    if sets:
        pick = next((t for t in sets if t["id"] == space[0].get("field_set")), sets[0])
        return pick.get("fields") or []
    tpl_id = ((space[0].get("statuses") or [{}])[0] or {}).get("tpl")
    if not tpl_id:
        return []
    tpl = sb.table("wf_templates").select("fields").eq("id", tpl_id).limit(1).execute().data
    return (tpl[0].get("fields") or []) if tpl else []


def _fill_workflow_bid(division_id, bid_id, project_key, bid_time):
    """Fill a new bid's blank due time (from the project's bid time), its
    "Original RFQ" field (the site Bid Scout found it on) and its Team (the
    project's state — see project_team_name). Never overwrites something
    already there. Fields are found by name/type on the template the bid's
    space follows, since field ids differ between templates."""
    sb = get_client()
    bid = sb.table("wf_bids").select("space_id,due,due_time,fields").eq("id", bid_id).limit(1).execute().data
    if not bid:
        return
    bid = bid[0]
    patch = {}
    hm = bid_time_hm(bid_time)
    if hm and bid.get("due") and not bid.get("due_time"):
        patch["due_time"] = hm
    site, _ = _split_site(project_key)
    fields = dict(bid.get("fields") or {})
    tfields = _space_fields(bid["space_id"])
    if site and tfields:
        rfq = next((f["id"] for f in tfields if (f.get("name") or "").strip().lower() == "original rfq"), None)
        if rfq and not fields.get(rfq):
            fields[rfq] = site
            patch["fields"] = fields
        team_f = next((f["id"] for f in tfields if f.get("type") == "team"), None)
        if team_f and not fields.get(team_f):
            team_id = team_for_project(division_id, project_key)
            if team_id:
                fields[team_f] = team_id
                patch["fields"] = fields
    if patch:
        sb.table("wf_bids").update(patch).eq("id", bid_id).execute()


def _board_date(iso):
    """'2026-10-23' -> 'Oct 23', as the board writes dates into a bid's history."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return "None"
    return f"{d.strftime('%b')} {d.day}"


def _board_time(hm):
    """'14:00' -> '2:00 PM', as the board writes times into a bid's history."""
    if not hm:
        return "None"
    h, m = (int(x) for x in hm[:5].split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def sync_workflow_bid_due(division_id, dates=None, times=None):
    """A scan saw a project's bid date ({project_key: 'YYYY-MM-DD'}) or bid
    time ({project_key: 'HH:MM'}) change — move the due date/time of any
    board bid made from that project to match, logged in the bid's history
    as Bid Scout. Only called on a change, so a date set by hand on the
    board stays until Bid Scout's own date moves again. Never fails a scan."""
    dates, times = dates or {}, times or {}
    keys = list(set(dates) | set(times))
    try:
        bids = (
            get_client().table("wf_bids").select("id,source_project_id,due,due_time,history")
            .eq("division_id", division_id).in_("source_project_id", keys).execute().data
        )
    except Exception as e:
        print(f"  ! couldn't check board bids for date changes ({e})")
        return
    now = datetime.now(timezone.utc)
    at = int(now.timestamp() * 1000)
    for b in bids:
        key = b["source_project_id"]
        patch, hist = {}, []
        if key in dates and dates[key] != b.get("due"):
            patch["due"] = dates[key]
            hist.append({"by": "Bid Scout", "at": at, "field": "Due date",
                         "from": _board_date(b.get("due")), "to": _board_date(dates[key])})
        cur = (b.get("due_time") or "")[:5]
        if key in times and times[key] != cur:
            patch["due_time"] = times[key]
            hist.append({"by": "Bid Scout", "at": at, "field": "Due time",
                         "from": _board_time(cur), "to": _board_time(times[key])})
        if not patch:
            continue
        patch["history"] = (b.get("history") or []) + hist
        patch["updated_at"] = now.isoformat()
        try:
            get_client().table("wf_bids").update(patch).eq("id", b["id"]).execute()
            print(f"  · bid board: moved {key} due to {patch.get('due', b.get('due'))} {patch.get('due_time', cur)}")
        except Exception as e:
            print(f"  ! couldn't update board bid for {key} ({e})")


def board_links(division_id, strict=False):
    """{project_key: bid_id} for every board bid made from (or linked to) a
    Bid Scout project. Empty if the board's tables aren't there — or None
    with strict, so a caller can tell "no bids" from "couldn't look"."""
    try:
        rows = (get_client().table("wf_bids").select("id,source_project_id")
                .eq("division_id", division_id).not_.is_("source_project_id", "null").execute().data)
    except Exception:
        return None if strict else {}
    return {r["source_project_id"]: r["id"] for r in rows}


# Projects put on the board from this day on are expected to have a bid
# there (Add to Board reliably made one from Oct 21); older Jira-only ones
# never did, so they're left alone.
BOARD_SINCE = "2026-10-21"
# Add to Board saves the decision a moment before the bid exists.
BOARD_GRACE = timedelta(minutes=2)


def _return_deleted_board_bids(division_id, decisions, board):
    """A project on the Bid Board tab whose bid was deleted off the board goes
    back to To Review (saved, with source "board" so the fit suggestions
    don't learn from it). Updates `decisions` in place."""
    if board is None:
        return
    cutoff = (datetime.now(timezone.utc) - BOARD_GRACE).isoformat()
    for key, d in list(decisions.items()):
        at = d.get("decided_at") or ""
        if d.get("decision") != "jira" or key in board or not (BOARD_SINCE <= at[:10] and at < cutoff):
            continue
        try:
            set_project_decision(division_id, key, None, source="board")
            del decisions[key]
            print(f"  · {key}: its bid was deleted off the board — back to To Review")
        except Exception as e:
            print(f"  ! couldn't move {key} back to review ({e})")


def _file_linked_board_bids(division_id, decisions, board, jira_keys):
    """A project tied to a board bid (or a Jira issue) but still undecided —
    e.g. linked by the Jira import — belongs on the Bid Board tab, so it's
    saved as "jira" (source "board": not a fit signal). Updates `decisions`.
    One a person last moved back to review is left where they put it; one
    the board moved back (its bid was gone for a while) is filed again."""
    todo = [k for k in set(board or {}) | {k for k, v in (jira_keys or {}).items() if v}
            if k not in decisions]
    if not todo:
        return
    try:
        last = {}
        for r in (get_client().table("project_decisions")
                  .select("project_key,source").eq("division_id", division_id)
                  .in_("project_key", todo).order("decided_at").execute().data):
            last[r["project_key"]] = r.get("source")
        seen = {k for k, src in last.items() if src != "board"}
    except Exception:
        return
    for key in todo:
        if key in seen:
            continue
        try:
            set_project_decision(division_id, key, "jira", source="board")
            decisions[key] = {"decision": "jira", "reason": None, "note": None,
                              "decided_at": datetime.now(timezone.utc).isoformat()}
            print(f"  · {key}: already on the board — filed under Bid Board")
        except Exception as e:
            print(f"  ! couldn't file {key} under Bid Board ({e})")


def unlinked_workflow_bids(division_id):
    """Board bids not tied to any Bid Scout project yet — typed in by hand —
    as [{"id", "key", "title", "due"}] (key like "BW-14")."""
    sb = get_client()
    bids = (sb.table("wf_bids").select("id,space_id,num,title,due")
            .eq("division_id", division_id).is_("source_project_id", "null").execute().data)
    keys = {sp["id"]: sp["key"] for sp in
            sb.table("wf_spaces").select("id,key").eq("division_id", division_id).execute().data}
    return [{"id": b["id"], "key": f"{keys.get(b['space_id'], 'BID')}-{b['num']}",
             "title": b["title"], "due": b.get("due")} for b in bids]


def link_workflow_bid(division_id, bid_id, project_key, source_url=None):
    """Tie a bid typed in by hand on the board to a Bid Scout project and
    bring it up to date: due date and time from Bid Scout (when it has
    them), a blank Original RFQ / Team filled in, and from then on its Bid
    Docs and Plan Holders tabs follow the project. Logged in the bid's
    history as Bid Scout. Raises ValueError if either side is already linked."""
    sb = get_client()
    rows = (sb.table("wf_bids").select("id,division_id,source_project_id,due,due_time,history")
            .eq("id", bid_id).limit(1).execute().data)
    if not rows or rows[0]["division_id"] != division_id:
        raise ValueError("that bid isn't on this division's board")
    b = rows[0]
    if b.get("source_project_id"):
        raise ValueError("that bid is already linked to a Bid Scout project")
    if board_links(division_id).get(project_key):
        raise ValueError("this project is already on the board")
    _, name = _split_site(project_key)
    now = datetime.now(timezone.utc)
    at = int(now.timestamp() * 1000)
    hist = [{"by": "Bid Scout", "at": at, "field": "Bid Scout project", "from": "None", "to": name}]
    patch = {"source_project_id": project_key, "source_url": source_url}
    bid_date = load_project_bid_dates(division_id).get(project_key)
    bid_time = load_project_bid_times(division_id).get(project_key)
    hm = bid_time_hm(bid_time)
    if bid_date and bid_date != b.get("due"):
        patch["due"] = bid_date
        hist.append({"by": "Bid Scout", "at": at, "field": "Due date",
                     "from": _board_date(b.get("due")), "to": _board_date(bid_date)})
    cur = (b.get("due_time") or "")[:5]
    if hm and (bid_date or b.get("due")) and hm != cur:
        patch["due_time"] = hm
        hist.append({"by": "Bid Scout", "at": at, "field": "Due time",
                     "from": _board_time(cur), "to": _board_time(hm)})
    patch["history"] = (b.get("history") or []) + hist
    patch["updated_at"] = now.isoformat()
    sb.table("wf_bids").update(patch).eq("id", bid_id).execute()
    _fill_workflow_bid(division_id, bid_id, project_key, bid_time)
    return bid_id


BID_FILES_BUCKET = "bid-files"


def load_workflow_bid(bid_id):
    """One bid board bid's {"id", "division_id", "title", "source_project_id",
    "attachments"}, or None."""
    r = (get_client().table("wf_bids").select("id,division_id,title,source_project_id,attachments")
         .eq("id", bid_id).limit(1).execute().data)
    return r[0] if r else None


def download_bid_file(path):
    """Bytes of a file uploaded on the bid board (the private "bid-files"
    bucket), or None."""
    try:
        return get_client().storage.from_(BID_FILES_BUCKET).download(path)
    except Exception as e:
        print(f"  ! couldn't fetch bid file {path} ({e})")
        return None


# Which state a site's projects are in, from its adapter / address / name.
# ConstructConnect covers several states, so it isn't here — its projects
# use their saved location instead.
_SITE_STATES = (("udot", "Utah"), ("utah.gov", "Utah"), ("itd_", "Idaho"), ("idaho.gov", "Idaho"),
                ("idot", "Idaho"), ("wydot", "Wyoming"), ("wyo", "Wyoming"), ("ndot", "Nevada"),
                ("nv.gov", "Nevada"))
_udot_regions = {"at": 0, "map": {}}
_STATE_NAMES = {"UT": "Utah", "ID": "Idaho", "NV": "Nevada", "WY": "Wyoming", "AZ": "Arizona",
                "CO": "Colorado", "MT": "Montana", "NM": "New Mexico", "OR": "Oregon",
                "WA": "Washington", "CA": "California", "TX": "Texas"}
# Utah County ZIPs in the 846xx range (the rest of 846xx is Juab, Sanpete
# and Millard — Southern Utah).
_UTAH_COUNTY_846 = {"84601", "84602", "84603", "84604", "84605", "84606", "84626", "84633",
                    "84651", "84653", "84655", "84660", "84663", "84664"}


def _utah_zip_is_north(zipcode):
    """Utah County and north = Northern Utah. By ZIP: 840/841/843/844 are the
    Wasatch Front, Cache, Box Elder, Summit, Wasatch, Tooele and the Uintah
    Basin; 845 (Price, Moab, San Juan) and 847 (St. George, Cedar City,
    Richfield) are south; 846 splits — Utah County north, the rest south."""
    p = zipcode[:3]
    if p in ("840", "841", "843", "844"):
        return True
    if p == "846":
        return zipcode in _UTAH_COUNTY_846
    return False


def _udot_region(project_name):
    """UDOT's region number (1-4) for an advertised project, from Contractor
    Zone's public list (cached 10 minutes), or None."""
    import time
    if time.time() - _udot_regions["at"] > 600:
        try:
            r = requests.get("https://contractorzone.udot.utah.gov/advertisements/projects",
                             params={"section": "advertisements"}, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; BidScoutBot/1.0)"})
            r.raise_for_status()
            m = {}
            for p in r.json().get("result") or []:
                num = re.search(r"\d+", p.get("region") or "")
                if num:
                    for k in (p.get("project_name"), p.get("project_number")):
                        if k:
                            m[k.strip().lower()] = int(num.group())
            _udot_regions.update(at=time.time(), map=m)
        except Exception as e:
            print(f"  ! couldn't read UDOT regions ({e})")
    return _udot_regions["map"].get((project_name or "").strip().lower())


def project_team_name(division_id, project_key):
    """The bid board team a project belongs to: its state, with Utah split at
    the south edge of Utah County. Uses the project's saved location when
    there is one (ConstructConnect: state + ZIP), else the site's state —
    UDOT Regions 1-3 are "Northern Utah", Region 4 "Southern Utah" (plain
    "Utah" when the region isn't known). None when the state isn't known."""
    site, project = _split_site(project_key)
    try:
        rows = (get_client().table("sites").select("name,adapter,url")
                .eq("division_id", division_id).eq("name", site).limit(1).execute().data)
    except Exception:
        rows = []
    hay = " ".join(str(rows[0].get(k) or "") for k in ("adapter", "url", "name")).lower() if rows else site.lower()
    loc = None
    try:
        fr = (get_client().table("project_flags").select("location")
              .eq("division_id", division_id).eq("project_key", project_key).limit(1).execute().data)
        loc = fr[0].get("location") if fr else None
    except Exception:
        pass
    m = re.search(r",\s*([A-Z]{2})\s+(\d{5})", loc or "")
    if m:
        state = _STATE_NAMES.get(m.group(1))
        if state != "Utah":
            return state
        return "Northern Utah" if _utah_zip_is_north(m.group(2)) else "Southern Utah"
    state = next((st for needle, st in _SITE_STATES if needle in hay), None)
    if state != "Utah":
        return state
    region = _udot_region(project) if "udot" in hay else None
    if region in (1, 2, 3):
        return "Northern Utah"
    if region == 4:
        return "Southern Utah"
    return "Utah"


def team_for_project(division_id, project_key):
    """The id of the division's bid board team named for the project's state
    (see project_team_name), or None if there's no such team."""
    name = project_team_name(division_id, project_key)
    if not name:
        return None
    try:
        teams = get_client().table("wf_teams").select("id,name").eq("division_id", division_id).execute().data
    except Exception:
        return None
    return next((t["id"] for t in teams if (t.get("name") or "").strip().lower() == name.lower()), None)


def duplicate_keys(division_id, project_key):
    """Keys of the projects marked as duplicates of this one."""
    return [k for k, d in load_project_decisions(division_id).items()
            if d.get("decision") == "duplicate" and d.get("duplicate_of") == project_key]


def load_project_files(division_id, project_key, with_duplicates=True):
    """[{"filename", "url", "status", "keywords"}] — one project's documents,
    newest row per filename (shown without the leading "<project> - "), for
    the bid board's Bid Docs tab. Paged, since a ConstructConnect project can
    have hundreds of files. Projects marked as its duplicates add theirs
    under a "From <site>/" folder."""
    out = _load_project_files(division_id, project_key)
    if with_duplicates:
        for dk in duplicate_keys(division_id, project_key):
            site, name = _split_site(dk)
            for f in _load_project_files(division_id, dk):
                out.append(dict(f, filename=f"From {site or name}/{f['filename']}"))
    return out


def _load_project_files(division_id, project_key):
    rows, start, step = [], 0, 1000
    while True:
        res = (
            get_client().table("scan_results")
            .select("filename,document_url,status,matched_keywords,run_date")
            .eq("division_id", division_id).eq("site", project_key)
            .order("run_date", desc=True).range(start, start + step - 1).execute()
        )
        rows.extend(res.data)
        if len(res.data) < step:
            break
        start += step
    _, project = _split_site(project_key)
    seen, out = set(), []
    for r in rows:
        fn = r.get("filename") or ""
        if not fn or fn in seen or not r.get("document_url"):
            continue
        seen.add(fn)
        # Filenames are stored as "<project> - <path>"; the bid already says which project.
        name = fn[len(project) + 3:] if project and fn.startswith(project + " - ") else fn
        out.append({
            "filename": name, "url": r["document_url"], "status": r.get("status") or "",
            "keywords": [k.strip() for k in (r.get("matched_keywords") or "").split(",") if k.strip()],
        })
    return out


def fit_training_stats(division_id):
    """{"decisions", "needed", "enabled", "sure_at", "active", "agreed",
    "compared"} for the dashboard: how many decisions the suggestions can learn from (same rules
    as fit_predictor.load_examples) and — once suggestions exist — how often
    the user's decision matched the suggestion showing at the time. Cached
    briefly; the results list polls."""
    hit = _fit_stats_cache.get(division_id)
    now = datetime.now(timezone.utc).timestamp()
    if hit and now - hit[0] < _FIT_STATS_TTL_S:
        return hit[1]
    settings = get_division_settings(division_id)
    min_decisions = settings["fit_min_decisions"]
    try:
        res = (
            get_client().table("project_decisions")
            .select("project_key,decision,reason,source,prediction:snapshot->prediction")
            .eq("division_id", division_id).order("decided_at", desc=True).execute()
        )
        rows = res.data
    except Exception:
        rows = []
    latest = {}
    for r in rows:
        latest.setdefault(r["project_key"], r)
    usable = [r for r in latest.values()
              if r.get("source") == "user" and r["decision"] in ("jira", "not_fit")
              and r.get("reason") != LEGACY_REASON]
    compared = [r for r in usable if isinstance(r.get("prediction"), dict)
                and r["prediction"].get("verdict")]
    agreed = sum(1 for r in compared
                 if (r["prediction"]["verdict"] == "fit") == (r["decision"] == "jira"))
    stats = {"decisions": len(usable), "needed": min_decisions,
             "enabled": settings["fit_enabled"], "sure_at": settings["fit_sure_at"],
             "active": settings["fit_enabled"] and len(usable) >= min_decisions,
             "agreed": agreed, "compared": len(compared)}
    _fit_stats_cache[division_id] = (now, stats)
    return stats


def load_docs_ack(division_id):
    """{project_key: iso} — the watermark up to which the user has acknowledged
    a project's documents (set when they mark it done). Empty if the column
    isn't there yet."""
    try:
        res = (
            get_client().table("project_flags")
            .select("project_key,docs_ack_through")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: r["docs_ack_through"]
            for r in res.data if r.get("docs_ack_through")}


def load_project_bid_dates(division_id):
    """{project_key: 'YYYY-MM-DD'} for projects with a known bid-opening date.
    Empty if the column/table isn't there yet."""
    try:
        res = (
            get_client().table("project_flags").select("project_key,bid_date")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: r["bid_date"] for r in res.data if r.get("bid_date")}


def save_project_bid_dates(division_id, mapping):
    """Upsert {project_key: 'YYYY-MM-DD'} from a scan. Only touches bid_date,
    so a project's `done` flag is left alone. No-ops if the table/column is
    missing (a scan shouldn't fail over this)."""
    rows = [
        {"division_id": division_id, "project_key": k, "bid_date": v,
         "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in mapping.items() if v
    ]
    if not rows:
        return
    old = load_project_bid_dates(division_id)
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save bid dates ({e})")
        return
    changed = {k: v for k, v in mapping.items() if v and old.get(k) != v}
    if changed:
        sync_workflow_bid_due(division_id, dates=changed)


def load_project_jira_keys(division_id):
    """{project_key: 'GEO-23'} for projects already filed to Jira (via "Add
    to Jira"). Empty if the column/table isn't there yet."""
    try:
        res = (
            get_client().table("project_flags").select("project_key,jira_key")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: r["jira_key"] for r in res.data if r.get("jira_key")}


def set_project_jira_key(division_id, project_key, jira_key):
    """Record the Jira issue key created for a project. Raises ValueError
    with a migration hint if the column is missing."""
    try:
        get_client().table("project_flags").upsert({
            "division_id": division_id,
            "project_key": project_key,
            "jira_key": jira_key,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        raise ValueError(
            "couldn't save — add the jira_key column from schema.sql"
        ) from e


def load_project_bid_times(division_id):
    """{project_key: raw display string} (e.g. '10:00am MT') for projects
    whose source portal shows a time alongside the bid-opening date — so far
    only ConstructConnect. Empty if the column/table isn't there yet."""
    try:
        res = (
            get_client().table("project_flags").select("project_key,bid_time")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: r["bid_time"] for r in res.data if r.get("bid_time")}


def save_project_bid_times(division_id, mapping):
    """Upsert {project_key: raw display string} from a scan. Only touches
    bid_time, same as save_project_bid_dates. No-ops if the table/column is
    missing."""
    rows = [
        {"division_id": division_id, "project_key": k, "bid_time": v,
         "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in mapping.items() if v
    ]
    if not rows:
        return
    old = load_project_bid_times(division_id)
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save bid times ({e})")
        return
    # Compared as clock times, so "2:00pm MT" -> "2:00 PM MT" isn't a change.
    changed = {k: bid_time_hm(v) for k, v in mapping.items()
               if bid_time_hm(v) and bid_time_hm(old.get(k)) != bid_time_hm(v)}
    if changed:
        sync_workflow_bid_due(division_id, times=changed)


def save_project_plan_holders(division_id, mapping):
    """Upsert {project_key: [plan-holder dict, ...]} from a scan (see
    adapters.plan_holder for the dict shape). Replaces the whole list each
    time — a refresh, not an append, so companies that drop off the portal's
    list drop off here too. Only touches plan_holders/plan_holders_at.
    No-ops if the columns are missing."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"division_id": division_id, "project_key": k, "plan_holders": v,
         "plan_holders_at": now, "updated_at": now}
        for k, v in mapping.items() if v is not None
    ]
    if not rows:
        return
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save plan holders ({e})")


def save_project_locations(division_id, mapping):
    """Upsert {project_key: "City, ST 12345"} from a scan. Only touches
    location. No-ops if the column is missing."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"division_id": division_id, "project_key": k, "location": v, "updated_at": now}
            for k, v in mapping.items() if v]
    if not rows:
        return
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save project locations ({e})")


def load_project_plan_holders(division_id, project_key, with_duplicates=True):
    """(holders_list_or_None, refreshed_at_iso_or_None) for one project.
    None = never fetched (or the site doesn't publish one / the column isn't
    there yet), as opposed to [] = fetched and the list was empty. Holders
    from projects marked as its duplicates are added (same company once)."""
    holders, at = _load_project_plan_holders(division_id, project_key)
    if not with_duplicates:
        return holders, at
    for dk in duplicate_keys(division_id, project_key):
        more, more_at = _load_project_plan_holders(division_id, dk)
        if more is None:
            continue
        have = {(h.get("company") or "").strip().lower() for h in holders or []}
        holders = (holders or []) + [h for h in more
                                     if (h.get("company") or "").strip().lower() not in have]
        at = max(filter(None, [at, more_at]), default=None)
    return holders, at


def _load_project_plan_holders(division_id, project_key):
    try:
        res = (
            get_client().table("project_flags").select("plan_holders,plan_holders_at")
            .eq("division_id", division_id).eq("project_key", project_key)
            .limit(1).execute()
        )
    except Exception:
        return None, None
    if not res.data:
        return None, None
    return res.data[0].get("plan_holders"), res.data[0].get("plan_holders_at")


def load_project_last_updated(division_id):
    """{project_id: 'Oct 4, 2026'} — the ConstructConnect results grid's own
    "Last Updated" date as of the last time each project was actually
    opened/downloaded, keyed by the project's numeric ConstructConnect id
    (not "SiteName — label" like bid_date/bid_time). Lets the adapter skip
    re-opening a project whose grid date hasn't changed since. Empty if the
    column/table isn't there yet."""
    try:
        res = (
            get_client().table("project_flags").select("project_key,cc_last_updated")
            .eq("division_id", division_id).execute()
        )
    except Exception:
        return {}
    return {r["project_key"]: r["cc_last_updated"] for r in res.data if r.get("cc_last_updated")}


def save_project_last_updated(division_id, mapping):
    """Upsert {project_id: 'Oct 4, 2026'} from a scan. Only touches
    cc_last_updated. No-ops if the table/column is missing."""
    rows = [
        {"division_id": division_id, "project_key": k, "cc_last_updated": v,
         "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in mapping.items() if v
    ]
    if not rows:
        return
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save last-updated watermarks ({e})")


_SCAN_RESULTS_PAGE_SIZE = 1000

# get_stats() and get_results_grouped() each independently call this with
# columns="*" to re-derive their own view of the same data, and the
# dashboard's poll loop calls both back-to-back every few seconds — without
# this, that's two full paginated re-fetches of the whole division's scan
# history (thousands of rows, every column) in quick succession, repeated on
# every poll tick. A few seconds of staleness is invisible anyway (the
# dashboard itself only polls this often), so a short cache collapses those
# into one real fetch per window. Per-process only (each gunicorn worker has
# its own), which is fine — it only needs to survive back-to-back calls
# within the same request burst, not stay consistent across workers.
_scan_results_cache = {}
_SCAN_RESULTS_CACHE_TTL_S = 8


def _fetch_all_scan_results(division_id, columns="*"):
    """Every scan_results row for a division, paginated past PostgREST's
    default row cap. A flat .limit(N) here used to silently lose data once a
    division's row count crossed N: ordering by run_date desc means the
    newest N rows win, and one adapter logging hundreds of rows in a single
    run (ConstructConnect's per-document zip extraction, 2026-09-20) could
    fill that whole window by itself, crowding out other sites' older rows
    entirely — they'd still exist in the table, just never get read. Paging
    with .range() instead means every row is always seen regardless of how
    lopsided one site's row count gets."""
    import time
    cache_key = (division_id, columns)
    now = time.monotonic()
    cached = _scan_results_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    client = get_client()
    rows, start = [], 0
    while True:
        batch = (
            client.table("scan_results").select(columns)
            .eq("division_id", division_id).order("run_date", desc=True)
            .range(start, start + _SCAN_RESULTS_PAGE_SIZE - 1).execute()
        ).data
        rows.extend(batch)
        if len(batch) < _SCAN_RESULTS_PAGE_SIZE:
            _scan_results_cache[cache_key] = (now + _SCAN_RESULTS_CACHE_TTL_S, rows)
            return rows
        start += _SCAN_RESULTS_PAGE_SIZE


def get_results_grouped(division_id, search=None, status=None, site=None, keyword=None,
                        bid_window=None, sort=None, include_closed=False,
                        updated_only=False, new_only=False, view=None,
                        project_key=None, page=1, page_size=15):
    """Scan results collapsed to one entry per project (the `site` value),
    each project's files nested underneath. Filtering and pagination happen
    over the grouped projects. Returns (projects, total_project_count,
    site_tabs, updated_total, new_total) where site_tabs is
    [{"name", "count", "flagged"}] reflecting every other active filter
    (status, search, keyword, bid window, review/jira/not-fit view,
    updated/new toggles) but not the `site` filter itself — so each tab's count matches
    what selecting it actually shows. updated_total is the count of projects
    with an unacknowledged "new document" notification, and new_total is the
    count of matched projects still inside their "New" window (see
    _is_new_project) — both of those are global badge counts over every
    visible project, not scoped to the current filters.

    Projects the source site no longer lists ("closed") are dropped unless
    include_closed is set, in which case each carries closed=True.
    """
    rows = _fetch_all_scan_results(division_id)

    # Newest row wins per (project, filename), so a re-scan doesn't duplicate.
    seen, latest = set(), []
    for r in rows:
        key = (r.get("site"), r.get("filename"))
        if key in seen:
            continue
        seen.add(key)
        latest.append(r)

    groups = {}
    for r in latest:
        site_key = r.get("site") or "(unknown)"
        prefix, project = _split_site(site_key)
        g = groups.setdefault(site_key, {
            "project": project, "source_prefix": prefix,
            "source_url": None, "latest_date": r.get("run_date") or "", "files": [],
        })
        if r.get("source_url") and not g["source_url"]:
            g["source_url"] = r["source_url"]
        if (r.get("run_date") or "") > g["latest_date"]:
            g["latest_date"] = r["run_date"]
        g["files"].append({
            "filename": r.get("filename") or "",
            "url": r.get("document_url") or "",
            "status": r.get("status") or "",
            "run_date": r.get("run_date") or "",
            "keywords": [k.strip() for k in (r.get("matched_keywords") or "").split(",") if k.strip()],
            "locations": r.get("keyword_locations") or "",
            "ai_notes": r.get("ai_notes") or "",
            "misses": r.get("misses") or 0,
            "last_seen_at": r.get("last_seen_at"),
        })

    decisions = load_project_decisions(division_id)
    fits = load_project_fit(division_id)
    bid_dates = load_project_bid_dates(division_id)
    bid_times = load_project_bid_times(division_id)
    jira_keys = load_project_jira_keys(division_id)
    board = board_links(division_id, strict=True)
    _return_deleted_board_bids(division_id, decisions, board)
    board = board or {}
    _file_linked_board_bids(division_id, decisions, board, jira_keys)
    docs_ack = load_docs_ack(division_id)
    # "New" badge visibility window — a match stops counting as new after
    # this long even if it's never acknowledged via "Mark done".
    new_cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEW_BADGE_HOURS)).isoformat()
    today_str = date.today().isoformat()

    projects = []
    for site_key, g in groups.items():
        real_files = [f for f in g["files"] if f["filename"]]
        gradeable = [f for f in real_files if f["status"] != "Download failed"]
        closed = bool(gradeable) and all(f["misses"] >= CLOSED_AFTER_MISSES for f in gradeable)
        last_seen = max((f["last_seen_at"] for f in g["files"] if f["last_seen_at"]), default=None)
        matched = sum(1 for f in g["files"] if f["status"].startswith("Matched"))
        failed = sum(1 for f in real_files if f["status"] == "Download failed")
        kws = []
        for f in g["files"]:
            for k in f["keywords"]:
                if k not in kws:
                    kws.append(k)
        if not real_files:
            proj_status = g["files"][0]["status"] if g["files"] else ""
        elif matched:
            proj_status = "Matched"
        elif failed == len(real_files):
            proj_status = "Download failed"
        else:
            proj_status = "No match"

        # "New document" flag: a file counts as new when it itself matched a
        # keyword (a file with no match, or one that failed to download,
        # wasn't actually a find — it shouldn't raise the flag even if some
        # other file in the project did), post-dates both the project's
        # first run_date (so a brand-new project isn't "updated") and the
        # user's acknowledgement watermark (set by "Mark done"), AND is
        # within the last NEW_BADGE_HOURS — otherwise an unacknowledged
        # match would stay flagged "New" forever.
        real_runs = [f["run_date"] for f in real_files if f["run_date"]]
        first_run = min(real_runs) if real_runs else ""
        ack = docs_ack.get(site_key) or ""
        new_file_count = 0
        for f in g["files"]:
            f["is_new"] = bool(
                f["filename"] and f["run_date"] and f["status"].startswith("Matched")
                and f["run_date"] > first_run and f["run_date"] > ack
                and f["run_date"] > new_cutoff
            )
            if f["is_new"]:
                new_file_count += 1
        is_updated = new_file_count > 0
        # Which tab it lives in. "Not a fit" comes back to review when a
        # newer matching document has arrived since the decision — checked
        # without the "New" badge's time window, so it stays in review
        # until someone decides again rather than drifting back by itself.
        d = decisions.get(site_key) or {}
        decision = d.get("decision")
        bucket = (decision if decision in DECISIONS
                  else "not_fit" if decision == "duplicate" else "review")
        reopened = False
        if decision == "not_fit" and any(
            f["filename"] and f["run_date"] and f["status"].startswith("Matched")
            and f["run_date"] > first_run and f["run_date"] > ack
            for f in g["files"]
        ):
            bucket, reopened = "review", True
        # Undecided and nothing matched a keyword -> straight to Not a Fit,
        # worked out fresh on every load rather than saved, so it moves to
        # To Review by itself the moment a matching document turns up — and
        # it never lands in project_decisions, so the fit suggestions only
        # learn from calls the user actually made.
        auto_reason = None
        if not decision and proj_status != "Matched":
            bucket = "not_fit"
            auto_reason = ("Couldn't download the documents" if proj_status == "Download failed"
                           else "No keyword matches")
        is_done = bucket != "review"
        bid_date = bid_dates.get(site_key)
        # Bid already opened and nobody acted on it — faded in the list (not
        # hidden; still here to act on) and, separately, the scanner's daily
        # retention sweep clears any stored document bytes for it (see
        # supabase_store.list_cached_documents_for_overdue_projects).
        overdue = bool(bid_date and bid_date < today_str and not is_done)

        projects.append({
            "key": site_key,
            "project": g["project"], "source_prefix": g["source_prefix"],
            "source_url": g["source_url"], "latest_date": g["latest_date"],
            "file_count": len(real_files), "matched_file_count": matched,
            "keywords": kws, "status": proj_status,
            "done": is_done,
            "bucket": bucket, "decision": decision,
            "decision_reason": auto_reason or d.get("reason"), "decision_note": d.get("note"),
            "auto_not_fit": bool(auto_reason),
            "decided_at": d.get("decided_at"),
            "duplicate_of": d.get("duplicate_of") if decision == "duplicate" else None,
            "duplicates": [],
            "fit": fits.get(site_key),
            "updated": is_updated, "new_file_count": new_file_count,
            "reopened": reopened,
            "closed": closed, "overdue": overdue, "last_seen_at": last_seen,
            "bid_date": bid_date,
            "bid_time": bid_times.get(site_key),
            "jira_key": jira_keys.get(site_key),
            "board_bid_id": board.get(site_key),
            "first_seen": first_run or None,
            "files": sorted(g["files"], key=lambda f: f["filename"]),
        })

    # Duplicates and their originals point at each other, and the original
    # also lists the duplicate's documents (in a "From <site>" folder).
    by_key = {p["key"]: p for p in projects}
    for p in projects:
        orig = by_key.get(p["duplicate_of"]) if p["duplicate_of"] else None
        if p["duplicate_of"]:
            o_site, o_name = _split_site(p["duplicate_of"])
            p["duplicate_of_name"] = o_name
            p["duplicate_of_site"] = o_site
        if not orig or orig is p:
            continue
        orig["duplicates"].append({"key": p["key"], "project": p["project"],
                                   "source_prefix": p["source_prefix"], "source_url": p["source_url"]})
        orig["files"] = orig["files"] + [dict(f, from_site=p["source_prefix"] or p["project"],
                                              from_project=p["project"]) for f in p["files"]]

    # Closed projects are out of the picture entirely unless asked for — so
    # they don't inflate the per-site tab counts either.
    if not include_closed:
        projects = [p for p in projects if not p["closed"]]

    # "New document" notifications outstanding, over every visible project —
    # a global badge count, not scoped to the current filters.
    updated_total = sum(1 for p in projects if p["updated"])
    # Matched projects still inside their "New" window (see _is_new_project).
    now = datetime.now(timezone.utc)
    for p in projects:
        p["is_new"] = p["status"] == "Matched" and _is_new_project(p["first_seen"], now)
    new_total = sum(1 for p in projects if p["is_new"])

    # Per-tab totals (To Review / Jira / Not a Fit) — every project in each
    # tab, before any filter (site, search, keyword, status, …), so the tab
    # counts stay put while filtering.
    view_counts = {"review": 0, "jira": 0, "not_fit": 0}
    for p in projects:
        view_counts[p["bucket"]] += 1

    if project_key:
        projects = [p for p in projects if p["key"] == project_key]
    if updated_only:
        projects = [p for p in projects if p["updated"]]
    if new_only:
        projects = [p for p in projects if p["is_new"]]
    if keyword:
        projects = [p for p in projects if keyword in p["keywords"]]
    if search:
        s = search.lower()
        projects = [
            p for p in projects
            if s in p["project"].lower()
            or any(s in f["filename"].lower() for f in p["files"])
            or any(s in k.lower() for k in p["keywords"])
        ]

    if bid_window:
        today = date.today().isoformat()
        if bid_window == "none":
            projects = [p for p in projects if not p["bid_date"]]
        elif bid_window == "past":
            projects = [p for p in projects if p["bid_date"] and p["bid_date"] < today]
        else:
            try:
                end = (date.today() + timedelta(days=int(bid_window))).isoformat()
                projects = [p for p in projects
                            if p["bid_date"] and today <= p["bid_date"] <= end]
            except ValueError:
                pass

    if view:
        projects = [p for p in projects if p["bucket"] == view]
    if status:
        projects = [p for p in projects if p["status"].startswith(status)]

    # Per-site tab list, computed AFTER every other active filter (status,
    # search, keyword, bid window, review/jira/not-fit view, updated/new toggles) so
    # each tab's count matches what actually appears when it's selected —
    # just not filtered by the site itself, since that's the choice being
    # offered. Every project has a source_prefix in practice (every adapter
    # sets one), so a project without one just isn't offered its own tab —
    # it still shows up under "All sites".
    site_tabs = {}
    for p in projects:
        name = p["source_prefix"]
        if not name:
            continue
        t = site_tabs.setdefault(name, {"name": name, "count": 0, "flagged": 0})
        t["count"] += 1
        if p["status"] == "Matched":
            t["flagged"] += 1
    site_tabs = sorted(site_tabs.values(), key=lambda t: (-t["flagged"], t["name"].lower()))

    if site:
        projects = [p for p in projects if (p["source_prefix"] or "Other") == site]

    # Default sort is bid date, soonest first — matches the dashboard's
    # default "Bid date — soonest" selection.
    if not sort:
        sort = "bid"

    if sort == "bid":
        projects.sort(key=lambda p: p["bid_date"] or "9999-99-99")
    elif sort == "keywords":
        projects.sort(key=lambda p: (-len(p["keywords"]), p["project"].lower()))
    elif sort == "recent":
        projects.sort(key=lambda p: p["latest_date"] or "", reverse=True)
    elif sort == "name":
        projects.sort(key=lambda p: p["project"].lower())
    elif sort == "flagged":  # projects with new docs first, then matched,
                             # then most-recently-scanned
        projects.sort(key=lambda p: p["latest_date"] or "", reverse=True)
        projects.sort(key=lambda p: (
            0 if p["status"] == "Matched" else 1,
            0 if p["updated"] else 1,
        ))

    total = len(projects)
    page = max(1, page)
    if page_size is None:   # every project, for server-side lookups (duplicate picker)
        page, page_size = 1, max(1, len(projects))
    else:
        page_size = max(1, min(page_size, 50))
    start = (page - 1) * page_size
    return (projects[start:start + page_size], total, site_tabs, updated_total, new_total,
            view_counts)


def get_stats(division_id):
    """Project- and document-level counts for the Results summary, plus the
    keywords hitting the most projects."""
    rows = _fetch_all_scan_results(division_id)

    projects_all, projects_flagged = set(), set()
    docs_scanned = docs_matched = 0
    kw_projects = {}
    for r in rows:
        site = r.get("site")
        fn = r.get("filename") or ""
        hit = (r.get("match_count") or 0) > 0
        if fn:
            docs_scanned += 1
            projects_all.add(site)
        if hit:
            docs_matched += 1
            projects_flagged.add(site)
            for k in (r.get("matched_keywords") or "").split(","):
                k = k.strip()
                if k:
                    kw_projects.setdefault(k, set()).add(site)

    # Every keyword that flagged at least one project, most-hit first — the
    # dashboard's filter menu lists them all (it scrolls).
    top = sorted(((k, len(v)) for k, v in kw_projects.items()),
                 key=lambda kv: (-kv[1], kv[0].lower()))
    return {
        "projects_scanned": len(projects_all),
        "projects_flagged": len(projects_flagged),
        "documents_scanned": docs_scanned,
        "documents_matched": docs_matched,
        "top_keywords": top,
    }

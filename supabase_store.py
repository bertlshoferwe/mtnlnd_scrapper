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
    without being marked done — feeds the retention job, same as
    list_expired_cached_documents, just a different trigger (the project
    itself is moot, not just old) than a fixed day count. A later view
    still works via the normal live re-fetch path (see api_proxy_pdf) since
    is_cached_document stays True."""
    today = date.today().isoformat()
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
# Project "done" flags (user-set)
# ---------------------------------------------------------------------------

def load_done_projects(division_id):
    """Set of project keys (scan_results.site values) the user marked done.
    Returns an empty set if the project_flags table isn't there yet."""
    try:
        res = (
            get_client().table("project_flags").select("project_key")
            .eq("division_id", division_id).eq("done", True).execute()
        )
    except Exception:
        return set()
    return {r["project_key"] for r in res.data}


def set_project_done(division_id, project_key, done):
    """Upsert a project's done flag. Marking a project done also acknowledges
    every document seen on it so far (docs_ack_through = now), clearing any
    "new document" flag until the next addendum. Raises ValueError with a
    migration hint if the table is missing."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "division_id": division_id,
        "project_key": project_key,
        "done": bool(done),
        "updated_at": now,
    }
    if done:
        payload["docs_ack_through"] = now
    try:
        get_client().table("project_flags").upsert(payload).execute()
    except Exception as e:
        # The docs_ack_through column may not exist yet — retry without it.
        if "docs_ack_through" in payload:
            payload.pop("docs_ack_through", None)
            try:
                get_client().table("project_flags").upsert(payload).execute()
                return
            except Exception:
                pass
        raise ValueError(
            "couldn't save — create the project_flags table from schema.sql"
        ) from e


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
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save bid dates ({e})")


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
    try:
        get_client().table("project_flags").upsert(rows).execute()
    except Exception as e:
        print(f"  ! couldn't save bid times ({e})")


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
            return rows
        start += _SCAN_RESULTS_PAGE_SIZE


def get_results_grouped(division_id, search=None, status=None, site=None, keyword=None,
                        bid_window=None, sort=None, include_closed=False,
                        updated_only=False, new_only=False, done_only=False,
                        page=1, page_size=15):
    """Scan results collapsed to one entry per project (the `site` value),
    each project's files nested underneath. Filtering and pagination happen
    over the grouped projects. Returns (projects, total_project_count,
    site_tabs, updated_total, new_total) where site_tabs is
    [{"name", "count", "flagged"}] over ALL projects (unaffected by the
    current filters), updated_total is the count of projects with an
    unacknowledged "new document" notification, and new_total is the count
    of matched projects still inside their "New" window (see
    _is_new_project) — both counts are over every visible project, not just
    the current page/filter.

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

    done_keys = load_done_projects(division_id)
    bid_dates = load_project_bid_dates(division_id)
    bid_times = load_project_bid_times(division_id)
    jira_keys = load_project_jira_keys(division_id)
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
        is_done = (site_key in done_keys) and not is_updated
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
            "updated": is_updated, "new_file_count": new_file_count,
            "reopened": is_updated and (site_key in done_keys),
            "closed": closed, "overdue": overdue, "last_seen_at": last_seen,
            "bid_date": bid_date,
            "bid_time": bid_times.get(site_key),
            "jira_key": jira_keys.get(site_key),
            "first_seen": first_run or None,
            "files": sorted(g["files"], key=lambda f: f["filename"]),
        })

    # Closed projects are out of the picture entirely unless asked for — so
    # they don't inflate the per-site tab counts either.
    if not include_closed:
        projects = [p for p in projects if not p["closed"]]

    # Per-site tab list, computed over every (visible) project, before filters.
    # Every project has a source_prefix in practice (every adapter sets one),
    # so a project without one just isn't offered its own tab — it still
    # shows up under "All sites".
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

    # "New document" notifications outstanding, over every visible project.
    updated_total = sum(1 for p in projects if p["updated"])
    # Matched projects still inside their "New" window (see _is_new_project).
    now = datetime.now(timezone.utc)
    for p in projects:
        p["is_new"] = p["status"] == "Matched" and _is_new_project(p["first_seen"], now)
    new_total = sum(1 for p in projects if p["is_new"])

    if updated_only:
        projects = [p for p in projects if p["updated"]]
    if new_only:
        projects = [p for p in projects if p["is_new"]]
    if done_only:
        projects = [p for p in projects if p["done"]]
    if site:
        projects = [p for p in projects if (p["source_prefix"] or "Other") == site]
    if keyword:
        projects = [p for p in projects if keyword in p["keywords"]]
    if status:
        projects = [p for p in projects if p["status"].startswith(status)]
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

    # A bid-date window with no explicit sort implies "soonest first".
    if not sort and bid_window and bid_window != "none":
        sort = "bid"

    if sort == "bid":
        projects.sort(key=lambda p: p["bid_date"] or "9999-99-99")
    elif sort == "keywords":
        projects.sort(key=lambda p: (-len(p["keywords"]), p["project"].lower()))
    elif sort == "recent":
        projects.sort(key=lambda p: p["latest_date"] or "", reverse=True)
    elif sort == "name":
        projects.sort(key=lambda p: p["project"].lower())
    else:  # "flagged" (default): projects with new docs first, then matched,
           # then most-recently-scanned
        projects.sort(key=lambda p: p["latest_date"] or "", reverse=True)
        projects.sort(key=lambda p: (
            0 if p["status"] == "Matched" else 1,
            0 if p["updated"] else 1,
        ))

    total = len(projects)
    page = max(1, page)
    page_size = max(1, min(page_size, 50))
    start = (page - 1) * page_size
    return projects[start:start + page_size], total, site_tabs, updated_total, new_total


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

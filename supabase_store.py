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

import os
import re
from datetime import datetime, timezone
from supabase import create_client

_client = None

# A project whose every non-failed document has missed this many consecutive
# site reconciles is treated as "no longer listed" (see reconcile_seen).
CLOSED_AFTER_MISSES = int(os.environ.get("CLOSED_AFTER_MISSES", "2"))


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
    res = (
        get_client().table("sites").select("*")
        .eq("division_id", division_id).order("id").execute()
    )
    # `active` may be absent on a DB that skipped the migration — default it
    # so both the dashboard and the scan worker see every site as active.
    for row in res.data:
        row.setdefault("active", True)
        if row.get("active") is None:
            row["active"] = True
    return res.data


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


def add_site(division_id, name, url, listing=None, tabs=None, adapter=None):
    row = {"division_id": division_id, "name": name, "url": url}
    if listing is not None:
        row["listing"] = listing
    if tabs is not None:
        row["tabs"] = tabs
    if adapter:
        row["adapter"] = adapter
    res = get_client().table("sites").insert(row).execute()
    return res.data[0]


def update_site(division_id, site_id, name, url, listing=None, adapter=None):
    """Overwrite an existing site's config. `listing` and `adapter` are
    written as given (including None, to clear a previously-set value) so the
    dashboard's edit form can move a site between strategies. `tabs` is left
    untouched — it has no dashboard UI."""
    patch = {
        "name": name,
        "url": url,
        "listing": listing,
        "adapter": adapter,
    }
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
                  source_url=None):
    get_client().table("scan_results").insert({
        "division_id": division_id,
        "run_date": run_date,
        "site": site,
        "document_url": document_url,
        "source_url": source_url,
        "filename": filename,
        "matched_keywords": matched_keywords,
        "match_count": match_count,
        "keyword_locations": keyword_locations,
        "status": status,
        "ai_notes": ai_notes,
    }).execute()


def already_scanned_urls(division_id):
    """{document_url: source_url_or_None} for every document this division has
    already processed to a non-failure status — so a re-run (especially an
    adapter that re-lists every advertised project each day) skips them
    instead of re-downloading and re-running the AI pass. 'Download failed'
    rows are excluded so those get retried."""
    res = (
        get_client().table("scan_results").select("document_url,status,source_url")
        .eq("division_id", division_id).execute()
    )
    return {
        r["document_url"]: r.get("source_url")
        for r in res.data
        if r.get("document_url") and r.get("status") != "Download failed"
    }


def scanned_document_urls_all(division_id):
    """Every document_url this division has ever logged (any status). Used to
    gate the PDF proxy so it can't be pointed at an arbitrary URL."""
    res = (
        get_client().table("scan_results").select("document_url")
        .eq("division_id", division_id).execute()
    )
    return {r["document_url"] for r in res.data if r.get("document_url")}


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
    """Upsert a project's done flag. Raises ValueError with a migration hint
    if the table is missing."""
    try:
        get_client().table("project_flags").upsert({
            "division_id": division_id,
            "project_key": project_key,
            "done": bool(done),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        raise ValueError(
            "couldn't save — create the project_flags table from schema.sql"
        ) from e


def get_results_grouped(division_id, search=None, status=None, site=None, keyword=None,
                        include_closed=False, page=1, page_size=15):
    """Scan results collapsed to one entry per project (the `site` value),
    each project's files nested underneath. Filtering and pagination happen
    over the grouped projects. Returns (projects, total_project_count,
    site_tabs) where site_tabs is [{"name", "count", "flagged"}] over ALL
    projects (unaffected by the current filters), for the per-site tabs.

    Projects the source site no longer lists ("closed") are dropped unless
    include_closed is set, in which case each carries closed=True.
    """
    rows = (
        get_client().table("scan_results").select("*")
        .eq("division_id", division_id).order("run_date", desc=True)
        .limit(5000).execute()
    ).data

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
            "keywords": [k.strip() for k in (r.get("matched_keywords") or "").split(",") if k.strip()],
            "locations": r.get("keyword_locations") or "",
            "ai_notes": r.get("ai_notes") or "",
            "misses": r.get("misses") or 0,
            "last_seen_at": r.get("last_seen_at"),
        })

    done_keys = load_done_projects(division_id)

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
        projects.append({
            "key": site_key,
            "project": g["project"], "source_prefix": g["source_prefix"],
            "source_url": g["source_url"], "latest_date": g["latest_date"],
            "file_count": len(real_files), "matched_file_count": matched,
            "keywords": kws, "status": proj_status,
            "done": site_key in done_keys,
            "closed": closed, "last_seen_at": last_seen,
            "files": sorted(g["files"], key=lambda f: f["filename"]),
        })

    # Closed projects are out of the picture entirely unless asked for — so
    # they don't inflate the per-site tab counts either.
    if not include_closed:
        projects = [p for p in projects if not p["closed"]]

    # Per-site tab list, computed over every (visible) project, before filters.
    site_tabs = {}
    for p in projects:
        name = p["source_prefix"] or "Other"
        t = site_tabs.setdefault(name, {"name": name, "count": 0, "flagged": 0})
        t["count"] += 1
        if p["status"] == "Matched":
            t["flagged"] += 1
    site_tabs = sorted(site_tabs.values(), key=lambda t: (-t["flagged"], t["name"].lower()))

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

    projects.sort(key=lambda p: p["latest_date"] or "", reverse=True)
    projects.sort(key=lambda p: 0 if p["status"] == "Matched" else 1)

    total = len(projects)
    page = max(1, page)
    page_size = max(1, min(page_size, 50))
    start = (page - 1) * page_size
    return projects[start:start + page_size], total, site_tabs


def get_stats(division_id):
    """Project- and document-level counts for the Results summary, plus the
    keywords hitting the most projects."""
    rows = (
        get_client().table("scan_results")
        .select("site,filename,match_count,matched_keywords,status")
        .eq("division_id", division_id).execute()
    ).data

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


def get_latest_summary_text(division_id):
    summaries = get_summaries(division_id, limit=1)
    return summaries[0]["summary"] if summaries else None


def log_summary(division_id, run_date, summary_text):
    if not summary_text:
        return
    get_client().table("daily_summaries").insert({
        "division_id": division_id, "run_date": run_date, "summary": summary_text,
    }).execute()


def get_summaries(division_id, limit=200):
    res = (
        get_client().table("daily_summaries").select("*")
        .eq("division_id", division_id).order("run_date", desc=True).limit(limit).execute()
    )
    return res.data

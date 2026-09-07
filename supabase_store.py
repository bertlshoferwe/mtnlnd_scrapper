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
    """Removes the division and (via ON DELETE CASCADE) all its sites,
    keywords, results, summaries, and run history. Unlike the local-file
    version of this app, there's no 'keep the data around just in case' — a
    delete here is final. The dashboard confirms before calling this."""
    res = get_client().table("divisions").delete().eq("id", division_id).execute()
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
    return res.data


def add_site(division_id, name, url, listing=None, tabs=None):
    row = {"division_id": division_id, "name": name, "url": url}
    if listing is not None:
        row["listing"] = listing
    if tabs is not None:
        row["tabs"] = tabs
    res = get_client().table("sites").insert(row).execute()
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


def _clean_keyword(kw):
    """Trim, drop Markdown emphasis/heading marks, and collapse internal
    whitespace so 'soil   sampling', '**soil sampling**' and 'soil sampling'
    are stored and compared as the same keyword."""
    kw = (kw or "").strip()
    kw = re.sub(r"^#+\s*", "", kw)          # "## Geotextiles" -> "Geotextiles"
    kw = kw.strip("*_`").strip()            # "**Filter fabric**" -> "Filter fabric"
    kw = kw.rstrip(":").strip()             # "Erosion control:" -> "Erosion control"
    return re.sub(r"\s+", " ", kw)


def looks_like_section_heading(text):
    """True for lines that are document structure, not keywords: anything
    that started with '#', or an ALL-CAPS label of 3+ words like
    'PRODUCT TYPE / MATERIAL CATEGORY TERMS'. Short all-caps acronyms
    (GCL, HDPE) are fine. Used for both single adds and file uploads so a
    pasted outline never becomes a keyword."""
    if not text:
        return False
    if text.lstrip().startswith("#"):
        return True
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
    get_client().table("keywords").delete() \
        .eq("division_id", division_id).ilike("keyword", keyword).execute()
    return load_keywords(division_id)


def clear_keywords(division_id):
    """Remove every keyword for a division (the dashboard's 'Clear all')."""
    get_client().table("keywords").delete().eq("division_id", division_id).execute()
    return []


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
                  matched_keywords, match_count, keyword_locations, status, ai_notes):
    get_client().table("scan_results").insert({
        "division_id": division_id,
        "run_date": run_date,
        "site": site,
        "document_url": document_url,
        "filename": filename,
        "matched_keywords": matched_keywords,
        "match_count": match_count,
        "keyword_locations": keyword_locations,
        "status": status,
        "ai_notes": ai_notes,
    }).execute()


def get_scan_results(division_id, limit=2000):
    """Most recent rows first, for building the downloadable spreadsheet."""
    res = (
        get_client().table("scan_results").select("*")
        .eq("division_id", division_id).order("run_date", desc=True).limit(limit).execute()
    )
    return res.data


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


def get_stats(division_id):
    """Documents scanned (rows with an actual filename) and matches found
    (rows with match_count > 0), for the dashboard's summary cards."""
    client = get_client()
    total = (
        client.table("scan_results").select("id", count="exact")
        .eq("division_id", division_id).neq("filename", "").execute()
    )
    matches = (
        client.table("scan_results").select("id", count="exact")
        .eq("division_id", division_id).gt("match_count", 0).execute()
    )
    return {
        "documents_scanned": total.count or 0,
        "matches_found": matches.count or 0,
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

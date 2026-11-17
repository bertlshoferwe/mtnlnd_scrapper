"""
A listing link pasted on a bid (the bid board's "Original listing") read
into Bid Scout, so the bid's Bid Docs, Plan Holders and due date fill in.

  python listing_import.py <import_id>     read one (started by scanner_service.py's /import-listing)
  python listing_import.py --recheck       re-read every link whose bid date hasn't passed
                                           (scraper.main runs this after the daily scan)

How a link is read, first that works:
  1. Its site is one Bid Scout scans with a portal adapter (UDOT, IDOT, ...):
     the adapter's listing, cut down to this project. Same project key as
     the daily scan, so the bid follows the project the scan keeps current.
  2. Its site is learned by AI: the approved recipe's project part, on this page.
  3. Anything else: the page in a browser — its document links, and the AI
     reads the project name, bid date and time, location and plan holders.
     When that finds no documents, the AI clicks around for a few steps
     (tabs, "Documents", "Download") to find them.
Documents go through scraper.process_document like any scan (downloaded,
keyword-checked, logged under the project), then the bid is linked to the
project. A due date or time the bid already has is never overwritten; a
different one from the listing is kept as result.conflict for the board to offer.
State lives in listing_imports (migrations/002_listing_imports.sql).
"""

import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

import adapters
import ai_provider
import scraper
import site_agent
import supabase_store

EXPLORE_STEPS = 12
PROGRESS_LINES = 20


class Failed(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _host(url):
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _same_page(a, b):
    def norm(u):
        p = urlparse((u or "").strip())
        return (p.netloc.lower().removeprefix("www."), p.path.rstrip("/").lower(), p.query)
    return norm(a) == norm(b)


class _Run:
    """One listing_imports row, written as it goes."""

    def __init__(self, row):
        self.row = row
        self.lines = []

    def progress(self, line):
        print(f"  [listing] {line}")
        self.lines = (self.lines + [str(line)[:200]])[-PROGRESS_LINES:]
        supabase_store.update_listing_import(self.row["id"], progress=self.lines)

    def save(self, **patch):
        supabase_store.update_listing_import(self.row["id"], **patch)


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------

def _site_for(division_id, url):
    """The Bid Scout site this link belongs to (any site in the shared list,
    on or off for the division), or None."""
    host = _host(url)
    for s in supabase_store.load_sites(division_id):
        if s.get("url") and _host(s["url"]) == host:
            return s
    cls = adapters.match_adapter_class(url)
    if cls is not None:
        return {"id": None, "name": cls.label, "url": url, "adapter": cls.key, "division_id": division_id}
    return None


def _read_with_adapter(site, url, run):
    adapter = adapters.adapter_for_site(site)
    if adapter is None or adapter.needs_browser:
        return None
    run.progress(f"Reading it the way Bid Scout reads {site['name']}")
    found = [t for t in adapter.find_documents(site) if _same_page(t[3], url)]
    if not found:
        run.progress(f"{site['name']}'s listing doesn't have this page — reading the page itself")
        return None
    scraper._save_plan_holders(site, adapter)
    scraper._save_locations(site, adapter)
    t = found[0]
    return {"name": t[0], "site": site["name"], "docs": [(d[1], d[2], None) for d in found],
            "bid_date": t[4], "bid_time": t[5] if len(t) > 5 else None, "location": None, "holders": None}


def _read_learned(site, url, fetcher, run):
    recipe = (site.get("learned") or {}).get("recipe")
    if site.get("adapter") != "learned" or not recipe:
        return None
    run.progress(f"Reading it with the recipe learned for {site['name']}")
    try:
        p = site_agent.read_project(recipe, fetcher, {"url": url, "ctx": {}, "row": None, "list_src": None},
                                    (site.get("learned") or {}).get("rules"))
    except Exception as e:
        run.progress(f"The learned recipe didn't fit this page ({e}) — reading the page itself")
        return None
    if not p.get("documents") or not p.get("name"):
        run.progress("The learned recipe found nothing here — reading the page itself")
        return None
    f = p.get("fields") or {}
    return {"name": p["name"][:200], "site": site["name"], "docs": [(d["url"], d["name"], None) for d in p["documents"]],
            "bid_date": (f.get("Bid date") or {}).get("iso"),
            "bid_time": (f.get("Bid time") or {}).get("value"),
            "location": (f.get("Location") or {}).get("value"), "holders": p.get("plan_holders")}


READ_PROMPT = """You are reading the web page of ONE construction project that is out for bid.
From the page below, answer with one JSON object, no other text:
{"name": "the project's name, as the page gives it",
 "bid_date": "when bids are due, YYYY-MM-DD, or null",
 "bid_time": "the time bids are due with its time zone if shown, e.g. 2:00 PM MT, or null",
 "location": "city/county and state, or null",
 "plan_holders": [{"company": "...", "contact": "...", "phone": "...", "email": "..."}],
 "documents": [{"name": "file name", "url": "absolute link"}]}
"documents" are the project's bid documents (plans, specifications, addenda, bid forms) — only links that
download a file, including download links without a file extension. Leave plan_holders [] if the page doesn't list them.

PAGE:
"""

EXPLORE_PROMPT = """You are collecting the bid documents for ONE construction project from its web site.
Start page: {url}
The first pass found no document links on the start page. Explore — tabs, "Documents", "Plans", "Bid
Documents", "Download" — until you find the links that download the project's files.
Answer each step with exactly one JSON object, no other text:
  {{"action": "open", "url": "...", "render": true}}      look at a page (render: true uses a real browser)
  {{"action": "click", "text": "Documents"}}             click visible text on the last rendered page
  {{"action": "done", "documents": [{{"name": "...", "url": "absolute link"}}], "name": "...", "bid_date": "YYYY-MM-DD or null",
    "bid_time": "...", "location": "...", "plan_holders": []}}
  {{"action": "give_up", "reason": "..."}}                 the documents aren't reachable (login, purchase, ...)
You have {steps} steps left.
WHAT HAS HAPPENED SO FAR:
{history}"""


def _page_docs(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    return [(u, fn) for u, fn in scraper._dedupe_doc_links(page_url, [a["href"] for a in soup.find_all("a", href=True)])]


def _read_generic(url, fetcher, ai, run, site_name):
    run.progress("Opening the page")
    html, final, json_reqs = fetcher.explore(url, True)
    links = _page_docs(html, final)
    info = {}
    if ai is not None:
        run.progress("Reading the project details")
        info = site_agent._ask(ai, READ_PROMPT + site_agent._snapshot(html, final, json_reqs), max_tokens=4000) or {}
    docs = {u: fn for u, fn in links}
    for d in info.get("documents") or []:
        u = urljoin(final, str(d.get("url") or ""))
        if u.startswith("http") and u not in docs:
            docs[u] = str(d.get("name") or u.rsplit("/", 1)[-1].split("?")[0])[:200]
    if not docs and ai is not None:
        found = _explore(url, fetcher, ai, run)
        if found:
            for d in found.get("documents") or []:
                u = urljoin(url, str(d.get("url") or ""))
                if u.startswith("http"):
                    docs.setdefault(u, str(d.get("name") or u.rsplit("/", 1)[-1].split("?")[0])[:200])
            info = {k: found.get(k) or info.get(k) for k in ("name", "bid_date", "bid_time", "location", "plan_holders")}
    name = re.sub(r"\s+", " ", str(info.get("name") or "")).strip()
    if not name:
        soup = BeautifulSoup(html, "html.parser")
        name = (soup.title.get_text(" ", strip=True) if soup.title else "") or _host(url)
    holders = [adapters.plan_holder(h.get("company"), contact=h.get("contact"), phone=h.get("phone"),
                                    emails=[h["email"]] if h.get("email") else [])
               for h in info.get("plan_holders") or [] if isinstance(h, dict) and h.get("company")]
    return {"name": name[:200], "site": site_name, "docs": [(u, fn, None) for u, fn in docs.items()],
            "bid_date": site_agent.parse_date(info.get("bid_date")) if info.get("bid_date") else None,
            "bid_time": site_agent.parse_time(info.get("bid_time")) if info.get("bid_time") else None,
            "location": info.get("location"), "holders": holders or None}


def _explore(url, fetcher, ai, run):
    run.progress("No document links on the page — looking around the site for them")
    history = []
    for step in range(EXPLORE_STEPS):
        reply = site_agent._ask(ai, EXPLORE_PROMPT.format(
            url=url, steps=EXPLORE_STEPS - step,
            history="\n".join(f"STEP {i + 1}: {a}\nRESULT: {o[:6000] if i >= len(history) - 2 else o[:300]}"
                              for i, (a, o) in enumerate(history)) or "  (nothing yet)"), max_tokens=4000)
        if reply is None:
            return None
        action = reply.get("action")
        try:
            if action == "open":
                run.progress(f"→ Opening {reply.get('url')}"[:200])
                obs = site_agent._snapshot(*fetcher.explore(reply["url"], bool(reply.get("render", True))))
            elif action == "click":
                run.progress(f"→ Clicking “{reply.get('text')}”"[:200])
                obs = site_agent._snapshot(*fetcher.click(reply["text"]))
            elif action == "done":
                return reply
            elif action == "give_up":
                run.progress("Couldn't reach the documents: " + str(reply.get("reason") or "")[:160])
                return None
            else:
                obs = f'Unknown action "{action}".'
        except Exception as e:
            obs = f"ERROR: {e}"
        history.append((json.dumps({k: v for k, v in reply.items()})[:500], obs))
    return None


# ---------------------------------------------------------------------------
# One import
# ---------------------------------------------------------------------------

def run_import(row):
    run = _Run(row)
    division_id, bid_id, url = row["division_id"], row["bid_id"], row["url"]
    run.save(status="reading", message=None, progress=[])
    run.progress(f"Reading {url}")
    site = _site_for(division_id, url)
    login = scraper._site_login(site) if site and site.get("login_username") else None
    ai = ai_provider.get_provider()
    fetcher = site_agent.Fetcher(login)
    try:
        if fetcher.login_result and not fetcher.login_result["ok"]:
            run.progress(f"Logging in to {site['name']} didn't work: {fetcher.login_result['message']}")
        got = None
        if site:
            got = _read_with_adapter(site, url, run) or _read_learned(site, url, fetcher, run)
        if got is None:
            got = _read_generic(url, fetcher, ai, run, site["name"] if site else _host(url))
        cookies = fetcher.cookies()
    finally:
        fetcher.close()

    # Keep the project it was read into before (a re-read), so files add to it.
    key = row.get("project_key") or f"{got['site']} — {got['name']}"
    have = {f["url"] for f in supabase_store.load_project_files(division_id, key, with_duplicates=False)}
    new_docs = [d for d in got["docs"] if d[0] not in have]
    run.progress(f"Found {len(got['docs'])} document(s)" + (f", {len(new_docs)} new" if have else ""))

    if got["bid_date"]:
        supabase_store.save_project_bid_dates(division_id, {key: got["bid_date"]})
    if got["bid_time"]:
        supabase_store.save_project_bid_times(division_id, {key: got["bid_time"]})
    if got["location"]:
        supabase_store.save_project_locations(division_id, {key: got["location"]})
    if got["holders"]:
        supabase_store.save_project_plan_holders(division_id, {key: got["holders"]})

    if new_docs:
        embedder = ai_provider.get_embedder()
        keywords = supabase_store.load_keywords(division_id)
        ctx = {"keywords": keywords, "embedder": embedder, "ai_client": ai,
               "keyword_vectors": scraper.ensure_keyword_embeddings(division_id, embedder) if keywords else {}}
        run_date = _now()
        for i, (doc_url, filename, content) in enumerate(new_docs, start=1):
            run.progress(f"Checking {i} of {len(new_docs)}: {filename}"[:200])
            scraper.process_document(division_id, ctx, run_date, key, doc_url, doc_url, filename,
                                     content=content, source_url=url, cookies=cookies)
    elif not got["docs"] and not have:
        # Still a project in Bid Scout (so the bid can link and show its date / plan holders).
        supabase_store.log_scan_row(division_id, _now(), key, url, "", "", 0, "", "No documents found", "",
                                    source_url=url)

    files = supabase_store.load_project_files(division_id, key, with_duplicates=False)
    result = {"name": got["name"], "site": got["site"], "files": len(files),
              "matched": sum(1 for f in files if (f.get("status") or "").startswith("Matched")),
              "bid_date": got["bid_date"], "bid_time": got["bid_time"], "location": got["location"],
              "holders": len(got["holders"] or [])}
    message = None
    bid = supabase_store.workflow_bid_due(bid_id)
    if bid is None:
        message = "The bid is gone from the board."
    elif bid.get("source_project_id") == key:
        result["conflict"] = supabase_store.bid_due_conflict(division_id, bid, key)
    elif bid.get("source_project_id"):
        message = ("This bid already gets its files from Bid Scout project “"
                   + supabase_store._split_site(bid["source_project_id"])[1] + "”, so the listing's are in Bid Scout on their own.")
    else:
        back = (row.get("app_url") or "").rstrip("/") + "/?" + urlencode({"division": division_id, "project": key})
        try:
            result["conflict"] = supabase_store.link_workflow_bid(
                division_id, bid_id, key, source_url=back if row.get("app_url") else None, fill_blank=True)
            supabase_store.set_project_decision(division_id, key, "jira")
        except ValueError as e:
            message = f"Read into Bid Scout, but the bid couldn't show it: {e}."
    run.progress("Done")
    run.save(status="done", project_key=key, result=result, message=message, last_read_at=_now())


def run_by_id(import_id):
    row = supabase_store.get_listing_import(import_id=import_id)
    if row is None:
        print(f"FATAL: no listing import {import_id}", file=sys.stderr)
        sys.exit(1)
    try:
        run_import(row)
    except Exception as e:
        supabase_store.update_listing_import(row["id"], status="failed", message=f"Couldn't read the listing: {e}")
        raise


def recheck_all():
    """Re-read each link whose bid date hasn't passed (new addenda, plan holders)."""
    rows = supabase_store.listing_imports_to_recheck()
    if rows:
        print(f"=== Re-reading {len(rows)} listing link(s) pasted on bids ===")
    for row in rows:
        try:
            run_import(row)
        except Exception as e:
            print(f"  ! {row['url']}: {e}")
            supabase_store.update_listing_import(row["id"], status="failed", message=f"Couldn't read the listing: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python listing_import.py <import_id> | --recheck", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "--recheck":
        recheck_all()
    else:
        run_by_id(sys.argv[1])

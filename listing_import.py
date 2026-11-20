"""
A listing link pasted on a bid (the bid board's "Original listing") read
into Bid Scout, so the bid's Bid Docs, Plan Holders and due date fill in.

  python listing_import.py <import_id>     read one (started by scanner_service.py's /import-listing)
  python listing_import.py --recheck       re-read every link whose bid date hasn't passed
                                           (scraper.main runs this after the daily scan)

How a link is read, first that works:
  0. Bidtracer: its bid page and Plans / Specs / Addenda folders, read
     directly (_read_bidtracer).
  1. Its site is one Bid Scout scans with a portal adapter (UDOT, IDOT, ...):
     the adapter's listing, cut down to this project. Same project key as
     the daily scan, so the bid follows the project the scan keeps current.
  2. Its site is learned by AI: the approved recipe's project part, on this page.
  3. Anything else: the page in a browser — its document links, and the AI
     reads the project name, bid date and time, location and plan holders.
     When that finds no documents, the AI clicks around for a few steps
     (tabs, "Documents", "Download") to find them.
Documents go through scraper.process_document like any scan, but are only
downloaded and saved under the project — not read or keyword-checked — then
the bid is linked to the project. A due date or time the bid already has is never overwritten; a
different one from the listing is kept as result.conflict for the board to offer.
State lives in listing_imports (migrations/002_listing_imports.sql).
"""

import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

import adapters
import ai_provider
import browser_crawl
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


SIGN_IN_URL = re.compile(r"log-?[io]n|sign-?in|auth|sso|account", re.I)


def _on_sign_in(page):
    """True when the open page is a sign-in form, not the project."""
    if page is None:
        return False
    try:
        return bool(browser_crawl._visible(page, "input[type=password]")
                    or (SIGN_IN_URL.search(page.url) and browser_crawl._visible(page, browser_crawl._EMAIL_SELECTOR)))
    except Exception:
        return False


def _needs_login(run, url, login, tried):
    """The listing is behind a sign-in: say so, and which domain's login would read it."""
    host = _host(url)
    domain = login["domain"] if login and login.get("domain") else ".".join(host.split(".")[-2:])
    if not login:
        msg = f"This listing is behind a sign-in. Sign in to {domain} to read it."
    elif tried and not tried["ok"]:
        msg = f"Not signed in to {domain}: {tried['message']}."
    else:
        msg = f"Signed in to {domain}, but the listing still shows a sign-in page — sign in again."
    run.progress(msg)
    run.save(status="failed", message=msg, result={"needs_login": domain, "has_login": bool(login)},
             last_read_at=_now())


TABS_BUDGET_S = 120


def _open_tabs(page, page_url, docs, run):
    """Click the page's file tabs (Plans / Specs / Addenda, Documents, ...) the
    way the scan's crawl does, adding each tab's document links to `docs`.
    Returns {key: (filename, bytes)} for files that only come as a download
    when a button is clicked (key: the download's address, or page#filename)."""
    if page is None:
        return {}
    run.progress("Opening its file tabs")
    caught = []
    page.on("download", lambda d: caught.append(d))
    try:
        anchors = browser_crawl._reveal_and_download(page, time.time() + TABS_BUDGET_S) + browser_crawl._anchors(page)
    except Exception as e:
        run.progress(f"Couldn't open its file tabs ({e})"[:200])
        return {}
    for u, fn in scraper._dedupe_doc_links(page_url, [h for h, _t in anchors if h]):
        docs.setdefault(u, fn)
    grabbed = {}
    for dl in caught:
        fn = (dl.suggested_filename or "").strip()
        if not fn.lower().endswith(browser_crawl.DOC_EXT):
            continue   # PDFs and Word files only, like the scan
        content = browser_crawl._save_download(dl)
        if content:
            grabbed[dl.url if dl.url.startswith("http") else f"{page_url}#{fn}"] = (fn[:200], content)
    return grabbed


def _read_generic(url, fetcher, ai, run, site_name):
    run.progress("Opening the page")
    html, final, json_reqs = fetcher.explore(url, True)
    if _on_sign_in(fetcher.page) and not _page_docs(html, final):
        return {"name": "", "site": site_name, "docs": [], "sign_in": True}
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
    grabbed = _open_tabs(fetcher.page, final, docs, run)
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
    return {"name": name[:200], "site": site_name,
            "docs": [(u, fn, None) for u, fn in docs.items() if u not in grabbed]
                    + [(u, fn, content) for u, (fn, content) in grabbed.items()],
            "bid_date": site_agent.parse_date(info.get("bid_date")) if info.get("bid_date") else None,
            "bid_time": site_agent.parse_time(info.get("bid_time")) if info.get("bid_time") else None,
            "location": info.get("location"), "holders": holders or None}


# Bidtracer (bidtracer.com/secure/bidmain.aspx?bidid=...), read the way a person
# does it (walked through by the user, 2026-11-19), no AI needed: the name, bid
# date and address are form fields on the bid page; its left menu has Plans,
# Specs and Addenda, each with a count badge when it holds files; each opens a
# folder page whose Download menu has "Download All", which downloads a zip.
BIDTRACER_FOLDERS = ("Plans", "Specs", "Addenda")
BIDTRACER_DOWNLOAD_S = 300     # the zip is built when asked for; a big plan set takes a while
BIDTRACER_MAX_ZIP = 500 * 1024 * 1024

_FIELD_JS = """label => {
  const norm = s => (s || '').replace(/[*]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const e of document.querySelectorAll('label, span, div, td, th, b, strong')) {
    if (norm(e.textContent) !== label) continue;
    for (let p = e, i = 0; p && i < 3; p = p.parentElement, i++) {
      const f = p.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea');
      if (f && f.value) return f.value;
    }
  }
  return null;
}"""

# The left menu's folder links, with their badge counts: [[text, href, count]],
# the first link of each name ("Plans 1" -> ["Plans", href, 1]).
_FOLDERS_JS = """names => {
  const seen = {};
  return [...document.querySelectorAll('a')].map(a => {
    const t = a.textContent || '';
    return [t.replace(/\\d+/g, '').replace(/\\s+/g, ' ').trim(), a.href, parseInt((t.match(/\\d+/g) || ['0']).pop(), 10)];
  }).filter(x => names.includes(x[0]) && !seen[x[0]] && (seen[x[0]] = true));
}"""


def _read_bidtracer(url, fetcher, run, with_docs=True):
    """{"name", "site", "docs", "bid_date", ...} like _read_generic, or None
    when the page isn't a Bidtracer bid page this understands. Without
    `with_docs`, just the bid page: no folder is downloaded."""
    run.progress("Opening the bid on Bidtracer")
    page = fetcher._open_page(url)
    try:
        if _on_sign_in(page):
            return {"name": "", "site": "Bidtracer", "docs": [], "sign_in": True}
        try:   # the bid's form can come in after the page itself
            page.wait_for_selector("text=Project Name", timeout=15000)
        except Exception:
            pass
        # Every frame: the bid's form may sit in one.
        def field(label):
            return next((v.strip() for fr in page.frames for v in [_try(fr.evaluate, _FIELD_JS, label)] if v), "")
        name = field("project name")
        if not name:
            run.progress(f"This doesn't look like a Bidtracer bid page — it opened {page.url[:120]}"
                         f" (“{_try(page.title) or ''}”)")
            _bidtracer_debug(page, "bid_page")
            return None
        bid_when = _bidtracer_when(field("bid date"))
        address = field("address") or None
        folders = next((f for fr in page.frames for f in [_try(fr.evaluate, _FOLDERS_JS, list(BIDTRACER_FOLDERS))] if f), [])
        folders = [f for f in folders if f[2] > 0]
        run.progress(f"{name}: bid date {bid_when or 'not shown'}; files in "
                     + (", ".join(f"{t} ({n})" for t, _h, n in folders) or "no folder"))
        docs = []
        for text, href, _n in folders if with_docs else ():
            docs += _bidtracer_folder(fetcher, page, url, text, href, run)
    finally:
        page.close()
    return {"name": name[:200], "site": "Bidtracer", "docs": docs,
            "bid_date": site_agent.parse_date(bid_when) if bid_when else None,
            "bid_time": site_agent.parse_time(bid_when) if bid_when else None,
            "location": address, "holders": None}


def _bidtracer_when(text):
    """Bid Date's field holds '2026-11-30-17-00-00' (the page shows it as
    '11/30/2026 5:00 PM') — made into the shown form for the date/time parsers."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})", text or "")
    if not m:
        return text
    h = int(m[4])
    return f"{m[2]}/{m[3]}/{m[1]} {h % 12 or 12}:{m[5]} {'PM' if h >= 12 else 'AM'}"


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _bidtracer_folder(fetcher, bid_page, bid_url, folder, href, run):
    """One folder's files, from its Download -> Download All zip:
    [(key, filename, bytes)]. The key is stable across re-reads (the bid's
    link + folder/filename), so a re-read only checks new files."""
    run.progress(f"Downloading {folder} — Download All")
    page = fetcher._open_page(href) if href.startswith("http") else None
    if page is None:   # a script link: click it from a fresh copy of the bid page
        page = fetcher._open_page(bid_url)
        site_agent._click_text(page, folder)
    caught = []
    page.on("download", lambda d: caught.append(d))
    page.context.on("page", lambda p: p.on("download", lambda d: caught.append(d)))   # a download that opens its own tab
    try:
        # "Download All" is a submit button in a menu that opens on hover
        # (ctl00_contentPlaceHolderBid_btnDownloadAll); hovering then clicking
        # the menu closed it again, so the button itself is pressed instead.
        button = (page.query_selector("input[type=submit][id$=btnDownloadAll]")
                  or page.query_selector("input[type=submit][value='Download All']"))
        if button is None:
            raise ValueError("no Download All button on the folder page")
        button.evaluate("e => e.click()")
        deadline = time.time() + BIDTRACER_DOWNLOAD_S
        while not caught and time.time() < deadline:
            page.wait_for_timeout(500)
        if not caught:
            raise ValueError(f"no download came within {BIDTRACER_DOWNLOAD_S // 60} minutes")
        dl = caught[0]
        path = dl.path()
        if not path or os.path.getsize(path) > BIDTRACER_MAX_ZIP:
            raise ValueError("the download was empty or too large")
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        run.progress(f"Couldn't download {folder}: {e}"[:200])
        _bidtracer_debug(page, f"{folder.lower()}_folder")
        return []
    finally:
        page.close()
    fn = (dl.suggested_filename or f"{folder}.zip").strip()
    if data[:2] != b"PK":   # a folder with one file may come as just that file
        return [(f"{bid_url}#{folder}/{fn}", f"{folder}/{fn}"[:200], data)] if fn.lower().endswith(browser_crawl.DOC_EXT) else []
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if member.lower().endswith(browser_crawl.DOC_EXT):
                    base = member.rsplit("/", 1)[-1]
                    out.append((f"{bid_url}#{folder}/{member}", f"{folder}/{base}"[:200], zf.read(member)))
    except zipfile.BadZipFile as e:
        run.progress(f"{folder}'s zip couldn't be opened ({e})")
    run.progress(f"{folder}: {len(out)} file(s)")
    return out


def _bidtracer_debug(page, what):
    """A screenshot and the page's HTML in debug/, for when Bidtracer's pages change."""
    try:
        os.makedirs("debug", exist_ok=True)
        page.screenshot(path=f"debug/bidtracer_{what}.png", full_page=True)
        with open(f"debug/bidtracer_{what}.html", "w") as f:
            f.write(page.content())
        print(f"  [listing] saved debug/bidtracer_{what}.png/.html")
    except Exception:
        pass


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

def _skip_docs(row):
    """True when the bid already has documents from somewhere else — its Bid
    Scout project (a ConstructConnect scan, say) or files uploaded on the bid —
    so the listing is read for its details only, nothing downloaded. A listing
    whose first read brought its own documents in keeps adding new ones."""
    prior = row.get("result") or {}
    if row.get("project_key") and prior and not prior.get("docs_skipped"):
        return False
    bid = supabase_store.load_workflow_bid(row["bid_id"]) or {}
    if any(a.get("kind") == "doc" for a in bid.get("attachments") or []):
        return True
    linked = bid.get("source_project_id")
    return bool(linked and supabase_store.load_project_files(row["division_id"], linked))


def run_import(row):
    run = _Run(row)
    division_id, bid_id, url = row["division_id"], row["bid_id"], row["url"]
    run.save(status="reading", message=None, progress=[])
    run.progress(f"Reading {url}")
    skip_docs = _skip_docs(row)
    if skip_docs:
        run.progress("The bid already has its documents — reading the listing's details only")
    site = _site_for(division_id, url)
    login = scraper._site_login(site) if site and site.get("login_username") else None
    shared = None if login else scraper.division_login(division_id, url)
    if shared:
        run.progress(f"Using the saved sign-in for {shared['domain']}")
    ai = ai_provider.get_provider()
    fetcher = site_agent.Fetcher(login or shared)
    try:
        tried = fetcher.login_result
        if shared and tried:
            scraper.keep_login_result(shared["id"], tried)
        elif login and tried and site.get("id"):
            scraper.keep_site_login_result(division_id, site["id"], tried)
        if tried and not tried["ok"]:
            run.progress(f"Signing in to {site['name'] if login else shared['domain']} didn't work: {tried['message']}")
        got = None
        if _host(url).endswith("bidtracer.com"):
            try:
                got = _read_bidtracer(url, fetcher, run, with_docs=not skip_docs)
            except Exception as e:
                run.progress(f"Reading it as a Bidtracer bid didn't work ({e}) — reading the page itself"[:200])
        if got is None and site:
            got = _read_with_adapter(site, url, run) or _read_learned(site, url, fetcher, run)
        if got is None:
            got = _read_generic(url, fetcher, ai, run, site["name"] if site else _host(url))
        cookies = fetcher.cookies()
    finally:
        fetcher.close()
    if got.get("sign_in") and not got["docs"]:
        _needs_login(run, url, login or shared, tried)
        return
    if skip_docs:
        got["docs"] = []

    # The project the bid already shows, if it has one (its files go in with the
    # rest); else the one it was read into before (a re-read), so files add to
    # it; else a new one. Files removed from Bid Docs count as had, so they stay gone.
    linked = (supabase_store.workflow_bid_due(bid_id) or {}).get("source_project_id")
    key = linked or row.get("project_key") or f"{got['site']} — {got['name']}"
    have = {f["url"] for f in supabase_store.load_project_files(division_id, key, with_duplicates=False,
                                                                include_removed=True)}
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
        run_date = _now()
        for i, (doc_url, filename, content) in enumerate(new_docs, start=1):
            run.progress(f"Saving {i} of {len(new_docs)}: {filename}"[:200])
            scraper.process_document(division_id, None, run_date, key, doc_url, doc_url, filename,
                                     content=content, source_url=url, cookies=cookies, check=False)
    elif not got["docs"] and not have:
        # Still a project in Bid Scout (so the bid can link and show its date / plan holders).
        supabase_store.log_scan_row(division_id, _now(), key, url, "", "", 0, "", "No documents found", "",
                                    source_url=url)

    files = supabase_store.load_project_files(division_id, key, with_duplicates=False)
    result = {"name": got["name"], "site": got["site"], "files": len(files),
              "matched": sum(1 for f in files if (f.get("status") or "").startswith("Matched")),
              "bid_date": got["bid_date"], "bid_time": got["bid_time"], "location": got["location"],
              "holders": len(got["holders"] or []), "docs_skipped": skip_docs}
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

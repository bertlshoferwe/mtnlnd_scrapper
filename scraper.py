"""
Bid Scout — daily scan worker, AI-centric edition, run by GitHub Actions,
storing everything in Supabase (this is the scan worker; api/index.py is
the Vercel dashboard that reads the same tables — see supabase_store.py).

For each site configured for a division:
  1. Finds document links, using one of three strategies per site:
       - plain page: reads links straight off the one URL given
       - tabs: opens each configured tab (via Firecrawl) before reading links
       - listing: for sites where documents are two pages deep (a listing
         page links to individual job/item pages, which link to the actual
         documents), crawls the listing page, follows each item link, and
         collects documents from every item page. If no CSS selector is
         configured, Claude reads the listing page's links itself and
         identifies which ones are job/bid pages — no manual selector needed.
     Firecrawl (if FIRECRAWL_API_KEY is set) handles JS-rendered pages,
     anti-bot protection, and clicking; a plain request is the fallback.
  2. Downloads each document
  3. Extracts text page-by-page
  4. Checks every document two ways:
       - a literal, case-insensitive substring search (deterministic,
         free, always runs — the safety net)
       - if an AI provider is configured (see ai_provider.py), Claude or
         Gemini reads the whole document and
         identifies every keyword substantively discussed there, including
         paraphrases and synonyms a literal search would miss entirely
         (e.g. "M&A" for "merger") — this runs on every document, not just
         ones the literal pass already flagged
     Results from both are merged; the AI's one-line reasoning per match
     becomes the ai_notes column.
  5. Inserts one row per document into Supabase's scan_results table,
     immediately (not batched at the end) — so a mid-run crash still leaves
     everything found up to that point queryable, rather than losing the
     whole run's results
  6. If an AI provider is configured, writes a short plain-English digest of the
     day's matches into daily_summaries

PDF page numbers are real. DOCX page numbers are only real if LibreOffice
(`soffice`) is installed on the runner (used to render the DOCX to PDF
first) — GitHub Actions' ubuntu-latest runners are full VMs, so `apt-get
install libreoffice-writer` in the workflow gets this working, unlike a
Vercel serverless function where it couldn't run at all. Otherwise DOCX
locations fall back to an approximate paragraph-block number, clearly
labeled as such. See README.md for details.

Every integration in this script (Firecrawl, Claude, LibreOffice) is
optional and degrades gracefully — without an AI provider configured, the script
still runs correctly on the literal substring pass alone.

With an AI provider configured, cost scales with the number of documents
*downloaded* per run (one semantic-scan call each), not just the number that
match — see README.md for the cost/accuracy tradeoff this implies.

This is multi-tenant: every site list, keyword list, and results table is
scoped per "division" in Supabase — see supabase_store.py.

Run locally:    python scraper.py [division_id]
                  No argument = scan every division (this is what the daily
                  GitHub Actions cron does — one shared schedule for all
                  divisions, since Vercel Hobby doesn't support per-division
                  dynamic scheduling anyway; see README.md).
Schedule:       .github/workflows/daily-scan.yml, a daily cron. The
                 dashboard's "Run Now" button triggers the same workflow via
                 workflow_dispatch, scoped to just one division.

Environment variables:
  SUPABASE_URL          Required. Your Supabase project's API URL.
  SUPABASE_SERVICE_KEY  Required. The service_role key (not the anon key —
                          this needs to bypass Row Level Security to write
                          on behalf of every division). Keep this secret;
                          it's a GitHub Actions secret, never exposed client-side.
  AI_PROVIDER          Optional. "anthropic" | "gemini" — which AI
                         provider to use (see ai_provider.py). If unset,
                         picks whichever provider's key is present, checked
                         in that order. Without any of the three keys below,
                         the scraper still works on literal substring
                         matching and manual selectors alone.
  ANTHROPIC_API_KEY    Get one at https://console.anthropic.com
  GEMINI_API_KEY       Get one at https://aistudio.google.com — free tier,
                         but see ai_provider.py's note on data usage terms
                         if scanned documents are sensitive.
  FIRECRAWL_API_KEY    Optional. Routes page-scraping through Firecrawl
                         instead of a plain request, for JS-rendered pages,
                         sites that block simple scraping, and clicking
                         (tabs, listing-page crawls).
                         Get one at https://firecrawl.dev
"""

import os
import sys
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import requests
import ai_provider
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import pdfplumber
from docx import Document as DocxDocument
from dotenv import load_dotenv

load_dotenv()  # picks up API keys from a local .env if present

DOC_EXTENSIONS = (".pdf", ".docx", ".doc")
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB safety cap per document
REQUEST_TIMEOUT = 30  # seconds
LIBREOFFICE_TIMEOUT = 60  # seconds, for docx->pdf conversion
PARAS_PER_PSEUDO_PAGE = 25  # only used for the DOCX fallback when LibreOffice isn't installed
# Model selection now lives in ai_provider.py (ANTHROPIC_MODEL / GEMINI_MODEL
# env vars, one per provider) since which model applies depends on which
# provider is active.
AI_DOC_CHAR_BUDGET = 60000  # cap on document text sent per semantic keyword scan
MAX_LISTING_ANCHORS_FOR_AI = 300  # cap on links sent per AI job-link identification call

# Semantic keyword pre-filter: when a division has more than
# AI_PREFILTER_SEND_ALL_MAX keywords, the semantic pass doesn't see all of
# them for a given document — it sees the literal-substring hits plus the
# AI_PREFILTER_TOP_N keywords whose embeddings are closest to that document.
# Keeps the per-document AI prompt (and its cost) flat as the list grows.
AI_PREFILTER_SEND_ALL_MAX = int(os.environ.get("AI_PREFILTER_SEND_ALL_MAX", "60"))
AI_PREFILTER_TOP_N = int(os.environ.get("AI_PREFILTER_TOP_N", "50"))
EMBED_DOC_CHARS = 8000  # doc text length embedded for the similarity ranking
# Hard wall-clock budget for one division's scan. The GitHub Actions job caps
# out at 120 min (see .github/workflows/daily-scan.yml); if the scan runs past
# that the runner is killed mid-flight and finish_run() never fires, leaving
# the dashboard showing a phantom "Checking …" forever. Stopping ourselves
# before then means the run always closes cleanly (as a partial success) and
# the next run resumes where this one left off (already-scanned docs are
# skipped). Scanning every division shares one job, so keep this well under
# 120 min. Override with SCAN_DIVISION_BUDGET_S.
SCAN_DIVISION_BUDGET_S = int(os.environ.get("SCAN_DIVISION_BUDGET_S", str(95 * 60)))

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_TIMEOUT = 60  # seconds — headless rendering is slower than a plain request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DocScannerBot/1.0)"
}

import supabase_store
import adapters
import browser_crawl


def load_config(division_id):
    """Return (sites, keywords) for one division, via the shared
    supabase_store module — the same one the Vercel dashboard reads/writes."""
    return supabase_store.load_sites(division_id), supabase_store.load_keywords(division_id)


def _extract_json(text):
    """
    Pull a JSON array/object out of a Claude response and parse it, tolerating
    the model wrapping it in markdown code fences or a sentence of preamble.
    Returns None if nothing parseable is found — callers must handle that.
    """
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _dedupe_doc_links(page_url, raw_links):
    """Filter raw href strings down to unique, absolute document links."""
    found = []
    seen = set()
    for href in raw_links:
        if not href:
            continue
        href = href.strip()
        if href.lower().split("?")[0].endswith(DOC_EXTENSIONS):
            abs_url = urljoin(page_url, href)
            if abs_url not in seen:
                seen.add(abs_url)
                filename = abs_url.split("/")[-1].split("?")[0]
                found.append((abs_url, filename))
    return found


def _fetch_html_direct(page_url):
    """Plain request. Works for static pages; blocked by some sites' anti-bot
    protection and can't see JS-rendered content or run any actions."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  ! Direct fetch failed for {page_url}: {e}")
        return None


def _fetch_html_firecrawl(page_url, api_key, actions=None):
    """
    Fetch the page's rendered HTML via Firecrawl's /scrape endpoint, which
    runs a real headless browser (handles JS and anti-bot measures a plain
    request can't). If `actions` is given (e.g. a click on a tab or a link),
    Firecrawl runs those first and returns the HTML *after* they complete.
    Returns None on any failure, so the caller can fall back to a direct request.
    """
    payload = {"url": page_url, "formats": ["html"], "onlyMainContent": False}
    if actions:
        payload["actions"] = actions

    try:
        resp = requests.post(
            FIRECRAWL_SCRAPE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=FIRECRAWL_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ! Firecrawl request failed for {page_url}: {e}")
        return None
    except ValueError as e:  # JSON decode error
        print(f"  ! Firecrawl returned unparseable response for {page_url}: {e}")
        return None

    return (data.get("data") or {}).get("html")


def fetch_page_html(page_url, actions=None):
    """
    Return a page's HTML, or None on total failure. Uses Firecrawl if
    FIRECRAWL_API_KEY is set (handles JS rendering, anti-bot protection, and
    `actions` like clicking); otherwise, or if the Firecrawl call fails,
    falls back to a plain request. A plain request cannot run `actions` at
    all — that's the one thing Firecrawl is required for.
    """
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
    if firecrawl_key:
        html = _fetch_html_firecrawl(page_url, firecrawl_key, actions=actions)
        if html is not None:
            return html
        print(f"  ! Falling back to direct request for {page_url}")
    elif actions:
        print(f"  ! FIRECRAWL_API_KEY not set — cannot perform page actions "
              f"(e.g. clicking) for {page_url}; scanning its default view only")

    return _fetch_html_direct(page_url)


def find_document_links(page_url, actions=None):
    """Return [(absolute_url, filename), ...] for document links on page_url."""
    html = fetch_page_html(page_url, actions=actions)
    if html is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    return _dedupe_doc_links(page_url, hrefs)


def find_document_links_across_tabs(site):
    """
    Return a list of (tab_label_or_None, absolute_url, filename) for every
    document link found on a site — across all its tabs if the site config
    defines any.

    Tabs are declared per-site in sites.yaml as:
        tabs:
          - label: "Project A"
            selector: "#tab-project-a"

    For each tab, if FIRECRAWL_API_KEY is set, this clicks that tab (via
    Firecrawl actions) in a real browser before reading its links — the only
    way to see content that only appears after a click. Without a Firecrawl
    key, tabs can't be clicked at all; only whatever's visible on a plain
    page load gets scanned (which still catches tabs that are simply
    CSS-hidden rather than loaded on demand), and a warning is surfaced both
    in the log and as a row in the spreadsheet so missing coverage isn't silent.
    """
    url = site["url"]
    tabs = site.get("tabs")

    if not tabs:
        return [(None, doc_url, fn) for doc_url, fn in find_document_links(url)]

    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
    results = []
    seen = set()

    def add(label, links):
        for doc_url, fn in links:
            if doc_url not in seen:
                seen.add(doc_url)
                results.append((label, doc_url, fn))

    # Always scan the page as-loaded first — covers tabs whose content is
    # already in the HTML (just CSS-hidden) and whatever the default/active
    # tab shows without any click.
    add("(default view)", find_document_links(url))

    if not firecrawl_key:
        print(f"  ! Tabs configured for '{site['name']}' but FIRECRAWL_API_KEY is not set — "
              f"only the default page view was scanned; content behind other tabs may be missing.")
        return results

    for tab in tabs:
        label = tab.get("label", tab.get("selector", "unnamed tab"))
        selector = tab.get("selector")
        if not selector:
            print(f"  ! Tab '{label}' on '{site['name']}' has no selector, skipping")
            continue
        actions = [
            {"type": "click", "selector": selector},
            {"type": "wait", "milliseconds": 1500},
        ]
        links = find_document_links(url, actions=actions)
        add(label, links)

    return results


DEFAULT_MAX_LISTING_PAGES = 50


def _extract_listing_page_links(listing_page_url, html, listing_config):
    """
    Return [(absolute_url, link_text), ...] for links on a listing page that
    lead to individual item pages (e.g. one job's detail page), as identified
    by `link_selector` (CSS selector) and/or `link_pattern` (substring the
    href must contain). At least one of the two must be configured — without
    either, there's no way to tell a "job link" apart from ordinary
    navigation, so this returns [] and logs why.
    """
    selector = listing_config.get("link_selector")
    pattern = listing_config.get("link_pattern")
    if not selector and not pattern:
        print("  ! 'listing' is configured but has no link_selector or link_pattern — "
              "cannot identify which links lead to job pages, skipping listing crawl")
        return []

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(selector) if selector else soup.find_all("a", href=True)

    found = []
    seen = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href:
            continue
        if pattern and pattern not in href:
            continue
        # A link that's itself a document doesn't need a detail-page visit.
        if href.lower().split("?")[0].endswith(DOC_EXTENSIONS):
            continue
        abs_url = urljoin(listing_page_url, href)
        if abs_url not in seen:
            seen.add(abs_url)
            label = a.get_text(strip=True) or abs_url
            found.append((abs_url, label))
    return found


def ai_identify_job_links(client, listing_url, html):
    """
    Ask Claude which links on a listing page lead to individual job/bid/
    project detail pages — replaces having to hand-configure a CSS selector.
    Returns [(absolute_url, link_text), ...], or None if the client is
    unavailable or the call fails (distinct from [] = "AI ran, found none").
    """
    if client is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    anchors = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        anchors.append((href, text))

    if not anchors:
        return []
    if len(anchors) > MAX_LISTING_ANCHORS_FOR_AI:
        print(f"  ! {len(anchors)} links on page — checking only the first "
              f"{MAX_LISTING_ANCHORS_FOR_AI} with AI")
        anchors = anchors[:MAX_LISTING_ANCHORS_FOR_AI]

    numbered = "\n".join(f'{i}: href="{href}" text="{text[:80]}"' for i, (href, text) in enumerate(anchors))
    prompt = (
        "Below is a numbered list of links found on a job/bid/project listing page.\n\n"
        f"{numbered}\n\n"
        "Identify which of these links lead to an individual job, bid, project, or "
        "opportunity's own detail page — NOT site navigation, home, about, contact, "
        "login, search/filter controls, pagination, social links, or direct document "
        "downloads (those are handled separately).\n\n"
        "Respond with ONLY a JSON array of the matching indices, e.g. [0, 3, 7]. "
        "If none match, respond with []. No other text."
    )

    text = client.complete(prompt, max_tokens=1000)
    if text is None:
        return None
    indices = _extract_json(text)

    if not isinstance(indices, list):
        print(f"  ! AI job-link identification returned an unparseable result for {listing_url}")
        return None

    results, seen = [], set()
    for i in indices:
        if not isinstance(i, int) or not (0 <= i < len(anchors)):
            continue
        href, text = anchors[i]
        abs_url = urljoin(listing_url, href)
        if abs_url not in seen:
            seen.add(abs_url)
            results.append((abs_url, text or abs_url))
    return results


def find_document_links_via_listing(site, ai_client):
    """
    For sites where documents live two pages deep — a listing page links to
    individual job/item pages, and those pages link to the actual documents —
    visit the listing page, follow each item link, and collect document
    links from every item page.

    Configured per-site in sites.yaml as:
        listing:
          link_selector: "a.job-title"   # CSS selector for job links, or...
          link_pattern: "/jobs/"          # ...a substring their href must contain
          max_pages: 50                   # optional safety cap, default 50

    If neither link_selector nor link_pattern is given, Claude identifies the
    job links itself by reading the listing page's link text — no manual CSS
    selector required. This needs an AI provider configured (see
    ai_provider.py); without it (and without a manual selector/pattern
    either), there's no way to tell a job link from ordinary site
    navigation, so the site is skipped with a clear warning.

    Following JS-loaded listing pages or item pages also needs FIRECRAWL_API_KEY
    (see fetch_page_html) — without it, this still attempts a plain-request
    crawl, which works for many sites but will miss anything that only loads
    via JavaScript.
    """
    listing_config = site.get("listing") or {}
    listing_url = site["url"]

    html = fetch_page_html(listing_url)
    if html is None:
        return []

    selector = listing_config.get("link_selector")
    pattern = listing_config.get("link_pattern")

    if selector or pattern:
        item_links = _extract_listing_page_links(listing_url, html, listing_config)
    else:
        item_links = ai_identify_job_links(ai_client, listing_url, html)
        if item_links is None:
            print("  ! No link_selector/link_pattern configured, and AI identification "
                  "is unavailable (configure AI_PROVIDER + a key) — cannot identify job links, skipping site")
            return []
        print(f"  AI identified {len(item_links)} job link(s) on the listing page")

    max_pages = listing_config.get("max_pages", DEFAULT_MAX_LISTING_PAGES)
    if len(item_links) > max_pages:
        print(f"  ! {len(item_links)} item links found, capping at {max_pages} "
              f"(raise 'max_pages' in config/sites.yaml if you need more)")
        item_links = item_links[:max_pages]
    print(f"  Found {len(item_links)} item page(s) to check for documents")

    results = []
    seen_docs = set()
    for item_url, item_label in item_links:
        docs = find_document_links(item_url)
        for doc_url, fn in docs:
            if doc_url not in seen_docs:
                seen_docs.add(doc_url)
                results.append((item_label, doc_url, fn))

    return results


def scan_site(site, ai_client):
    """
    Dispatch to the right document-finding strategy for a site and return a
    uniform list of 5-tuples:
        (label_or_None, url_or_None, filename, content_bytes_or_None, source_url_or_None)
    `content_bytes` is set only for files captured from a JavaScript download
    (no URL to fetch later); otherwise it's None and the caller downloads
    `url`. `source_url` is a link to the job/project page the file belongs to.
    """
    adapter = adapters.adapter_for_site(site)
    if adapter is not None:
        print(f"  Adapter: {adapter.label}")
        return [(lbl, url, fn, None, src) for lbl, url, fn, src in adapter.find_documents(site)]
    if (site.get("adapter") or "").strip():
        print(f"  ! Site '{site['name']}' has unknown adapter '{site['adapter']}' — skipping")
        return []

    listing = site.get("listing") or {}
    if listing.get("link_selector") or listing.get("link_pattern"):
        # Only a *configured* listing (has a selector or URL pattern) uses the
        # targeted listing crawl. An empty {} — left over from the old
        # "let AI find the links" checkbox — falls through to the browser crawler.
        return [(lbl, url, fn, None, None)
                for lbl, url, fn in find_document_links_via_listing(site, ai_client)]
    if site.get("tabs"):
        return [(lbl, url, fn, None, None)
                for lbl, url, fn in find_document_links_across_tabs(site)]

    # Default: a real browser crawl — renders JS, follows links, clicks
    # "Documents/Plans" tabs, captures JS downloads. Falls back to the plain
    # requests link finder if Playwright/Chromium isn't available.
    crawled = browser_crawl.crawl_site(site["url"])
    if crawled is not None:
        return crawled
    return [(None, doc_url, fn, None, site["url"])
            for doc_url, fn in find_document_links(site["url"])]


def download_document(url):
    """Return raw bytes, or None on failure / oversize."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! Failed to download {url}: {e}")
        return None

    content = io.BytesIO()
    size = 0
    for chunk in resp.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            print(f"  ! Skipping {url}: exceeds {MAX_DOWNLOAD_BYTES} byte cap")
            return None
        content.write(chunk)
    return content.getvalue()


def _extract_pdf_pages(raw_bytes):
    """Return [(page_number, page_text), ...], 1-indexed, from real PDF pages."""
    pages = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append((i, page.extract_text() or ""))
    return pages


def _extract_docx_pages_via_libreoffice(raw_bytes):
    """
    Convert the DOCX to PDF with headless LibreOffice, then read real page
    boundaries from that PDF. Returns None if LibreOffice isn't installed or
    the conversion fails, so the caller can fall back gracefully.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        docx_path = os.path.join(tmp, "doc.docx")
        with open(docx_path, "wb") as f:
            f.write(raw_bytes)
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, docx_path],
                check=True, timeout=LIBREOFFICE_TIMEOUT,
                capture_output=True,
            )
        except Exception as e:
            print(f"  ! LibreOffice conversion failed, falling back to approximate location: {e}")
            return None

        pdf_path = os.path.join(tmp, "doc.pdf")
        if not os.path.exists(pdf_path):
            return None
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        return _extract_pdf_pages(pdf_bytes)


def _extract_docx_pages_fallback(raw_bytes):
    """
    DOCX files don't store real page numbers (pagination is computed at
    render/print time, not stored in the file). Without LibreOffice available
    to render real pages, group paragraphs into fixed-size chunks and label
    them as approximate locations rather than claiming a real page number.
    """
    doc = DocxDocument(io.BytesIO(raw_bytes))
    paras = [p.text for p in doc.paragraphs]
    pages = []
    for i in range(0, len(paras), PARAS_PER_PSEUDO_PAGE):
        chunk = "\n".join(paras[i:i + PARAS_PER_PSEUDO_PAGE])
        pages.append((i // PARAS_PER_PSEUDO_PAGE + 1, chunk))
    return pages


def extract_pages(raw_bytes, filename):
    """
    Return (pages, page_numbers_are_real) where pages is a list of
    (page_number, page_text) tuples. page_numbers_are_real is True for actual
    document pages (native PDF, or DOCX converted via LibreOffice), and False
    for the DOCX paragraph-chunk approximation.
    """
    lower = filename.lower()
    try:
        if lower.endswith(".pdf"):
            return _extract_pdf_pages(raw_bytes), True
        elif lower.endswith(".docx"):
            pages = _extract_docx_pages_via_libreoffice(raw_bytes)
            if pages is not None:
                return pages, True
            return _extract_docx_pages_fallback(raw_bytes), False
        elif lower.endswith(".doc"):
            # Legacy binary .doc is not reliably parseable without external
            # tools. Flagged as found, but not scanned.
            return [], False
    except Exception as e:
        print(f"  ! Failed to extract text from {filename}: {e}")
        return [], False
    return [], False


def check_keywords_by_page(pages, keywords):
    """Return {keyword: [page_numbers...]} for every keyword found, case-insensitive substring match."""
    hits = {}
    for page_num, text in pages:
        if not text:
            continue
        lower_text = text.lower()
        for kw in keywords:
            if kw.lower() in lower_text:
                hits.setdefault(kw, []).append(page_num)
    return hits


def format_locations(hits, page_numbers_are_real):
    """Turn {keyword: [pages]} into a readable string like 'risk: p.2, p.5 | merger: p.7'."""
    if not hits:
        return ""
    label = "p." if page_numbers_are_real else "~para-block "
    parts = []
    for kw in sorted(hits.keys()):
        pages_str = ", ".join(f"{label}{n}" for n in sorted(set(hits[kw])))
        parts.append(f"{kw}: {pages_str}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Optional AI integration (Anthropic or Gemini — see ai_provider.py)
# ---------------------------------------------------------------------------

def get_ai_client():
    """Return an AIProvider (Anthropic or Gemini, per AI_PROVIDER), or None
    if nothing is configured (AI features disabled, literal matching only)."""
    return ai_provider.get_provider()


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def ensure_keyword_embeddings(division_id, embedder):
    """Make sure every keyword in the division has a cached embedding vector,
    computing (and storing) any that are missing. Returns {keyword: vector}
    for all keywords that have one. A no-op when there's no embedder or the
    embed call fails — the pre-filter then falls back to literal hits only.
    """
    rows = supabase_store.load_keyword_rows(division_id)
    have = {r["keyword"]: r["embedding"] for r in rows if r.get("embedding")}
    missing = [r for r in rows if not r.get("embedding")]
    if not missing:
        return have
    if embedder is None:
        print(f"  No embedder (set GEMINI_API_KEY) — {len(missing)} keyword(s) unembedded, "
              "semantic pre-filter will use literal hits only")
        return have

    vectors = embedder.embed([r["keyword"] for r in missing])
    if not vectors or len(vectors) != len(missing):
        print(f"  ! Could not embed {len(missing)} keyword(s) — semantic pre-filter "
              "will use literal hits only this run")
        return have

    for r, v in zip(missing, vectors):
        r["embedding"] = v
        have[r["keyword"]] = v
    supabase_store.save_keyword_embeddings(division_id, missing)
    print(f"  Embedded {len(missing)} new keyword(s)")
    return have


def select_semantic_keywords(pages, keywords, keyword_vectors, literal_hits, embedder):
    """Trim the keyword list the semantic pass has to weigh against one
    document. Always includes the literal-substring hits; when the full list
    is larger than AI_PREFILTER_SEND_ALL_MAX, adds the AI_PREFILTER_TOP_N
    keywords whose embeddings are closest to the document text. Returns the
    list in the caller's original order.
    """
    if len(keywords) <= AI_PREFILTER_SEND_ALL_MAX:
        return keywords

    keep = set(literal_hits)
    doc_text = "\n".join(t for _, t in pages if t)[:EMBED_DOC_CHARS].strip()
    doc_vec = embedder.embed([doc_text]) if (embedder and doc_text) else None

    if doc_vec:
        ranked = sorted(
            ((k, _cosine(doc_vec[0], keyword_vectors[k])) for k in keywords if k in keyword_vectors),
            key=lambda kv: kv[1], reverse=True,
        )
        keep.update(k for k, _ in ranked[:AI_PREFILTER_TOP_N])
    # else: no embeddings available — semantic pass still runs, but only over
    # the literal hits (so it catches synonyms *of those*, not brand-new terms).

    selected = [k for k in keywords if k in keep]
    return selected or keywords[:AI_PREFILTER_SEND_ALL_MAX]


def ai_semantic_keyword_scan(client, filename, pages, keywords):
    """
    Ask Claude to read the whole document, page by page, and identify every
    keyword substantively discussed there — including clear paraphrases and
    synonyms a literal substring search would miss (e.g. "M&A" for "merger"),
    while skipping incidental or boilerplate mentions. Runs on every document
    with extractable text and at least one keyword configured, regardless of
    whether the literal substring pass found anything.

    Returns ({keyword: [(page, reason), ...]}, was_truncated). Returns
    ({}, False) if the client is unavailable, there's no usable text, or the
    call fails — never raises, so a failed AI call never breaks the scan;
    the literal substring pass (check_keywords_by_page) still runs
    independently as the deterministic baseline either way.
    """
    if client is None or not pages or not keywords:
        return {}, False

    chunks, total, truncated = [], 0, False
    for page_num, text in pages:
        if not text:
            continue
        piece = f"--- Page {page_num} ---\n{text}\n"
        if total + len(piece) > AI_DOC_CHAR_BUDGET:
            truncated = True
            break
        chunks.append(piece)
        total += len(piece)

    if not chunks:
        return {}, False

    doc_text = "\n".join(chunks)
    keyword_list = ", ".join(f'"{k}"' for k in keywords)

    prompt = (
        f"Document: {filename}\n\n{doc_text}\n\n"
        f"Keywords to check for: {keyword_list}\n\n"
        "For each keyword substantively discussed anywhere above — including clear "
        "paraphrases or synonyms, not only the exact wording — respond with one entry "
        "per page it appears on. Skip incidental or boilerplate mentions (e.g. only "
        "appearing in a generic disclaimer or an unrelated section header).\n\n"
        "Respond with ONLY a JSON array, no other text, in this exact shape:\n"
        '[{"keyword": "...", "page": <int>, "reason": "one short sentence"}, ...]\n'
        "If nothing qualifies, respond with []."
    )

    text = client.complete(prompt, max_tokens=1500)
    if text is None:
        return {}, truncated
    parsed = _extract_json(text)

    if not isinstance(parsed, list):
        print(f"  ! AI semantic scan returned an unparseable result for {filename}")
        return {}, truncated

    hits = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        kw, page, reason = entry.get("keyword"), entry.get("page"), entry.get("reason", "")
        if not isinstance(kw, str) or not isinstance(page, int):
            continue
        # Only accept keywords actually on our list (case-insensitively) — guards
        # against the model inventing or rephrasing a keyword we didn't ask about.
        matched_kw = next((k for k in keywords if k.lower() == kw.lower()), None)
        if matched_kw is None:
            continue
        hits.setdefault(matched_kw, []).append((page, reason))

    return hits, truncated


def generate_daily_summary(client, rows):
    """
    Ask Claude for a short plain-English summary of everything matched in this
    run. Returns '' if the client is unavailable, nothing matched, or the call
    fails — never raises.
    """
    if client is None:
        return ""

    matched_rows = [r for r in rows if r[5] and r[5] > 0]  # Match Count column
    if not matched_rows:
        return ""

    lines = []
    for r in matched_rows:
        # Date, Site, Document URL, Filename, Matched Keywords, Match Count, Keyword Locations, Status, AI Notes
        lines.append(f"- {r[1]} / {r[3]}: keywords [{r[4]}] at {r[6]}")

    prompt = (
        "Here is today's automated document scan. Each line is one document with a "
        "keyword match:\n\n" + "\n".join(lines) + "\n\n"
        "Write a brief (3-5 sentence) plain-English summary a busy person could read in "
        "10 seconds, highlighting anything that looks notable or worth a closer look. "
        "No headers, no bullet list — just prose."
    )

    text = client.complete(prompt, max_tokens=400)
    return text.strip() if text else ""


# ---------------------------------------------------------------------------
# Local Excel file
# ---------------------------------------------------------------------------

def _scan_division(division):
    """Run the full scan for one division (a dict with 'id' and 'name'),
    logging every row and the daily summary to Supabase as it goes."""
    division_id, division_name = division["id"], division["name"]
    print(f"=== Running scan for division: {division_name} ({division_id}) ===")

    run_id = supabase_store.start_run(division_id)
    try:
        supabase_store.update_run_progress(run_id, label="Getting started")
        sites, keywords = load_config(division_id)
        if not keywords:
            print(f"No keywords configured for division '{division_id}' — nothing to check for.")
        run_date = datetime.now(timezone.utc).isoformat()

        ai_client = get_ai_client()
        if ai_client is None:
            print("No AI provider configured (set AI_PROVIDER + a matching API key) — "
                  "running without semantic matching, AI Notes, or summary.")

        # Semantic pre-filter: embed the keyword list once so each document's
        # AI pass only weighs the terms relevant to that document, not all of
        # them. Keeps cost/accuracy stable as the list grows past ~60.
        embedder = ai_provider.get_embedder()
        if keywords:
            supabase_store.update_run_progress(run_id, label="Preparing keywords")
        keyword_vectors = ensure_keyword_embeddings(division_id, embedder) if keywords else {}
        if keywords and len(keywords) > AI_PREFILTER_SEND_ALL_MAX:
            print(f"  {len(keywords)} keywords — semantic pass will use a per-document "
                  f"top-{AI_PREFILTER_TOP_N} pre-filter"
                  + ("" if keyword_vectors else " (literal hits only — no embeddings yet)"))

        # Documents already processed in a previous run — skip re-downloading
        # and re-scanning them (adapters re-list every advertised project each
        # day, so without this every run redoes all of them).
        already_scanned = supabase_store.already_scanned_urls(division_id)
        skipped_seen = 0

        rows = []  # kept in memory too, just to build the daily summary at the end
        n_sites = len(sites)
        overall_done = 0
        deadline = time.monotonic() + SCAN_DIVISION_BUDGET_S
        stopped_early = False
        for site_i, site in enumerate(sites, start=1):
            name = site["name"]
            url = site.get("url") or ""

            if time.monotonic() > deadline:
                remaining = n_sites - site_i + 1
                print(f"! Wall-clock budget ({SCAN_DIVISION_BUDGET_S}s) reached — "
                      f"stopping with {remaining} site(s) unscanned "
                      f"({', '.join(s['name'] for s in sites[site_i - 1:])}). "
                      f"The next run picks these up.")
                stopped_early = True
                break

            print(f"Scanning site: {name}" + (f" ({url})" if url else ""))

            _l = site.get("listing") or {}
            if _l.get("link_selector") or _l.get("link_pattern"):
                print("  Listing mode: crawling item pages for documents")
            elif site.get("tabs"):
                print(f"  {len(site['tabs'])} tab(s) configured")

            # Discovery phase — doc count unknown, so total=0 (dashboard shows
            # an indeterminate bar until we can count this site's documents).
            supabase_store.update_run_progress(
                run_id, label=f"Checking {name}", site_i=site_i, site_n=n_sites,
                done=0, total=0, overall=overall_done,
            )
            doc_links = scan_site(site, ai_client)
            site_total = sum(
                1 for _, du, fn, _, _ in doc_links
                if (du or f"{url}#{fn}") not in already_scanned
            )
            site_done = 0
            supabase_store.update_run_progress(
                run_id, label=f"Scanning {name}", site_i=site_i, site_n=n_sites,
                done=0, total=site_total, overall=overall_done,
            )

            if site.get("tabs") and not os.environ.get("FIRECRAWL_API_KEY"):
                row = [run_date, name, url, "", "", 0, "",
                       "Tabs configured but FIRECRAWL_API_KEY not set — only default view scanned", ""]
                rows.append(row)
                supabase_store.log_scan_row(division_id, *row)

            print(f"  Found {len(doc_links)} document link(s) total")

            if not doc_links:
                row = [run_date, name, url, "", "", 0, "", "No documents found", ""]
                rows.append(row)
                supabase_store.log_scan_row(division_id, *row)
                continue

            for label, doc_url, filename, content, source_url in doc_links:
                # For a URL-less capture, key the "already scanned" / logged
                # URL off the site + filename so re-runs still skip it.
                doc_key = doc_url or f"{url}#{filename}"
                if doc_key in already_scanned:
                    skipped_seen += 1
                    # Backfill a source_url onto rows logged before we tracked it.
                    if source_url and not already_scanned.get(doc_key):
                        supabase_store.backfill_source_url(division_id, doc_key, source_url)
                        already_scanned[doc_key] = source_url
                    continue
                row_site_name = f"{name} — {label}" if label else name
                site_done += 1
                overall_done += 1
                if site_done % 3 == 0 or site_done == site_total:
                    supabase_store.update_run_progress(
                        run_id, label=f"Scanning {name}", site_i=site_i, site_n=n_sites,
                        done=site_done, total=site_total, overall=overall_done,
                    )

                raw = content if content is not None else download_document(doc_url)
                if raw is None:
                    row = [run_date, row_site_name, doc_key, filename, "", 0, "", "Download failed", ""]
                    rows.append(row)
                    supabase_store.log_scan_row(division_id, *row, source_url=source_url)
                    continue

                pages, page_numbers_are_real = extract_pages(raw, filename)

                # Literal substring pass — the deterministic baseline, always runs.
                literal_hits = check_keywords_by_page(pages, keywords)

                # AI semantic pass — runs on every document with text, not just
                # ones the literal pass already flagged, so it can catch
                # paraphrases/synonyms the literal pass would miss entirely.
                # The keyword list is pre-filtered per document (see
                # select_semantic_keywords) so a huge list doesn't bloat the prompt.
                semantic_keywords = select_semantic_keywords(
                    pages, keywords, keyword_vectors, literal_hits, embedder
                )
                ai_hits, ai_truncated = ai_semantic_keyword_scan(
                    ai_client, filename, pages, semantic_keywords
                )

                merged_pages = {}
                for kw, page_list in literal_hits.items():
                    merged_pages.setdefault(kw, set()).update(page_list)
                ai_reason_notes = []
                for kw, entries in ai_hits.items():
                    for page, reason in entries:
                        merged_pages.setdefault(kw, set()).add(page)
                        if reason:
                            ai_reason_notes.append(f"{kw} (p.{page}): {reason}")

                matched = sorted(merged_pages.keys())
                locations = format_locations({k: sorted(v) for k, v in merged_pages.items()}, page_numbers_are_real)

                if not pages:
                    status = "No text extracted"
                elif matched:
                    status = "Matched" if page_numbers_are_real else "Matched (approx. location)"
                else:
                    status = "No match"

                ai_notes = " | ".join(ai_reason_notes)
                if ai_truncated:
                    print(f"    (AI scan covered the first {AI_DOC_CHAR_BUDGET} chars of {filename})")

                row = [run_date, row_site_name, doc_key, filename,
                       ", ".join(matched), len(matched), locations, status, ai_notes]
                rows.append(row)
                supabase_store.log_scan_row(division_id, *row, source_url=source_url)
                already_scanned[doc_key] = source_url
                print(f"  - [{label or 'page'}] {filename}: {status} ({locations if locations else 'none'})")

        if skipped_seen:
            print(f"  Skipped {skipped_seen} document(s) already scanned in a previous run")

        supabase_store.update_run_progress(
            run_id, label="Wrapping up", site_i=n_sites, site_n=n_sites,
            done=0, total=0, overall=overall_done,
        )
        summary = generate_daily_summary(ai_client, rows)
        supabase_store.log_summary(division_id, run_date, summary)

        supabase_store.finish_run(run_id, "partial" if stopped_early else "success")
        print(f"=== Division '{division_id}' "
              f"{'stopped early (budget)' if stopped_early else 'done'}: "
              f"{len(rows)} new row(s) logged, "
              f"{skipped_seen} skipped as already scanned ===")
    except Exception as e:
        supabase_store.finish_run(run_id, f"error: {e}")
        raise


def main(division_id=None):
    """
    If division_id is given, scan only that division. If it's None (or an
    empty string — the GitHub Actions workflow_dispatch default when no
    input is given), scan every division in sequence. This is what lets one
    daily scheduled run cover every division without per-division cron jobs.
    """
    if division_id:
        division = supabase_store.get_division(division_id)
        if division is None:
            print(f"FATAL: no such division '{division_id}'", file=sys.stderr)
            sys.exit(1)
        divisions = [division]
    else:
        divisions = supabase_store.load_divisions()
        if not divisions:
            print("No divisions configured yet — nothing to scan.")
            return
        print(f"No division specified — scanning all {len(divisions)} division(s).")

    failures = []
    for division in divisions:
        try:
            _scan_division(division)
        except Exception as e:
            print(f"! Division '{division['id']}' failed: {e}", file=sys.stderr)
            failures.append(division["id"])

    if failures:
        print(f"FATAL: {len(failures)} division(s) failed: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # Usage: python scraper.py [division_id]
    # No argument (or an empty string, as GitHub Actions passes when a
    # workflow_dispatch input is left blank) means "scan every division".
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    try:
        main(arg or None)
    except SystemExit:
        raise
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)

"""
Generic headless-browser crawler — the default document finder for any site
that isn't handled by a portal adapter.

A real Chromium browser (via Playwright, run in the GitHub Actions job) so it
works on every kind of site:
  - server-rendered pages and JavaScript SPAs alike (it renders the page)
  - documents linked as <a href="...plans.pdf"> (collected directly)
  - documents behind "Project Files / Documents / Plans" tabs or buttons
    (clicked to reveal them)
  - documents that download via JavaScript with no URL at all (captured from
    the browser's download events as raw bytes)

It crawls same-domain links a few levels deep, preferring links that look like
bid/project/solicitation pages, with page/depth/time caps so a run stays
bounded.

Entry point:
    crawl_site(start_url, login=None) ->
        ([(label, url_or_None, filename, content_or_None, page_url)],
         cookies, login_result)

`content` is raw bytes for files captured from a JS download (no URL to fetch
later); it's None for ordinary linked files, which the caller downloads by URL.
`page_url` is the page the file was found on (used for the "open project" link).
If Playwright or its browser isn't available, returns (None, {}, None) and
logs why — the caller falls back to the plain-requests link finder.

`login`, when given, is {"username", "password", "url"}: before crawling,
the browser context logs in at `url` by auto-filling the first visible
password field and a preceding username/email field, then submits. The
returned `cookies` ({name: value}) carry that authenticated session so the
caller can pass them to plain `requests` downloads of documents that were
only linked (not captured as a JS download) during the crawl. `login_result`
is None when no login was configured, otherwise {"ok": bool, "message":
str_or_None} — the caller persists this so the dashboard can show whether
the site's saved credentials are working.
"""

import os
import re
import time
from urllib.parse import urlparse

DOC_EXT = (".pdf", ".docx", ".doc")

MAX_PAGES = int(os.environ.get("CRAWL_MAX_PAGES", "40"))
MAX_DEPTH = int(os.environ.get("CRAWL_MAX_DEPTH", "3"))
MAX_DOCS = int(os.environ.get("CRAWL_MAX_DOCS", "250"))
PAGE_TIMEOUT_MS = int(os.environ.get("CRAWL_PAGE_TIMEOUT_MS", "25000"))
SITE_BUDGET_S = int(os.environ.get("CRAWL_SITE_BUDGET_S", "600"))
MAX_CAPTURED_BYTES = 25 * 1024 * 1024

# A link is worth following if its text/href hints at more bid content.
_FOLLOW = re.compile(
    r"project|bid|solicit|rfp|rfq|rfb|ifb|advertis|contract|opportun|proposal|"
    r"plan|document|detail|package|addend|attach|procurement|award",
    re.I,
)
# Buttons / tabs that reveal a document list when clicked.
_REVEAL = re.compile(
    r"(project files|^files$|documents?|plans?|attachments?|downloads?|"
    r"bid docs?|specifications?|addend|planholder|plan holder|related files)",
    re.I,
)
# Text that means "this click actually downloads a file".
_DOWNLOAD = re.compile(r"^\s*(download all|download|save|get file|export)\s*$", re.I)
# Never click these.
_AVOID = re.compile(
    r"(submit|log ?in|sign ?in|log ?out|sign ?out|register|create account|apply|"
    r"bid now|checkout|delete|remove|pay|purchase|add to)",
    re.I,
)
# Links never worth queueing.
_SKIP_LINK = re.compile(
    r"^(mailto:|tel:|javascript:|#)|"
    r"/(login|logout|signin|sign-in|register|account|profile|cart|checkout|"
    r"privacy|terms|contact|about|help|faq|sitemap)(/|$|\?)",
    re.I,
)


def _is_doc_href(href):
    path = href.lower().split("?")[0].split("#")[0]
    return path.endswith(DOC_EXT)


def _filename_from_url(href):
    from urllib.parse import unquote
    tail = unquote(href.split("?")[0].split("#")[0].rstrip("/").split("/")[-1])
    return tail or "document"


_CLICK_SELECTOR = (
    "a, button, [role=tab], [role=button], "
    "[class*=tab] > *, mat-tab-header *, .nav-link, summary"
)


def _visible_clickables(frame):
    """[(handle, text)] for a-, button- and role-based controls that are
    visible and have short label text, in one frame."""
    out = []
    try:
        handles = frame.query_selector_all(_CLICK_SELECTOR)
    except Exception:
        return out
    for h in handles:
        try:
            if not h.is_visible():
                continue
            text = (h.inner_text() or "").strip()
        except Exception:
            continue
        if 0 < len(text) <= 40:
            out.append((h, text))
    return out


def _click_matching(page, frame, pattern, deadline, expect_download, limit):
    clicked = 0
    for handle, text in _visible_clickables(frame):
        if clicked >= limit or time.time() > deadline:
            break
        if _AVOID.search(text) or not pattern.search(text):
            continue
        try:
            if expect_download:
                with page.expect_download(timeout=8000):
                    handle.click(timeout=4000)
            else:
                handle.click(timeout=4000)
                page.wait_for_timeout(900)
            clicked += 1
        except Exception:
            continue
    return clicked


def _reveal_and_download(page, deadline):
    """Round 1: click tabs/links that reveal a file list. Round 2: click the
    resulting Download buttons (their downloads are caught by the page's
    'download' handler). Done for the main frame and every child frame, since
    many bid pages embed a third-party portal in an <iframe>."""
    for frame in page.frames:
        _click_matching(page, frame, _REVEAL, deadline, expect_download=False, limit=6)
    page.wait_for_timeout(1000)
    for frame in page.frames:
        _click_matching(page, frame, _DOWNLOAD, deadline, expect_download=True, limit=30)
    page.wait_for_timeout(1200)


def _page_label(page, url):
    try:
        t = (page.title() or "").strip()
        if t:
            return t[:120]
    except Exception:
        pass
    return url


def _save_download(dl):
    try:
        path = dl.path()
        if not path:
            return None
        if os.path.getsize(path) > MAX_CAPTURED_BYTES:
            print(f"  ! captured download {dl.suggested_filename} too large, skipping")
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"  ! could not read captured download: {e}")
        return None


# Candidate username/email fields, tried in order, first visible one wins.
_USERNAME_SELECTOR = (
    "input[type=email], input[autocomplete=username], "
    "input[type=text][name*=user i], input[type=text][name*=email i], "
    "input[type=text][id*=user i], input[type=text][id*=email i], "
    "input[type=text]"
)
_SUBMIT_SELECTOR = (
    "button[type=submit], input[type=submit], "
    "button:has-text('Log in'), button:has-text('Log In'), "
    "button:has-text('Sign in'), button:has-text('Sign In')"
)


def _attempt_login(ctx, login):
    """Best-effort: open the login page, fill the first visible password
    field and a preceding username/email field, submit, and wait for the
    page to settle. Returns {"ok": bool, "message": str_or_None} — `ok` is a
    heuristic (the password field is gone after submitting, i.e. we're not
    still sitting on the login form), not a guarantee the site accepted the
    credentials, but it does catch the common case of a login page that just
    reloads itself on a wrong password."""
    page = ctx.new_page()
    try:
        page.goto(login["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(800)

        pw_input = page.query_selector("input[type=password]")
        if not pw_input or not pw_input.is_visible():
            msg = f"no password field found on {login['url']}"
            print(f"  ! Login: {msg}")
            return {"ok": False, "message": msg}
        user_input = page.query_selector(_USERNAME_SELECTOR)
        if not user_input or not user_input.is_visible():
            msg = f"no username field found on {login['url']}"
            print(f"  ! Login: {msg}")
            return {"ok": False, "message": msg}

        user_input.fill(login["username"])
        pw_input.fill(login["password"])

        submit = page.query_selector(_SUBMIT_SELECTOR)
        if submit:
            submit.click(timeout=4000)
        else:
            pw_input.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            pass  # some sites never go idle (polling/websockets) — check below anyway

        still_on_login = page.query_selector("input[type=password]")
        if still_on_login and still_on_login.is_visible():
            msg = "still on a page with a password field after submitting — check the username/password"
            print(f"  ! Login: {msg}")
            return {"ok": False, "message": msg}

        print(f"  Logged in as {login['username']} at {login['url']}")
        return {"ok": True, "message": None}
    except Exception as e:
        msg = str(e)
        print(f"  ! Login attempt failed ({login['url']}): {msg}")
        return {"ok": False, "message": msg}
    finally:
        page.close()


def crawl_site(start_url, login=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! Playwright not installed — falling back to plain-request crawl")
        return None, {}, None

    origin = urlparse(start_url).netloc
    seen_pages = set()
    queue = [(start_url, 0)]
    found = {}  # (filename, key) -> (label, url_or_None, content_or_None, page_url)
    deadline = time.time() + SITE_BUDGET_S

    try:
        pw = sync_playwright().start()
    except Exception as e:
        print(f"  ! Could not start Playwright ({e}) — falling back to plain-request crawl")
        return None, {}, None

    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        print(f"  ! Chromium not available ({e}) — run 'playwright install chromium'. "
              "Falling back to plain-request crawl")
        pw.stop()
        return None, {}, None

    ctx = browser.new_context(
        accept_downloads=True,
        user_agent="Mozilla/5.0 (compatible; BidScoutBot/1.0)",
    )

    cookies = {}
    login_result = None
    if login:
        login_result = _attempt_login(ctx, login)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}

    try:
        while queue and len(seen_pages) < MAX_PAGES and len(found) < MAX_DOCS:
            if time.time() > deadline:
                print("  ! Crawl time budget reached, stopping")
                break
            url, depth = queue.pop(0)
            if url in seen_pages:
                continue
            seen_pages.add(url)

            page = ctx.new_page()
            captured = []
            page.on("download", lambda d: captured.append(d))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"  ! {url}: {e}")
                page.close()
                continue

            _reveal_and_download(page, deadline)

            anchors = []
            for frame in page.frames:
                try:
                    anchors += frame.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => [e.href, (e.textContent || '').trim()])",
                    )
                except Exception:
                    pass

            label = _page_label(page, url)
            liberal_left = 25 if depth == 0 else 0  # first hop: follow links even without a keyword hint
            for href, text in anchors:
                if not href:
                    continue
                if _is_doc_href(href):
                    fn = _filename_from_url(href)
                    found.setdefault((fn, href), (label, href, None, url))
                    continue
                if (
                    depth >= MAX_DEPTH
                    or urlparse(href).netloc != origin
                    or _SKIP_LINK.search(href)
                    or href in seen_pages
                    or any(q[0] == href for q in queue)
                ):
                    continue
                if _FOLLOW.search(href) or _FOLLOW.search(text or ""):
                    queue.append((href, depth + 1))
                elif liberal_left > 0:
                    queue.append((href, depth + 1))
                    liberal_left -= 1

            for dl in captured:
                fn = (dl.suggested_filename or "").strip() or "download"
                if not fn.lower().endswith(DOC_EXT):
                    continue
                if any(k[0] == fn for k in found):
                    continue
                data = _save_download(dl)
                if data:
                    found[(fn, url + "#dl")] = (label, None, data, url)

            page.close()
    finally:
        try:
            browser.close()
        finally:
            pw.stop()

    results = [(label, u, fn, content, page_url)
               for (fn, _), (label, u, content, page_url) in found.items()]
    print(f"  Crawled {len(seen_pages)} page(s), found {len(results)} document(s)")
    return results, cookies, login_result

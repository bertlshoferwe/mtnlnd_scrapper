"""
Site adapters — per-portal scraping logic for bid sites whose documents can't
be reached by the generic HTML crawl (JS-rendered SPAs, tabbed navigation,
download buttons wired to XHR/blob). Each adapter talks to that portal's own
API and returns the same (label, document_url, filename) tuples that
scraper.scan_site() produces, so the rest of the pipeline is unchanged.

A site opts into an adapter by setting its `adapter` column to the adapter's
`key` (chosen from a dropdown in the dashboard). When set, the site's URL and
listing/tabs config are ignored — the adapter knows where to look.

Adding a portal: subclass SiteAdapter, implement find_documents(), and add the
class to the ADAPTERS list at the bottom.
"""

import io
import os
import re
import time
import zipfile
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests

REQUEST_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BidScoutBot/1.0)"}
DOC_EXTENSIONS = (".pdf", ".docx", ".doc")


def parse_bid_date(text):
    """'September 10, 2026' / 'Sep 10, 2026' / '9/10/2026' / '2026-09-10'
    (asterisks and surrounding junk tolerated) -> '2026-09-10', or None."""
    t = re.sub(r"\*+", "", text or "").strip()
    m = re.search(r"[A-Za-z]+ +\d{1,2}, *\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}", t)
    if not m:
        return None
    t = m.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


class SiteAdapter:
    key = ""        # stable id stored in sites.adapter
    label = ""      # shown in the dashboard dropdown
    help = ""       # one-liner under the dropdown
    hosts = ()      # hostnames this adapter recognizes from a plain site URL
    # True for an adapter that needs a real logged-in browser session (see
    # ConstructConnectAdapter) rather than a plain requests call — scan_site()
    # calls find_documents_with_browser() instead of find_documents() for one
    # of these, and passes it the site's saved login.
    needs_browser = False

    @classmethod
    def matches_url(cls, url):
        host = urlparse(url or "").netloc.lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    def find_documents(self, site):
        """Return [(label, document_url, filename, project_page_url, bid_date),
        ...] for every document the portal is currently advertising. `label` is
        the job/project name; `project_page_url` is a link to that job's page
        (or None); `bid_date` is the bid-opening date as 'YYYY-MM-DD' (or None).
        `site` is the sites row (dict)."""
        raise NotImplementedError

    def find_documents_with_browser(self, site, login):
        """Only for needs_browser=True adapters. Return (doc_links, login_result):
        doc_links is [(label, document_url_or_None, filename, content_bytes_or_None,
        project_page_url, bid_date), ...] — already in scan_site()'s final 6-tuple
        shape, since a browser-driven adapter typically has content bytes rather
        than a URL the caller can fetch later. `login_result` is the {"ok",
        "message"} dict from the login attempt (or None if login was never
        attempted). `login` is {"username", "password", "url"} or None."""
        raise NotImplementedError


class UDOTMasterworksAdapter(SiteAdapter):
    """Utah DOT Contractor Zone (Masterworks) — contractorzone.udot.utah.gov.

    Public JSON API, no auth:
      GET /advertisements/projects?section=advertisements   -> project list
      GET /project-files/{uuid}/project-files               -> file list
      GET /project-files/download/{uuid}/project-files?file_name=... -> the PDF
    """

    key = "udot_masterworks"
    label = "UDOT Contractor Zone (advertised projects)"
    help = "Pulls every advertised project's plan set / NTC / items PDFs plus any addenda straight from contractorzone.udot.utah.gov. The site URL field is ignored."
    hosts = ("contractorzone.udot.utah.gov",)

    BASE = "https://contractorzone.udot.utah.gov"
    SECTION = "advertisements"
    # Document tabs on a project's page, each a section of the project-files API:
    # GET /project-files/{uuid}/{section} lists it, and the download URL uses the
    # same section. "project-files" = the plan set / NTC / items; "addendum" =
    # addenda issued after advertisement.
    FILE_SECTIONS = ("project-files", "addendum")

    def _get_json(self, path, **params):
        r = requests.get(f"{self.BASE}{path}", params=params or None,
                         headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def find_documents(self, site):
        listing = self._get_json("/advertisements/projects", section=self.SECTION)
        projects = listing.get("result") or []
        print(f"  UDOT: {len(projects)} advertised project(s)")

        out = []
        for p in projects:
            uuid = p.get("record_uuid")
            if not uuid:
                continue
            label = p.get("project_name") or p.get("project_number") or str(uuid)
            bid_date = parse_bid_date(p.get("bid_opening_date"))
            project_url = f"{self.BASE}/project/{uuid}/project-files"

            for section in self.FILE_SECTIONS:
                try:
                    files = self._get_json(f"/project-files/{uuid}/{section}")
                except requests.RequestException as e:
                    # A missing plan set means the project isn't ready; a missing
                    # addendum section is normal — only bail on the main one.
                    print(f"  ! UDOT: couldn't list {section} for {label}: {e}")
                    if section == "project-files":
                        break
                    continue
                for f in files.get("result") or []:
                    fn = (f.get("file_name") or "").strip()
                    if not fn.lower().endswith(DOC_EXTENSIONS):
                        continue
                    url = (f"{self.BASE}/project-files/download/{uuid}/{section}"
                           f"?file_name={requests.utils.quote(fn)}")
                    # Same `label` for every section so addenda group under the
                    # same project as the plan set (rather than as a new project).
                    out.append((label, url, fn, project_url, bid_date))
        return out


class ITDAdvertisedAdapter(SiteAdapter):
    """Idaho Transportation Dept — itd.idaho.gov/contractor-bidding.

    That page is a WordPress page with three TablePress tables:
      - tablepress-2  "Currently Advertised Major Highways Projects" ($1M+)
      - tablepress-4  "Currently Advertised SIA/IRP Highways Projects" ($50K-$5M)
      - tablepress-1  "Bid Results for Highways Projects" — a 250+ row archive
    Only the first two are live opportunities; the generic crawler otherwise
    wanders the whole site and the huge results archive (every br*.pdf /
    abst*.pdf), which is what made this site so slow. This adapter reads just
    those two tables. Each row's "Project" cell links straight to the Notice
    to Contractors PDF (apps.itd.idaho.gov/apps/contractors/NTC*.pdf); the
    "Reference Files" cell holds any addenda/plans. Plain HTML, no JS.
    The site URL field is ignored.
    """

    key = "itd_advertised"
    label = "Idaho Transportation Dept (advertised projects)"
    help = ("Reads only the two 'Currently Advertised' tables on "
            "itd.idaho.gov/contractor-bidding (Major Highways + SIA/IRP) and "
            "their linked PDFs. Skips the bid-results archive. The site URL is ignored.")
    hosts = ("itd.idaho.gov",)

    PAGE = "https://itd.idaho.gov/contractor-bidding/"
    TABLE_IDS = ("tablepress-2", "tablepress-4")
    # ITD key numbers look like 24502 / 23719q / 23733-24350. Rows whose "Key
    # Number" cell doesn't (e.g. the "2026 Buy America" info handout parked in
    # the Major Highways table) aren't real advertised projects — skip them.
    _KEY_RE = re.compile(r"^\d[\d\-]*[a-z]?$", re.I)

    def find_documents(self, site):
        from bs4 import BeautifulSoup

        r = requests.get(self.PAGE, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        out, seen = [], set()
        for tid in self.TABLE_IDS:
            table = soup.find("table", id=tid)
            if table is None:
                print(f"  ! ITD: table #{tid} not found — page layout may have changed")
                continue
            body = table.find("tbody") or table
            for tr in body.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                key_no = cells[1].get_text(strip=True).strip("*").strip()
                if key_no and not self._KEY_RE.match(key_no):
                    continue
                bid_date = parse_bid_date(cells[0].get_text(" ", strip=True))
                anchors = tr.find_all("a", href=True)
                for a in anchors:
                    href = urljoin(self.PAGE, a.get("href", "").strip())
                    if not href.lower().split("?")[0].endswith(DOC_EXTENSIONS):
                        continue
                    if href in seen:
                        continue
                    seen.add(href)
                    link_text = a.get_text(" ", strip=True).strip("*").strip()
                    label = f"Key {key_no} — {link_text}" if key_no else (link_text or href)
                    fn = href.split("/")[-1].split("?")[0]
                    out.append((label, href, fn, self.PAGE, bid_date))

        print(f"  ITD: {len(out)} document link(s) from the advertised-project tables")
        return out


class WYDOTExevisionAdapter(SiteAdapter):
    """Wyoming DOT — wydot.exevision.com/ws.

    Plain server-rendered HTML (one GET, no JS). Each advertised project is a
    `<table border="1">` block: Call Order / Project Number / Description /
    County on the left, a list of document links on the right. Those links —
    "E-79 (Invitation For Bids)", "View Bid Items", "View Addendum # N" — point
    at Google Drive share URLs (`drive.google.com/file/d/<id>/view`), so the
    generic crawler's ".pdf/.docx" href filter skips every one of them and the
    site scans nothing. This adapter pulls the Drive file id out of each link
    and rewrites it to a direct-download URL. The full plan sets live on
    QuestCDN behind a paywall and are not reachable here; the E-79 + bid items
    + addenda are. "View Planholder's List" is an HTML roster, not a document,
    and is skipped. The site URL field is ignored.
    """

    key = "wydot_exevision"
    label = "Wyoming DOT (advertised projects)"
    help = ("Reads wydot.exevision.com/ws and follows each project's Google "
            "Drive links (E-79, bid items, addenda). Full plans are on QuestCDN "
            "and not included. The site URL is ignored.")
    hosts = ("wydot.exevision.com",)

    PAGE = "https://wydot.exevision.com/ws/"
    _DRIVE_ID = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")
    _PROJ = re.compile(r"Project Number:\s*([A-Za-z0-9\-]+)")
    _DESC = re.compile(r"Description:\s*(.+?)\s*(?:County:|Engineer:|$)", re.S)
    _LETTING = re.compile(r"Scheduled letting for\s+([A-Za-z]+ +\d{1,2},? *\d{4})")

    @staticmethod
    def _drive_download_url(file_id):
        return (f"https://drive.usercontent.google.com/download"
                f"?id={file_id}&export=download&confirm=t")

    @staticmethod
    def _filename(proj, link_text):
        t = re.sub(r"\s+", " ", link_text or "").strip()
        low = t.lower()
        if "e-79" in low or "e79" in low:
            name = "E-79"
        elif "bid item" in low:
            name = "Bid Items"
        elif t.lstrip().startswith("#"):
            name = "Addendum " + t.lstrip("# ").strip()
        else:
            name = re.sub(r"[^A-Za-z0-9 .\-]", "", t) or "document"
        return f"{proj} {name}.pdf"

    def find_documents(self, site):
        from bs4 import BeautifulSoup

        r = requests.get(self.PAGE, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        out, seen = [], set()
        for block in soup.find_all("table", border="1"):
            text = block.get_text(" ", strip=True)
            m = self._PROJ.search(text)
            if not m:
                continue
            proj = m.group(1)
            desc = self._DESC.search(text)
            label = f"{proj} {desc.group(1).strip()}" if desc else proj
            lm = self._LETTING.search(text)
            bid_date = parse_bid_date(lm.group(1)) if lm else None

            for a in block.find_all("a", href=True):
                dm = self._DRIVE_ID.search(a["href"])
                if not dm:
                    continue
                file_id = dm.group(1)
                if file_id in seen:
                    continue
                seen.add(file_id)
                out.append((
                    label,
                    self._drive_download_url(file_id),
                    self._filename(proj, a.get_text(" ", strip=True)),
                    self.PAGE,
                    bid_date,
                ))

        print(f"  WYDOT: {len(out)} document(s) across the advertised projects")
        return out


class ConstructConnectAdapter(SiteAdapter):
    """ConstructConnect — app.constructconnect.com.

    Unlike every other adapter here, this is a heavy authenticated Angular
    SPA with no plain API: a real login (see needs_browser) and a real
    browser to page through results and trigger each project's document
    download.

    Current phase: prove out login + document discovery + download before
    adding any scoping. It logs in, confirms the SSO redirect actually
    landed in the authenticated app (see _wait_left_login_host — the
    identity provider's own stuck "Loading..." screen looks enough like a
    successful login to fool the generic password-field heuristic), then
    works through whatever project list that lands on: page through it
    (MAX_PROJECTS_PER_SEARCH cap), open each project, click "View/Download
    Documents" (opens a new tab), and click "Download All" there — every
    document in the project merged into one PDF. (The split button also
    offers a "Zipped PDFs" format that keeps documents separate, but that
    picker needs the docviewer sidebar in a state that hasn't shown up
    reliably; one merged PDF per project is simpler and was confirmed by
    the user as the intended approach.) That merged PDF becomes a single
    document for the project in the keyword-matching pipeline — not split
    back into per-document files.

    Next phase (not wired up yet): ConstructConnect's own filter UI doesn't
    put its state in the URL — applying filters leaves the address bar
    unchanged — so once the above is confirmed working, scope this to the
    account's curated Saved Searches (Search > Saved Searches) instead of
    whatever the default view shows. SAVED_SEARCHES below is a start on
    that list, kept in sync by hand with the account.

    The site URL field is ignored for navigation (same as the other
    adapters) but is still used as the login page when no separate Login
    page URL is set on the Login tab.
    """

    key = "constructconnect"
    label = "ConstructConnect"
    help = ("Logs in and downloads documents from whatever project list is "
            "on screen after login (not yet scoped to a Saved Search — see "
            "adapters.py). Needs a login set on the Login tab.")
    hosts = ("app.constructconnect.com",)
    needs_browser = True

    # Kept for the next phase (see find_documents_with_browser) — sync this
    # by hand with Search > Saved Searches in the account when it's back in use.
    SAVED_SEARCHES = (
        "denver storm",
        "Erosion AND Sedimentation Controls - General Terms - Documents",
        "Gabions - General Terms - Documents",
        "Soil Reinforcement - General Terms - Documents",
        "Soil Stabilization - General Terms - Documents",
        "Southern Utah",
    )

    BASE = "https://app.constructconnect.com"
    PAGE_TIMEOUT_MS = 25000
    RUN_BUDGET_S = 1800              # whole adapter run
    MAX_PROJECTS_PER_SEARCH = 100    # safety cap per results view
    MAX_ZIP_BYTES = 500 * 1024 * 1024
    LOGIN_REDIRECT_TIMEOUT_S = 25    # how long to wait for the SSO redirect back into the app
    # A generic browser-flavored UA and Playwright's default navigator.webdriver=true
    # got the SSO login (login.io.constructconnect.com) stuck forever on a
    # "Loading..." screen — never redirecting back into the app — which looks
    # like automation detection on the identity provider's side. This is a
    # plain desktop Chrome UA instead; combined with the launch arg and init
    # script below, it's a standard (not foolproof) way to look less like a
    # bot to that kind of check.
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

    def find_documents_with_browser(self, site, login):
        if not login:
            print("  ! ConstructConnect: no login configured on this site — skipping")
            return [], None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("  ! ConstructConnect: Playwright not installed — skipping")
            return [], None

        import browser_crawl  # reuse its best-effort login helper

        deadline = time.time() + self.RUN_BUDGET_S
        try:
            pw = sync_playwright().start()
        except Exception as e:
            print(f"  ! ConstructConnect: could not start Playwright ({e})")
            return [], None

        try:
            browser = pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"  ! ConstructConnect: Chromium not available ({e})")
            pw.stop()
            return [], None

        ctx = browser.new_context(
            accept_downloads=True,
            user_agent=self.USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()
        try:
            # Reuses this same page for login (rather than the default
            # throwaway one _attempt_login would open and close) — the login
            # goes through an SSO redirect (login.io.constructconnect.com ->
            # /api/gcipconsume?returnUrl=...), and a fresh page.goto() to a
            # different URL right after was bouncing back to the login
            # screen instead of landing in the authenticated app.
            login_result = browser_crawl._attempt_login(ctx, login, page=page)
            if not login_result.get("ok"):
                return [], login_result

            # _attempt_login's "password field is gone" heuristic is also
            # satisfied by the SSO's own stuck "Loading..." screen (it has
            # no password field either) — confirm we actually left that
            # host before treating this as a real login.
            if not self._wait_left_login_host(page):
                msg = (f"stuck on the SSO login page ({page.url}) after submitting "
                       "credentials — looks like it's being blocked as automated, "
                       "not a bad password")
                print(f"  ! ConstructConnect: {msg}")
                self._save_debug_screenshot(page, "stuck_on_login")
                return [], {"ok": False, "message": msg}

            print(f"  ConstructConnect: authenticated, landed on {page.url}")
            self._save_debug_screenshot(page, "landed")  # always, while this is still new
            self._dismiss_cookie_banner(page)

            # Not filtering by Saved Search yet (SAVED_SEARCHES above is
            # parked for that) — first checking whether documents can be
            # found and downloaded at all from whatever login lands us on.
            out = self._scan_results_page(page, deadline)
            if not out:
                self._save_debug_screenshot(page, "no_docs_found")
            return out, login_result
        finally:
            try:
                browser.close()
            finally:
                pw.stop()

    def _dismiss_cookie_banner(self, page):
        """The cookie consent banner sits fixed at the bottom of the
        viewport. It was still showing in a debug screenshot taken well
        into a run (after several page.go_back() calls), so a one-time
        dismissal right after landing isn't enough — this is also called
        before each project, and is a cheap no-op once the banner is
        actually gone (query_selector just finds nothing)."""
        try:
            btn = page.query_selector("button:has-text('Necessary Only')")
            if btn and btn.is_visible():
                btn.click(timeout=4000)
                page.wait_for_timeout(300)
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't dismiss the cookie banner ({e})")

    def _wait_left_login_host(self, page):
        """_attempt_login's "password field is gone" heuristic is also
        satisfied by the SSO's own stuck "Loading..." screen (no password
        field there either), so confirm here that the browser actually
        navigated away from login.io.constructconnect.com within a
        reasonable window before calling it a real login."""
        deadline = time.time() + self.LOGIN_REDIRECT_TIMEOUT_S
        while time.time() < deadline:
            if "login.io.constructconnect.com" not in page.url:
                return True
            page.wait_for_timeout(1000)
        return "login.io.constructconnect.com" not in page.url

    # Documents-column statuses that mean the project has nothing uploaded
    # yet — attempting these always ends in either a disabled "View/Download
    # Documents" button (8s timeout) or an opened-but-empty documents view
    # (no "Zipped PDFs" option), so skip them instead of burning the run
    # budget on a guaranteed miss.
    NO_DOCS_STATUSES = {"Requesting Plans", "Sent to Scan", "Project Details Only"}

    def _scan_results_page(self, page, deadline):
        """Work through whatever project list is currently on screen —
        no Saved Search filtering yet (see SAVED_SEARCHES / the note in
        find_documents_with_browser), just proving out login + document
        discovery + download first."""
        out = []
        project_count = 0
        skipped_no_docs = 0
        while project_count < self.MAX_PROJECTS_PER_SEARCH and time.time() < deadline:
            rows = self._project_rows(page)
            if not rows:
                break
            for label, doc_status in rows:
                if project_count >= self.MAX_PROJECTS_PER_SEARCH or time.time() > deadline:
                    break
                if doc_status in self.NO_DOCS_STATUSES:
                    skipped_no_docs += 1
                    project_count += 1
                    continue
                try:
                    out.extend(self._download_project(page, label))
                except Exception as e:
                    print(f"  ! ConstructConnect: project '{label}' failed: {e}")
                project_count += 1
            if not self._go_to_next_page(page):
                break
            page.wait_for_timeout(1500)
        print(f"  ConstructConnect: {project_count} project(s) checked, "
              f"{skipped_no_docs} skipped (no documents posted yet), "
              f"{len(out)} document(s) found")
        return out

    def _save_debug_screenshot(self, page, tag):
        """Save a screenshot + the page's visible text under a distinct
        name for this point in the run, so a person can see what the
        crawler actually saw instead of guessing blind from the text log
        alone. Picked up by the GitHub Actions workflow as a build artifact
        when present."""
        try:
            page.screenshot(path=f"constructconnect_{tag}.png", full_page=True)
            text = page.evaluate("document.body.innerText") or ""
            with open(f"constructconnect_{tag}.txt", "w") as f:
                f.write(f"URL: {page.url}\n\n{text[:20000]}")
            print(f"  ConstructConnect: saved constructconnect_{tag}.png/.txt "
                  "(uploaded as a workflow artifact)")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't save debug screenshot ({tag}): {e}")

    def _project_rows(self, page):
        """The Project Name column's link text plus the Documents column's
        status text, for every row on the current results page — matched by
        header position so the name doesn't accidentally pick up the
        Documents column's own short "Drawing"/"Specs..." links.

        The results grid turned out to have zero <table> rows in practice
        (a debug run landed fine, with the grid's data visibly rendered in
        page.innerText, but this method's `table` selector timed out and
        the whole scan reported "0 project(s) checked"). Angular Material's
        flex-based mat-table renders a div/role grid instead of a real
        <table> — no <tr>/<td>/<th> at all, just role="row"/"columnheader"/
        "cell" attributes on divs. Real <table> markup is tried first since
        it's cheaper and was presumably what this was written against; the
        role-based grid is the fallback that actually matches what's live.
        """
        try:
            page.wait_for_selector("table, [role='table']", timeout=10000)
        except Exception:
            return []

        header_cells = page.query_selector_all("table thead th, table tr:first-child th")
        if not header_cells:
            header_cells = page.query_selector_all(
                "[role='table'] [role='row']:first-of-type [role='columnheader'], "
                "[role='table'] [role='row']:first-of-type [role='cell']"
            )
        headers = [(h.inner_text() or "").strip() for h in header_cells]
        name_col = headers.index("Project Name") if "Project Name" in headers else None
        docs_col = headers.index("Documents") if "Documents" in headers else None

        body_rows = page.query_selector_all("table tbody tr")
        if not body_rows:
            all_role_rows = page.query_selector_all("[role='table'] [role='row']")
            body_rows = all_role_rows[1:] if len(all_role_rows) > 1 else []

        rows = []
        for tr in body_rows:
            cells = tr.query_selector_all("td")
            if not cells:
                cells = tr.query_selector_all("[role='cell'], [role='gridcell']")
            a = None
            if name_col is not None and name_col < len(cells):
                a = cells[name_col].query_selector("a")
            if a is None:
                a = tr.query_selector("a")  # fallback: first link in the row
            if a is None:
                continue
            try:
                label = (a.inner_text() or "").strip()
            except Exception:
                continue
            if not label:
                continue
            doc_status = ""
            if docs_col is not None and docs_col < len(cells):
                try:
                    doc_status = (cells[docs_col].inner_text() or "").strip()
                except Exception:
                    doc_status = ""
            rows.append((label, doc_status))
        return rows

    def _go_to_next_page(self, page):
        try:
            btn = (page.query_selector("button[aria-label*='Next' i]")
                   or page.query_selector("button[title*='Next' i]"))
            if btn and btn.is_visible() and btn.is_enabled():
                btn.click(timeout=4000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        return False

    def _download_project(self, page, label):
        self._dismiss_cookie_banner(page)
        try:
            page.click(f"text={label}", timeout=8000)
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't open '{label}': {e}")
            return []
        try:
            page.wait_for_selector("text=View/Download Documents", timeout=self.PAGE_TIMEOUT_MS)
        except Exception:
            print(f"  ! ConstructConnect: '{label}' has no Documents button — skipping")
            page.go_back()
            page.wait_for_timeout(1000)
            return []

        project_url = page.url
        out = []
        doc_page = None
        try:
            doc_page, download = self._click_view_download(page)
            if download:
                out = self._handle_download(download, label, project_url)
            elif doc_page:
                download = self._click_download_all(doc_page)
                if download:
                    out = self._handle_merged_pdf(download, label, project_url)
                else:
                    print(f"  ! ConstructConnect: 'Download All' didn't produce "
                          f"anything for '{label}'")
            else:
                print(f"  ! ConstructConnect: 'View/Download Documents' did nothing "
                      f"observable for '{label}'")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't download documents for '{label}': {e}")
        finally:
            if doc_page:
                try:
                    doc_page.close()
                except Exception:
                    pass

        page.go_back()
        page.wait_for_timeout(1200)
        return out

    def _click_view_download(self, page):
        """A debug screenshot showed the project page completely unchanged
        right after clicking "View/Download Documents" — no in-page modal
        ever appears there. So instead of assuming one, catch whichever of
        the two things the button actually does: open the download picker
        in a new tab, or trigger a file download directly on this same
        page. Returns (new_page_or_None, download_or_None)."""
        ctx = page.context
        new_pages = []
        downloads = []
        ctx.on("page", lambda p: new_pages.append(p))
        page.on("download", lambda d: downloads.append(d))
        page.click("text=View/Download Documents", timeout=8000)
        deadline = time.time() + 6
        while time.time() < deadline and not new_pages and not downloads:
            page.wait_for_timeout(200)
        if downloads:
            print("  ConstructConnect: 'View/Download Documents' triggered a direct download")
            return None, downloads[0]
        if new_pages:
            doc_page = new_pages[0]
            try:
                doc_page.wait_for_load_state(timeout=self.PAGE_TIMEOUT_MS)
            except Exception:
                pass
            print(f"  ConstructConnect: 'View/Download Documents' opened a new tab "
                  f"({doc_page.url})")
            return doc_page, None
        return None, None

    def _handle_download(self, download, label, project_url):
        """A download that fired directly off "View/Download Documents"
        (no "Download All"/"Zipped PDFs" picker involved) — could be a zip
        or a single document depending on how many files the project has."""
        path = download.path()
        if not path:
            return []
        if os.path.getsize(path) > self.MAX_ZIP_BYTES:
            print("  ! ConstructConnect: download too large, skipping")
            return []
        name = download.suggested_filename or ""
        with open(path, "rb") as f:
            data = f.read()
        if name.lower().endswith(".zip"):
            return self._extract_zip(data, label, project_url)
        if name.lower().endswith(DOC_EXTENSIONS):
            return [(label, None, f"{label} - {name}", data, project_url, None)]
        print(f"  ! ConstructConnect: downloaded '{name}' — unrecognized type, skipping")
        return []

    def _handle_merged_pdf(self, download, label, project_url):
        """The docviewer's "Download All" always merges every document into
        one PDF — trust that instead of the download's suggested filename,
        which truncates unpredictably for a project name containing a
        semicolon (ConstructConnect's Content-Disposition header isn't
        quoted, so a bare ";" in the name — e.g. "US-6; Improve Ints..." —
        ends the filename early; the browser then reports it as just "US-6",
        with no extension at all, which broke the old extension-sniffing
        check in _handle_download)."""
        path = download.path()
        if not path:
            return []
        if os.path.getsize(path) > self.MAX_ZIP_BYTES:
            print("  ! ConstructConnect: download too large, skipping")
            return []
        with open(path, "rb") as f:
            data = f.read()
        return [(label, None, f"{label} - All Documents.pdf", data, project_url, None)]

    # The docviewer's "Download All" button has this stable id (confirmed
    # by the user via devtools) — target it directly rather than a text
    # match, which is more prone to ambiguity (e.g. matching a wrapping
    # element instead of the actual clickable button).
    DOWNLOAD_ALL_SELECTOR = "#download_button"

    def _wait_downloads_ready(self, page, timeout_s=45):
        """The docviewer tab loads its own document list asynchronously —
        a debug screenshot caught "Download All" (and its arrow) still
        greyed out under a "Document is loading, please wait..." overlay
        well after the tab's browser-level load event had already fired.
        Poll until the button actually reports enabled instead of assuming
        a fixed short wait covers it."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            btn = page.query_selector(self.DOWNLOAD_ALL_SELECTOR)
            if btn and btn.is_enabled():
                return True
            page.wait_for_timeout(500)
        return False

    def _click_download_all(self, page):
        """Click "Download All" on the docviewer tab and return the
        resulting Download — every document in the project merged into one
        PDF. (The split button's arrow also offers a "Zipped PDFs" format
        that keeps documents separate, but that picker needs the sidebar in
        a state that hasn't shown up reliably; a single merged PDF per
        project is simpler and is the intended approach here.)"""
        if not self._wait_downloads_ready(page):
            print("  ! ConstructConnect: document never finished loading — "
                  "'Download All' stayed disabled")
            if not getattr(self, "_saved_download_modal_debug", False):
                self._saved_download_modal_debug = True
                self._save_debug_screenshot(page, "download_modal")
            return None

        try:
            with page.expect_download(timeout=45000) as dl_info:
                page.click(self.DOWNLOAD_ALL_SELECTOR, timeout=8000)
            return dl_info.value
        except Exception as e:
            print(f"  ! ConstructConnect: download didn't start: {e}")
            return None

    def _extract_zip(self, zip_bytes, label, project_url):
        out = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(DOC_EXTENSIONS):
                        continue
                    data = zf.read(name)
                    # Prefixed with the project name so the same filename
                    # (e.g. "Addendum 1.pdf") across different projects
                    # doesn't collide in the site-wide "already scanned" key.
                    fn = f"{label} - {name.split('/')[-1]}"
                    out.append((label, None, fn, data, project_url, None))
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't read the zip for '{label}': {e}")
        return out


_ADAPTER_CLASSES = [UDOTMasterworksAdapter, ITDAdvertisedAdapter, WYDOTExevisionAdapter,
                     ConstructConnectAdapter]
ADAPTERS = {cls.key: cls for cls in _ADAPTER_CLASSES}


def get_adapter(key):
    cls = ADAPTERS.get(key)
    return cls() if cls else None


def match_adapter_class(url):
    """The adapter class whose hosts recognize this URL, or None. Shared by
    adapter_for_site() (the scan-time fallback) and the dashboard's site
    save handler (which uses it to attach the adapter up front, so the
    choice is persisted and visible rather than a silent runtime fallback)."""
    for cls in _ADAPTER_CLASSES:
        if cls.matches_url(url):
            return cls
    return None


def adapter_for_site(site):
    """The adapter to use for a site: the one it explicitly selected, or —
    failing that — one that recognizes the site's URL host (so a plain-URL
    site pointed at a known portal still gets the fast path even if it
    predates auto-attachment, or was added some other way)."""
    key = (site.get("adapter") or "").strip()
    if key:
        return get_adapter(key)
    cls = match_adapter_class(site.get("url") or "")
    return cls() if cls else None


def list_adapters():
    return [{"key": c.key, "label": c.label, "help": c.help, "hosts": list(c.hosts),
             "needs_browser": c.needs_browser}
            for c in _ADAPTER_CLASSES]

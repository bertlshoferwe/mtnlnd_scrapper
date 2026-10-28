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
from datetime import datetime, timedelta, timezone
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


_PLACEHOLDER_EMAIL = re.compile(r"@(example\.(com|org|net)|test\.com)$|^(test|none|na|n/a)@", re.I)


def clean_emails(raw):
    """'a@x.com; b@y.com;' / 'a@x.com, b@y.com' -> ['a@x.com', 'b@y.com'],
    deduped, dropping placeholders like test@example.com."""
    out = []
    for e in re.split(r"[;,\s]+", raw or ""):
        e = e.strip().strip(".").lower()
        if "@" in e and not _PLACEHOLDER_EMAIL.search(e) and e not in out:
            out.append(e)
    return out


def plan_holder(company, role=None, contact=None, title=None, emails=None, phone=None,
                address=None, city=None, state=None, zipcode=None, cert=None):
    """One plan-holder entry in the shape the dashboard's Plan Holders tab
    reads. Every field but company is optional — the tab only shows a column
    when at least one entry on that project has it (UDOT has no emails,
    WYDOT has no role, and so on)."""
    def c(v):
        v = re.sub(r"\s+", " ", str(v or "")).strip()
        return v if v and v.upper() not in ("N/A", "NA", "-", "NONE") else None
    return {
        "company": c(company) or "(unknown company)",
        "role": c(role), "contact": c(contact), "title": c(title),
        "emails": emails or [], "phone": c(phone), "address": c(address),
        "city": c(city), "state": c(state), "zip": c(zipcode), "cert": c(cert),
    }


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

    def __init__(self):
        # {label: [plan_holder(...), ...]} — filled in as a side effect of
        # find_documents()/find_documents_with_browser() for portals that
        # publish a plan-holders list, keyed by the same `label` the doc
        # tuples use. scraper.scan_site() saves it per project after the
        # adapter runs. Empty for an adapter that doesn't support it.
        self.plan_holders = {}
        # {label: "City, ST 12345"} — where a project is, for portals that
        # show it per project (ConstructConnect). Saved like plan_holders.
        self.locations = {}
        # Every project label still on the portal's listing this run, even
        # ones skipped without fetching documents (ConstructConnect skips
        # unchanged projects) — so seen-tracking doesn't count a skipped
        # project as gone and hide it as closed.
        self.listed_labels = set()

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

    def find_documents_with_browser(self, site, login, known_updated=None, holders_at=None):
        """Only for needs_browser=True adapters. Return (doc_links,
        login_result, current_updated):
        doc_links is [(label, document_url_or_None, filename, content_bytes_or_None,
        project_page_url, bid_date, bid_time), ...] — already in scan_site()'s
        final 7-tuple shape, since a browser-driven adapter typically has content
        bytes rather than a URL the caller can fetch later. `bid_time` is a raw
        display string (e.g. '10:00am MT') for portals that show a time alongside
        the bid-opening date, or None. `login_result` is the {"ok", "message"}
        dict from the login attempt (or None if login was never attempted).
        `login` is {"username", "password", "url"} or None. `known_updated`
        (optional) and `current_updated` (always returned, {} if unused) are
        an opaque {project_id: watermark} pair an adapter may use to skip
        re-fetching a project whose listing hasn't changed since the previous
        run — see ConstructConnectAdapter, the only adapter using this so
        far."""
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
    help = "Automatically downloads plans, notices, and addenda for every advertised UDOT project."
    hosts = ("contractorzone.udot.utah.gov",)

    BASE = "https://contractorzone.udot.utah.gov"
    SECTION = "advertisements"
    # Document tabs on a project's page, each a section of the project-files API:
    # GET /project-files/{uuid}/{section} lists it, and the download URL uses the
    # same section. "project-files" = the plan set / NTC / items; "addendum" =
    # addenda issued after advertisement.
    FILE_SECTIONS = ("project-files", "addendum")
    # UDOT opens every bid at 2:00 PM Mountain: its API only gives the date,
    # and the Contractor Zone site itself prints "at 2:00 PM MST" after it.
    BID_TIME = "2:00pm MT"

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
            self._load_plan_holders(uuid, label)

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
                    out.append((label, url, fn, project_url, bid_date, self.BID_TIME if bid_date else None))
        return out

    def _load_plan_holders(self, uuid, label):
        """The project page's "Plan Holders" tab — company, Prime/Sub,
        address, phone. No contact names or emails (UDOT only shares those
        with other plan holders who opted in). Best-effort: a failure here
        never blocks the documents."""
        try:
            rows = self._get_json(f"/advertisements/plan_holders/list/{uuid}").get("result") or []
        except (requests.RequestException, ValueError) as e:
            print(f"  ! UDOT: couldn't load plan holders for {label}: {e}")
            return
        holders = []
        for r in rows:
            phone = re.sub(r"\D", "", str(r.get("phone") or ""))
            if len(phone) == 10:
                phone = f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"
            addr = ", ".join(x for x in (r.get("addr"), r.get("addr2")) if x and str(x).strip())
            holders.append(plan_holder(
                r.get("company_name"), role=r.get("bidding_as"), phone=phone or r.get("phone"),
                address=addr, city=(r.get("city") or "").title(), state=r.get("state"),
                zipcode=r.get("zipcode"),
            ))
        self.plan_holders[label] = holders


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
    help = "Automatically downloads plans and notices for Idaho DOT's currently advertised projects."
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
        self._load_plan_holders({lbl for lbl, *_ in out})
        return out

    # ITD's plan holders live on QuestCDN (ITD moved bidding there late 2025)
    # — public, no login, but the plan-holder data call only answers inside a
    # cookie session that has "walked" there the way a browser does: the
    # postings page, the postings list, the project, then its plan-holders
    # page. Skipping a step gets a login page or a 500 instead (confirmed
    # 2026-10-18).
    QUEST_BASE = "https://qcpi.questcdn.com"
    QUEST_GROUP = "1950787"
    QUEST_PAUSE_S = 3
    _QUEST_KEY_RE = re.compile(r"Key\s*No\.?\s*([\d\s]+)", re.I)

    def _load_plan_holders(self, labels):
        """Best-effort: fills self.plan_holders for every ITD label whose key
        number matches a QuestCDN posting ("Key No. 22751 FY27 D2 SEAL COATS").
        Any failure just leaves ITD without plan holders this run."""
        by_key = {}
        for lbl in labels:
            m = re.match(r"Key ([\w\-]+)", lbl or "")
            if m:
                for k in m.group(1).split("-"):
                    by_key.setdefault(re.sub(r"\D", "", k), []).append(lbl)
        if not by_key:
            return
        try:
            postings = self._quest_postings()
        except Exception as e:
            print(f"  ! ITD: couldn't list QuestCDN postings for plan holders: {e}")
            return
        found = 0
        for i, post in enumerate(postings):
            m = self._QUEST_KEY_RE.search(post["name"])
            keys = m.group(1).split() if m else []
            targets = {lbl for k in keys for lbl in by_key.get(k, [])}
            if not targets:
                continue
            prev_id = postings[i - 1]["id"] if i > 0 else ""
            next_id = postings[i + 1]["id"] if i + 1 < len(postings) else ""
            # QuestCDN starts answering 403 to a quick burst of requests
            # (seen 2026-10-18 after ~10 projects back to back) — pace them
            # and retry once after a longer pause.
            time.sleep(self.QUEST_PAUSE_S)
            try:
                try:
                    holders = self._quest_plan_holders(post["id"], prev_id, next_id)
                except requests.HTTPError as e:
                    if e.response is None or e.response.status_code != 403:
                        raise
                    time.sleep(self.QUEST_PAUSE_S * 10)
                    holders = self._quest_plan_holders(post["id"], prev_id, next_id)
            except Exception as e:
                print(f"  ! ITD: couldn't load plan holders for {post['name']}: {e}")
                continue
            for lbl in targets:
                self.plan_holders[lbl] = holders
            found += 1
        print(f"  ITD: plan holders for {found} project(s) from QuestCDN")

    def _quest_session(self):
        if getattr(self, "_qs", None) is None:
            self._qs = requests.Session()
            self._qs.headers.update({"User-Agent": "Mozilla/5.0"})
            self._qs.get(self._quest_postings_url(), timeout=REQUEST_TIMEOUT)
        return self._qs

    def _quest_postings_url(self):
        return f"{self.QUEST_BASE}/cdn/posting/?group={self.QUEST_GROUP}&provider={self.QUEST_GROUP}"

    def _quest_postings(self):
        s = self._quest_session()
        r = s.get(f"{self.QUEST_BASE}/cdn/browse_posting/",
                  params={"posting_type": 1, "group": self.QUEST_GROUP,
                          "provider": self.QUEST_GROUP, "draw": 1, "start": 0, "length": 200},
                  headers={"X-Requested-With": "XMLHttpRequest",
                           "Referer": self._quest_postings_url()},
                  timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return [{"id": str(d.get("project_id")),
                 "name": re.sub(r"<[^>]+>", " ", d.get("render_name") or "")}
                for d in r.json().get("data") or [] if d.get("project_id")]

    def _quest_plan_holders(self, project_id, prev_id, next_id):
        s = self._quest_session()
        xhr = {"X-Requested-With": "XMLHttpRequest", "Referer": self._quest_postings_url()}
        s.get(f"{self.QUEST_BASE}/cdn/util/get_posting/",
              params={"current_project_id": project_id, "next_project_id": next_id,
                      "prev_project_id": prev_id}, headers=xhr, timeout=REQUEST_TIMEOUT)
        page = s.get(f"{self.QUEST_BASE}/cdn/view_plan_holders/",
                     params={"project_id": project_id, "path": 269, "search_id": ""},
                     timeout=REQUEST_TIMEOUT)
        r = s.get(f"{self.QUEST_BASE}/cdn/planholder_data/",
                  params={"project_id": project_id, "draw": 1, "start": 0, "length": -1,
                          "search[value]": ""},
                  headers={**xhr, "Referer": page.url,
                           "Accept": "application/json, text/javascript, */*; q=0.01"},
                  timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        holders = []
        for d in r.json().get("data") or []:
            company = re.sub(r"<[^>]+>", "", d.get("render_company") or "")
            addr = d.get("address") or ""
            # "1649 W Shoreline Drive, Suite 100, Boise, ID-83702"
            m = re.match(r"(.*),\s*([^,]+),\s*([A-Z]{2})-?\s*([\d\-]*)\s*$", addr)
            street, city, state, zipcode = (m.groups() if m else (addr, None, None, None))
            holders.append(plan_holder(
                company, role=d.get("business_designation"), contact=d.get("contact"),
                emails=clean_emails(d.get("email_address")),
                phone=re.sub(r"<[^>]+>", "", d.get("render_phone_number") or ""),
                address=street, city=city, state=state, zipcode=zipcode,
                cert=d.get("business_certification_list"),
            ))
        return holders


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
    help = ("Automatically downloads bid documents and addenda for Wyoming DOT's "
            "advertised projects. Full plan sets aren't included — those require "
            "a separate paid QuestCDN membership.")
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

            ph = block.find("a", href=re.compile(r"PlanHolders\.aspx", re.I))
            if ph:
                self._load_plan_holders(urljoin(self.PAGE, ph["href"]), label)

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

    def _load_plan_holders(self, url, label):
        """The project's "View Planholder's List" page (PlanHolders.aspx) —
        one plain HTML table: Name / Address / City / ST / Postal / Phone /
        FAX / Email, emails ';'-separated. No Prime/Sub. Best-effort."""
        from bs4 import BeautifulSoup
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ! WYDOT: couldn't load plan holders for {label}: {e}")
            return
        soup = BeautifulSoup(r.text, "html.parser")
        holders = []
        for table in soup.find_all("table"):
            cols = None
            for tr in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                low = [c.lower() for c in cells]
                if "name" in low and "phone" in low:
                    cols = {c: i for i, c in enumerate(low)}
                    continue
                if not cols or len(cells) < len(cols) or not cells[cols["name"]]:
                    continue
                get = lambda k: cells[cols[k]] if k in cols else None
                holders.append(plan_holder(
                    get("name"), phone=get("phone"), emails=clean_emails(get("email")),
                    address=get("address"), city=(get("city") or "").title(),
                    state=get("st"), zipcode=get("postal"),
                ))
            if holders:
                break
        self.plan_holders[label] = holders


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
    (MAX_PROJECTS_PER_SEARCH cap), open each project, click into its
    Documents panel (see DOCUMENT_CATEGORIES — replaced the site's old
    single "View/Download Documents" button as of 2026-10-04, opens a new
    tab the same way), and on the docviewer tab use the split button's
    arrow to pick "Multi-page PDF" and click Start — every document in
    the project merged into one PDF (user's pick, 2026-10-27). (The flyout
    that arrow opens renders at a fixed, occluded position — real and
    enabled per computed style, just never visible to a human because
    other page chrome sits on top of it; see _click_download_format's
    force=True clicks, confirmed working via devtools 2026-09-20.) Falls
    back to "Zipped PDFs" (every document a separate file) only if that
    doesn't behave as expected.

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
    help = "Logs in and automatically downloads bid documents for listed projects. Needs a login set on the Login tab."
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

    # The states Mountainland actually cares about (confirmed by the user
    # via screenshot 2026-10-04) — narrows the default unscoped nationwide
    # view (12,941 results) down to ~9,000. This filter does NOT persist on
    # the account (confirmed: resets every login — no "default service
    # area" saved), so _apply_location_filter has to redo it every run.
    LOCATION_FILTER_STATES = ("Arizona", "Colorado", "Idaho", "Nevada", "Utah", "Wyoming")

    BASE = "https://app.constructconnect.com"
    PAGE_TIMEOUT_MS = 25000
    # Scoped results ran ~722 projects with the old stage filter (261 with
    # the bid date filter that replaced it, 2026-10-27); at the
    # ~23s/project pace seen 2026-10-04, a full pass takes ~4.5hrs. Raised
    # from the original 1800s/100 (which only ever reached 79 projects
    # before the old 30-min budget cut it off) — fine to run this long on
    # the user's own self-hosted box (no shared-runner time limit to worry
    # about), so size both to comfortably clear the whole scoped set in one
    # run rather than crawling through it piecemeal over many days.
    RUN_BUDGET_S = 6 * 3600          # whole adapter run
    MAX_PROJECTS_PER_SEARCH = 1000   # safety cap per results view
    MAX_ZIP_BYTES = 500 * 1024 * 1024
    # A big project's merged PDF or zip is built server-side on demand — 45s
    # wasn't enough for larger ones (e.g. "Pedestrian and Bicylce Paths" and
    # "Dominion WSD - CBWRF Balance of Plant" both timed out and got skipped
    # entirely 2026-09-20). Raised well past the largest projects seen so far.
    DOWNLOAD_TIMEOUT_MS = 180000
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

    # Projects skipped as unchanged still get their Contacts re-read when the
    # saved plan holders are missing or older than this — companies keep
    # joining right up to bid day, while the documents stay the same. Capped
    # per run so a first pass over a long list doesn't eat the whole budget.
    CONTACTS_STALE_S = 20 * 3600
    MAX_CONTACT_REFRESH = 40

    def find_documents_with_browser(self, site, login, known_updated=None, holders_at=None):
        self._site_name = site.get("name") or ""
        self._holders_at = holders_at or {}
        if not login:
            print("  ! ConstructConnect: no login configured on this site — skipping")
            return [], None, {}

        deadline = time.time() + self.RUN_BUDGET_S
        pw, browser, page, login_result = self._start_login(login)
        try:
            if page is None:
                return [], login_result, {}

            # Not filtering by Saved Search yet (SAVED_SEARCHES above is
            # parked for that) — but the location, category and bid date
            # filters scope the default nationwide view down to what matters.
            self._apply_location_filter(page)
            self._apply_category_filter(page)
            self._apply_bid_date_filter(page)
            out, current_updated = self._scan_results_page(page, deadline, known_updated or {})
            if not out:
                self._save_debug_screenshot(page, "no_docs_found")
            return out, login_result, current_updated
        finally:
            self._close_browser(pw, browser)

    def refetch_project_documents(self, login, project_url, label):
        """Re-fetches one project's documents live — for when a previously
        cached copy (see is_cached_document in schema.sql) has expired and
        someone views it again. Reuses the same login flow
        find_documents_with_browser uses, then jumps straight to the
        project's own page (its stored source_url) instead of paging
        through the results list to find it by name again."""
        if not login:
            print("  ! ConstructConnect: no login configured — can't re-fetch")
            return []
        pw, browser, page, login_result = self._start_login(login)
        try:
            if page is None:
                print(f"  ! ConstructConnect: re-fetch login failed ({login_result.get('message')})")
                return []
            try:
                page.goto(project_url, timeout=self.PAGE_TIMEOUT_MS)
            except Exception as e:
                print(f"  ! ConstructConnect: couldn't open {project_url} ({e})")
                return []
            return self._download_current_project(page, label)
        finally:
            self._close_browser(pw, browser)

    def _start_login(self, login):
        """Launches headless Chromium and logs into ConstructConnect.
        Returns (pw, browser, page, login_result) — page is None on any
        failure (caller should treat that as "couldn't do it" and stop).
        pw/browser may also be None if Playwright/Chromium itself never
        came up, in which case there's nothing to close. Shared by
        find_documents_with_browser (a full scan) and
        refetch_project_documents (a single project, live) so the login
        dance only lives in one place."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("  ! ConstructConnect: Playwright not installed — skipping")
            return None, None, None, {"ok": False, "message": "Playwright not installed"}

        try:
            pw = sync_playwright().start()
        except Exception as e:
            print(f"  ! ConstructConnect: could not start Playwright ({e})")
            return None, None, None, {"ok": False, "message": str(e)}

        try:
            browser = pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"  ! ConstructConnect: Chromium not available ({e})")
            pw.stop()
            return None, None, None, {"ok": False, "message": str(e)}

        ctx = browser.new_context(
            accept_downloads=True,
            user_agent=self.USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()

        import browser_crawl  # reuse its best-effort login helper

        # Reuses this same page for login (rather than the default
        # throwaway one _attempt_login would open and close) — the login
        # goes through an SSO redirect (login.io.constructconnect.com ->
        # /api/gcipconsume?returnUrl=...), and a fresh page.goto() to a
        # different URL right after was bouncing back to the login screen
        # instead of landing in the authenticated app.
        login_result = browser_crawl._attempt_login(ctx, login, page=page)
        if not login_result.get("ok"):
            return pw, browser, None, login_result

        # _attempt_login's "password field is gone" heuristic is also
        # satisfied by the SSO's own stuck "Loading..." screen (it has no
        # password field either) — confirm we actually left that host
        # before treating this as a real login.
        if not self._wait_left_login_host(page):
            msg = (f"stuck on the SSO login page ({page.url}) after submitting "
                   "credentials — looks like it's being blocked as automated, "
                   "not a bad password")
            print(f"  ! ConstructConnect: {msg}")
            self._save_debug_screenshot(page, "stuck_on_login")
            return pw, browser, None, {"ok": False, "message": msg}

        print(f"  ConstructConnect: authenticated, landed on {page.url}")
        self._save_debug_screenshot(page, "landed")  # always, while this is still new
        self._dismiss_cookie_banner(page)
        return pw, browser, page, login_result

    def _close_browser(self, pw, browser):
        try:
            if browser:
                browser.close()
        finally:
            if pw:
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

    def _apply_location_filter(self, page):
        """Scopes the results grid to LOCATION_FILTER_STATES instead of
        ConstructConnect's unfiltered nationwide default — walks the same
        Filters -> Project Location -> "Counties or States/Provinces" ->
        check states -> Apply flow a person would use (confirmed via user
        screenshot 2026-10-04), starting from "Clear All" so the result is
        deterministic regardless of whatever the dialog defaults to.

        Best-effort: this is a lot of UI surface for ConstructConnect to
        change out from under us again, so any failure here just leaves
        the run unfiltered (logged + screenshotted) rather than aborting
        the whole scan — a wider net is a much smaller problem than a
        crashed run."""
        self._dismiss_cookie_banner(page)
        try:
            try:
                page.click("button:has-text('Filters')", timeout=5000)
            except Exception:
                pass  # the filter sidebar may already be open
            page.click("text=Project Location", timeout=8000)
            # Stable ids confirmed via devtools 2026-10-04 (not just text —
            # this control is a custom combobox, not a real <select>).
            page.click("#location", timeout=8000)
            page.click("#location-listbox-option-county", timeout=8000)

            dialog = page.get_by_role("dialog")
            # Confirmed via a debug screenshot 2026-10-04: the cookie
            # banner reappeared sitting right on top of this dialog's
            # Clear All/Apply row, blocking the very first click here even
            # though it had already been dismissed once before the dialog
            # opened — it comes back on its own, not just after
            # page.go_back() (see _dismiss_cookie_banner's own note).
            self._dismiss_cookie_banner(page)
            # Best-effort: confirmed via debug screenshot 2026-10-04 that
            # the dialog can open with nothing already selected, in which
            # case "Clear All" is itself disabled (nothing to clear) and
            # waiting on a permanently-disabled button just times out —
            # it's a no-op we wanted for a clean slate, not a required step,
            # so skip it rather than let it sink the whole filter attempt.
            try:
                dialog.get_by_text("Clear All", exact=True).click(timeout=3000)
            except Exception:
                pass
            for state in self.LOCATION_FILTER_STATES:
                self._dismiss_cookie_banner(page)  # it can reappear mid-loop too
                # Each state is a CountrySelect_stateItemWrapper containing
                # two siblings: a checkbox (what actually selects it) and a
                # separate title/chevron row that just expands to show
                # counties — clicking the title does NOT toggle selection.
                # DOM confirmed via devtools 2026-10-04.
                title = dialog.get_by_text(state, exact=True)
                wrapper = title.locator(
                    "xpath=ancestor::*[contains(@class, 'CountrySelect_stateItemWrapper')]")
                wrapper.locator("label").click(timeout=5000)
            self._dismiss_cookie_banner(page)  # belt-and-suspenders, see above
            dialog.get_by_role("button", name="Apply").click(timeout=8000)
            page.wait_for_timeout(1500)

            print(f"  ConstructConnect: scoped results to "
                  f"{', '.join(self.LOCATION_FILTER_STATES)}")
            self._save_debug_screenshot(page, "location_filter_applied")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't apply the location filter, "
                  f"scanning unfiltered instead ({e})")
            self._save_debug_screenshot(page, "location_filter_failed")
            # A failure partway through can leave the dialog (or its
            # backdrop) open on screen, which then blocks every click for
            # the rest of the run — confirmed 2026-10-04: a failed filter
            # attempt was followed by all 100 subsequent project-open
            # clicks timing out, not just the filter itself. Escape closes
            # it per the dialog's own "Press Escape to close" hint; a
            # left/right-arrow-free click on the page body is a fallback
            # in case Escape alone doesn't drop a lingering backdrop.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                page.mouse.click(5, 5)
            except Exception:
                pass

    CATEGORY = "Civil"

    def _expand_filter_section(self, page, key, content_selector):
        """Opens the sidebar's collapsible filter section with
        data-section-key=key (its header is a role="button" div, confirmed
        via devtools 2026-10-27) unless content_selector inside it is
        already showing — clicking an open header would collapse it."""
        section = page.locator(f'[data-section-key="{key}"]').first
        content = section.locator(content_selector).first
        if not (content.count() and content.is_visible()):
            section.locator('[class*="CollapsibleSection_header"][role="button"]').first.click(timeout=8000)
            page.wait_for_timeout(500)
        return section

    def _apply_category_filter(self, page):
        """Checks CATEGORY in the sidebar's Project Category tree — checking
        "Civil" checks all 9 of its children (Bridges / Tunnels, Pipeline,
        Roads / Highways, Water / Sewer, ...), confirmed via user screenshot
        2026-10-27. Not saved on the account, so redone every run like the
        location filter. Same react-checkbox-tree as the old Project Stage
        filter (DOM confirmed via devtools 2026-10-27): section
        data-section-key="category", <div id="projectCategoryFilter">,
        <input id="projectCategoryFilter-Civil" type="checkbox"> wrapped in
        <label for="projectCategoryFilter-Civil">. Left alone if it's already
        checked so a click can't turn it off. Best-effort like the other
        filters."""
        self._dismiss_cookie_banner(page)
        box_id = f"projectCategoryFilter-{self.CATEGORY}"
        try:
            self._expand_filter_section(page, "category", "#projectCategoryFilter")
            if not page.locator(f"#{box_id}").is_checked():
                page.click(f"label[for='{box_id}']", timeout=8000)
            page.wait_for_timeout(1500)
            print(f"  ConstructConnect: scoped results to {self.CATEGORY} projects")
            self._save_debug_screenshot(page, "category_filter_applied")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't apply the category filter, "
                  f"scanning without it instead ({e})")
            self._save_debug_screenshot(page, "category_filter_failed")

    # Options are #bidDate-listbox-option-{7,14,30,60,90,CUSTOM}, labelled
    # "Next N Days" (confirmed via devtools 2026-10-27).
    BID_DATE_DAYS = 60
    BID_DATE_WINDOW = f"Next {BID_DATE_DAYS} Days"

    def _apply_bid_date_filter(self, page):
        """Picks BID_DATE_WINDOW in the sidebar's Bid Date section. With the
        location and category filters this matches the view the user works
        from — 261 projects on 2026-10-27, vs. ~3,600 for location+category
        alone. Replaced the old "Bidding" stage checkbox: past bid dates drop
        Post-Bid on their own, and Conceptual projects with a real bid date
        coming up are wanted.

        DOM confirmed via devtools 2026-10-27 — the same custom combobox as
        the location picker: section data-section-key="bidDate", trigger
        <button id="bidDate" role="combobox"> showing the current value (or
        a "Select Bid Date" placeholder), options in <div id="bidDate-listbox"
        role="listbox">. Skipped if it already shows BID_DATE_WINDOW.
        Best-effort like the other filters — a failure just leaves the run
        wider, logged + screenshotted."""
        self._dismiss_cookie_banner(page)
        try:
            self._expand_filter_section(page, "bidDate", "#bidDate")
            # The whole button's text, not .dropdown-input__value — that
            # span only exists once something is picked; a fresh login
            # shows a "Select Bid Date" placeholder instead (2026-10-27 run).
            current = page.locator("#bidDate").inner_text(timeout=5000).strip()
            if current != self.BID_DATE_WINDOW:
                page.click("#bidDate", timeout=8000)
                page.click(f"#bidDate-listbox-option-{self.BID_DATE_DAYS}", timeout=8000)
            page.wait_for_timeout(1500)
            count = re.search(r"Projects\s*([\d,]+)", page.evaluate("document.body.innerText") or "")
            print(f"  ConstructConnect: scoped results to bid dates in the {self.BID_DATE_WINDOW.lower()}"
                  + (f" ({count.group(1)} projects)" if count else ""))
            self._save_debug_screenshot(page, "bid_date_filter_applied")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't apply the bid date filter, "
                  f"scanning without it instead ({e})")
            self._save_debug_screenshot(page, "bid_date_filter_failed")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

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

    def _scan_results_page(self, page, deadline, known_updated):
        """Work through whatever project list is currently on screen — no
        Saved Search filtering yet (see SAVED_SEARCHES / the note in
        find_documents_with_browser), just the location, category and bid
        date filters applied earlier.

        known_updated is {project_id: 'Oct 4, 2026'} from the previous run
        (see supabase_store.load_project_last_updated) — a project whose
        grid "Last Updated" date hasn't changed since is skipped entirely
        (no open, no download) instead of re-fetched from scratch every
        single run, same reasoning as the existing NO_DOCS_STATUSES skip.
        Added 2026-10-04 after raising MAX_PROJECTS_PER_SEARCH/RUN_BUDGET_S
        to cover the whole ~722-project scoped set — without this, every
        run would re-download everything nationwide-filter-sized runs used
        to only ever get a random slice of.

        Returns (out, current_updated) — current_updated is the fresh
        {project_id: last_updated} map for every row actually seen this
        run (whether downloaded, skipped as unchanged, or skipped as
        no-docs-yet), for the caller to persist for next time."""
        out = []
        current_updated = {}
        project_count = 0
        skipped_no_docs = 0
        skipped_unchanged = 0
        contacts_refreshed = 0
        while project_count < self.MAX_PROJECTS_PER_SEARCH and time.time() < deadline:
            rows = self._project_rows(page)
            if not rows:
                break
            for label, doc_status, project_id, last_updated in rows:
                if project_count >= self.MAX_PROJECTS_PER_SEARCH or time.time() > deadline:
                    break
                if label:
                    self.listed_labels.add(label)
                if project_id and last_updated:
                    current_updated[project_id] = last_updated
                if doc_status in self.NO_DOCS_STATUSES:
                    skipped_no_docs += 1
                    project_count += 1
                    continue
                if (project_id and last_updated
                        and known_updated.get(project_id) == last_updated):
                    skipped_unchanged += 1
                    project_count += 1
                    if contacts_refreshed < self.MAX_CONTACT_REFRESH and self._contacts_stale(label):
                        contacts_refreshed += 1
                        try:
                            self._refresh_contacts(page, label)
                        except Exception as e:
                            print(f"  ! ConstructConnect: couldn't refresh plan holders for '{label}': {e}")
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
              f"{skipped_unchanged} skipped (unchanged since last scan; "
              f"{contacts_refreshed} of those had their plan holders refreshed), "
              f"{len(out)} document(s) found")
        return out, current_updated

    def _save_debug_screenshot(self, page, tag):
        """Save a screenshot + the page's visible text under a distinct
        name for this point in the run, so a person can see what the
        crawler actually saw instead of guessing blind from the text log
        alone. Written under debug/ (see self-host/docker-compose.yml's
        scanner volume mount) so they survive the run and are reachable
        from the host without exec'ing into the container."""
        try:
            os.makedirs("debug", exist_ok=True)
            page.screenshot(path=f"debug/constructconnect_{tag}.png", full_page=True)
            text = page.evaluate("document.body.innerText") or ""
            with open(f"debug/constructconnect_{tag}.txt", "w") as f:
                f.write(f"URL: {page.url}\n\n{text[:20000]}")
            print(f"  ConstructConnect: saved debug/constructconnect_{tag}.png/.txt")
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't save debug screenshot ({tag}): {e}")

    def _project_rows(self, page):
        """Returns (label, doc_status, project_id, last_updated) for every
        row on the current results page:
        - label / doc_status: the Project Name column's link text and the
          Documents column's status text.
        - project_id: parsed out of the project link's own href
          (/project2/<id>/...) — no separate column needed for this one.
        - last_updated: the grid's own "Last Updated" column text (e.g.
          "Oct 4, 2026"), used by _scan_results_page to skip re-downloading
          an unchanged project.

        Each <td> carries a data-label attribute matching its column header
        (confirmed via devtools 2026-10-04, e.g.
        <td data-label="Last Updated">Oct 4, 2026</td>) — tried first since
        it's immune to column reordering, falling back to header-position
        matching (confirmed working previously) in case data-label isn't
        present on every column.

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
            print(f"  ! ConstructConnect: no results grid found on {page.url} "
                  "within 10s — landed page may not be a results list")
            self._save_debug_screenshot(page, "no_grid")
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
        updated_col = headers.index("Last Updated") if "Last Updated" in headers else None

        body_rows = page.query_selector_all("table tbody tr")
        if not body_rows:
            all_role_rows = page.query_selector_all("[role='table'] [role='row']")
            body_rows = all_role_rows[1:] if len(all_role_rows) > 1 else []

        rows = []
        for tr in body_rows:
            cells = tr.query_selector_all("td")
            if not cells:
                cells = tr.query_selector_all("[role='cell'], [role='gridcell']")

            name_cell = tr.query_selector('[data-label="Project Name"]')
            a = name_cell.query_selector("a") if name_cell else None
            if a is None and name_col is not None and name_col < len(cells):
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

            project_id = None
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"/project2?/(\d+)", href)
                if m:
                    project_id = m.group(1)
            except Exception:
                pass

            docs_cell = tr.query_selector('[data-label="Documents"]')
            if docs_cell is None and docs_col is not None and docs_col < len(cells):
                docs_cell = cells[docs_col]
            doc_status = (docs_cell.inner_text() or "").strip() if docs_cell else ""

            updated_cell = tr.query_selector('[data-label="Last Updated"]')
            if updated_cell is None and updated_col is not None and updated_col < len(cells):
                updated_cell = cells[updated_col]
            last_updated = (updated_cell.inner_text() or "").strip() if updated_cell else ""

            rows.append((label, doc_status, project_id, last_updated))
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

    def _contacts_stale(self, label):
        at = self._holders_at.get(f"{self._site_name} — {label}")
        if not at:
            return True
        try:
            seen = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - seen).total_seconds() > self.CONTACTS_STALE_S

    def _refresh_contacts(self, page, label):
        """Opens an unchanged project from the results list just to re-read
        its Contacts (plan holders), then comes back to the list."""
        self._dismiss_cookie_banner(page)
        page.click(f"text={label}", timeout=8000)
        try:
            page.wait_for_selector("text=Documents", timeout=self.PAGE_TIMEOUT_MS)
            self._extract_contacts(page, label, page.url)
        finally:
            page.go_back()
            page.wait_for_timeout(1200)

    def _download_project(self, page, label):
        """Navigates to `label`'s project from the current results list,
        downloads its documents, and returns to the list. For a live
        re-fetch of an already-known project (expired cache, see
        refetch_project_documents), use _download_current_project directly
        instead — there's no list to come back to in that case."""
        self._dismiss_cookie_banner(page)
        try:
            page.click(f"text={label}", timeout=8000)
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't open '{label}': {e}")
            return []
        out = self._download_current_project(page, label)
        page.go_back()
        page.wait_for_timeout(1200)
        return out

    def _download_current_project(self, page, label):
        """Assumes `page` is already on a project's detail page (just
        navigated there from the results list, or via a direct page.goto()
        for a live re-fetch) — clicks through to the actual documents and
        returns them. `label` is only used for log messages and tagging
        the returned tuples; it doesn't have to match on-page text here."""
        try:
            page.wait_for_selector("text=Documents", timeout=self.PAGE_TIMEOUT_MS)
        except Exception:
            print(f"  ! ConstructConnect: '{label}' has no Documents panel — skipping")
            return []

        project_url = page.url
        bid_date, bid_time = self._extract_bid_info(page)
        loc = self._extract_location(page)
        if loc:
            self.locations[label] = loc
        out = []
        doc_page = None
        try:
            doc_page, download = self._click_view_download(page)
            if download:
                out = self._handle_download(download, label, project_url)
            elif doc_page:
                # One multi-page PDF per project (user's pick, 2026-10-27 —
                # reversed the 2026-09-20 switch to "Zipped PDFs"), through
                # the same arrow -> format -> Start picker; "Zipped PDFs" is
                # the fallback.
                download = self._click_download_format(
                    doc_page, self.PDF_FORMAT_LABEL_SELECTOR, "Multi-page PDF")
                if download:
                    out = self._handle_merged_pdf(download, label, project_url)
                else:
                    download = self._click_download_format(
                        doc_page, self.ZIP_FORMAT_LABEL_SELECTOR, "Zipped PDFs")
                    if download:
                        out = self._handle_zip_download(download, label, project_url)
                    else:
                        print(f"  ! ConstructConnect: neither download format "
                              f"produced anything for '{label}'")
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

        if out and (bid_date or bid_time):
            out = [(lbl, u, fn, data, src, bid_date, bid_time)
                   for (lbl, u, fn, data, src, _bd, _bt) in out]

        # After the documents, never before — the documents are what the scan
        # is for; plan holders are a best-effort extra on the same visit.
        if out:
            self._extract_contacts(page, label, project_url)

        return out

    # The project page's "Contacts" tab (confirmed via screenshot + devtools
    # 2026-10-18): sub-tabs All / Design Team / Bidders / Planholders /
    # Interested Parties, each a `button[role=tab]` inside
    # `[role=tablist][aria-label=Contacts]` with an <h2> name and a count
    # badge. Each contact is a card; fields are matched on partial
    # CSS-module class names (the hash suffix changes per deploy) and read
    # from their `title` attribute, which holds the clean value. The data is
    # server-rendered with the page — no separate XHR to listen for. Design
    # Team (owner/architect) is deliberately left out: not sales leads.
    CONTACT_GROUPS = (("Design Team", "Design Team"),
                      ("Bidders", "Bidder"), ("Planholders", "Planholder"),
                      ("Interested Parties", "Interested Party"))
    CONTACT_CARD = '[class*="ProjectContactCard_card"]'
    CONTACT_MAX_PAGES = 10

    def _extract_contacts(self, page, label, project_url):
        try:
            tab = page.locator(
                'button:has-text("Contacts"), a:has-text("Contacts"), [role="tab"]:has-text("Contacts")'
            ).first
            tab.click(timeout=8000)
            page.wait_for_selector('[role="tablist"][aria-label="Contacts"]', timeout=10000)
        except Exception as e:
            print(f"  ! ConstructConnect: no Contacts tab for '{label}' ({e})")
            self._return_to(page, project_url)
            return
        try:
            # Card view (the List/Card toggle may remember List per account).
            page.locator('button:has-text("Card")').first.click(timeout=2000)
        except Exception:
            pass

        holders, seen = [], set()
        try:
            tablist = page.locator('[role="tablist"][aria-label="Contacts"]')
            for tab_name, role in self.CONTACT_GROUPS:
                sub = tablist.locator('button[role="tab"]').filter(
                    has=page.locator("h2", has_text=re.compile(rf"^\s*{re.escape(tab_name)}\s*$"))
                ).first
                if not sub.count():
                    continue
                badge = (sub.locator(".badge").first.inner_text(timeout=2000) or "0").strip()
                expected = int(re.sub(r"\D", "", badge) or 0)
                if not expected:
                    continue
                sub.click(timeout=5000)
                page.wait_for_timeout(800)
                got = 0
                for _ in range(self.CONTACT_MAX_PAGES):
                    try:
                        page.wait_for_selector(self.CONTACT_CARD, timeout=8000)
                    except Exception:
                        break
                    for card in page.query_selector_all(self.CONTACT_CARD):
                        h = self._read_contact_card(card, role)
                        if not h:
                            continue
                        got += 1
                        k = (h["company"], h["contact"], role)
                        if k not in seen:
                            seen.add(k)
                            holders.append(h)
                    if got >= expected or not self._contacts_next_page(page):
                        break
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't read contacts for '{label}' ({e})")
            if not getattr(self, "_saved_contacts_debug", False):
                self._saved_contacts_debug = True
                self._save_debug_screenshot(page, "contacts_failed")
        self.plan_holders[label] = holders
        self._return_to(page, project_url)

    @staticmethod
    def _read_contact_card(card, role):
        def field(part):
            el = card.query_selector(f'[class*="ProjectContactCard_{part}"]')
            if not el:
                return None
            v = (el.get_attribute("title") or el.inner_text() or "").strip()
            return v if v and v != "-" else None

        company, name = field("company"), field("name")
        if not company and not name:
            return None
        phone = None
        for info in card.query_selector_all('[class*="ProjectContactCard_info_"]'):
            lab = info.query_selector('[class*="ProjectContactCard_infoLabel"]')
            val = info.query_selector('[class*="ProjectContactCard_infoValue"]')
            if lab and val and "phone" in (lab.inner_text() or "").lower():
                phone = (val.get_attribute("title") or val.inner_text() or "").strip()
        city, state = None, None
        loc = field("location")
        if loc:
            parts = [x.strip() for x in loc.rsplit(",", 1)]
            city, state = (parts[0], parts[1]) if len(parts) == 2 else (loc, None)
        return plan_holder(
            company or name, role=role, contact=name if company else None,
            title=field("role"), emails=clean_emails(field("email")), phone=phone,
            city=city, state=state,
        )

    @staticmethod
    def _contacts_next_page(page):
        """Contacts paginate 20 per page ("1-20 of 34" + arrows in
        #TableFooter). Clicks the next arrow; False when there isn't one or
        it's disabled. Best-effort — the footer's markup isn't confirmed."""
        try:
            btns = page.locator("#TableFooter button")
            n = btns.count()
            if not n:
                return False
            nxt = btns.nth(n - 1)
            if nxt.is_disabled() or nxt.get_attribute("aria-disabled") == "true":
                return False
            nxt.click(timeout=3000)
            page.wait_for_timeout(800)
            return True
        except Exception:
            return False

    def _return_to(self, page, project_url):
        """Clicking the Contacts tab may push its own history entry —
        _download_project's page.go_back() has to land on the results list,
        not back on the project's Overview tab."""
        try:
            for _ in range(4):
                if page.url == project_url:
                    break
                page.go_back()
                page.wait_for_timeout(800)
        except Exception:
            pass

    # Each event on the project page ("Bid Date", "Start Date", "End Date",
    # ...) is one of these blocks — matched on a partial class name since the
    # CSS-module hash suffix can change on ConstructConnect's next deploy.
    # Replaced ProjectInformation_* with EventItem_* as of ConstructConnect's
    # 2026-10 redesign (confirmed via devtools 2026-10-04) — also split what
    # used to be one combined "Sep 17, 2026 @ 10:00am MT" string into a
    # separate day/month block (EventItem_date — no year at all) and a
    # title/time block (EventItem_details).
    BID_EVENT_ROW_SELECTOR = '[class*="EventItem_item"]'

    def _extract_location(self, page):
        """The project's location from the line under its title, next to the
        map pin — e.g. "Multiple Locations, Apache Junction, AZ 85120"
        (confirmed via user screenshot 2026-10-21) — or None. It's the first
        short element on the page ending in ", ST 12345"; the Events panel's
        addresses (meetings, bid opening) come later in the page."""
        try:
            text = page.evaluate("""() => {
                const re = /,\\s*[A-Z]{2}\\s+\\d{5}(-\\d{4})?\\s*$/;
                for (const el of document.querySelectorAll('a, span, div, p')) {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 160 && !t.includes('\\n') && re.test(t)) return t;
                }
                return null;
            }""")
        except Exception:
            text = None
        if not text and not getattr(self, "_saved_location_debug", False):
            self._saved_location_debug = True
            self._save_debug_screenshot(page, "location_not_found")
        return text

    def _extract_bid_info(self, page):
        """Reads the project page's "Events" section for the "Bid Date" row
        and returns (bid_date_iso_or_None, bid_time_or_None), e.g.
        ('2026-10-01', '2:00pm PT').

        Confirmed via debug screenshot 2026-10-04: the project page's
        "Events" section loads independently of (and slower than) the
        Documents panel this is called right after — a project caught
        mid-load still showed "Please wait while we load your project
        information" with zero event rows yet. Waiting here for at least
        one event row before reading avoids treating "hasn't loaded yet"
        as "this project has no Bid Date"."""
        try:
            page.wait_for_selector(self.BID_EVENT_ROW_SELECTOR, timeout=8000)
        except Exception:
            pass  # genuinely no events on this project is also possible
        try:
            for row in page.query_selector_all(self.BID_EVENT_ROW_SELECTOR):
                title_el = row.query_selector('[class*="EventItem_title"]')
                if not title_el or "Bid Date" not in (title_el.inner_text() or ""):
                    continue
                day_el = row.query_selector('[class*="EventItem_day"]')
                month_el = row.query_selector('[class*="EventItem_month"]')
                time_el = row.query_selector('[class*="EventItem_time"]')
                bid_date = self._infer_event_date(
                    day_el.inner_text() if day_el else None,
                    month_el.inner_text() if month_el else None,
                )
                time_text = (time_el.inner_text() if time_el else "") or ""
                return bid_date, (time_text.strip() or None)
        except Exception as e:
            print(f"  ! ConstructConnect: couldn't read the bid date ({e})")
            return None, None
        if not getattr(self, "_saved_bid_date_debug", False):
            self._saved_bid_date_debug = True
            self._save_debug_screenshot(page, "bid_date_not_found")
        return None, None

    @staticmethod
    def _infer_event_date(day_text, month_abbr):
        """EventItem_date gives only a day + 3-letter month ("1"/"Oct") —
        no year anywhere on the page. Infers the year that puts the date
        closest to today — a project stays listed after its bid opens, so
        "Oct 1" seen on Oct 26 is this October (already past), not next
        year's; "Jan 5" seen in November is next January. Returns an ISO
        date string, or None if unparseable."""
        if not day_text or not month_abbr:
            return None
        try:
            month_num = datetime.strptime(month_abbr.strip()[:3], "%b").month
            day_num = int(re.sub(r"\D", "", day_text))
        except (ValueError, TypeError):
            return None
        today = datetime.now()
        candidates = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(datetime(year, month_num, day_num))
            except ValueError:
                pass  # Feb 29 outside a leap year
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c - today)).date().isoformat()

    # ConstructConnect dropped the old single "View/Download Documents"
    # button in favor of a per-category Documents panel on the project page
    # (Plans / Specifications / Addenda / Other, each row reading e.g.
    # "Plans   Available ›" when that category has anything) — confirmed via
    # screenshot 2026-10-04. Clicking any one "Available" category opens the
    # same docviewer tab listing every category together, so trying these in
    # order and taking whichever one actually opens something is enough —
    # no need to parse which categories are non-empty first.
    DOCUMENT_CATEGORIES = ("Plans", "Specifications", "Addenda", "Other")

    def _click_view_download(self, page):
        """Clicks through the project page's Documents panel (see
        DOCUMENT_CATEGORIES) and catches whichever of the two things it
        actually does: open the download picker in a new tab, or trigger a
        file download directly on this same page. Returns
        (new_page_or_None, download_or_None)."""
        ctx = page.context
        new_pages = []
        downloads = []
        ctx.on("page", lambda p: new_pages.append(p))
        page.on("download", lambda d: downloads.append(d))

        for cat in self.DOCUMENT_CATEGORIES:
            try:
                page.click(f"text={cat}", timeout=3000)
            except Exception:
                continue
            deadline = time.time() + 6
            while time.time() < deadline and not new_pages and not downloads:
                page.wait_for_timeout(200)
            if new_pages or downloads:
                break

        if downloads:
            print("  ConstructConnect: Documents panel triggered a direct download")
            return None, downloads[0]
        if new_pages:
            doc_page = new_pages[0]
            try:
                doc_page.wait_for_load_state(timeout=self.PAGE_TIMEOUT_MS)
            except Exception:
                pass
            print(f"  ConstructConnect: Documents panel opened a new tab ({doc_page.url})")
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
            return [(label, None, f"{label} - {name}", data, project_url, None, None)]
        print(f"  ! ConstructConnect: downloaded '{name}' — unrecognized type, skipping")
        return []

    def _handle_merged_pdf(self, download, label, project_url):
        """The "Multi-page PDF" format merges every document into
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
        if data[:2] == b"PK":  # came back zipped after all
            return self._extract_zip(data, label, project_url)
        return [(label, None, f"{label} - All Documents.pdf", data, project_url, None, None)]

    # The docviewer's "Download All" button has this stable id (confirmed
    # by the user via devtools) — only used to tell when the document list
    # has finished loading (see _wait_downloads_ready).
    DOWNLOAD_ALL_SELECTOR = "#download_button"

    # The split button's dropdown arrow next to "Download All" — its own id
    # is a React-generated auto-id (e.g. "_r_4_-trigger") that isn't stable
    # across page loads, but aria-haspopup="listbox" is (confirmed via
    # devtools 2026-09-20, alongside the two ids below).
    DOWNLOAD_DROPDOWN_TRIGGER_SELECTOR = 'button[aria-haspopup="listbox"]'
    ZIP_FORMAT_LABEL_SELECTOR = "#zip_option_radio_label"
    PDF_FORMAT_LABEL_SELECTOR = "#pdf_option_radio_label"   # confirmed via devtools 2026-10-27
    DOWNLOAD_START_SELECTOR = "#download_start"

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

    def _click_download_format(self, page, format_selector, format_name):
        """Open the split button's flyout, pick a File Format radio
        ("Multi-page PDF" = #pdf_option_radio_label, the menu's default;
        "Zipped PDFs" = #zip_option_radio_label) and click Start —
        ConstructConnect builds the file, then the download fires. Returns
        that Download, or None if any step doesn't behave.

        Devtools (2026-09-20) showed why this was flaky before: the flyout
        (`aria-controls`'d content div) is real, enabled, and
        `visibility: visible`, but rendered at `position: fixed;
        top: ~99px; left: 10px` — off from the button and behind other page
        chrome, so a human never sees it open. Playwright's normal click()
        fails its actionability check because that spot is occluded, even
        though the element itself is genuinely clickable — so every click
        here uses force=True (skips the occlusion/visibility check) and
        waits for the target to be `attached` rather than `visible`.
        The format radio is clicked even when it's already selected rather
        than assumed."""
        if not self._wait_downloads_ready(page):
            return None
        trigger = page.query_selector(self.DOWNLOAD_DROPDOWN_TRIGGER_SELECTOR)
        if not trigger:
            print("  ! ConstructConnect: no dropdown arrow next to 'Download All'")
            return None
        try:
            trigger.click(timeout=4000, force=True)
            page.wait_for_selector(format_selector, state="attached", timeout=5000)
            page.click(format_selector, timeout=4000, force=True)
            page.wait_for_selector(self.DOWNLOAD_START_SELECTOR, state="attached", timeout=5000)
            with page.expect_download(timeout=self.DOWNLOAD_TIMEOUT_MS) as dl_info:
                page.click(self.DOWNLOAD_START_SELECTOR, timeout=8000, force=True)
            return dl_info.value
        except Exception as e:
            print(f"  ! ConstructConnect: '{format_name}' download didn't work ({e})")
            if not getattr(self, "_saved_zip_picker_debug", False):
                self._saved_zip_picker_debug = True
                self._save_debug_screenshot(page, "download_picker_failed")
            try:
                page.keyboard.press("Escape")  # so a retry's arrow click opens the menu, not closes it
            except Exception:
                pass
            return None

    def _handle_zip_download(self, download, label, project_url):
        """The "Zipped PDFs" download is a zip with every document in the
        project kept separate — extract it the same way _handle_download
        does for a zip that fires directly off "View/Download Documents"."""
        path = download.path()
        if not path:
            return []
        if os.path.getsize(path) > self.MAX_ZIP_BYTES:
            print("  ! ConstructConnect: download too large, skipping")
            return []
        with open(path, "rb") as f:
            data = f.read()
        return self._extract_zip(data, label, project_url)

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
                    # Keeps the zip's full relative path (not just the
                    # basename) — ConstructConnect's "Zipped PDFs" nests
                    # deeply (plans/, specifications/<CSI division>/..., etc,
                    # confirmed via a real download 2026-09-20) and the path
                    # is the only thing that says which section a file like
                    # "262726 Wiring Devices.pdf" came from; it also rules
                    # out two different folders coincidentally reusing the
                    # same filename within one project's zip.
                    fn = f"{label} - {name}"
                    out.append((label, None, fn, data, project_url, None, None))
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

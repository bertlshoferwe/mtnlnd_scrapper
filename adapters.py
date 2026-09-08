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

import re
from urllib.parse import urljoin, urlparse

import requests

REQUEST_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BidScoutBot/1.0)"}
DOC_EXTENSIONS = (".pdf", ".docx", ".doc")


class SiteAdapter:
    key = ""        # stable id stored in sites.adapter
    label = ""      # shown in the dashboard dropdown
    help = ""       # one-liner under the dropdown
    hosts = ()      # hostnames this adapter recognizes from a plain site URL

    @classmethod
    def matches_url(cls, url):
        host = urlparse(url or "").netloc.lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    def find_documents(self, site):
        """Return [(label, document_url, filename, project_page_url), ...] for
        every document the portal is currently advertising. `label` is the
        job/project name; `project_page_url` is a link to that job's page
        (or None). `site` is the sites row (dict)."""
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
    help = "Pulls every advertised project's plan set / NTC / items PDFs straight from contractorzone.udot.utah.gov. The site URL field is ignored."
    hosts = ("contractorzone.udot.utah.gov",)

    BASE = "https://contractorzone.udot.utah.gov"
    SECTION = "advertisements"

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
            project_url = f"{self.BASE}/project/{uuid}/project-files"
            try:
                files = self._get_json(f"/project-files/{uuid}/project-files")
            except requests.RequestException as e:
                print(f"  ! UDOT: couldn't list files for {label}: {e}")
                continue
            for f in files.get("result") or []:
                fn = (f.get("file_name") or "").strip()
                if not fn.lower().endswith(DOC_EXTENSIONS):
                    continue
                url = (f"{self.BASE}/project-files/download/{uuid}/project-files"
                       f"?file_name={requests.utils.quote(fn)}")
                out.append((label, url, fn, project_url))
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
                    out.append((label, href, fn, self.PAGE))

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
                ))

        print(f"  WYDOT: {len(out)} document(s) across the advertised projects")
        return out


_ADAPTER_CLASSES = [UDOTMasterworksAdapter, ITDAdvertisedAdapter, WYDOTExevisionAdapter]
ADAPTERS = {cls.key: cls for cls in _ADAPTER_CLASSES}


def get_adapter(key):
    cls = ADAPTERS.get(key)
    return cls() if cls else None


def adapter_for_site(site):
    """The adapter to use for a site: the one it explicitly selected, or —
    failing that — one that recognizes the site's URL host (so a plain-URL
    site pointed at a known portal still gets the fast path)."""
    key = (site.get("adapter") or "").strip()
    if key:
        return get_adapter(key)
    url = site.get("url") or ""
    for cls in _ADAPTER_CLASSES:
        if cls.matches_url(url):
            return cls()
    return None


def list_adapters():
    return [{"key": c.key, "label": c.label, "help": c.help} for c in _ADAPTER_CLASSES]

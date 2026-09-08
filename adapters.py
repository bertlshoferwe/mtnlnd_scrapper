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

from urllib.parse import urlparse

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


_ADAPTER_CLASSES = [UDOTMasterworksAdapter]
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

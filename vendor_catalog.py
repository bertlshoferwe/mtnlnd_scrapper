"""
What each vendor in Vendors sells, read from its website once a week (and a
logo while it's there). Runs in the scan worker: loop() checks hourly for
vendors not pulled in a week; scanner_service.py's /vendor-pull runs one now
("Try again", "Pull again", a new vendor, an uploaded PDF catalog).

How a site is read, first that finds products:
  feed        a store's own product list (Shopify /products.json, WooCommerce's Store API)
  schema_org  JSON-LD Product / ItemList markup on the home page and its product-looking pages
  llm         the AI reads those pages' text and lists what they sell
  pdf         an uploaded PDF catalog, read by the AI
Each division's keywords (Bid Scout's) only decide which of those products
belong in that division (the union across the divisions the vendor serves
steers what the AI pulls); nothing records which keyword matched.

Changes go to the review queue (vendor_catalog_changes) for an admin, except
a vendor's first good pull, which goes straight in. Items added by hand are
never removed by a pull. robots.txt is respected, requests are spaced out,
the user agent says who it is, and a site behind a sign-in is left alone.
"""

import json
import re
import threading
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

import ai_provider
import supabase_store
import vendors_store as vs

UA = "BidScoutVendors/1.0 (weekly vendor catalog check; one request every couple of seconds)"
GAP_S = 2.0
TIMEOUT_S = 20
MAX_PAGES = 20
MAX_PRODUCTS = 600
WEEK_S = 7 * 86400
DAY_S = 86400   # Pull now skips a vendor pulled this recently
CHECK_EVERY_S = 3600
FIRST_RUN_AFTER_S = 300
_PRODUCTISH = re.compile(r"/(products?|catalog|shop|store|category|categories|collections?|items?|equipment|supplies)(/|$|\?)", re.I)
_LOGINISH = re.compile(r"log-?in|sign-?in|account/login|auth", re.I)

_busy = set()
_busy_lock = threading.Lock()


class Failed(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetching politely
# ---------------------------------------------------------------------------

class _Site:
    def __init__(self, website):
        w = (website or "").strip()
        if not w:
            raise Failed("No web address")
        self.base = w if re.match(r"^https?://", w, re.I) else "https://" + w
        self.host = urlparse(self.base).netloc.lower()
        self._last = 0.0
        self.robots = RobotFileParser()
        try:
            r = self._get(urljoin(self.base, "/robots.txt"), check=False)
            self.robots.parse(r.text.splitlines() if r is not None and r.status_code == 200 else [])
        except Exception:
            self.robots.parse([])

    def _get(self, url, check=True, **kw):
        if check and not self.robots.can_fetch(UA, url):
            return None
        wait = GAP_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        return requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US"}, timeout=TIMEOUT_S, **kw)

    def get(self, url, check=True):
        try:
            r = self._get(url, check)
        except requests.RequestException:
            return None
        if r is None:
            return None
        if r.status_code in (401, 403, 429, 503) and url.rstrip("/") == self.base.rstrip("/"):
            raise Failed("Site blocks automated requests")
        if r.status_code != 200:
            return None
        if _LOGINISH.search(urlparse(r.url).path) and not _LOGINISH.search(urlparse(url).path):
            raise Failed("Site needs a sign-in")
        return r

    def same_site(self, url):
        h = urlparse(url).netloc.lower()
        return h == self.host or h.removeprefix("www.") == self.host.removeprefix("www.")


# ---------------------------------------------------------------------------
# Reading products
# ---------------------------------------------------------------------------

def _clean(name):
    n = re.sub(r"\s+", " ", BeautifulSoup(str(name or ""), "html.parser").get_text(" ")).strip(" -–|•")
    return n[:200] if 2 <= len(n) <= 200 else None


def _dedupe(names):
    seen, out = set(), []
    for n in names:
        n = _clean(n)
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out[:MAX_PRODUCTS]


def _from_feed(site):
    names = []
    for page in range(1, 5):   # Shopify
        r = site.get(urljoin(site.base, f"/products.json?limit=250&page={page}"))
        try:
            got = [p.get("title") for p in (r.json().get("products") or [])] if r is not None else []
        except ValueError:
            got = []
        names += got
        if len(got) < 250:
            break
    if not names:
        for page in range(1, 6):   # WooCommerce
            r = site.get(urljoin(site.base, f"/wp-json/wc/store/v1/products?per_page=100&page={page}"))
            try:
                got = [p.get("name") for p in r.json()] if r is not None else []
            except (ValueError, AttributeError):
                got = []
            names += got
            if len(got) < 100:
                break
    return _dedupe(names)


def _jsonld(soup):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
            elif isinstance(d, dict):
                out.append(d)
                for k in ("@graph", "itemListElement", "item", "hasOfferCatalog", "isRelatedTo"):
                    if isinstance(d.get(k), (list, dict)):
                        stack.append(d[k])
    return out


def _products_in(objs):
    names = []
    for d in objs:
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x in ("Product", "ProductGroup", "IndividualProduct", "Offer") for x in types) and d.get("name"):
            names.append(d["name"])
        elif "ListItem" in types and d.get("name") and not isinstance(d.get("item"), dict):
            names.append(d["name"])
    return names


def _pages(site):
    """(url, soup) for the home page and the product-looking pages it and its sitemap point to."""
    home = site.get(site.base, check=True)
    if home is None:
        raise Failed("The site didn't answer")
    soup = BeautifulSoup(home.text, "html.parser")
    out, seen = [(home.url, soup)], {home.url.rstrip("/")}
    links = [urljoin(home.url, a["href"]) for a in soup.find_all("a", href=True)]
    sm = site.get(urljoin(site.base, "/sitemap.xml"))
    if sm is not None:
        links += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text)[:400]
    for u in links:
        u = u.split("#")[0]
        if len(out) >= MAX_PAGES or not site.same_site(u) or u.rstrip("/") in seen or not _PRODUCTISH.search(urlparse(u).path):
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|xml)$", u, re.I):
            continue
        seen.add(u.rstrip("/"))
        r = site.get(u)
        if r is not None and "html" in (r.headers.get("content-type") or ""):
            out.append((r.url, BeautifulSoup(r.text, "html.parser")))
    return out


def _page_text(soup, limit=6000):
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "svg", "header", "footer"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()[:limit]


EXTRACT_PROMPT = """You are building a list of the products a construction supply vendor sells, from {what}.
List the products or product types, as short plain names a contractor would search for (e.g. "Geotextile fabric 6 oz",
"18-inch CMP culvert", "Silt fence"). Leave out services, company news, brands on their own, and navigation text.
{focus}
Answer with one JSON object, no other text: {{"items": ["...", "..."]}}

{text}
"""


def _ai_items(text, keywords, what):
    ai = ai_provider.get_provider()
    if ai is None:
        raise Failed("Reading this needs the AI provider, and none is set up")
    import site_agent
    focus = ("Concentrate on products related to these: " + ", ".join(keywords[:150]) + ".") if keywords else ""
    reply = site_agent._ask(ai, EXTRACT_PROMPT.format(what=what, focus=focus, text=text), max_tokens=6000)
    if not reply:
        raise Failed("The AI didn't answer (it may be over its daily limit)")
    return _dedupe(reply.get("items") or [])


def _from_site(website, keywords):
    """(names, method) or raises Failed."""
    site = _Site(website)
    names = _from_feed(site)
    if names:
        return names, "feed"
    pages = _pages(site)
    names = _dedupe(n for _u, soup in pages for n in _products_in(_jsonld(soup)))
    if len(names) >= 3:
        return names, "schema_org"
    text = "\n\n".join(f"PAGE {u}\n{_page_text(soup)}" for u, soup in pages[:8])[:40000]
    if len(text) < 200:
        raise Failed("No product list found on the site")
    return _ai_items(text, keywords, "the text of their website"), "llm"


def _from_pdf(path, keywords):
    data = vs.get_file(path)
    if not data:
        raise Failed("The uploaded PDF couldn't be read back")
    import scraper
    pages, _real = scraper.extract_pages(data, "catalog.pdf")
    text = "\n".join(t for _n, t in pages)[:60000]
    if len(text) < 100:
        raise Failed("No text in the PDF (a scanned catalog can't be read yet)")
    return _ai_items(text, keywords, "their PDF catalog")


# ---------------------------------------------------------------------------
# Which division each product belongs in
# ---------------------------------------------------------------------------

def _stem(w):
    """Plurals to singular, roughly: "fences" -> "fence", "assemblies" -> "assembly"."""
    w = w.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _matches(name, keyword):
    words = {_stem(w) for w in re.findall(r"[a-z0-9#]+", name.lower())}
    kw = [_stem(w) for w in re.findall(r"[a-z0-9#]+", keyword.lower()) if len(w) > 1]
    return bool(kw) and all(k in words for k in kw)


ASSIGN_PROMPT = """Products a construction supply vendor sells, numbered:
{products}

Divisions of a contractor and the kinds of items each one bids (its keywords):
{divisions}

For each division, which products are related to its keywords? A product can fit several divisions or none.
Answer with one JSON object, no other text: {{"divisions": {{"<division id>": [product numbers]}}}}
"""


def _assign(names, keywords_by_div):
    """{division_id: [names]} — a division with no keywords takes everything."""
    out = {d: list(names) for d, kws in keywords_by_div.items() if not kws}
    rest = {d: kws for d, kws in keywords_by_div.items() if kws}
    if not rest:
        return out
    ai = ai_provider.get_provider()
    if ai is not None and len(names) <= 400:
        import site_agent
        reply = site_agent._ask(ai, ASSIGN_PROMPT.format(
            products="\n".join(f"{i}. {n}" for i, n in enumerate(names)),
            divisions="\n".join(f"{d}: {', '.join(k[:120])}" for d, k in rest.items())), max_tokens=6000)
        got = (reply or {}).get("divisions")
        if isinstance(got, dict):
            for d in rest:
                idx = [i for i in got.get(d) or [] if isinstance(i, int) and 0 <= i < len(names)]
                out[d] = [names[i] for i in sorted(set(idx))]
            return out
    for d, kws in rest.items():   # no AI: a product fits when it has every word of one of the keywords
        out[d] = [n for n in names if any(_matches(n, k) for k in kws)]
    return out


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

_IMG = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
        "image/svg+xml": "svg", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico"}


def pull_logo(v, force=False):
    """The site's logo: JSON-LD logo, og:image, apple-touch-icon, then the favicon.
    Never replaces one uploaded by hand. Returns True when it saved one."""
    if v.get("logo_source") == "manual" or not v.get("website"):
        return False
    if v.get("logo_path") and v.get("logo_source") == "card" and not force:
        return False
    site = _Site(v["website"])
    home = site.get(site.base)
    if home is None:
        return False
    soup = BeautifulSoup(home.text, "html.parser")
    cands = []
    for d in _jsonld(soup):
        lg = d.get("logo")
        lg = lg.get("url") if isinstance(lg, dict) else lg
        if isinstance(lg, str):
            cands.append(lg)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        cands.append(og["content"])
    links = [([r.lower() for r in l.get("rel") or []], l["href"]) for l in soup.find_all("link", href=True)]
    for rel in ("apple-touch-icon", "apple-touch-icon-precomposed", "icon"):
        cands += [href for rels, href in links if rel in rels]
    cands.append("/favicon.ico")
    for c in cands:
        u = urljoin(home.url, c)
        r = site.get(u)
        mime = ((r.headers.get("content-type") or "").split(";")[0].strip().lower()) if r is not None else ""
        if r is None or mime not in _IMG or not 100 < len(r.content) < 2_000_000:
            continue
        path = f"logos/{v['id']}-site.{_IMG[mime]}"
        vs.put_file(path, r.content, mime)
        if v.get("logo_path") and v["logo_path"] != path:
            vs.remove_file(v["logo_path"])
        vs.update_vendor(v["id"], {"logo_path": path, "logo_source": "site", "logo_pulled_at": vs._now()}, "weekly pull")
        print(f"  [vendors] {v['name']}: logo from {u}", flush=True)
        return True
    return False


# ---------------------------------------------------------------------------
# One pull
# ---------------------------------------------------------------------------

def pull(vendor_id, what="all", pdf_path=None, force=False):
    """what: "all" (items and logo), "logo", or "pdf" (pdf_path). Returns {"ok", "message"}."""
    with _busy_lock:
        if vendor_id in _busy:
            return {"ok": False, "message": "it's already being pulled"}
        _busy.add(vendor_id)
    try:
        return _pull(vendor_id, what, pdf_path, force)
    finally:
        with _busy_lock:
            _busy.discard(vendor_id)


def _pull(vendor_id, what, pdf_path, force):
    v = vs.get_vendor(vendor_id)
    if v is None:
        return {"ok": False, "message": "that vendor is gone"}
    if what in ("all", "logo"):
        try:
            got = pull_logo(v, force)
        except Failed as e:
            got = False
            if what == "logo":
                return {"ok": False, "message": str(e)}
        except Exception as e:
            print(f"  ! [vendors] {v['name']}: logo: {e}", flush=True)
            got = False
        if what == "logo":
            return {"ok": got, "message": None if got else "No logo found on their site"}
    live = {d["id"] for d in supabase_store.load_divisions()}
    divisions = [d for d in vs.divisions_of(vendor_id) if d in live] or sorted(live)
    keywords = {d: supabase_store.load_keywords(d) for d in divisions}
    union = sorted({k for ks in keywords.values() for k in ks})
    run = vs.start_run(vendor_id)
    method = "pdf" if what == "pdf" else None
    try:
        if what == "pdf":
            names = _from_pdf(pdf_path, union)
        else:
            names, method = _from_site(v.get("website"), union)
        if not names:
            raise Failed("No product list found on the site")
        first = not vs.had_good_run(vendor_id, run["id"])
        queued = 0
        for div, keep in _assign(names, keywords).items():
            have = {i["name"].lower(): i for i in vs.items_of(vendor_id, div) if i["active"]}
            added = [n for n in keep if n.lower() not in have]
            lower = {n.lower() for n in keep}
            removed = [] if what == "pdf" else [i["name"] for k, i in have.items()
                                                if i["source"] != "manual" and k not in lower]
            if first:
                for n in added:
                    vs.add_item(vendor_id, div, n, source="pdf" if what == "pdf" else "scrape", log=False)
            else:
                queued += vs.queue_changes(vendor_id, div, run["id"], added, removed)
        vs.finish_run(run["id"], True, method)
        msg = (f"{len(names)} products found" + (", added straight in (first pull)" if first
                                                  else f", {queued} change(s) to review"))
        print(f"  [vendors] {v['name']}: {msg} ({method})", flush=True)
        return {"ok": True, "message": msg}
    except Failed as e:
        vs.finish_run(run["id"], False, method, str(e))
        print(f"  [vendors] {v['name']}: pull failed — {e}", flush=True)
        return {"ok": False, "message": str(e)}
    except Exception as e:
        vs.finish_run(run["id"], False, method, f"Something went wrong: {e}")
        print(f"  ! [vendors] {v['name']}: {e}", flush=True)
        return {"ok": False, "message": str(e)}


_all_lock = threading.Lock()


def pull_all():
    """Pull Now: every vendor with a website, one at a time, whenever they were
    last pulled. False if one is already going."""
    if not _all_lock.acquire(blocking=False):
        return False
    try:
        run_due(force=True)
    finally:
        _all_lock.release()
    return True


def run_due(force=False):
    """Pull every vendor with a website not pulled in a week, one at a time.
    With force (Pull now): every one not pulled in the last day — a pull that
    failed in that time is tried again."""
    from datetime import datetime
    runs = vs.last_runs()
    now = time.time()
    for v in vs.all_vendors():
        last = runs.get(v["id"])
        if not v.get("website"):
            continue
        if last:
            age = now - datetime.fromisoformat(last["started_at"].replace("Z", "+00:00")).timestamp()
            if force and last["status"] != "failed" and age < DAY_S:
                print(f"  [vendors] {v['name']}: pulled in the last day — skipped", flush=True)
                continue
            if not force and age < WEEK_S:
                continue
        pull(v["id"])


def loop():
    try:
        n = vs.fail_running("The pull stopped partway: the scan worker restarted")
        if n:
            print(f"  [vendors] {n} pull(s) cut off by the restart marked failed", flush=True)
    except Exception as e:
        print(f"  ! [vendors] couldn't tidy up interrupted pulls: {e}", flush=True)
    time.sleep(FIRST_RUN_AFTER_S)
    while True:
        try:
            run_due()
        except Exception as e:
            print(f"  ! [vendors] weekly pull: {e}", flush=True)
        time.sleep(CHECK_EVERY_S)

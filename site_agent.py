"""
Learned sites — read any bid site without a hand-written adapter.

A site whose adapter is "learned" goes through two phases:

  1. Learning (`python site_agent.py learn <division_id> <site_id>`, started
     by scanner_service.py's /learn-site). An AI explores the site one step
     at a time: it opens pages, clicks tabs, watches the JSON the page loads
     behind the scenes, and tries recipes until one reads the bid list and
     every project's details. The finished recipe and a preview of what it
     found are saved as a draft for the user to review in the dashboard.
     Their corrections (a wrong value, a bid that shouldn't be there, notes
     about the site) are saved with the site and become checks that every
     later re-learn has to pass.

  2. Daily scans (scan_learned_site, called from scraper.scan_site). The
     approved recipe is replayed with plain requests (or a browser only
     where the recipe says the page needs one) — no AI. If it stops
     working (nothing listed, or a check fails), the AI re-learns the site
     on its own; a site it can't fix is flagged instead of guessed at.

A recipe is plain JSON (see RECIPE_FORMAT below, which is also what the AI
is shown). State lives in sites.learned (jsonb):

  status        "learning" | "review" (a draft is waiting) | "live" |
                "failed" | "attention" (a daily scan couldn't fix it)
  message       the latest problem or note, shown on the site
  progress      the last few lines of what the learner is doing
  progress_at   when progress last moved (a stale "learning" = it died)
  hints         the user's notes about the site
  checks        corrections the recipe must honor — {kind: "value",
                project, project_name, field, expected, note} or {kind:
                "exclude", project, project_name, note}
  rules         confirmed skip rules — {text, skip: {field, matches}}
  recipe        the approved recipe the daily scan replays
  summary       one line, e.g. "Bid list → project page → Documents tab"
  draft         {recipe, summary} waiting for approval
  preview       what the draft found on a sample of projects
  learned_at / approved_at / last_run

Project details beyond what the results page shows (the division's
learn_fields list) are saved to project_flags.extra_fields, unshown for now.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import adapters
import ai_provider
import browser_crawl
import credentials
import supabase_store

# The fields every learned site collects — the ones the results page shows.
# Plan holders and documents have their own recipe sections.
BUILT_IN_FIELDS = ["Project name", "Bid date", "Bid time", "Location"]

PREVIEW_PROJECTS = 8
REQUEST_TIMEOUT = 30
PAGE_TIMEOUT_MS = 25000
LEARN_MAX_STEPS = int(os.environ.get("LEARN_MAX_STEPS") or "40")
# Gap between AI calls while learning — keeps the Gemini free tier's
# requests-per-minute limit from tripping.
AI_MIN_INTERVAL_S = float(os.environ.get("LEARN_AI_INTERVAL_S") or "5")
MAX_LIST_PAGES = int(os.environ.get("LEARN_MAX_LIST_PAGES") or "20")
MAX_PROJECTS = int(os.environ.get("LEARN_MAX_PROJECTS") or "300")
DOC_EXT = (".pdf", ".docx", ".doc")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class LearnFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetching — plain requests, with a browser started only when needed
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self, login=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.login = login
        self.login_result = None
        self._pw = self._browser = self._ctx = None
        self._cache = {}
        self.page = None  # the learner's current page, for click()
        self.page_json = []
        if login:
            self._start_browser()

    def _start_browser(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(accept_downloads=True, user_agent=UA)
        if self.login:
            self.login_result = browser_crawl._attempt_login(self._ctx, self.login)
            for c in self._ctx.cookies():
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/")

    def cookies(self):
        return {c.name: c.value for c in self.session.cookies}

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._pw = self._browser = self._ctx = self.page = None

    def json(self, url):
        key = ("json", url)
        if key not in self._cache:
            r = self.session.get(url, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
            r.raise_for_status()
            self._cache[key] = r.json()
        return self._cache[key]

    def html(self, url, render=False, clicks=()):
        """(html, final_url). `clicks` are visible texts (tabs, buttons) to
        click first — only with render, since clicking needs a browser."""
        key = ("html", url, bool(render or clicks), tuple(clicks or ()))
        if key not in self._cache:
            if render or clicks:
                page = self._open_page(url)
                try:
                    for text in clicks or ():
                        _click_text(page, text)
                    self._cache[key] = (page.content(), page.url)
                finally:
                    page.close()
            else:
                r = self.session.get(url, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                self._cache[key] = (r.text, r.url)
        return self._cache[key]

    def _open_page(self, url, record_json=None):
        self._start_browser()
        page = self._ctx.new_page()
        if record_json is not None:
            def on_response(resp):
                try:
                    if resp.request.resource_type not in ("xhr", "fetch"):
                        return
                    if "json" not in (resp.headers.get("content-type") or ""):
                        return
                    record_json.append((resp.request.method, resp.url, resp.text()[:400]))
                except Exception:
                    pass
            page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        return page

    # -- for the learner: a page that stays open between steps --

    def explore(self, url, render):
        if self.page is not None:
            self.page.close()
            self.page = None
        if not render:
            html, final = self.html(url)
            return html, final, []
        self.page_json = []
        self.page = self._open_page(url, record_json=self.page_json)
        return self.page.content(), self.page.url, self.page_json

    def click(self, text):
        if self.page is None:
            raise ValueError("no open page to click on — open one with render: true first")
        before = len(self.page_json)
        _click_text(self.page, text)
        return self.page.content(), self.page.url, self.page_json[before:]


def _click_text(page, text):
    loc = page.get_by_text(text, exact=True)
    if loc.count() == 0:
        loc = page.get_by_text(text)
    for i in range(min(loc.count(), 5)):
        el = loc.nth(i)
        if el.is_visible():
            el.click(timeout=5000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            return
    raise ValueError(f'nothing visible says "{text}"')


# ---------------------------------------------------------------------------
# Recipe replay
# ---------------------------------------------------------------------------

def json_path(obj, path):
    """'result.0.name' into nested dicts/lists. '' -> obj. Missing -> None."""
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def fill(template, ctx):
    """'{a}' / '{a.b}' from ctx; '{a|q}' URL-encodes. A missing value
    raises KeyError so a half-built URL is never fetched."""
    def sub(m):
        v = json_path(ctx, m.group(1).strip())
        if v is None:
            raise KeyError(m.group(1))
        return quote(str(v), safe="") if m.group(2) else str(v)
    return re.sub(r"\{([^{}|]+)(\|q)?\}", sub, str(template))


def _text_lines(soup):
    return [re.sub(r"\s+", " ", t).strip() for t in soup.get_text("\n").split("\n") if t.strip()]


def _clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v or None


class _Source:
    """One fetched page or JSON response for a project, read lazily."""

    def __init__(self, name, kind, url, data):
        self.name, self.kind, self.url, self.data = name, kind, url, data
        self._lines = None

    @property
    def lines(self):
        if self._lines is None:
            if self.kind == "html":
                self._lines = _text_lines(self.data)
            else:
                self._lines = [json.dumps(self.data)[:20000]]
        return self._lines

    def where(self):
        if self.name == "list":
            return "the bid list"
        if self.name == "main":
            return "the project page"
        return f'the "{self.name}" ' + ("data" if self.kind == "json" else "page")


def _extract(spec, get_source):
    """(value, from_note) for one field spec, or (None, why)."""
    if "value" in spec:
        return _clean(spec["value"]), "the recipe (the same for every bid)"
    src = get_source(spec.get("from") or "main")
    value, snippet = None, None
    if "path" in spec:
        value = json_path(src.data, spec["path"]) if src.kind == "json" else None
        snippet = f'{spec["path"]}: {str(value)[:120]}' if value is not None else None
    elif "selector" in spec:
        if src.kind == "html":
            el = src.data.select_one(spec["selector"])
            if el is not None:
                value = el.get(spec["attr"]) if spec.get("attr") else el.get_text(" ", strip=True)
                snippet = el.get_text(" ", strip=True)[:160]
    elif "label" in spec:
        label = spec["label"].lower().rstrip(":")
        lines = src.lines
        for exact in (True, False):
            for i, line in enumerate(lines):
                low = line.lower()
                pos = 0 if low.startswith(label) else (low.find(label) if not exact else -1)
                if pos < 0:
                    continue
                rest = line[pos + len(label):].strip(" :-\t")
                if not rest and i + 1 < len(lines):
                    rest, line = lines[i + 1], f"{line} {lines[i + 1]}"
                if rest:
                    value, snippet = rest, line[:160]
                    break
            if value:
                break
    elif "regex" in spec:
        m = re.search(spec["regex"], "\n".join(src.lines), re.I)
        if m:
            value = m.group(1) if m.groups() else m.group(0)
            snippet = m.group(0)[:160]
    value = _clean(value if not isinstance(value, (dict, list)) else json.dumps(value))
    if value and spec.get("pattern"):
        m = re.search(spec["pattern"], value, re.I)
        value = _clean(m.group(1) if m and m.groups() else m.group(0) if m else None)
    if not value:
        return None, f"nothing found in {src.where()}"
    return value, f'{src.where()} — "{snippet or value}"'


_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?![a-z])", re.I)
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ](\d{2}):(\d{2})")


def parse_date(text):
    iso = adapters.parse_bid_date(text)
    if iso:
        return iso
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", text or "")
    if m:
        try:
            return datetime.strptime(m.group(0), "%m/%d/%y").date().isoformat()
        except ValueError:
            return None
    return None


def parse_time(text):
    """'2:00 PM MST' -> '2:00pm MT'; '2026-12-03T14:00:00' -> '2:00pm'."""
    t = text or ""
    m = _TIME.search(t)
    if m:
        h, mn, ap = int(m.group(1)), m.group(2) or "00", m.group(3).lower()
    else:
        m = _ISO_TIME.search(t)
        if not m or (m.group(1), m.group(2)) == ("00", "00"):
            return None
        h24, mn = int(m.group(1)), m.group(2)
        h, ap = (h24 % 12 or 12), ("p" if h24 >= 12 else "a")
    if not 1 <= h <= 12:
        return None
    zone = " MT" if re.search(r"\b(MT|MST|MDT|Mountain)\b", t) else ""
    return f"{h}:{mn}{ap}m{zone}"


def _filename(name, url):
    name = _clean(name) or ""
    if not name.lower().endswith(DOC_EXT):
        tail = requests.utils.unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        name = tail if tail.lower().endswith(DOC_EXT) else (name or tail or "document")
    if not name.lower().endswith(DOC_EXT):
        name += ".pdf"
    return name


def _list_items(recipe, fetcher):
    """[{"url", "ctx", "row", "list_src"}] for every project on the bid list."""
    spec = recipe.get("list") or {}
    if not spec.get("url"):
        raise ValueError("the recipe has no list.url")
    items, seen = [], set()

    def add(url, ctx, row, src):
        if url and url not in seen and len(items) < MAX_PROJECTS:
            seen.add(url)
            items.append({"url": url, "ctx": ctx, "row": row, "list_src": src})

    if spec.get("type") == "json":
        page_param = spec.get("page_param")
        page = int(spec.get("page_start", 1))
        for _ in range(MAX_LIST_PAGES if page_param else 1):
            url = fill(spec["url"], {})
            if page_param:
                url += ("&" if "?" in url else "?") + f"{page_param}={page}"
            data = fetcher.json(url)
            rows = json_path(data, spec.get("items_path") or "")
            if not isinstance(rows, list) or not rows:
                break
            before = len(items)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    item_url = fill(spec["item_url"], row)
                except KeyError:
                    continue
                add(item_url, dict(row), json.dumps(row)[:600], _Source("list", "json", url, row))
            if len(items) == before:
                break
            page += 1
        return items

    url = fill(spec["url"], {})
    for _ in range(MAX_LIST_PAGES):
        html, final = fetcher.html(url, render=spec.get("render"))
        soup = BeautifulSoup(html, "html.parser")
        for el in soup.select(spec.get("item_selector") or "a[href]"):
            a = el if el.name == "a" else el.find("a", href=True)
            if a is None or not a.get("href"):
                continue
            item_url = urljoin(final, a["href"])
            row_el = a.find_parent(["tr", "li"]) or el
            ctx = {"item_url": item_url, "link_text": a.get_text(" ", strip=True)}
            if spec.get("item_id_pattern"):
                m = re.search(spec["item_id_pattern"], item_url)
                if m:
                    ctx.update({k: v for k, v in m.groupdict().items() if v})
            row_soup = BeautifulSoup(str(row_el), "html.parser")
            add(item_url, ctx, row_el.get_text(" ", strip=True)[:600], _Source("list", "html", final, row_soup))
        nxt = soup.select_one(spec["next_selector"]) if spec.get("next_selector") else None
        if nxt is None or not nxt.get("href") or len(items) >= MAX_PROJECTS:
            break
        nxt_url = urljoin(final, nxt["href"])
        if nxt_url == url:
            break
        url = nxt_url
    return items


def _skip_reason(rules, fields, row, main_text):
    for rule in rules or []:
        skip = rule.get("skip") or rule
        field, pattern = skip.get("field"), skip.get("matches")
        if not pattern:
            continue
        hay = (row if field == "_row" else main_text() if field == "_page"
               else (fields.get(field) or {}).get("value"))
        try:
            if hay and re.search(pattern, hay, re.I):
                return rule.get("text") or f"{field} matches {pattern}"
        except re.error:
            continue
    return None


def read_project(recipe, fetcher, item, rules=()):
    """Everything the recipe reads for one project: {"url", "name", "fields":
    {name: {value, from}}, "documents": [{name, url}], "docs_from",
    "plan_holders", "holders_from", "skipped", "errors"}."""
    proj = recipe.get("project") or {}
    source_specs = dict(proj.get("sources") or {})
    source_specs.setdefault("main", {"url": "{item_url}", "type": "html"})
    ctx = dict(item["ctx"], item_url=item["url"])
    loaded = {"list": item["list_src"]}
    errors = []

    def get_source(name):
        if name not in loaded:
            spec = source_specs.get(name)
            if spec is None:
                raise ValueError(f'the recipe has no source named "{name}"')
            url = fill(spec.get("url") or "{item_url}", ctx)
            if spec.get("type") == "json":
                loaded[name] = _Source(name, "json", url, fetcher.json(url))
            else:
                html, final = fetcher.html(url, render=spec.get("render"), clicks=spec.get("click") or ())
                loaded[name] = _Source(name, "html", final, BeautifulSoup(html, "html.parser"))
        return loaded[name]

    def safe(fn, what):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{what}: {e}")
            return None

    fields = {}
    for fname, spec in (proj.get("fields") or {}).items():
        got = safe(lambda: _extract(spec, get_source), fname)
        if got:
            fields[fname] = {"value": got[0], "from": got[1]}
    name = (fields.get("Project name") or {}).get("value") or item["ctx"].get("link_text") or item["url"]

    bid = (fields.get("Bid date") or {}).get("value")
    if bid:
        iso = parse_date(bid)
        if iso:
            fields["Bid date"]["iso"] = iso
        if not fields.get("Bid time") and parse_time(bid):
            fields["Bid time"] = {"value": parse_time(bid), "from": fields["Bid date"]["from"]}
    if fields.get("Bid time"):
        fields["Bid time"]["value"] = parse_time(fields["Bid time"]["value"]) or fields["Bid time"]["value"]

    out = {"url": item["url"], "name": name, "fields": fields, "documents": [], "docs_from": [],
           "plan_holders": None, "holders_from": None, "skipped": None, "errors": errors}

    def main_text():
        src = safe(lambda: get_source("main"), "main page")
        return "\n".join(src.lines) if src else ""

    out["skipped"] = _skip_reason(list(recipe.get("skip") or []) + list(rules or []),
                                  fields, item["row"], main_text)
    if out["skipped"]:
        return out

    docs = proj.get("documents") or []
    for spec in docs if isinstance(docs, list) else [docs]:
        found = safe(lambda: _read_documents(spec, get_source, ctx), "documents")
        if found:
            out["documents"] += found
            out["docs_from"].append(get_source(spec.get("from") or "main").where())
    seen, uniq = set(), []
    for d in out["documents"]:
        if d["url"] not in seen:
            seen.add(d["url"])
            uniq.append(d)
    out["documents"] = uniq

    if proj.get("plan_holders"):
        spec = proj["plan_holders"]
        out["plan_holders"] = safe(lambda: _read_holders(spec, get_source), "plan holders")
        if out["plan_holders"] is not None:
            out["holders_from"] = get_source(spec.get("from") or "main").where()
    return out


def _read_documents(spec, get_source, ctx):
    src = get_source(spec.get("from") or "main")
    out = []
    if src.kind == "json":
        rows = json_path(src.data, spec.get("items_path") or "")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            c = dict(ctx, **row)
            try:
                url = fill(spec["url"], c)
                name = fill(spec.get("name") or "", c)
            except KeyError:
                continue
            out.append({"name": _filename(name, url), "url": url})
        return out
    selector = spec.get("selector")
    for a in src.data.select(selector) if selector else src.data.find_all("a", href=True):
        if a.name != "a":
            a = a.find("a", href=True) or a
        href = a.get("href")
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(src.url, href)
        if not selector and not urlparse(url).path.lower().endswith(DOC_EXT):
            continue
        out.append({"name": _filename(a.get_text(" ", strip=True), url), "url": url})
    return out


_HOLDER_KEYS = ("company", "role", "contact", "title", "emails", "phone",
                "address", "city", "state", "zip", "cert")


def _holder(values):
    return adapters.plan_holder(
        values.get("company"), role=values.get("role"), contact=values.get("contact"),
        title=values.get("title"), emails=adapters.clean_emails(values.get("emails") or ""),
        phone=values.get("phone"), address=values.get("address"), city=values.get("city"),
        state=values.get("state"), zipcode=values.get("zip"), cert=values.get("cert"))


def _read_holders(spec, get_source):
    src = get_source(spec.get("from") or "main")
    mapping = spec.get("map") or {"company": "company"}
    out = []
    if src.kind == "json":
        rows = json_path(src.data, spec.get("items_path") or "")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                vals = {k: json_path(row, v) for k, v in mapping.items() if k in _HOLDER_KEYS}
                if vals.get("company"):
                    out.append(_holder({k: str(v) if v is not None else None for k, v in vals.items()}))
        return out
    table = src.data.select_one(spec.get("table_selector") or "table")
    if table is None:
        return out
    rows = table.find_all("tr")
    if not rows:
        return out
    headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
    cols = {}
    for key, header in mapping.items():
        for i, h in enumerate(headers):
            if str(header).lower() in h:
                cols[key] = i
                break
    for tr in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        vals = {k: cells[i] for k, i in cols.items() if i < len(cells) and k in _HOLDER_KEYS}
        if vals.get("company"):
            out.append(_holder(vals))
    return out


def replay(recipe, fetcher, rules=(), excludes=(), limit=None, include=()):
    """Run a recipe. `limit` reads only the first N projects (plus any in
    `include`, so corrected projects are always checked). Excluded project
    URLs are dropped. Returns {"listed", "projects", "errors"}."""
    result = {"listed": 0, "projects": [], "errors": []}
    try:
        items = _list_items(recipe, fetcher)
    except Exception as e:
        result["errors"].append(f"bid list: {e}")
        return result
    result["listed"] = len(items)
    excludes, include = set(excludes or ()), set(include or ())
    for i, item in enumerate(items):
        if item["url"] in excludes:
            continue
        if limit is not None and i >= limit and item["url"] not in include:
            continue
        result["projects"].append(read_project(recipe, fetcher, item, rules))
    return result


# ---------------------------------------------------------------------------
# Checks — the user's corrections, which every recipe has to keep honoring
# ---------------------------------------------------------------------------

def _norm(v):
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _same(field, got, expected):
    if field == "Bid date":
        a, b = parse_date(got), parse_date(expected)
        if a and b:
            return a == b
    if field == "Bid time":
        a, b = parse_time(got), parse_time(expected)
        if a and b:
            return a.replace(" MT", "") == b.replace(" MT", "")
    g, e = _norm(got), _norm(expected)
    return bool(g and e) and (g == e or e in g)


def check_failures(result, checks):
    """Plain-English list of checks the result breaks. A check about a
    project that isn't listed any more can't be judged, so it passes."""
    by_url = {p["url"]: p for p in result["projects"]}
    fails = []
    for c in checks or []:
        p = by_url.get(c.get("project"))
        label = c.get("project_name") or c.get("project")
        if c.get("kind") == "exclude":
            if p and not p.get("skipped"):
                fails.append(f'"{label}" should be left out ({c.get("note") or "the user removed it"}), but it is still there')
        elif c.get("kind") == "value" and c.get("expected") and p and not p.get("skipped"):
            got = (p["fields"].get(c["field"]) or {}).get("value")
            if not _same(c["field"], got, c["expected"]):
                fails.append(f'"{label}": {c["field"]} should be "{c["expected"]}" but came out '
                             f'{json.dumps(got) if got else "empty"}'
                             + (f' (the user said: {c["note"]})' if c.get("note") else ""))
    return fails


def _excluded_urls(state):
    return {c["project"] for c in state.get("checks") or [] if c.get("kind") == "exclude" and c.get("project")}


def _value_check_urls(state):
    return {c["project"] for c in state.get("checks") or [] if c.get("kind") == "value" and c.get("project")}


def _soft_problems(result, fields):
    probs = []
    live = [p for p in result["projects"] if not p.get("skipped")]
    if live and not any(p["documents"] for p in live):
        probs.append("none of the projects came back with any documents")
    for f in BUILT_IN_FIELDS[:2] + list(fields):
        have = sum(1 for p in live if (p["fields"].get(f) or {}).get("value"))
        if live and have < len(live) / 2:
            probs.append(f'"{f}" was found on only {have} of {len(live)} projects')
    errs = [e for p in live for e in p["errors"]][:5]
    if errs:
        probs.append("errors: " + "; ".join(errs))
    return probs


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

RECIPE_FORMAT = r"""
A recipe is JSON with these parts:

"list": where the bids are listed. Either an HTML page:
    {"type": "html", "url": "https://site/bids", "render": false,
     "item_selector": "CSS for each project's link (an <a>, or a row containing one)",
     "next_selector": "CSS for the next-page <a>, or omit",
     "item_id_pattern": "optional regex with named groups run on each project URL, e.g. /project/(?P<id>\\d+)"}
  or a JSON feed (much better when the page loads one — look at the JSON requests in each snapshot):
    {"type": "json", "url": "https://site/api/projects?status=open",
     "items_path": "result",               (dotted path to the array of projects)
     "item_url": "https://site/project/{record_uuid}",  (each project's page; {key} comes from the array item)
     "page_param": "page", "page_start": 1} (only if the feed is paged)
  "render": true means the page needs a real browser (content built by JavaScript). Avoid it when plain HTML works.

"project": how to read one project.
  "sources": named pages/feeds per project. "main" defaults to {"url": "{item_url}", "type": "html"}.
     {"files": {"type": "json", "url": "https://site/api/files/{record_uuid}"},
      "bidders": {"type": "html", "url": "{item_url}", "render": true, "click": ["Plan Holders"]}}
     Templates can use any key from the list item, plus {item_url}, {link_text} and named groups from item_id_pattern.
     Add |q to URL-encode, e.g. {file_name|q}. "click" lists visible texts to click (tabs) before reading; it implies render.
     The special source "list" is the project's own entry on the bid list (the JSON item, or the HTML row).
  "fields": {"Field name": spec}. A spec is one of:
     {"from": "list", "path": "bid_opening_date"}              (JSON: dotted path)
     {"from": "main", "selector": "h1.title"}                 (HTML: CSS, text of the first match; add "attr": "href" for an attribute)
     {"from": "main", "label": "Bid Opening"}                 (HTML: the text after this label on the page, or on the line below it)
     {"from": "main", "regex": "Bids due:?\s*(.+)"}           (first group)
     {"value": "2:00 PM MT"}                                  (fixed — only when the site states it once for everything)
     Any spec can add "pattern": "regex" to trim the value.
  "documents": a list of specs, one per place files are listed (plan set, addenda, ...):
     {"from": "main", "selector": "#documents a"}             (HTML links; omit selector to take every .pdf/.docx/.doc link)
     {"from": "files", "items_path": "result", "name": "{file_name}",
      "url": "https://site/download/{record_uuid}?file_name={file_name|q}"}   (JSON; templates see the file item and the project)
  "plan_holders" (only if the site lists who pulled plans):
     {"from": "bidders", "table_selector": "table.planholders",
      "map": {"company": "Company", "phone": "Phone", "contact": "Contact"}}        (HTML table: map key -> column header)
     {"from": "holders", "items_path": "result", "map": {"company": "company_name", "role": "bidding_as"}}  (JSON paths)
     Keys: company, role, contact, title, emails, phone, address, city, state, zip, cert.

"skip": optional rules for listed items that are not open bids:
   [{"field": "Status", "matches": "awarded|closed"}]   field can be any recipe field, "_row" (the list entry) or "_page" (main page text).
"""


def _learn_prompt(site, fields, state, history, steps_left):
    hints = (state.get("hints") or "").strip()
    checks = state.get("checks") or []
    rules = state.get("rules") or []
    lines = [
        "You are teaching a scraper to read a construction bid website. Explore the site step by step, "
        "then write a recipe (a small JSON program) that reads every open bid on it. The recipe will run "
        "every day without you, so prefer stable things: JSON feeds over HTML, ids and labels over positions.",
        "",
        f"SITE: {site.get('name')} — {site.get('url')}",
        "The scraper is already logged in to this site." if site.get("login_username") else "",
        "",
        "COLLECT FOR EACH PROJECT:",
        "  Project name, Bid date (when bids are due), Bid time (if shown), Location (city/county/state — "
        "if shown), every document (plans, specs, addenda), and plan holders (if the site lists them).",
    ]
    if fields:
        lines.append("  Also, when the site shows them: " + ", ".join(fields) + ". Use these exact field names.")
    if hints:
        lines += ["", "NOTES FROM THE USER ABOUT THIS SITE (follow them):", hints]
    if rules:
        lines += ["", "THE USER CONFIRMED THESE SKIP RULES (they are applied automatically):"]
        lines += [f"  - {r.get('text')}" for r in rules]
    value_checks = [c for c in checks if c.get("kind") == "value"]
    excl = [c for c in checks if c.get("kind") == "exclude"]
    if value_checks or excl:
        lines += ["", "CORRECTIONS THE RECIPE MUST HONOR (checked automatically when you finish):"]
        for c in value_checks:
            what = f'should be "{c["expected"]}"' if c.get("expected") else "was wrong"
            lines.append(f'  - Project {c.get("project_name")!r} ({c.get("project")}): {c["field"]} {what}'
                         + (f' — the user says: {c["note"]}' if c.get("note") else ""))
        for c in excl:
            lines.append(f'  - Project {c.get("project_name")!r} ({c.get("project")}) is not a bid we want'
                         + (f' — {c["note"]}' if c.get("note") else "")
                         + ". It is filtered out automatically; if others like it exist, add a skip rule.")
    old = state.get("recipe") or (state.get("draft") or {}).get("recipe")
    if old:
        lines += ["", "THE PREVIOUS RECIPE (fix or improve it; it may be out of date):", json.dumps(old)]
    lines += [
        "", "RECIPE FORMAT:", RECIPE_FORMAT,
        "ACTIONS (answer with exactly one JSON object, no other text):",
        '  {"say": "short note for the user", "action": "open", "url": "...", "render": true}'
        "      — look at a page. render: true uses a real browser and also shows the JSON requests the page makes.",
        '  {"say": "...", "action": "click", "text": "Plan Holders"}     — click visible text on the last rendered page',
        '  {"say": "...", "action": "fetch_json", "url": "..."}          — look at a JSON URL',
        '  {"say": "...", "action": "try", "recipe": {...}}               — run a recipe on the first 3 projects',
        '  {"say": "...", "action": "done", "recipe": {...}, "summary": "Bid list → project page → Documents tab"}',
        '  {"say": "...", "action": "give_up", "reason": "..."}           — only if the site truly cannot be read',
        "\"say\" is shown to the user as progress, in plain words (\"Found the bid list\", \"Checking the Documents tab\").",
        "Use \"try\" before \"done\". \"done\" is checked against the corrections and sent back if it fails.",
        f"You have {steps_left} steps left.",
        "", "WHAT HAS HAPPENED SO FAR:",
    ]
    if not history:
        lines.append("  (nothing yet — start by opening the site URL with render: true)")
    for i, (act, obs) in enumerate(history):
        recent = i >= len(history) - 3
        lines.append(f"STEP {i + 1}: {act}")
        lines.append("RESULT: " + (obs[:7000] if recent else obs[:400] + (" …" if len(obs) > 400 else "")))
    return "\n".join(x for x in lines if x is not None)


def _parse_reply(text):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except ValueError:
                    return None
    return None


_last_ai_call = [0.0]


def _ask(ai, prompt, max_tokens=8000):
    """One AI turn as parsed JSON, or None after a few spaced-out retries
    (a free tier that's over its limit answers nothing for a while)."""
    for wait in (0, 20, 60):
        if wait:
            time.sleep(wait)
        gap = AI_MIN_INTERVAL_S - (time.monotonic() - _last_ai_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_ai_call[0] = time.monotonic()
        parsed = _parse_reply(ai.complete(prompt, max_tokens))
        if isinstance(parsed, dict):
            return parsed
    return None


def _snapshot(html, url, json_reqs):
    """What the learner sees of a page: links, buttons/tabs, tables, JSON
    requests and the start of its text."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    out = [f"URL: {url}", f"TITLE: {title}"]
    if json_reqs:
        out.append("JSON REQUESTS THIS PAGE MADE:")
        seen = set()
        for method, u, body in json_reqs:
            if u in seen or len(seen) >= 25:
                continue
            seen.add(u)
            out.append(f"  {method} {u}\n    {re.sub(r'\s+', ' ', body)[:300]}")
    buttons = []
    for el in soup.select("button, [role=tab], [role=button], .nav-link, .tab, summary"):
        t = el.get_text(" ", strip=True)
        if t and len(t) <= 40 and t not in buttons:
            buttons.append(t)
    if buttons:
        out.append("BUTTONS/TABS: " + " | ".join(buttons[:40]))
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href in seen or a["href"].startswith(("javascript:", "mailto:", "tel:")):
            continue
        seen.add(href)
        links.append(f'  "{a.get_text(" ", strip=True)[:70]}" -> {href}')
    if links:
        out.append(f"LINKS ({len(links)}):")
        out += links[:120]
    for i, table in enumerate(soup.find_all("table")[:3]):
        rows = table.find_all("tr")
        out.append(f"TABLE {i + 1} ({len(rows)} rows; id={table.get('id')!r} class={table.get('class')!r}):")
        for tr in rows[:3]:
            out.append("  | " + " | ".join(c.get_text(" ", strip=True)[:40] for c in tr.find_all(["th", "td"])))
    text = "\n".join(_text_lines(soup))
    out.append("TEXT:\n" + text[:3500])
    return "\n".join(out)


def _json_preview(obj, depth=0):
    if isinstance(obj, list):
        head = [_json_preview(x, depth + 1) for x in obj[:2]]
        return head + ([f"... {len(obj) - 2} more items"] if len(obj) > 2 else [])
    if isinstance(obj, dict):
        return {k: _json_preview(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, str) and len(obj) > 200:
        return obj[:200] + "…"
    return obj


def describe_result(result, max_projects=3):
    out = [f"Projects listed: {result['listed']}"]
    out += [f"ERROR {e}" for e in result["errors"]]
    for p in result["projects"][:max_projects]:
        out.append(f"PROJECT {p['url']}")
        if p.get("skipped"):
            out.append(f"  skipped: {p['skipped']}")
            continue
        for f, v in p["fields"].items():
            out.append(f"  {f}: {v['value']!r}   (from {v['from']})")
        out.append(f"  documents: {len(p['documents'])}"
                   + (" — " + ", ".join(d["name"] for d in p["documents"][:4]) if p["documents"] else ""))
        if p["plan_holders"] is not None:
            out.append(f"  plan holders: {len(p['plan_holders'])}"
                       + (" — " + ", ".join(h["company"] for h in p["plan_holders"][:3]) if p["plan_holders"] else ""))
        out += [f"  error: {e}" for e in p["errors"]]
    return "\n".join(out)


def learn(site, fields, state, ai, fetcher, progress=print):
    """Explore the site and return (recipe, summary, warnings). Raises
    LearnFailed when the AI gives up, stops answering or runs out of steps
    without a recipe that reads anything."""
    history, best, done_tries, ai_misses = [], None, 0, 0
    rules = state.get("rules") or []
    excludes = _excluded_urls(state)
    include = _value_check_urls(state) | excludes
    for step in range(LEARN_MAX_STEPS):
        reply = _ask(ai, _learn_prompt(site, fields, state, history, LEARN_MAX_STEPS - step))
        if reply is None:
            ai_misses += 1
            if ai_misses >= 3:
                break
            history.append(("(no answer)", "Your last answer wasn't a single JSON object. Answer with exactly one."))
            continue
        if reply.get("say"):
            progress(str(reply["say"])[:200])
        action = reply.get("action")
        shown = {k: v for k, v in reply.items() if k not in ("say", "recipe")}
        obs = ""
        try:
            if action == "open":
                obs = _snapshot(*fetcher.explore(reply["url"], bool(reply.get("render"))))
            elif action == "click":
                obs = _snapshot(*fetcher.click(reply["text"]))
            elif action == "fetch_json":
                obs = json.dumps(_json_preview(fetcher.json(reply["url"])), indent=1)[:6000]
            elif action == "try":
                shown["recipe"] = reply.get("recipe")
                res = replay(reply.get("recipe") or {}, fetcher, rules, excludes, limit=3)
                obs = describe_result(res)
                if res["projects"] and (best is None or len(res["projects"]) > best[3]):
                    best = (reply["recipe"], reply.get("summary") or "", [], len(res["projects"]))
            elif action == "done":
                recipe = reply.get("recipe") or {}
                shown["recipe"] = recipe
                done_tries += 1
                res = replay(recipe, fetcher, rules, excludes, limit=PREVIEW_PROJECTS, include=include)
                hard = check_failures(res, state.get("checks"))
                if not res["projects"]:
                    hard.insert(0, "the recipe found no projects" + (f" ({'; '.join(res['errors'])})" if res["errors"] else ""))
                soft = _soft_problems(res, fields)
                summary = str(reply.get("summary") or "")[:200]
                if res["projects"]:
                    best = (recipe, summary, soft, len(res["projects"]))
                if not hard and (not soft or done_tries >= 2):
                    return recipe, summary, soft
                obs = ("NOT ACCEPTED YET:\n- " + "\n- ".join(hard + soft)
                       + ("\n(If a soft problem is just how this site is, send done again and it will be accepted.)" if not hard else "")
                       + "\n\n" + describe_result(res))
            elif action == "give_up":
                raise LearnFailed(str(reply.get("reason") or "the AI couldn't make sense of this site"))
            else:
                obs = f'Unknown action "{action}". Use one of: open, click, fetch_json, try, done, give_up.'
        except LearnFailed:
            raise
        except Exception as e:
            obs = f"ERROR: {e}"
        history.append((json.dumps(shown)[:3000], obs))
    if best:
        recipe, summary, soft, _ = best
        return recipe, summary, soft + ["the learner ran out of steps — this is its best attempt"]
    if ai_misses >= 3:
        raise LearnFailed("The AI stopped answering (it may be over its free-tier limit for now). Try again later.")
    raise LearnFailed("The learner ran out of steps without finding the bids on this site.")


def propose_rule(ai, project, reason, others):
    """When the user removes a project: what makes it different from the
    ones they kept, as a rule they can confirm. Returns {"text", "skip":
    {"field", "matches"}} or None when nothing general stands out."""
    prompt = "\n".join([
        "A user reviewing bids scraped from a website removed one as not wanted.",
        f"Their reason: {reason or '(none given)'}",
        "REMOVED: " + json.dumps(project)[:3000],
        "KEPT: " + json.dumps(others)[:6000],
        "Is there a simple rule that would skip the removed one and others like it, but none of the kept ones?",
        'Answer with one JSON object: {"text": "Skip projects whose Status is Awarded", '
        '"skip": {"field": "<a field name from the projects, or _row for the bid-list entry text>", '
        '"matches": "<case-insensitive regex>"}} — or {"text": null} if it looks like a one-off.',
    ])
    reply = _ask(ai, prompt, max_tokens=2000)
    if not reply or not reply.get("text") or not (reply.get("skip") or {}).get("matches"):
        return None
    try:
        re.compile(reply["skip"]["matches"])
    except re.error:
        return None
    return {"text": str(reply["text"])[:200], "skip": {"field": reply["skip"].get("field") or "_row",
                                                       "matches": reply["skip"]["matches"]}}


# ---------------------------------------------------------------------------
# State, preview, and the learn command
# ---------------------------------------------------------------------------

def site_login(site):
    username = (site.get("login_username") or "").strip()
    enc = site.get("login_password_enc")
    if not username or not enc:
        return None
    try:
        password = credentials.decrypt_password(enc)
    except Exception as e:
        print(f"  ! Could not decrypt login credentials for '{site.get('name')}': {e}")
        return None
    return {"username": username, "password": password,
            "url": (site.get("login_url") or "").strip() or site.get("url")}


def division_fields(division_id):
    return [f for f in (supabase_store.get_division_extra(division_id, "learn_fields") or [])
            if f and f not in BUILT_IN_FIELDS]


def _now():
    return datetime.now(timezone.utc).isoformat()


class _State:
    """sites.learned for one site, saved back on every change."""

    def __init__(self, site):
        self.site = site
        self.data = dict(site.get("learned") or {})

    def save(self, **patch):
        self.data.update(patch)
        supabase_store.save_site_learned(self.site["division_id"], self.site["id"], self.data)

    def progress(self, line):
        print(f"  [learn] {line}")
        self.save(progress=((self.data.get("progress") or []) + [line])[-12:], progress_at=_now())


def _preview(result, fields):
    projects = []
    for p in result["projects"]:
        if p.get("skipped"):
            continue
        projects.append({
            "url": p["url"], "name": p["name"],
            "fields": {k: v for k, v in p["fields"].items() if k in BUILT_IN_FIELDS or k in fields},
            "documents": p["documents"][:60], "document_count": len(p["documents"]),
            "docs_from": p["docs_from"],
            "plan_holders": (p["plan_holders"] or [])[:40] if p["plan_holders"] is not None else None,
            "holders_from": p["holders_from"], "errors": p["errors"][:5],
        })
    return {"listed": result["listed"], "projects": projects, "errors": result["errors"][:5]}


def _compare_with_adapter(site):
    """For a site a built-in adapter also covers (the UDOT proof of concept),
    what that adapter finds — so the preview can say how close it came."""
    cls = adapters.match_adapter_class(site.get("url") or "")
    if cls is None or cls.needs_browser:
        return None
    try:
        found = cls().find_documents(site)
    except Exception as e:
        return {"label": cls.label, "error": str(e)}
    return {"label": cls.label, "projects": len({t[0] for t in found}), "documents": len(found)}


def run_learn(division_id, site_id):
    site = supabase_store.get_site(division_id, site_id)
    if site is None:
        print(f"FATAL: no site {site_id} in {division_id}", file=sys.stderr)
        sys.exit(1)
    state = _State(site)
    state.save(status="learning", message=None, progress=[], progress_at=_now())
    ai = ai_provider.get_provider()
    if ai is None:
        state.save(status="failed", message="No AI is set up on the scan worker (AI_PROVIDER + a key).")
        return
    fields = division_fields(division_id)
    fetcher = None
    try:
        fetcher = Fetcher(site_login(site))
        if fetcher.login_result and not fetcher.login_result["ok"]:
            state.progress(f"Login didn't work: {fetcher.login_result['message']}")
        state.progress("Opening the site")
        recipe, summary, warnings = learn(site, fields, state.data, ai, fetcher, progress=state.progress)
        state.progress("Reading a sample of projects for the preview")
        result = replay(recipe, fetcher, state.data.get("rules"), _excluded_urls(state.data),
                        limit=PREVIEW_PROJECTS, include=_value_check_urls(state.data))
        preview = _preview(result, fields)
        preview["warnings"] = warnings
        preview["check_failures"] = check_failures(result, state.data.get("checks"))
        preview["compare"] = _compare_with_adapter(site)
        state.save(status="review", draft={"recipe": recipe, "summary": summary}, preview=preview,
                   learned_at=_now(), message=None)
        state.progress("Ready for review")
    except LearnFailed as e:
        state.save(status="live" if state.data.get("recipe") else "failed", message=str(e))
        state.progress("Stopped: " + str(e))
    except Exception as e:
        state.save(status="live" if state.data.get("recipe") else "failed", message=f"Learning crashed: {e}")
        raise
    finally:
        if fetcher:
            fetcher.close()


# ---------------------------------------------------------------------------
# Daily scan
# ---------------------------------------------------------------------------

def scan_learned_site(site, ai_client):
    """scraper.scan_site's branch for a learned site — same return shape:
    (doc_links 7-tuples, cookies, login_result, listed_labels). Saves plan
    holders, locations and the extra fields itself."""
    state = _State(site)
    recipe = state.data.get("recipe")
    if not recipe:
        print("  Learned site not approved yet — open it in Sites to review. Skipping.")
        return [], {}, None, set()
    print(f"  Learned site: {state.data.get('summary') or 'replaying the saved recipe'}")
    fields = division_fields(site["division_id"])
    rules, excludes = state.data.get("rules"), _excluded_urls(state.data)
    fetcher = None
    try:
        fetcher = Fetcher(site_login(site))
        result = replay(recipe, fetcher, rules, excludes)
        broken = check_failures(result, state.data.get("checks"))
        if not result["projects"]:
            broken.insert(0, "the saved recipe found no projects"
                          + (f" ({'; '.join(result['errors'])})" if result["errors"] else ""))
        if broken:
            print("  ! Recipe stopped working: " + "; ".join(broken))
            result = _heal(site, state, fields, ai_client, fetcher, broken)
            if result is None:
                return [], fetcher.cookies(), fetcher.login_result, set()
        live = [p for p in result["projects"] if not p.get("skipped")]
        state.save(last_run={"at": _now(), "projects": len(live),
                             "documents": sum(len(p["documents"]) for p in live)})
        return _to_scan_output(site, live, fields), fetcher.cookies(), fetcher.login_result, set(
            _labels(live).values())
    except Exception as e:
        print(f"  ! Learned site failed: {e}")
        try:
            state.save(status="attention", message=f"The daily scan hit an error: {e}")
        except Exception:
            pass
        return [], {}, None, set()
    finally:
        if fetcher:
            fetcher.close()


def _heal(site, state, fields, ai, fetcher, broken):
    """Re-learn a live site that stopped working. Takes the new recipe only
    if it passes every check and finds at least half as many projects as
    the last good run; otherwise flags the site and returns None."""
    if ai is None:
        state.save(status="attention", message="Stopped working: " + "; ".join(broken))
        return None
    print("  Re-learning the site")
    state.save(progress=[], progress_at=_now())
    try:
        recipe, summary, _ = learn(site, fields, state.data, ai, fetcher, progress=state.progress)
    except LearnFailed as e:
        state.save(status="attention", message=f"Stopped working ({'; '.join(broken)}) and re-learning failed: {e}")
        return None
    result = replay(recipe, fetcher, state.data.get("rules"), _excluded_urls(state.data))
    live = [p for p in result["projects"] if not p.get("skipped")]
    before = (state.data.get("last_run") or {}).get("projects") or 0
    fails = check_failures(result, state.data.get("checks"))
    if fails or not live or len(live) < before / 2:
        why = "; ".join(fails) or f"it now finds {len(live)} projects (last time {before})"
        state.save(status="attention", draft={"recipe": recipe, "summary": summary},
                   preview=dict(_preview(result, fields), check_failures=fails, warnings=[]),
                   message=f"Stopped working and the re-learned version needs a look: {why}")
        return None
    state.save(recipe=recipe, summary=summary, status="live",
               message=f"Re-learned automatically on {datetime.now().strftime('%b %-d')} ({'; '.join(broken)})")
    return result


def _labels(projects):
    """{url: label} — the project name, made unique within the site."""
    out, used = {}, {}
    for p in projects:
        name = p["name"][:200]
        used[name] = used.get(name, 0) + 1
        out[p["url"]] = name if used[name] == 1 else f"{name} ({used[name]})"
    return out


def _to_scan_output(site, projects, fields):
    labels = _labels(projects)
    doc_links, holders, locations, extras = [], {}, {}, {}
    for p in projects:
        label = labels[p["url"]]
        key = f"{site['name']} — {label}"
        f = p["fields"]
        bid_date = (f.get("Bid date") or {}).get("iso")
        bid_time = (f.get("Bid time") or {}).get("value") if bid_date else None
        for d in p["documents"]:
            doc_links.append((label, d["url"], d["name"], None, p["url"], bid_date, bid_time))
        if p["plan_holders"] is not None:
            holders[key] = p["plan_holders"]
        if (f.get("Location") or {}).get("value"):
            locations[key] = f["Location"]["value"]
        extra = {k: f[k]["value"] for k in fields if (f.get(k) or {}).get("value")}
        if extra:
            extras[key] = extra
    division_id = site["division_id"]
    if holders:
        supabase_store.save_project_plan_holders(division_id, holders)
    if locations:
        supabase_store.save_project_locations(division_id, locations)
    if extras:
        supabase_store.save_project_extra_fields(division_id, extras)
    print(f"  {len(projects)} project(s), {len(doc_links)} document(s)")
    return doc_links


if __name__ == "__main__":
    # Usage: python site_agent.py learn <division_id> <site_id>
    if len(sys.argv) != 4 or sys.argv[1] != "learn":
        print("usage: python site_agent.py learn <division_id> <site_id>", file=sys.stderr)
        sys.exit(2)
    run_learn(sys.argv[2], int(sys.argv[3]))

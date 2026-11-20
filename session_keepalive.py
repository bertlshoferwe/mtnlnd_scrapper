"""
Keeps saved sign-in sessions alive. Bidtracer's sign-in cookie lasts an
hour from its last use, even with "Remember me" (2026-11-19), and signing in
again means its Cloudflare check — so instead each login with a kept session
opens a page on its site with that session, the way a listing read would:
when the session's soonest-ending cookie has under DUE_WITHIN_S left, and
otherwise every MAX_GAP_S. A site
that slides its expiry along with use hands back fresher cookies, which are
kept; the session then never runs out. Nothing here ever fills in a sign-in
form.

A visit that lands on a sign-in form means the session has ended: the login
is marked failed with a note to sign in again ("Sign in yourself"), and its
kept cookies are left as they were (see supabase_store.record_site_login).

The page opened: a site login's own sign-in page or site link (the
ConstructConnect results page is behind its sign-in), or for a login in
Settings → Logins, its sign-in page if one is saved, else the last listing
read on that site — a page only reachable signed in — else the site's root.
"""

import os
import re
import time
from urllib.parse import urlparse

import browser_crawl
import scraper
import site_agent
import supabase_store

CHECK_EVERY_S = 15 * 60
DUE_WITHIN_S = 45 * 60
MAX_GAP_S = int(float(os.environ.get("SESSION_KEEPALIVE_HOURS") or "4") * 3600)
FIRST_RUN_AFTER_S = 60
ENDED = "the saved session has ended — sign in again (Sign in yourself)"


def _targets():
    """[(label, login, url, keep(result))] for every login with a kept session."""
    shared, own = supabase_store.logins_with_sessions()
    out = []
    for row in shared:
        login = scraper.login_from_row(row)
        if not login:
            continue
        url = (row.get("login_url") or supabase_store.last_listing_url(row["division_id"], row["domain"])
               or login["url"])
        out.append((f"{row['domain']} ({row['division_id']})", login, url,
                    lambda r, lid=row["id"]: scraper.keep_login_result(lid, r)))
    for site in own:
        login = scraper._site_login(site)
        if not login or not login.get("session"):
            continue
        out.append((site.get("name") or login["domain"], login, login["url"],
                    lambda r, s=site: scraper.keep_site_login_result(s.get("requested_by"), s["id"], r)))
    return out


_last_visit = {}   # label -> when it was last visited, this process


def _due(label, login):
    """Visit now? When a cookie for the site with an end date has under
    DUE_WITHIN_S left (Bidtracer's hour), or it's been MAX_GAP_S."""
    now = time.time()
    ends = [c["expires"] for c in login.get("session") or []
            if (c.get("expires") or -1) > 0 and c.get("domain", "").lstrip(".").endswith(login.get("domain") or "")]
    soonest = min((e for e in ends if e > now - 86400), default=None)
    return (soonest is not None and soonest - now < DUE_WITHIN_S) or now - _last_visit.get(label, 0) > MAX_GAP_S


def _visit(ctx, login, url):
    """{"ok", "message", "session"} — is the session still signed in at `url`?"""
    ctx.clear_cookies()
    browser_crawl._restore_session(ctx, login["session"])
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=browser_crawl.PAGE_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass   # some pages never go idle
        if (browser_crawl._visible(page, "input[type=password]")
                or browser_crawl._SIGN_IN_URL.search(page.url)):
            return {"ok": False, "message": ENDED}
        cookies = ctx.cookies()
        host = re.sub(r"^www[.]", "", urlparse(page.url).hostname or "")
        print(f"  [keepalive] {login['username']}: still signed in "
              f"({browser_crawl.session_lifetimes(cookies, host)})", flush=True)
        return {"ok": True, "message": None, "session": cookies}
    finally:
        page.close()


def run_once():
    targets = [t for t in _targets() if _due(t[0], t[1])]
    if not targets:
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=site_agent.UA)
        try:
            for label, login, url, keep in targets:
                _last_visit[label] = time.time()
                try:
                    result = _visit(ctx, login, url)
                except Exception as e:
                    # A page that didn't load says nothing about the session: leave it be.
                    print(f"  [keepalive] {label}: couldn't check ({e})", flush=True)
                    continue
                if not result["ok"]:
                    print(f"  [keepalive] {label}: {ENDED}", flush=True)
                keep(result)
        finally:
            browser.close()


def loop():
    time.sleep(FIRST_RUN_AFTER_S)
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"  ! [keepalive] {e}", flush=True)
        time.sleep(CHECK_EVERY_S)

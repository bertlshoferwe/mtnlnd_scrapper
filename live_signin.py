"""
"Sign in yourself": for a saved login the scan worker can't sign in with on
its own (Bidtracer's Cloudflare check turns the automatic sign-in away), the
sign-in page is opened in a browser here, on the scan worker, and the bid
board shows it as a live picture, passing the person's clicks and typing
through. The username and password are filled in and "Remember me" ticked
first, so usually all that's left is the check and the sign-in button.

Once they're signed in, the browser's cookies are kept as the login's
session (the same session_enc an automatic sign-in keeps), so later reads
restore it and go straight in without ever seeing the form.

The sign-in happens in this browser, not the person's own, because a
Cloudflare pass belongs to the browser and network that earned it.

One sign-in at a time. Playwright's sync API only works on the thread that
started it, so each sign-in's browser lives on a thread of its own and the
web requests hand it jobs.
"""

import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import browser_crawl
import scraper
import site_agent
import supabase_store

VIEWPORT = {"width": 1100, "height": 760}
IDLE_CLOSE_S = 600          # closed after this long with nobody looking
JOB_TIMEOUT_S = 30
# How long the page has to stay off the sign-in form before it counts as
# signed in — not just the moment between pressing the button and the next page.
SIGNED_IN_AFTER_S = 2

_NAMED_KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "Home", "End", "PageUp", "PageDown",
               "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}
_KEY = re.compile(r"^(?:(?:Control|Shift|Alt)\+)*(?:[A-Za-z0-9]|" + "|".join(_NAMED_KEYS) + r")$")
_SIGN_IN_URL = re.compile(r"log-?[io]n|sign-?in|auth|sso", re.I)
_CODE_FIELD = "input[autocomplete=one-time-code], input[name*=code i], input[id*=code i]"

_lock = threading.Lock()
_current = None


class SignIn:
    def __init__(self, division_id, login):
        self.id = uuid.uuid4().hex
        self.division_id = division_id
        self.login = login
        self.status = "starting"   # starting -> open -> saved | failed | closed
        self.message = None
        self.url = login["url"]
        self.touched = time.time()
        self._jobs = queue.Queue()
        self._saw_form = False
        self._off_form_since = None
        self._ctx = self._page = None
        threading.Thread(target=self._run, daemon=True).start()

    # --- called from the web request threads ---

    def live(self):
        return self.status in ("starting", "open")

    def state(self):
        return {"id": self.id, "status": self.status, "message": self.message, "url": self.url,
                "domain": self.login["domain"], "username": self.login["username"]}

    def call(self, fn):
        """Runs fn(page) on the browser's thread and returns what it returns."""
        if not self.live():
            raise RuntimeError("this sign-in has ended")
        self.touched = time.time()
        done, box = threading.Event(), {}
        self._jobs.put((fn, done, box))
        if not done.wait(JOB_TIMEOUT_S):
            raise RuntimeError("the sign-in browser didn't answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def frame(self):
        return self.call(lambda page: page.screenshot(type="jpeg", quality=70))

    def click(self, x, y):
        x = min(max(int(x), 0), VIEWPORT["width"] - 1)
        y = min(max(int(y), 0), VIEWPORT["height"] - 1)
        self.call(lambda page: page.mouse.click(x, y))

    def type(self, text):
        self.call(lambda page: page.keyboard.type(str(text)[:500]))

    def key(self, name):
        if not _KEY.match(name or ""):
            raise ValueError(f"key {name!r} isn't passed through")
        self.call(lambda page: page.keyboard.press(name))

    def wheel(self, dy):
        dy = max(-2000, min(2000, int(dy)))
        self.call(lambda page: page.mouse.wheel(0, dy))

    def save(self):
        """Keep the session now — for a sign-in the page check didn't catch."""
        self.call(lambda page: self._save())

    def close(self, message=None):
        if self.live():
            self.status, self.message = "closed", message
            self._jobs.put(None)

    # --- the browser's own thread ---

    def _run(self):
        pw = browser = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = _launch(pw)
            self._ctx = browser.new_context(viewport=VIEWPORT, user_agent=site_agent.UA)
            # Same as ConstructConnect's sign-in (adapters.py): a browser that says
            # it's automated can be refused the check even with a person doing it.
            self._ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if self.login.get("session"):
                browser_crawl._restore_session(self._ctx, self.login["session"])
            self._ctx.on("page", self._new_tab)
            self._page = self._ctx.new_page()
            self._open()
            if self.status == "starting":
                self.status = "open"
            while self.live():
                try:
                    job = self._jobs.get(timeout=1)
                except queue.Empty:
                    job = None
                if job:
                    fn, done, box = job
                    try:
                        box["value"] = fn(self._current_page())
                    except Exception as e:
                        box["error"] = e
                    done.set()
                if self.live():
                    self._check_signed_in()
                if self.live() and time.time() - self.touched > IDLE_CLOSE_S:
                    self.status, self.message = "closed", "Closed after 10 minutes with nobody signing in."
        except Exception as e:
            print(f"  ! Sign-in for {self.login['domain']}: {e}", flush=True)
            self.status, self.message = "failed", f"The sign-in browser stopped: {e}"
        finally:
            while not self._jobs.empty():   # anyone still waiting gets an answer
                job = self._jobs.get_nowait()
                if job:
                    job[2]["error"] = RuntimeError("this sign-in has ended")
                    job[1].set()
            for closer in (browser and browser.close, pw and pw.stop):
                try:
                    closer and closer()
                except Exception:
                    pass

    def _new_tab(self, page):
        self._page = page   # a sign-in that opens its own tab (some SSO) is followed there

    def _current_page(self):
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            self._page = pages[-1] if pages else self._ctx.new_page()
        return self._page

    def _open(self):
        """The sign-in page, filled in as far as it can be without pressing the button."""
        page = self._page
        page.goto(self.url, wait_until="domcontentloaded", timeout=browser_crawl.PAGE_TIMEOUT_MS)
        browser_crawl._wait_visible(page, "input[type=password], " + browser_crawl._EMAIL_SELECTOR, 10000)
        pw_input = browser_crawl._visible(page, "input[type=password]")
        user_input = browser_crawl._visible(
            page, browser_crawl._USERNAME_SELECTOR if pw_input else browser_crawl._EMAIL_SELECTOR)
        try:
            if user_input and not (user_input.input_value() or "").strip():
                user_input.fill(self.login["username"])
            if pw_input:
                pw_input.fill(self.login["password"])
                browser_crawl._tick_remember_me(page)
        except Exception as e:
            print(f"  ! Sign-in for {self.login['domain']}: couldn't fill the form in ({e})", flush=True)

    def _check_signed_in(self):
        """Signed in = the sign-in form was there, and now hasn't been for a moment."""
        page = self._current_page()
        try:
            self.url = page.url
            on_form = bool(browser_crawl._visible(page, "input[type=password]")
                           or browser_crawl._visible(page, _CODE_FIELD)
                           or (_SIGN_IN_URL.search(page.url) and browser_crawl._visible(page, browser_crawl._EMAIL_SELECTOR)))
        except Exception:
            return   # mid-navigation
        if on_form:
            self._saw_form, self._off_form_since = True, None
            return
        if not self._saw_form:
            return
        if self._off_form_since is None:
            self._off_form_since = time.time()
        elif time.time() - self._off_form_since >= SIGNED_IN_AFTER_S:
            self._save()

    def _save(self):
        cookies = self._ctx.cookies()
        scraper.keep_login_result(self.login["id"], {"ok": True, "message": None, "session": cookies})
        self.status, self.message = "saved", None
        print(f"  Sign-in for {self.login['domain']} as {self.login['username']}: session saved "
              f"({_lifetimes(cookies, self.login['domain'])})", flush=True)


def _launch(pw):
    args = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
    try:
        # The full browser's own headless mode, closer to a real Chrome than
        # Playwright's default headless shell.
        return pw.chromium.launch(headless=True, channel="chromium", args=args)
    except Exception:
        return pw.chromium.launch(headless=True, args=args)


def _lifetimes(cookies, domain):
    """How long the saved cookies last, for the log — names and dates, never values."""
    out = []
    for c in cookies:
        if not c.get("domain", "").lstrip(".").endswith(domain):
            continue
        exp = c.get("expires") or -1
        out.append(f"{c['name']} until " + (
            datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d") if exp > 0 else "the browser closes"))
    return ", ".join(out) or "no cookies for the site"


def start(division_id, login_id, url=None):
    """Opens a sign-in for one of the division's saved logins (closing any
    other that's open). `url` is where to sign in when the login has no
    sign-in page saved — the listing link that needed it."""
    global _current
    row = next((r for r in supabase_store.load_site_logins(division_id) if r["id"] == login_id), None)
    if row is None:
        raise ValueError("no such login")
    login = scraper.login_from_row(row, url)
    if login is None:
        raise ValueError("the saved login couldn't be decrypted")
    with _lock:
        if _current:
            _current.close("Another sign-in was started.")
        _current = SignIn(division_id, login)
        return _current


def get(division_id, sid):
    s = _current
    return s if s and s.id == sid and s.division_id == division_id else None

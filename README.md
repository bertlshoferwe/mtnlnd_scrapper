# Bid Scout — self-hosted Docker + Supabase

Scans planning/bid sites daily for new construction plans, then reads each
linked PDF/DOCX and flags the ones worth pursuing based on a user-supplied
keyword list, using Claude for semantic matching. Everything runs on your own
Docker server, split into pieces each doing the part it's actually good at:

| Piece | What it does | Why split out |
|---|---|---|
| **app** container | Hosts the dashboard (add/view sites, keywords, results) | A normal long-running Flask app (gunicorn) — no function time limit to work around anymore |
| **scanner** container | Runs the actual daily scan (scraping, downloads, PDF parsing, Claude calls) and a tiny internal API for "Run Now"/"Cancel" | Kept separate from the app so a multi-minute scan can't block dashboard requests, and so it alone needs the heavy Playwright/LibreOffice dependencies |
| **local Supabase** (Postgres) | Stores every division's sites, keywords, and scan results, plus uploaded documents in Storage | Self-hosted via Supabase's own docker-compose, so nothing leaves your network |
| **Watchtower** | Polls GHCR and auto-pulls/restarts the app/scanner images | Means pushing to `main` is enough to deploy — see `.github/workflows/docker-publish.yml` |

The dashboard's "Run Now" button asks the scanner container's internal
`/run-now` endpoint to start `scraper.py` immediately; "Cancel scan" asks its
`/cancel` endpoint to kill that process.

## One-time setup

See `self-host/README.md` for the full walkthrough. In short:

1. **Local Supabase** — follow `self-host/supabase/README.md` to pull the
   official self-hosted compose, generate secrets, start it, apply
   `schema.sql`, and create the `scanned-documents` Storage bucket.
2. **Configure the app stack** — `cp self-host/.env.example self-host/.env`
   and fill in `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` (from step 1), your AI
   provider key (see the table below), and anything else you need.
3. **Push to GitHub** — `docker-publish.yml` builds and pushes
   `ghcr.io/<you>/<repo>-app` and `-scanner` to GHCR on every push to `main`.
4. **On the server**: `docker login ghcr.io` once (a PAT with
   `read:packages` is enough) so Watchtower and `docker compose pull` can
   fetch the (likely private) images, then
   `docker compose -f self-host/docker-compose.yml up -d`.
5. Point Traefik/Cloudflare Tunnel at the `app` service (see the labels in
   `self-host/docker-compose.yml` — adjust to match your actual Traefik
   network/entrypoint names).
6. Open the dashboard and create your first division, add sites/keywords,
   and click **"Run scan now"** to confirm the scanner container picks it up
   (check `docker compose logs -f scanner`).

From here, every push to `main` rebuilds the images; Watchtower picks up the
new ones on its next poll (default every 60s) with no manual redeploy step.

### Choosing an AI provider

This project can use **Anthropic (Claude) or Google Gemini** — pick one via the `AI_PROVIDER` env var, or leave it unset and it auto-picks whichever key is present (Anthropic first, then Gemini). Both plug into the same code path (`ai_provider.py`) — switching later is just changing an env var, no code changes.

| Provider | Free? | Get a key at |
|---|---|---|
| **Anthropic (Claude)** | No — pay-as-you-go from the start, but cheap at this scan volume (Haiku, one call per document) | [console.anthropic.com](https://console.anthropic.com) |
| **Google Gemini** | Yes, genuinely, no card required — but Google may use free-tier prompts/responses to improve their products. Worth weighing if your documents are commercially sensitive | [aistudio.google.com](https://aistudio.google.com) |

## How matching works

Every document goes through two independent passes, merged together:

1. **Literal substring search** — case-insensitive, exact text only, always runs, no API key needed. The deterministic floor.
2. **AI semantic scan** (if an AI provider is configured) — reads the entire document and identifies every keyword substantively discussed, including paraphrases and synonyms literal matching can't see (e.g. "M&A" for "merger"). Runs on every downloaded document, not just already-flagged ones.

**Semantic pre-filter (large keyword lists).** When a division has more than `AI_PREFILTER_SEND_ALL_MAX` keywords (default 60), the semantic pass for a given document doesn't weigh all of them — it weighs the literal-substring hits plus the `AI_PREFILTER_TOP_N` (default 50) keywords whose embeddings are closest to that document. Keyword embeddings are computed once (lazily, on the first scan after a keyword is added) and cached in the `keywords.embedding` column; the ranking is done in Python, so no pgvector setup is needed. This keeps the per-document prompt — and its cost — roughly flat whether the list has 60 keywords or 6,000. Embeddings go through Gemini, so set `GEMINI_API_KEY` even if `AI_PROVIDER=anthropic`; without it the pre-filter falls back to literal hits only.

**Cost implication**: the AI scan runs per document downloaded, not per match — cost scales with scan volume, not hit rate. Both providers use their fast/cheap model tier by default (see `ai_provider.py` for the exact model names, overridable via `ANTHROPIC_MODEL`/`GEMINI_MODEL`).

## Portal adapters

Some bid portals render everything client-side (Angular/React SPAs) and put
documents behind tab navigation or XHR download buttons — the generic HTML
crawl can't see them. For those, `adapters.py` holds per-portal code that
talks to the portal's own API and returns the same document list.

Set a site's **Portal adapter** dropdown in the dashboard (Sites → the adapter
select). When an adapter is chosen the site URL and listing/selector options
are ignored — the adapter knows where to look. Bundled: **UDOT Contractor
Zone** (`udot_masterworks`), which pulls every advertised project's PDFs from
`contractorzone.udot.utah.gov`.

Adding a portal: subclass `SiteAdapter` in `adapters.py`, implement
`find_documents()`, add it to the `_ADAPTER_CLASSES` list.

## Sites learned by AI (no adapter)

Pick **Learned by AI (no adapter)** in a site's Portal adapter dropdown (keep
its URL). On save, the scan worker starts `site_agent.py`: an AI explores the
site — opening pages, clicking tabs, watching the JSON the pages load — and
writes a *recipe* (plain JSON) that reads the bid list and each project's name,
bid date/time, location, documents and plan holders. The site's **review**
tag in Sites opens what it found, with where each value came from:

- ✎ on a wrong value: type the right one, or say where to find it. Saved as a
  correction that every later re-learn must get right.
- ⊘ on a bid that shouldn't be there: it's left out from then on, and the AI
  suggests a rule for others like it ("Skip projects whose Status is Awarded").
- Notes about the site, then **Re-learn**, and **Approve** when it looks right.
- While it learns, every step shows (each page opened, tab clicked, recipe
  tried and what it found), and **Tell it something** sends a message it
  reads before its next step ("Wrong page — the bids are under
  Advertisements"). Messages last for that run; notes last for every run.
- **Edit recipe** opens the recipe as JSON (the AI's latest, or a starter)
  with its format. **Try it** runs it on a few projects in the scan worker
  and shows the result as a normal review to approve — no AI.

Daily scans replay the approved recipe with no AI. If it stops working (finds
nothing, or breaks a correction) the AI re-learns it on its own; if that
doesn't pass, the site is tagged **needs a look**. One site learns at a time,
paced for the Gemini free tier (`LEARN_AI_INTERVAL_S`, default 5s between
calls; `LEARN_MAX_STEPS`, default 40). **Fields to collect** in Sites adds
more fields per division (e.g. Owner) — saved in `project_flags.extra_fields`,
not shown anywhere yet. Needs these columns:

```sql
alter table sites add column if not exists learned jsonb;
alter table project_flags add column if not exists extra_fields jsonb;
```

**Re-scan skipping.** Because an adapter re-lists every advertised project on
every run, the scan worker skips any document URL it has already processed to
a non-failure status (`already_scanned_urls` in `supabase_store.py`) — so a
daily run only downloads and AI-scans genuinely new documents.

## A listing link pasted on a bid

On the bid board, saving a link as a bid's **Original listing** has the scan
worker read it (`listing_import.py`, state in `listing_imports` — run
`migrations/002_listing_imports.sql` once). A site Bid Scout already scans is
read the same way (its portal adapter, AI-learned recipe and saved login);
anything else is opened in a browser, its document links collected and the AI
reads the project name, bid date and time, location and plan holders — and
when the page shows no documents, clicks around for a few steps to find them.
The documents are downloaded and keyword-checked like any scan, into a Bid
Scout project the bid is then linked to, so its Bid Docs and Plan Holders fill
in. A due date or time the bid already has is never changed; a different one
from the listing shows as "The listing says … Use it". Each link is re-read
after the daily scan until its bid date passes, so addenda come in.

**Sites behind a sign-in** (BuildingConnected, PlanHub, SmartBid, ...): each
division keeps one login per domain in the bid board's Settings → Logins
(`site_logins` — run `migrations/003_site_logins.sql` once; encrypted with
`CREDENTIALS_KEY` like site logins, and never sent back to the browser, so a
login can be replaced or removed but not viewed). A link on that domain, or
a scanned site with no login of its own, is read signed in. Two-step sign-ins
(email, then password) work, and the cookies from a good sign-in are kept
(encrypted) so the next read skips signing in. A listing that lands on a
sign-in page fails with "Add a login for …", which opens the form and reads
the link again once it's saved.

**Sign in yourself**: when a site won't take the automatic sign-in (a
Cloudflare "verify you are human" check, like Bidtracer's), the failed
listing's **Sign in yourself** (or Settings → Logins → **Sign in**) shows
the site's sign-in page in the scan worker's own browser, live, with the
username and password filled in and "Remember me" ticked. Once you're
signed in, the session is kept for that login — a site's own login too,
after running `migrations/004_site_login_session.sql` once.

## Vendors

`/vendors/` (in the apps menu next to Bid Scout and the bid board) is a
directory of vendors and their salesmen ("contacts"): "I'm bidding this item
in this state — who do I call?". Run `migrations/006_vendors.sql` once (it
also makes the private `vendor-files` storage bucket). The page is
`vendors/index.html`; its API is `vendors_api.py`, the tables
`vendors_store.py`. The browser never reads the tables directly — RLS is on
with no policies — and every write goes through `_can_edit()`, every admin
call through `_is_admin()`, so sign-in, when it comes, only changes those two.

- **Contacts and vendors are shared by every division**; what a vendor sells
  (`vendor_items`) is kept per division, and a vendor shows in the divisions
  it was added to or has items in. A contact's service area is nationwide,
  states, or named regions inside a state — or not set, which stays visible
  ("Coverage not set") when a job-state filter is on.
- **Search** runs in the database (`vendor_search()`): every word has to match
  the vendor's name or website, one of its items in the division, or one of
  its contacts (name, title, email, phone, state or region).
- **Business cards** are read on the phone with Tesseract.js (the photo is
  shrunk, read, then always shown in the review form — nothing is saved until
  you press Save, and the service area is set by hand). The photo is kept
  with the contact. `VENDORS_CARD_READER` (default `browser`) is the switch
  for another reader later; one only has to return the same fields.
- **Items** are pulled from every vendor's website once a week (Sunday 2 AM
  Mountain, `PULL_DAY` / `PULL_HOUR`), and from a vendor's right away when it
  gets a website, by the scan worker
  (`vendor_catalog.py`): a store's product feed, then schema.org product
  markup, then the AI reading the pages, or an uploaded PDF catalog. Each
  division's Bid Scout keywords decide which products belong in it. The first
  pull goes straight in; later changes wait in **Catalog review** (triple-click
  the "Vendors" title) for Approve / Reject, next to Failed pulls and vendor
  websites. robots.txt is respected, requests are spaced out, and sites
  behind a sign-in are skipped. The site's logo is pulled too, unless one was
  uploaded by hand.
- Installable as an app (manifest and a service worker that keeps only the
  page itself — no data offline).

Tests: `python -m unittest tests.test_vendors` (needs the scanner's packages,
e.g. inside the scanner container).

## Choosing between Anthropic and Gemini

Both plug into the exact same three functions (semantic keyword matching, AI job-link identification, daily summary) via `ai_provider.py`'s common `.complete(prompt, max_tokens)` interface — the rest of the codebase doesn't know or care which one is active. A few practical notes beyond the cost table in Setup step 3:

- **Switching providers** is a one-line change: update `AI_PROVIDER` (or just add/remove the relevant API key) in `self-host/.env` and restart the scanner container — provider selection only affects `scraper.py`.
- **Quality**: both are strong at the structured-JSON-following this pipeline depends on. If a provider's response can't be parsed as valid JSON, the code falls back to treating that document as "AI scan found nothing" for that call — it never crashes the run, just silently does less on that one document. Check `docker compose logs scanner` for `unparseable result` warnings if matches seem to be missing.
- **Gemini's model name churns faster than Anthropic's** — Google renames/retires aliases often. If `ai_provider.py`'s default (`gemini-3.6-flash`) stops working, check [ai.google.dev](https://ai.google.dev) for the current model list and set `GEMINI_MODEL` to override. The semantic pre-filter also needs `GEMINI_API_KEY` for embeddings even when `AI_PROVIDER=anthropic`.

## Known limitations (carried over from the core scanning logic)

- Without Firecrawl configured, only follows document links visible in a plain page fetch — no JS-rendered content, no clicking tabs, and some sites will block the request outright.
- Tab clicking waits a fixed 1.5 seconds after each click.
- Listing crawl goes exactly one level deep (listing page → job page → documents).
- Legacy `.doc` (pre-2007 binary Word) files are detected but not text-extracted.
- Keyword matching combines literal + AI semantic passes but isn't guaranteed to catch everything.
- AI job-link identification is capped at the first 300 links per listing page; the AI semantic scan is capped at ~60,000 characters of document text per call.
- Every run logs new rows even if a document is unchanged from a previous run — nothing dedupes.

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

**Re-scan skipping.** Because an adapter re-lists every advertised project on
every run, the scan worker skips any document URL it has already processed to
a non-failure status (`already_scanned_urls` in `supabase_store.py`) — so a
daily run only downloads and AI-scans genuinely new documents.

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

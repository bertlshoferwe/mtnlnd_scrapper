# Document Scanner — Vercel + Supabase + GitHub Actions

Scans configured sites daily for keyword matches in linked PDF/DOCX documents,
using Claude for semantic matching. This edition splits the work across three
services, each doing the part it's actually good at:

| Service | What it does | Why it, not something else |
|---|---|---|
| **Vercel** | Hosts the dashboard (add/view sites, keywords, results) | Free, fast, zero server maintenance — but functions time out in ~10s on Hobby, so it never does the actual scanning |
| **Supabase** (Postgres) | Stores every division's sites, keywords, and scan results | Vercel functions have no persistent filesystem — this replaces the local files/xlsx entirely |
| **GitHub Actions** | Runs the actual daily scan (scraping, downloads, PDF parsing, Claude calls) | Jobs get up to 6 hours, not 10 seconds, and it's a full Ubuntu VM — so LibreOffice (real DOCX page numbers) still works, unlike on Vercel |

The dashboard's "Run Now" button doesn't scan anything itself — it asks
GitHub to run the same workflow immediately, via GitHub's API.

## One-time setup

### 1. Create the Supabase project

1. Go to [supabase.com](https://supabase.com) → New Project. Free tier is enough for this.
2. Once it's created: **Project Settings → API** → copy the **Project URL** and the **`service_role` secret key** (not the `anon` key — this app needs to bypass Row Level Security since it's trusted server-side code, not browser code). Keep the service_role key secret; it has full database access.
3. **SQL Editor → New query** → paste the contents of `schema.sql` from this project → Run. This creates all six tables.

### 2. Push this project to a GitHub repo

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 3. Choose an AI provider and add GitHub Actions secrets

This project can use **Anthropic (Claude) or Google Gemini** — pick one via the `AI_PROVIDER` secret, or leave it unset and it auto-picks whichever key you've added (Anthropic first, then Gemini). Both plug into the same code path (`ai_provider.py`) — switching later is just changing which secret is set, no code changes.

| Provider | Free? | Get a key at |
|---|---|---|
| **Anthropic (Claude)** | No — pay-as-you-go from the start, but cheap at this scan volume (Haiku, one call per document) | [console.anthropic.com](https://console.anthropic.com) |
| **Google Gemini** | Yes, genuinely, no card required — but Google may use free-tier prompts/responses to improve their products. Worth weighing if your documents are commercially sensitive | [aistudio.google.com](https://aistudio.google.com) |

In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `AI_PROVIDER` — `anthropic` or `gemini` (optional if you're only adding one key below)
- Whichever of `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` matches your chosen provider — see "How matching works" below for why this matters
- `FIRECRAWL_API_KEY` (optional — get one at [firecrawl.dev](https://firecrawl.dev) if you need JS-rendered pages or anti-bot handling)

You only need to add the secret for the provider you're actually using — the workflow passes both through as environment variables regardless, but `ai_provider.py` only initializes the one that's configured.

### 4. Set your schedule

Open `.github/workflows/daily-scan.yml` and edit the `cron` line. **GitHub Actions cron is UTC only** and, like Vercel Hobby, only guarantees the job starts sometime within that hour, not to the exact minute. Convert your local time to UTC — e.g., 1am Mountain Time (MDT, UTC-6) is `0 7 * * *`; during MST (UTC-7) it's `0 8 * * *`. Commit and push the change.

This is the **one shared schedule for every division** — Vercel Hobby doesn't support per-division dynamic scheduling (that would need a persistent process, which is exactly what Vercel doesn't offer), so all divisions scan together in one workflow run, looping through them in sequence.

### 5. Create a GitHub Personal Access Token (for the dashboard's "Run Now" button)

1. GitHub → **Settings** (your account, not the repo) → **Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Scope it to this one repository, with **Actions: Read and write** permission.
3. Copy the token — you'll paste it into Vercel next.

### 6. Deploy to Vercel

1. [vercel.com](https://vercel.com) → **Add New → Project** → import your GitHub repo. Vercel auto-detects the Python app in `api/index.py` — no build configuration needed.
2. Before or after the first deploy, go to **Project Settings → Environment Variables** and add:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `GITHUB_TOKEN` — the token from step 5
   - `GITHUB_OWNER` — your GitHub username or org
   - `GITHUB_REPO` — the repo name
   - `GITHUB_WORKFLOW_FILE` — `daily-scan.yml` (matches the filename in `.github/workflows/`)
   - `GITHUB_REF` — `main` (or whatever your default branch is)
   - `DISPLAY_SCHEDULE_UTC` — optional, e.g. `08:00 UTC`, just cosmetic text shown on the dashboard
3. Redeploy if you added the environment variables after the first deploy (**Deployments → ⋯ → Redeploy**) so the function picks them up.
4. Open the URL Vercel gives you (`https://<project>.vercel.app`). You should see the dashboard with one empty division ready to go, or create your first one with "+ New division".

### 7. Add your divisions, sites, and keywords

Everything from here is through the dashboard: create a division per team, add sites (with the "Advanced" toggle for listing-page CSS selectors or AI-detected job links), and add keywords. This writes straight to Supabase — the next scheduled GitHub Actions run (or a manual "Run Now") will pick it up.

### 8. Test it

Click **"Run scan now"** on any division. This should:
- Return almost instantly (Vercel just tells GitHub to start the job — it doesn't wait for it)
- Show up under the repo's **Actions** tab within a few seconds, running
- Update the dashboard's Status card to "success" (or show the error) once it finishes — the dashboard polls every few seconds

If "Run Now" returns an error about `GITHUB_TOKEN`/`GITHUB_OWNER`/`GITHUB_REPO`, double-check those three Vercel environment variables and redeploy.

## How matching works

Every document goes through two independent passes, merged together:

1. **Literal substring search** — case-insensitive, exact text only, always runs, no API key needed. The deterministic floor.
2. **AI semantic scan** (if an AI provider is configured) — reads the entire document and identifies every keyword substantively discussed, including paraphrases and synonyms literal matching can't see (e.g. "M&A" for "merger"). Runs on every downloaded document, not just already-flagged ones.

**Cost implication**: the AI scan runs per document downloaded, not per match — cost scales with scan volume, not hit rate. Both providers use their fast/cheap model tier by default (see `ai_provider.py` for the exact model names, overridable via `ANTHROPIC_MODEL`/`GEMINI_MODEL`).

## Choosing between Anthropic and Gemini

Both plug into the exact same three functions (semantic keyword matching, AI job-link identification, daily summary) via `ai_provider.py`'s common `.complete(prompt, max_tokens)` interface — the rest of the codebase doesn't know or care which one is active. A few practical notes beyond the cost table in Setup step 3:

- **Switching providers** is a one-line change: update the `AI_PROVIDER` secret (or just add/remove the relevant API key secret) and re-run — no code or redeploy needed on the Vercel side, since provider selection only affects the GitHub Actions scan worker.
- **Quality**: both are strong at the structured-JSON-following this pipeline depends on. If a provider's response can't be parsed as valid JSON, the code falls back to treating that document as "AI scan found nothing" for that call — it never crashes the run, just silently does less on that one document. Check the Actions logs for `unparseable result` warnings if matches seem to be missing.
- **Gemini's model name churns faster than Anthropic's** — Google renames/retires free-tier aliases more often. If `ai_provider.py`'s default (`gemini-2.5-flash`) stops working, check [ai.google.dev](https://ai.google.dev) for the current model list and set `GEMINI_MODEL` to override.

## Known differences from the local/Docker version of this project

- **No per-division custom schedule.** One shared cron in `daily-scan.yml` for everyone, per your Hobby-plan constraint. If you later upgrade to Vercel Pro or move off Vercel, per-division scheduling could come back (e.g. via a small always-on worker again).
- **No local `.xlsx` file that persists between runs.** The Download button on the dashboard *builds* a fresh `.xlsx` on the spot from whatever's in Supabase — there's no file sitting on disk anywhere. Functionally equivalent, just generated on demand instead of accumulated.
- **Results are browsable in the dashboard, not just downloadable.** The Results card shows document/match counts, the latest AI-written daily summary, and a searchable, filterable, paginated table of every logged row — you don't have to open the spreadsheet just to check whether anything matched today.
- **"Run Now" triggers GitHub, not an immediate local scan.** There's a few seconds of latency between clicking the button and the scan actually starting (GitHub has to schedule the Actions runner), unlike the old design where a background thread started instantly.
- **DOCX page numbers still work.** GitHub Actions' `ubuntu-latest` runners are full VMs — LibreOffice installs and runs there exactly like it did in the Docker/local versions. This was the one piece that genuinely couldn't run on Vercel itself.
- **Deleting a division is permanent.** Supabase's `ON DELETE CASCADE` removes all its sites, keywords, and historical results immediately — there's no "data preserved on disk" safety net like the local-file version had. The dashboard still confirms before deleting, but there's no undo.

## Known limitations (carried over from the core scanning logic)

- Without Firecrawl configured, only follows document links visible in a plain page fetch — no JS-rendered content, no clicking tabs, and some sites will block the request outright.
- Tab clicking waits a fixed 1.5 seconds after each click.
- Listing crawl goes exactly one level deep (listing page → job page → documents).
- Legacy `.doc` (pre-2007 binary Word) files are detected but not text-extracted.
- Keyword matching combines literal + AI semantic passes but isn't guaranteed to catch everything.
- AI job-link identification is capped at the first 300 links per listing page; the AI semantic scan is capped at ~60,000 characters of document text per call.
- Every run logs new rows even if a document is unchanged from a previous run — nothing dedupes.

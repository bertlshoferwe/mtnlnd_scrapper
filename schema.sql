-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query)
-- to create every table this app needs.

create table if not exists divisions (
  id text primary key,
  name text not null,
  created_at timestamptz default now()
);

create table if not exists sites (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  name text not null,
  url text not null,
  listing jsonb,          -- null = not a listing site; {} = AI-detected; {"link_selector": "..."} = manual
  tabs jsonb,              -- null = no tabs; otherwise a list of {"label": "...", "selector": "..."}
  adapter text,            -- null = generic HTML crawl; otherwise a key from adapters.py (portal-specific API)
  active boolean not null default true,  -- false = kept in the list but skipped by every scan
  -- Drag-to-reorder position in the dashboard — also the order sites are
  -- scanned in (see scraper.py's _scan_division, which just iterates
  -- load_sites() in the order it returns). Defaults to id so a fresh site
  -- lands at the end of the list.
  sort_order bigint,
  -- Login, for a site whose documents sit behind a login form. Only used by
  -- the generic browser crawl (adapters and listing/tabs crawls don't use a
  -- real browser session). login_password_enc is Fernet-encrypted with
  -- CREDENTIALS_KEY (see credentials.py) — never sent to the dashboard.
  login_username text,
  login_password_enc text,
  login_url text,          -- login page, if different from url; null = use url
  -- Outcome of the most recent login attempt (set by the scan worker right
  -- after it tries to log in) — null until the site with a login has been
  -- scanned at least once.
  login_last_ok boolean,
  login_last_checked_at timestamptz,
  login_last_error text,
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table sites add column if not exists adapter text;
--   alter table sites add column if not exists active boolean not null default true;
--   alter table sites add column if not exists login_username text;
--   alter table sites add column if not exists login_password_enc text;
--   alter table sites add column if not exists login_url text;
--   alter table sites add column if not exists login_last_ok boolean;
--   alter table sites add column if not exists login_last_checked_at timestamptz;
--   alter table sites add column if not exists login_last_error text;
--   alter table sites add column if not exists sort_order bigint;
--   update sites set sort_order = id where sort_order is null;

create table if not exists keywords (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  keyword text not null,
  -- Cached embedding vector (JSON array of floats) for the semantic
  -- pre-filter in scraper.py. Populated lazily on the first scan after a
  -- keyword is added; NULL until then. No pgvector extension needed — the
  -- similarity ranking is done in Python over a division's (small) list.
  embedding jsonb,
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table keywords add column if not exists embedding jsonb;

-- One row per document found during a run.
create table if not exists scan_results (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  run_date timestamptz not null,
  site text,
  document_url text,       -- direct link to the file (may be a synthetic key for JS-captured downloads)
  source_url text,         -- the project/job page the file belongs to, for the "open project" link
  -- Path in the "scanned-documents" Supabase Storage bucket, set only for
  -- documents with no real per-document URL to re-fetch from later (browser-
  -- captured JS downloads, e.g. ConstructConnect) — the raw bytes are
  -- persisted once at scan time since they're only ever available then.
  -- NULL for portal adapters (UDOT/ITD/WYDOT), whose document_url is a real,
  -- live-fetchable link the PDF proxy re-fetches on demand instead.
  storage_path text,
  -- True forever once a row is first inserted with storage_path set, even
  -- after the retention job later clears storage_path back to NULL —
  -- distinguishes "cached copy expired, needs a live re-fetch" from "this
  -- adapter never stores anything, document_url is always live-fetchable."
  is_cached_document boolean not null default false,
  filename text,
  matched_keywords text,
  match_count int default 0,
  keyword_locations text,
  status text,
  ai_notes text,
  -- "Still advertised?" tracking. last_seen_at is the run_date of the most
  -- recent scan where the source site still listed this document; misses is
  -- how many consecutive reconciles of its site have gone by without it.
  -- A project whose every (non-failed) document has misses >= 2 is treated
  -- as "no longer listed" and hidden from the dashboard by default.
  last_seen_at timestamptz,
  misses int default 0,
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table scan_results add column if not exists source_url text;
--   alter table scan_results add column if not exists last_seen_at timestamptz;
--   alter table scan_results add column if not exists misses int default 0;
--   alter table scan_results add column if not exists storage_path text;
--   alter table scan_results add column if not exists is_cached_document boolean not null default false;
-- Also needs a private Storage bucket named "scanned-documents" (Storage >
-- New bucket in the Supabase dashboard, "Public bucket" left OFF) — the app
-- creates it automatically on the next scan if missing, but you can create
-- it by hand too.
create index if not exists scan_results_division_idx on scan_results(division_id, run_date desc);

-- Per-project extras (keyed by the scan_results.site value): the user-set
-- "done" flag, and the bid-opening date pulled from the source portal.
create table if not exists project_flags (
  division_id text not null references divisions(id) on delete cascade,
  project_key text not null,
  done boolean not null default false,
  bid_date date,
  -- Raw display string for portals that show a time alongside the
  -- bid-opening date (e.g. "10:00am MT") — so far only ConstructConnect.
  -- Purely cosmetic (appended after bid_date wherever it's shown); not used
  -- for date comparisons/sorting, so it doesn't need to be a real time type.
  bid_time text,
  -- "New document" notification watermark. A project (that has matched
  -- keywords and was first seen on an earlier run) is flagged as updated
  -- when it has a file whose run_date is newer than the project's first
  -- run_date AND newer than docs_ack_through. Hitting "Mark done" sets
  -- docs_ack_through = now(), acknowledging every file seen so far; the flag
  -- clears until the next new document. NULL = nothing acknowledged yet.
  docs_ack_through timestamptz,
  -- ConstructConnect-only: the results grid's own "Last Updated" display
  -- string (e.g. "Oct 4, 2026") as of the last time this project was
  -- actually opened/downloaded. Keyed by the project's numeric
  -- ConstructConnect id here (not "SiteName — label" like bid_date/bid_time
  -- above), since it's purely an internal watermark the adapter checks
  -- before re-opening a project, not something displayed. Unchanged since
  -- last scan -> skip re-downloading it entirely (see adapters.py's
  -- _scan_results_page) instead of re-fetching all ~700+ scoped projects
  -- from scratch every single run.
  cc_last_updated text,
  -- The Jira issue key created for this project via "Add to Jira" (e.g.
  -- "GEO-23"), or NULL if it hasn't been filed. Set once and never cleared
  -- automatically, so the dashboard button can switch to "View in Jira"
  -- instead of letting a project be filed twice.
  jira_key text,
  -- Plan holders (companies that pulled plans) for portals that publish them
  -- — UDOT, WYDOT, ITD (via QuestCDN), ConstructConnect. A JSON array of
  -- {company, role, contact, title, emails[], phone, address, city, state,
  -- zip, cert}, replaced wholesale on every scan (see
  -- scraper._save_plan_holders). NULL = never fetched for this project.
  plan_holders jsonb,
  plan_holders_at timestamptz,
  -- Which results tab the project is in: 'jira' (added to Jira), 'not_fit'
  -- (passed on, with one of the one-click reasons in
  -- supabase_store.NOT_FIT_REASONS + an optional note), or NULL (To Review).
  -- A 'not_fit' project shows in To Review again when a newer matching
  -- document arrives after decided_at. `done` above is kept in step
  -- (decision is not null) but nothing reads it any more.
  decision text,
  decision_reason text,
  decision_note text,
  decided_at timestamptz,
  -- AI suggestion for a project still in To Review (see fit_predictor.py):
  -- verdict 'fit' / 'not_fit', the likely Not-a-fit reason, 0-100
  -- confidence, one plain sentence, when it was made and how many past
  -- decisions it was based on (re-judged once that pool grows enough).
  fit_verdict text,
  fit_reason text,
  fit_confidence int,
  fit_explanation text,
  fit_predicted_at timestamptz,
  fit_basis_count int,
  updated_at timestamptz default now(),
  primary key (division_id, project_key)
);
-- Migration for existing databases:
--   create table above, then
--   alter table project_flags add column if not exists bid_date date;
--   alter table project_flags add column if not exists docs_ack_through timestamptz;
--   alter table project_flags add column if not exists bid_time text;
--   alter table project_flags add column if not exists cc_last_updated text;
--   alter table project_flags add column if not exists jira_key text;
--   alter table project_flags add column if not exists plan_holders jsonb;
--   alter table project_flags add column if not exists plan_holders_at timestamptz;
--   alter table project_flags add column if not exists decision text;
--   alter table project_flags add column if not exists decision_reason text;
--   alter table project_flags add column if not exists decision_note text;
--   alter table project_flags add column if not exists decided_at timestamptz;
--   alter table project_flags add column if not exists fit_verdict text;
--   alter table project_flags add column if not exists fit_reason text;
--   alter table project_flags add column if not exists fit_confidence int;
--   alter table project_flags add column if not exists fit_explanation text;
--   alter table project_flags add column if not exists fit_predicted_at timestamptz;
--   alter table project_flags add column if not exists fit_basis_count int;
--   alter table project_decisions add column if not exists embedding jsonb;
--   -- one-time: carry the old Done / Jira flags over to the new tabs
--   update project_flags set decision = 'jira', decided_at = updated_at
--     where jira_key is not null and decision is null;
--   update project_flags set decision = 'not_fit', decision_reason = 'Unknown (from before)',
--     decided_at = updated_at where done and decision is null;

-- Every Jira / Not a fit / back-to-review decision, append-only, with a
-- snapshot of the project at that moment (keywords and pages per matched
-- file, AI notes, bid timing, plan holders — see
-- supabase_store.build_project_snapshot). This is the history the AI will
-- learn "worth bidding vs. just information" from, so rows are never
-- updated or deleted, and the snapshot survives scan data and cached files
-- aging out. source = 'user', or 'jira_deleted' when a Jira issue vanished
-- and the project went back to review on its own.
create table if not exists project_decisions (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  project_key text not null,
  decision text not null,   -- 'jira' | 'not_fit' | 'review'
  reason text,
  note text,
  source text not null default 'user',
  snapshot jsonb,
  -- Cached embedding of the snapshot, filled in lazily by fit_predictor.py
  -- (Gemini only) so each decision is embedded once.
  embedding jsonb,
  decided_at timestamptz not null default now()
);
create index if not exists project_decisions_division_idx
  on project_decisions(division_id, decided_at desc);

-- One row per run, whether or not anything matched. Powers the Daily Summary sheet equivalent.
create table if not exists daily_summaries (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  run_date timestamptz not null,
  summary text,
  created_at timestamptz default now()
);

-- One row per scan run, for the dashboard's Status card.
create table if not exists scan_runs (
  id bigint generated always as identity primary key,
  division_id text not null references divisions(id) on delete cascade,
  started_at timestamptz not null,
  finished_at timestamptz,
  status text,             -- 'running' | 'success' | 'error: ...'
  progress_done int default 0,     -- documents processed in the CURRENT site
  progress_total int default 0,    -- documents in the current site (0 = still discovering)
  progress_label text,             -- current phase, e.g. "Scanning UDOT"
  progress_site_i int default 0,   -- current site number
  progress_site_n int default 0,   -- total sites this run
  progress_overall int default 0,  -- documents scanned across all sites so far
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table scan_runs add column if not exists progress_done int default 0;
--   alter table scan_runs add column if not exists progress_total int default 0;
--   alter table scan_runs add column if not exists progress_label text;
--   alter table scan_runs add column if not exists progress_site_i int default 0;
--   alter table scan_runs add column if not exists progress_site_n int default 0;
--   alter table scan_runs add column if not exists progress_overall int default 0;
create index if not exists scan_runs_division_idx on scan_runs(division_id, started_at desc);

-- Row Level Security: this app talks to Supabase using the service_role key,
-- which bypasses RLS entirely, from server-side code only (Vercel functions,
-- GitHub Actions) — never from the browser. Enabling RLS with no policies
-- is a safety net in case the anon/public key is ever used by mistake; it
-- blocks all access via that key while leaving service_role access untouched.
alter table divisions enable row level security;
alter table sites enable row level security;
alter table keywords enable row level security;
alter table scan_results enable row level security;
alter table daily_summaries enable row level security;
alter table scan_runs enable row level security;
alter table project_flags enable row level security;
alter table project_decisions enable row level security;

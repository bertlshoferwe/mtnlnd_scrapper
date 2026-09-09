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
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table sites add column if not exists adapter text;
--   alter table sites add column if not exists active boolean not null default true;

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
create index if not exists scan_results_division_idx on scan_results(division_id, run_date desc);

-- Per-project extras (keyed by the scan_results.site value): the user-set
-- "done" flag, and the bid-opening date pulled from the source portal.
create table if not exists project_flags (
  division_id text not null references divisions(id) on delete cascade,
  project_key text not null,
  done boolean not null default false,
  bid_date date,
  -- "New document" notification. update_since is set by a scan when a new file
  -- shows up on a project that was already being tracked (see
  -- supabase_store.apply_document_updates): every file whose run_date is >=
  -- update_since is shown as NEW. reopened_at is set when that project had been
  -- marked done — it's flipped back to not-done and shown in a distinct colour.
  -- Both clear when the user marks the project done (acknowledging the update).
  update_since timestamptz,
  reopened_at timestamptz,
  updated_at timestamptz default now(),
  primary key (division_id, project_key)
);
-- Migration for existing databases:
--   create table above, then
--   alter table project_flags add column if not exists bid_date date;
--   alter table project_flags add column if not exists update_since timestamptz;
--   alter table project_flags add column if not exists reopened_at timestamptz;

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

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
  created_at timestamptz default now()
);
-- Migration for existing databases:
--   alter table sites add column if not exists adapter text;

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
  document_url text,
  filename text,
  matched_keywords text,
  match_count int default 0,
  keyword_locations text,
  status text,
  ai_notes text,
  created_at timestamptz default now()
);
create index if not exists scan_results_division_idx on scan_results(division_id, run_date desc);

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
  created_at timestamptz default now()
);
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

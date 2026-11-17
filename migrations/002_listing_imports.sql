-- Links pasted on a bid ("Original listing"): the scan worker reads each one
-- (listing_import.py) into a Bid Scout project — downloads and keyword-checks
-- its documents, saves its bid date/time, location and plan holders — and
-- links the bid to it. Re-read nightly until the bid date passes, so addenda
-- and new plan holders come in. One row per bid. Safe to run more than once.

create table if not exists listing_imports (
  id uuid primary key default gen_random_uuid(),
  division_id text not null references divisions(id) on delete cascade,
  bid_id text not null unique,
  url text not null,
  status text not null default 'queued',   -- queued | reading | done | failed
  message text,                            -- what went wrong, or a note about the result
  progress jsonb,                          -- the last few lines of what it's doing
  project_key text,                        -- the Bid Scout project it read the listing into
  result jsonb,                            -- {name, files, matched, bid_date, bid_time, location, holders, conflict}
  recheck boolean not null default true,   -- re-read nightly until the bid date passes
  app_url text,                            -- the dashboard's address, for the bid's link back to the project
  last_read_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists listing_imports_division_idx on listing_imports(division_id);
alter table listing_imports enable row level security;
alter table listing_imports add column if not exists app_url text;

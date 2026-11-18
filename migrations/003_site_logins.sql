-- Logins for sites whose documents sit behind a sign-in (BuildingConnected,
-- PlanHub, SmartBid, ...), one per domain per division. Used whenever a link
-- on that domain is read — a listing pasted on a bid, or a scanned site with
-- no login of its own. Write-only from the dashboard: a login is replaced or
-- removed, never shown. password_enc and session_enc are Fernet-encrypted
-- with CREDENTIALS_KEY (see credentials.py); session_enc is the browser's
-- cookies after the last good sign-in, so the next read can skip signing in
-- (and the site's "new device" checks). Safe to run more than once.

create table if not exists site_logins (
  id uuid primary key default gen_random_uuid(),
  division_id text not null references divisions(id) on delete cascade,
  domain text not null,                    -- buildingconnected.com: also covers app.buildingconnected.com
  username text not null,
  password_enc text not null,
  login_url text,                          -- sign-in page; null = the link being read
  session_enc text,
  last_ok boolean,                         -- outcome of the most recent sign-in, null until tried
  last_checked_at timestamptz,
  last_error text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (division_id, domain)
);
alter table site_logins enable row level security;

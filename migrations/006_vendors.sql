-- Vendors (vendors/index.html, served at /vendors/): a directory of vendors and
-- the salesmen ("contacts") at each, shared by every division, with what each
-- vendor sells kept per division. Only the app server reads and writes these
-- (with the service key; RLS is on with no policies, so the browser's anon key
-- gets nothing) — the permission checks live there, in vendors_api.py.
--
-- Divisions are Bid Scout's (divisions.id). There's no foreign key to them:
-- deleting a division in Bid Scout leaves its vendor rows in place, and every
-- read is for a division that exists, so they're hidden rather than lost.
-- Safe to run more than once.

create extension if not exists pg_trgm;

create table if not exists vendors (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  website text,
  logo_path text,                          -- in the private vendor-files bucket
  logo_source text check (logo_source in ('site', 'card', 'manual')),
  logo_pulled_at timestamptz,
  deleted_at timestamptz,                  -- soft delete: hidden everywhere, kept for the audit log
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Which divisions a vendor shows in. A vendor added in a division (by hand, or
-- with a contact) is listed there even before it has any items.
create table if not exists vendor_divisions (
  vendor_id uuid not null references vendors(id) on delete cascade,
  division_id text not null,
  created_at timestamptz default now(),
  primary key (vendor_id, division_id)
);

create table if not exists vendor_contacts (
  id uuid primary key default gen_random_uuid(),
  vendor_id uuid not null references vendors(id) on delete cascade,
  name text not null,
  title text,
  phone text,
  email text,                              -- stored lowercased
  nationwide boolean not null default false,
  card_image_path text,                    -- the scanned card, in vendor-files
  created_by text,
  deleted_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- A contact's service area: one row per state, and one per named region in a
-- state (region_label null = the whole state). No rows and nationwide false =
-- "Coverage not set", which the page shows as such.
create table if not exists vendor_contact_areas (
  id bigint generated always as identity primary key,
  contact_id uuid not null references vendor_contacts(id) on delete cascade,
  state_code text not null check (state_code ~ '^[A-Z]{2}$'),
  region_label text
);

create table if not exists vendor_items (
  id uuid primary key default gen_random_uuid(),
  vendor_id uuid not null references vendors(id) on delete cascade,
  division_id text not null,
  name text not null,
  source text not null check (source in ('scrape', 'manual', 'pdf')),
  first_seen_at timestamptz default now(),
  last_seen_at timestamptz default now(),
  active boolean not null default true
);
create unique index if not exists vendor_items_one_name
  on vendor_items (vendor_id, division_id, lower(name));

create table if not exists vendor_scrape_runs (
  id uuid primary key default gen_random_uuid(),
  vendor_id uuid not null references vendors(id) on delete cascade,
  started_at timestamptz default now(),
  finished_at timestamptz,
  status text check (status in ('running', 'ok', 'failed')),
  failure_reason text,
  method text check (method in ('feed', 'schema_org', 'llm', 'pdf'))
);

-- The review queue: what a pull found changed, waiting for an admin.
create table if not exists vendor_catalog_changes (
  id uuid primary key default gen_random_uuid(),
  vendor_id uuid not null references vendors(id) on delete cascade,
  division_id text not null,
  kind text not null check (kind in ('added', 'removed')),
  item_name text not null,
  run_id uuid references vendor_scrape_runs(id) on delete set null,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  created_at timestamptz default now()
);

create table if not exists vendor_audit_log (
  id bigint generated always as identity primary key,
  actor text,
  action text not null,                    -- create / update / delete / approve / reject
  table_name text not null,
  row_id text,
  diff jsonb,
  at timestamptz default now()
);

-- Search: substring matches (ILIKE) use these trigram indexes.
create index if not exists vendors_name_trgm on vendors using gin (lower(name) gin_trgm_ops);
create index if not exists vendors_website_trgm on vendors using gin (lower(coalesce(website, '')) gin_trgm_ops);
create index if not exists vendor_contacts_text_trgm on vendor_contacts using gin
  (lower(name || ' ' || coalesce(title, '') || ' ' || coalesce(email, '') || ' ' || coalesce(phone, '')) gin_trgm_ops);
create index if not exists vendor_items_name_trgm on vendor_items using gin (lower(name) gin_trgm_ops);
create index if not exists vendor_contacts_vendor on vendor_contacts (vendor_id);
create index if not exists vendor_items_vendor_div on vendor_items (vendor_id, division_id);
create index if not exists vendor_contact_areas_contact on vendor_contact_areas (contact_id);
create index if not exists vendor_changes_pending on vendor_catalog_changes (status, vendor_id);
create index if not exists vendor_runs_vendor on vendor_scrape_runs (vendor_id, started_at desc);

-- The vendors in a division that match every word of a search: each word has
-- to turn up somewhere in the vendor — its name or web address, one of its
-- active items in that division, or one of its contacts (name, title, email,
-- phone, a state or region they cover, or "nationwide"). No words = every
-- vendor in the division.
create or replace function vendor_search(p_division text, p_words text[])
returns setof uuid language sql stable as $$
  select v.id
  from vendors v
  where v.deleted_at is null
    and (exists (select 1 from vendor_divisions d where d.vendor_id = v.id and d.division_id = p_division)
         or exists (select 1 from vendor_items i where i.vendor_id = v.id and i.division_id = p_division and i.active))
    and not exists (
      select 1 from unnest(coalesce(p_words, '{}')) w
      where not (
        lower(v.name) like '%' || w || '%'
        or lower(coalesce(v.website, '')) like '%' || w || '%'
        or exists (select 1 from vendor_items i
                   where i.vendor_id = v.id and i.division_id = p_division and i.active
                     and lower(i.name) like '%' || w || '%')
        or exists (select 1 from vendor_contacts c
                   where c.vendor_id = v.id and c.deleted_at is null
                     and (lower(c.name || ' ' || coalesce(c.title, '') || ' ' || coalesce(c.email, '') || ' ' || coalesce(c.phone, '')) like '%' || w || '%'
                          or (c.nationwide and 'nationwide' like '%' || w || '%')
                          or exists (select 1 from vendor_contact_areas a
                                     where a.contact_id = c.id
                                       and (lower(a.state_code) = w or lower(coalesce(a.region_label, '')) like '%' || w || '%'))))
      )
    )
$$;

do $$
declare t text;
begin
  foreach t in array array['vendors', 'vendor_divisions', 'vendor_contacts', 'vendor_contact_areas', 'vendor_items',
                           'vendor_scrape_runs', 'vendor_catalog_changes', 'vendor_audit_log'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

-- Logos, scanned cards and uploaded PDF catalogs. Private: the app server
-- hands them out.
insert into storage.buckets (id, name, public) values ('vendor-files', 'vendor-files', false)
on conflict (id) do nothing;

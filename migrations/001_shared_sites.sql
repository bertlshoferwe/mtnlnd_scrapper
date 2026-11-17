-- Shared sites: one list of sites for every division. Each division turns a
-- site on or off and keeps its own order (division_sites); the site itself —
-- URL, login, AI-learned recipe — is kept once. Sites can also be requested
-- (sites.status = 'requested') and added to the list from the hidden menu.
--
-- Run step 1 first and read it: those copies get merged into one site.
-- Then run step 2 (one transaction). Safe to run step 2 more than once.


-- ---------------------------------------------------------------------------
-- Step 1 — PREVIEW (read-only). Each row is one URL saved in more than one
-- division. The first id in `ids` is the copy that's kept (an approved AI
-- recipe beats a working login beats a saved login beats a portal adapter,
-- then the oldest); the rest are deleted and their divisions switched to the
-- kept one. If the names differ, scan history under the other names stays
-- but no longer matches a site — rename the kept one afterwards if needed.
-- ---------------------------------------------------------------------------
select lower(regexp_replace(trim(url), '/+$', '')) as url,
       array_agg(id order by (learned->'recipe' is not null) desc, (login_last_ok is true) desc,
                 (login_password_enc is not null) desc, (adapter is not null) desc, id) as ids,
       array_agg(distinct division_id) as divisions,
       array_agg(distinct name) as names
from sites
where division_id is not null
group by 1
having count(*) > 1;


-- ---------------------------------------------------------------------------
-- Step 2 — the change.
-- ---------------------------------------------------------------------------
begin;

create table if not exists division_sites (
  division_id text not null references divisions(id) on delete cascade,
  site_id bigint not null references sites(id) on delete cascade,
  active boolean not null default true,
  sort_order bigint,
  primary key (division_id, site_id)
);
alter table division_sites enable row level security;

-- Settings shared by every division (e.g. "learn_fields").
create table if not exists app_settings (
  key text primary key,
  value jsonb
);
alter table app_settings enable row level security;

alter table sites add column if not exists status text;          -- null = in the list; 'requested'; 'declined'
alter table sites add column if not exists requested_by text references divisions(id) on delete cascade;
alter table sites add column if not exists request_note text;
alter table sites add column if not exists decline_reason text;
alter table sites alter column division_id drop not null;

-- Each division's switch and order, as they are today.
insert into division_sites (division_id, site_id, active, sort_order)
select division_id, id, coalesce(active, true), coalesce(sort_order, id)
from sites where division_id is not null
on conflict do nothing;

-- Merge copies of the same URL into one (the order matches step 1).
create temp table site_merge on commit drop as
with ranked as (
  select id, row_number() over (
           partition by lower(regexp_replace(trim(url), '/+$', ''))
           order by (learned->'recipe' is not null) desc, (login_last_ok is true) desc,
                    (login_password_enc is not null) desc, (adapter is not null) desc, id) as rn,
         first_value(id) over (
           partition by lower(regexp_replace(trim(url), '/+$', ''))
           order by (learned->'recipe' is not null) desc, (login_last_ok is true) desc,
                    (login_password_enc is not null) desc, (adapter is not null) desc, id) as keep_id
  from sites where division_id is not null
)
select id as dupe_id, keep_id from ranked where rn > 1;

-- A division that had any copy switched on keeps the site on.
insert into division_sites (division_id, site_id, active, sort_order)
select ds.division_id, m.keep_id, ds.active, ds.sort_order
from division_sites ds join site_merge m on ds.site_id = m.dupe_id
on conflict (division_id, site_id) do update set active = division_sites.active or excluded.active;

delete from sites where id in (select dupe_id from site_merge);

-- Sites no longer belong to a division (deleting a division mustn't delete them).
update sites set division_id = null where division_id is not null;

-- "Fields to collect" was saved per division; it's one list now.
insert into app_settings (key, value)
select 'learn_fields', coalesce(jsonb_agg(distinct f), '[]'::jsonb)
from divisions, jsonb_array_elements_text(coalesce(settings->'learn_fields', '[]'::jsonb)) as f
on conflict (key) do nothing;

commit;

-- sites.active and sites.sort_order are no longer read (division_sites holds
-- them). Once everything looks right they can go:
--   alter table sites drop column active, drop column sort_order;

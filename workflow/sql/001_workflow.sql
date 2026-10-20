-- Bid workflow board: tables, access, realtime and the "send to workflow" function.
-- Safe to run more than once. In Supabase Studio, choose "Run without RLS" if it
-- warns: it misreads PL/pgSQL "select ... into <variable>" as creating a table,
-- and "Run and enable RLS" inserts lines that break the functions (the RLS
-- setup for these tables is done below). Divisions are NOT created here: the board reads
-- Bid Scout's existing divisions table (see the README for how to point at it).

create table if not exists wf_templates (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  statuses    jsonb not null default '[]'::jsonb,   -- [{name, cat}] cat = todo | doing | done
  created_at  timestamptz not null default now()
);

create table if not exists wf_people (
  id          uuid primary key default gen_random_uuid(),
  division_id text not null,                         -- Bid Scout division id, kept as text
  name        text not null,
  created_at  timestamptz not null default now()
);
create index if not exists wf_people_division_idx on wf_people (division_id);

-- Remembers each division's name, so a division deleted in Bid Scout can still be shown by name as "archived".
create table if not exists wf_division_names (
  division_id text primary key,
  name        text not null,
  archived_at timestamptz,                             -- set when the division is deleted in Bid Scout
  updated_at  timestamptz not null default now()
);

create table if not exists wf_spaces (
  id          uuid primary key default gen_random_uuid(),
  division_id text not null,
  parent_id   uuid references wf_spaces (id) on delete cascade,   -- sub-spaces
  name        text not null,
  key         text not null,                         -- prefix for bid keys, like GEO-12
  ci          int  not null default 0,               -- colour slot
  next        int  not null default 1,               -- next bid number
  statuses    jsonb not null default '[]'::jsonb,    -- [{id, name, cat}] the space's workflow
  created_at  timestamptz not null default now(),
  unique (division_id, key)
);
create index if not exists wf_spaces_division_idx on wf_spaces (division_id);

create table if not exists wf_bids (
  id                uuid primary key default gen_random_uuid(),
  space_id          uuid not null references wf_spaces (id) on delete cascade,
  division_id       text not null,
  num               int  not null,
  rank              double precision not null default 0,
  type              text not null default 'Task',
  title             text not null,
  descr             text not null default '',
  status            text not null,                   -- id of a status in the space's workflow
  priority          text not null default 'Medium',
  assignee          text,                            -- wf_people.id
  reporter          text not null default '',
  labels            jsonb not null default '[]'::jsonb,
  due               date,
  created           date not null default current_date,
  done_at           date,
  parent_id         uuid,                            -- parent bid for subtasks
  comments          jsonb not null default '[]'::jsonb,
  history           jsonb not null default '[]'::jsonb,
  source_project_id text,                            -- Bid Scout project this bid came from
  source_url        text,
  updated_at        timestamptz not null default now()
);
create index if not exists wf_bids_space_idx on wf_bids (space_id);
create index if not exists wf_bids_division_idx on wf_bids (division_id);
-- one bid per Bid Scout project per division: clicking the button twice is harmless
create unique index if not exists wf_bids_source_uidx
  on wf_bids (division_id, source_project_id) where source_project_id is not null;

-- Trusted-network access: anyone with the anon key can read and write.
-- To lock this down later (per-division privacy), replace these policies.
do $$
declare t text;
begin
  foreach t in array array['wf_templates','wf_people','wf_spaces','wf_bids','wf_division_names'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists wf_open on %I', t);
    execute format('create policy wf_open on %I for all to anon, authenticated using (true) with check (true)', t);
    execute format('grant select, insert, update, delete on %I to anon, authenticated', t);
  end loop;
end $$;

-- Live updates in the board (Supabase Realtime).
do $$
declare t text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach t in array array['wf_templates','wf_people','wf_spaces','wf_bids','wf_division_names'] loop
      begin
        execute format('alter publication supabase_realtime add table %I', t);
      exception when duplicate_object then null;
      end;
    end loop;
  end if;
end $$;

-- The Bid Scout button calls this. It creates a bid in the division's inbox space
-- (made on first use, with the "Bid pipeline" template's columns if one exists)
-- and returns the bid id. Calling it again for the same project returns the same bid.
create or replace function wf_create_bid_from_project(
  p_division_id text,
  p_project_id  text,
  p_title       text,
  p_due         date default null,
  p_source_url  text default null,
  p_description text default null,
  p_created_by  text default 'Bid Scout',
  p_space_key   text default 'BS',
  p_space_name  text default 'Bid Scout inbox'
) returns uuid
language plpgsql
security definer
set search_path = public
as $fn$
declare
  v_id       uuid;
  v_space    wf_spaces;
  v_tpl      jsonb;
  v_statuses jsonb;
  v_num      int;
  v_rank     double precision;
begin
  if coalesce(trim(p_division_id), '') = '' or coalesce(trim(p_project_id), '') = '' or coalesce(trim(p_title), '') = '' then
    raise exception 'division, project and title are required';
  end if;

  v_id := (select id from wf_bids where division_id = p_division_id and source_project_id = p_project_id);
  if v_id is not null then return v_id; end if;

  v_space := (select s from wf_spaces s where s.division_id = p_division_id and s.key = p_space_key);
  if v_space.id is null then
    v_tpl := (select statuses from wf_templates where lower(name) = 'bid pipeline' order by created_at limit 1);
    if v_tpl is null or jsonb_array_length(v_tpl) = 0 then
      v_tpl := '[{"name":"To do","cat":"todo"},{"name":"In progress","cat":"doing"},{"name":"Done","cat":"done"}]'::jsonb;
    end if;
    v_statuses := (select jsonb_agg(jsonb_build_object('id', gen_random_uuid()::text, 'name', e->>'name', 'cat', e->>'cat') order by ord)
                     from jsonb_array_elements(v_tpl) with ordinality as t(e, ord));
    insert into wf_spaces (division_id, name, key, statuses)
      values (p_division_id, p_space_name, p_space_key, v_statuses)
      on conflict (division_id, key) do nothing;
    v_space := (select s from wf_spaces s where s.division_id = p_division_id and s.key = p_space_key);
  end if;

  -- serialise numbering for this space
  perform 1 from wf_spaces where id = v_space.id for update;
  v_space := (select s from wf_spaces s where s.id = v_space.id);
  v_num := (select coalesce(max(num), 0) + 1 from wf_bids where space_id = v_space.id);
  v_rank := (select coalesce(max(rank), 0) + 1000 from wf_bids where space_id = v_space.id);
  v_num := greatest(v_num, v_space.next);
  update wf_spaces set next = v_num + 1 where id = v_space.id;

  insert into wf_bids (space_id, division_id, num, rank, type, title, descr, status, priority, reporter,
                       due, source_project_id, source_url, history)
  values (v_space.id, p_division_id, v_num, v_rank, 'Task', trim(p_title), coalesce(p_description, ''),
          v_space.statuses->0->>'id', 'Medium', coalesce(p_created_by, 'Bid Scout'),
          p_due, p_project_id, p_source_url,
          jsonb_build_array(jsonb_build_object('by', coalesce(p_created_by, 'Bid Scout'),
                                               'at', (extract(epoch from now()) * 1000)::bigint,
                                               'field', 'created')))
  on conflict (division_id, source_project_id) where source_project_id is not null do nothing
  returning id into v_id;

  if v_id is null then  -- lost a race with another click: return the winner
    v_id := (select id from wf_bids where division_id = p_division_id and source_project_id = p_project_id);
  end if;
  return v_id;
end;
$fn$;

grant execute on function wf_create_bid_from_project(text, text, text, date, text, text, text, text, text) to anon, authenticated;

-- Automations: "when a bid enters <column> in this space, add a copy to a sub-space".
-- Rules live on the parent space as JSON: [{"id":"..","status":"Quoted","to":"<child space id>","col":""}]
--   status = column NAME in the parent (case-insensitive, so applying a template doesn't break rules)
--   to     = id of a DIRECT child space (parent_id = this space)
--   col    = column NAME to land in, or "" for the child's first column
alter table wf_spaces add column if not exists rules jsonb not null default '[]'::jsonb;
alter table wf_bids   add column if not exists origin_bid_id uuid;   -- the bid this one was copied from
-- a bid is copied into a given space only once, however often it moves in and out of the trigger column
create unique index if not exists wf_bids_origin_uidx on wf_bids (origin_bid_id, space_id) where origin_bid_id is not null;

create or replace function wf_apply_space_rules() returns trigger
language plpgsql
security definer
set search_path = public
as $fn$
declare
  v_space wf_spaces;
  v_target wf_spaces;
  v_stat text;
  v_col text;
  v_num int;
  v_rank double precision;
  r jsonb;
begin
  if NEW.type = 'Subtask' then
    return NEW;
  end if;
  if TG_OP = 'UPDATE' and NEW.status is not distinct from OLD.status then
    return NEW;
  end if;
  v_space := (select s from wf_spaces s where s.id = NEW.space_id);
  if v_space.id is null or jsonb_array_length(coalesce(v_space.rules, '[]'::jsonb)) = 0 then
    return NEW;
  end if;
  v_stat := (select e ->> 'name' from jsonb_array_elements(v_space.statuses) e where e ->> 'id' = NEW.status limit 1);
  if v_stat is null then
    return NEW;
  end if;
  for r in select e from jsonb_array_elements(v_space.rules) e loop
    begin   -- a broken rule must never stop the bid itself from saving
      if lower(coalesce(r ->> 'status', '')) is distinct from lower(v_stat) then
        continue;
      end if;
      if coalesce(r ->> 'to', '') !~* '^[0-9a-f-]{36}$' then
        continue;
      end if;
      v_target := (select s from wf_spaces s where s.id = (r ->> 'to')::uuid and s.parent_id = v_space.id);
      if v_target.id is null then
        continue;
      end if;
      if exists (select 1 from wf_bids where origin_bid_id = NEW.id and space_id = v_target.id) then
        continue;
      end if;
      perform 1 from wf_spaces where id = v_target.id for update;   -- serialise numbering
      v_target := (select s from wf_spaces s where s.id = v_target.id);
      v_col := null;
      if coalesce(r ->> 'col', '') != '' then
        v_col := (select e ->> 'id' from jsonb_array_elements(v_target.statuses) e where lower(e ->> 'name') = lower(r ->> 'col') limit 1);
      end if;
      if v_col is null then
        v_col := v_target.statuses -> 0 ->> 'id';
      end if;
      if v_col is null then
        continue;
      end if;
      v_num := (select coalesce(max(num), 0) + 1 from wf_bids where space_id = v_target.id);
      v_rank := (select coalesce(max(rank), 0) + 1000 from wf_bids where space_id = v_target.id);
      v_num := greatest(v_num, v_target.next);
      update wf_spaces set next = v_num + 1 where id = v_target.id;
      -- carries over: type, title, description, priority, labels, due date, Bid Scout link.
      -- does not carry over: assignee, comments, history, or the Bid Scout project id (one bid per project).
      insert into wf_bids (space_id, division_id, num, rank, type, title, descr, status, priority, reporter, labels, due, source_url, origin_bid_id, history)
      values (v_target.id, v_target.division_id, v_num, v_rank, NEW.type, NEW.title, NEW.descr, v_col, NEW.priority, 'Automation', NEW.labels, NEW.due, NEW.source_url, NEW.id,
              jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', (extract(epoch from now()) * 1000)::bigint, 'field', 'created')))
      on conflict (origin_bid_id, space_id) where origin_bid_id is not null do nothing;
    exception when others then
      raise warning 'wf_apply_space_rules skipped a rule: %', sqlerrm;
    end;
  end loop;
  return NEW;
end;
$fn$;

drop trigger if exists wf_bids_rules on wf_bids;
create trigger wf_bids_rules after insert or update of status on wf_bids
  for each row execute function wf_apply_space_rules();

-- Make the new tables and function visible to the REST API right away.
notify pgrst, 'reload schema';

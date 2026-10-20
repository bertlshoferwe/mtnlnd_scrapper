-- Teams (e.g. regions) per division, edited under Members in a space's Settings.
-- A bid's Team field (type "team") picks one. Safe to run more than once.

create table if not exists wf_teams (
  id uuid primary key default gen_random_uuid(),
  division_id text not null,
  name text not null,
  members jsonb not null default '[]',
  created_at timestamptz not null default now()
);

alter table wf_teams enable row level security;

drop policy if exists wf_open on wf_teams;

create policy wf_open on wf_teams
  for all to anon, authenticated using (true) with check (true);

grant select, insert, update, delete on wf_teams to anon, authenticated;

do $do$
begin
  alter publication supabase_realtime add table wf_teams;
exception when duplicate_object then null;
end
$do$;

-- The Bid pipeline's Team field becomes a pick-a-team dropdown.
update wf_templates
set fields = (
  select jsonb_agg(case when e->>'id' = 'f_team'
                        then jsonb_set(e, '{type}', '"team"') else e end)
  from jsonb_array_elements(fields) e)
where fields @> '[{"id": "f_team"}]';

notify pgrst, 'reload schema';

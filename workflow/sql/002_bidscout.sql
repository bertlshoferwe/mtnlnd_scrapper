-- Bid Scout hookup for the workflow board. Run after 001_workflow.sql, in the
-- Supabase SQL Editor. Safe to run more than once.

-- 1. Divisions the board can read.
-- The board runs in the browser with the anon key, and `divisions` has RLS on
-- with no policies (schema.sql), so it can't read that table directly. This
-- view exposes only id + name; views run with their owner's rights, so it
-- reads past RLS without opening up `divisions` (or its settings) itself.
-- The board is pointed at it via DIVISIONS_TABLE in /workflow/config.js.
create or replace view wf_divisions as
  select id, name from divisions;
grant select on wf_divisions to anon, authenticated;

-- 2. Keep wf_division_names exact (adapted from the board's
-- 003_archive_divisions.sql.example): names recorded on create/rename, and a
-- deleted division is marked archived so its bids stay readable.
create or replace function wf_sync_division_name() returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into wf_division_names (division_id, name, archived_at, updated_at)
  values (NEW.id::text, NEW.name::text, null, now())
  on conflict (division_id) do update set name = excluded.name, archived_at = null, updated_at = now();
  return NEW;
end;
$$;

create or replace function wf_archive_division() returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into wf_division_names (division_id, name, archived_at, updated_at)
  values (OLD.id::text, OLD.name::text, now(), now())
  on conflict (division_id) do update set name = excluded.name, archived_at = now(), updated_at = now();
  return OLD;
end;
$$;

drop trigger if exists wf_division_saved   on divisions;
drop trigger if exists wf_division_deleted on divisions;
create trigger wf_division_saved   after insert or update of name on divisions
  for each row execute function wf_sync_division_name();
create trigger wf_division_deleted after delete on divisions
  for each row execute function wf_archive_division();

insert into wf_division_names (division_id, name)
select id::text, name::text from divisions
on conflict (division_id) do update set name = excluded.name;

-- 3. The "Bid pipeline" template — same columns as the Jira workflow. Every
-- division's "Bid Scout inbox" space is created with these on first use.
insert into wf_templates (name, statuses)
select 'Bid pipeline', jsonb_build_array(
  jsonb_build_object('name', 'Received', 'cat', 'todo'),
  jsonb_build_object('name', 'Takeoff - In Process', 'cat', 'doing'),
  jsonb_build_object('name', 'Ready for Review', 'cat', 'doing'),
  jsonb_build_object('name', 'Email - In Process', 'cat', 'doing'),
  jsonb_build_object('name', 'Submitted', 'cat', 'done'),
  jsonb_build_object('name', 'Won', 'cat', 'done'),
  jsonb_build_object('name', 'Lost', 'cat', 'done'),
  jsonb_build_object('name', 'On Hold', 'cat', 'done')
)
where not exists (select 1 from wf_templates where lower(name) = 'bid pipeline');

notify pgrst, 'reload schema';

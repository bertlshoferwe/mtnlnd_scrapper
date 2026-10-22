-- First name, last name and email for the board's people (Settings → Division → Members).
-- name stays as "First Last" for everything that shows a person. Safe to run more than once.
alter table wf_people add column if not exists first_name text;
alter table wf_people add column if not exists last_name text;
alter table wf_people add column if not exists email text;
notify pgrst, 'reload schema';

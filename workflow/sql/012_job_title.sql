-- Job title for the board's people (Settings → Division → Members). Optional. Safe to run more than once.
alter table wf_people add column if not exists job_title text;
notify pgrst, 'reload schema';

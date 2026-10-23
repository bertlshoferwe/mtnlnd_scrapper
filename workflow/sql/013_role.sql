-- Role for the board's people (Settings → Division → Members): Estimator, Inside Sales or Outside Sales. Optional. Safe to run more than once.
alter table wf_people add column if not exists role text;
notify pgrst, 'reload schema';

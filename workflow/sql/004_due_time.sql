-- Optional time on a bid's due date (shown next to the date). Safe to run more than once.
alter table wf_bids add column if not exists due_time time;

-- The Bid Date field is covered by the due date and time now.
update wf_templates
set fields = coalesce(
  (select jsonb_agg(e) from jsonb_array_elements(fields) e where e->>'id' <> 'f_biddate'),
  '[]'::jsonb)
where fields @> '[{"id": "f_biddate"}]';

notify pgrst, 'reload schema';

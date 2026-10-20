-- Custom fields on bids. Run after 002_bidscout.sql. Safe to run more than once.
-- A workflow template lists its fields (wf_templates.fields: [{id, name, type}],
-- type = text | number | money | date | person); each bid keeps its values in
-- wf_bids.fields ({field id: value}). Fields are added, renamed and reordered in
-- the board's Settings window; no further SQL needed.

alter table wf_bids add column if not exists fields jsonb default '{}';

alter table wf_templates add column if not exists fields jsonb default '[]';

-- Starting fields for the Bid pipeline workflow (only if it has none yet).
update wf_templates set fields = jsonb_build_array(
  jsonb_build_object('id', 'f_ticket', 'name', 'Ticket #', 'type', 'text'),
  jsonb_build_object('id', 'f_rfq', 'name', 'Original RFQ', 'type', 'text'),
  jsonb_build_object('id', 'f_biddate', 'name', 'Bid Date', 'type', 'date'),
  jsonb_build_object('id', 'f_team', 'name', 'Team', 'type', 'text'),
  jsonb_build_object('id', 'f_coassign', 'name', 'Co-Assignee', 'type', 'person'),
  jsonb_build_object('id', 'f_contractor', 'name', 'Contractor Name', 'type', 'text'),
  jsonb_build_object('id', 'f_quoted', 'name', 'Date Quoted', 'type', 'date'),
  jsonb_build_object('id', 'f_amount', 'name', 'Quoted Amount', 'type', 'money')
)
where lower(name) = 'bid pipeline'
  and (fields is null or fields = '[]'::jsonb);

notify pgrst, 'reload schema';

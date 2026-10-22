-- Field setups separate from workflows: a template row with kind 'fields' holds just fields; every space uses the oldest one (the default) unless wf_spaces.field_set picks another. Copies Bid pipeline's fields into the first setup, keeping field ids so bids keep their values. Safe to run more than once.
alter table wf_templates add column if not exists kind text not null default 'workflow';
alter table wf_spaces add column if not exists field_set text;
insert into wf_templates (name, statuses, fields, kind) select name, '[]'::jsonb, fields, 'fields' from wf_templates where kind = 'workflow' and jsonb_array_length(coalesce(fields, '[]'::jsonb)) > 0 and not exists (select 1 from wf_templates where kind = 'fields') order by created_at limit 1;
notify pgrst, 'reload schema';

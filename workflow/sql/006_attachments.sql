-- File attachments on bids. Files go in a private Storage bucket "bid-files"
-- (opened through short-lived links); each bid lists its files in
-- wf_bids.attachments. Same open access as the rest of the board until
-- sign-in is added. Safe to run more than once.

alter table wf_bids add column if not exists attachments jsonb default '[]';

insert into storage.buckets (id, name, public)
values ('bid-files', 'bid-files', false)
on conflict (id) do nothing;

drop policy if exists wf_files_read on storage.objects;
create policy wf_files_read on storage.objects
  for select to anon, authenticated using (bucket_id = 'bid-files');

drop policy if exists wf_files_add on storage.objects;
create policy wf_files_add on storage.objects
  for insert to anon, authenticated with check (bucket_id = 'bid-files');

drop policy if exists wf_files_remove on storage.objects;
create policy wf_files_remove on storage.objects
  for delete to anon, authenticated using (bucket_id = 'bid-files');

notify pgrst, 'reload schema';

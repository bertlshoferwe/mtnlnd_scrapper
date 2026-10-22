-- Email notifications: remembers, per bid, the newest change already emailed about. Only the server reads it. Safe to run more than once.
create table if not exists wf_notify (bid_id text primary key, through bigint not null default 0, updated_at timestamptz not null default now());
alter table wf_notify enable row level security;
notify pgrst, 'reload schema';

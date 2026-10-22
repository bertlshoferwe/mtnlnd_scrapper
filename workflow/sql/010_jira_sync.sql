-- Jira sync: each board bid remembers the Jira issue it goes with, and when that issue last changed, so re-running the import updates instead of duplicating. Safe to run more than once.
alter table wf_bids add column if not exists jira_key text;
alter table wf_bids add column if not exists jira_updated text;
create unique index if not exists wf_bids_jira_uidx on wf_bids (division_id, jira_key) where jira_key is not null;
notify pgrst, 'reload schema';

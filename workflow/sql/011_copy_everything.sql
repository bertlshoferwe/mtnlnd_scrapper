-- Automations copy everything: when a bid lands in a column with a "copy to space" rule, the copy
-- gets all of the bid's information (assignee, reporter, every field, due time, files, comments
-- and history), not just its name and dates. Also fills in copies made before this. Safe to run more than once.
create or replace function wf_apply_space_rules() returns trigger
language plpgsql
security definer
set search_path = public
as $fn$
declare
  v_space wf_spaces;
  v_target wf_spaces;
  v_stat text;
  v_col text;
  v_num int;
  v_rank double precision;
  v_new uuid;
  v_now bigint := (extract(epoch from now()) * 1000)::bigint;
  r jsonb;
begin
  if NEW.type = 'Subtask' then
    return NEW;
  end if;
  if TG_OP = 'UPDATE' and NEW.status is not distinct from OLD.status then
    return NEW;
  end if;
  v_space := (select s from wf_spaces s where s.id = NEW.space_id);
  if v_space.id is null or jsonb_array_length(coalesce(v_space.rules, '[]'::jsonb)) = 0 then
    return NEW;
  end if;
  v_stat := (select e ->> 'name' from jsonb_array_elements(v_space.statuses) e where e ->> 'id' = NEW.status limit 1);
  if v_stat is null then
    return NEW;
  end if;
  for r in select e from jsonb_array_elements(v_space.rules) e loop
    begin   -- a broken rule must never stop the bid itself from saving
      if lower(coalesce(r ->> 'status', '')) is distinct from lower(v_stat) then
        continue;
      end if;
      if coalesce(r ->> 'to', '') !~* '^[0-9a-f-]{36}$' then
        continue;
      end if;
      v_target := (select s from wf_spaces s where s.id = (r ->> 'to')::uuid and s.parent_id = v_space.id);
      if v_target.id is null then
        continue;
      end if;
      if exists (select 1 from wf_bids where origin_bid_id = NEW.id and space_id = v_target.id) then
        continue;
      end if;
      perform 1 from wf_spaces where id = v_target.id for update;   -- serialise numbering
      v_target := (select s from wf_spaces s where s.id = v_target.id);
      v_col := null;
      if coalesce(r ->> 'col', '') != '' then
        v_col := (select e ->> 'id' from jsonb_array_elements(v_target.statuses) e where lower(e ->> 'name') = lower(r ->> 'col') limit 1);
      end if;
      if v_col is null then
        v_col := v_target.statuses -> 0 ->> 'id';
      end if;
      if v_col is null then
        continue;
      end if;
      v_num := (select coalesce(max(num), 0) + 1 from wf_bids where space_id = v_target.id);
      v_rank := (select coalesce(max(rank), 0) + 1000 from wf_bids where space_id = v_target.id);
      v_num := greatest(v_num, v_target.next);
      update wf_spaces set next = v_num + 1 where id = v_target.id;
      -- carries over everything except what belongs to one bid only: its number, column, parent
      -- (subtask parents live in the old space), the Bid Scout project id and the Jira issue
      -- (one bid each; the copy shows the original's Bid Scout documents through origin_bid_id).
      v_new := null;
      insert into wf_bids (space_id, division_id, num, rank, type, title, descr, status, priority, assignee, reporter,
                           labels, due, due_time, created, fields, attachments, comments, history, source_url, origin_bid_id)
      values (v_target.id, v_target.division_id, v_num, v_rank, NEW.type, NEW.title, NEW.descr, v_col, NEW.priority,
              NEW.assignee, NEW.reporter, NEW.labels, NEW.due, NEW.due_time, NEW.created, NEW.fields, NEW.attachments,
              NEW.comments,
              coalesce(NEW.history, '[]'::jsonb) || jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', v_now, 'field', 'created')),
              NEW.source_url, NEW.id)
      on conflict (origin_bid_id, space_id) where origin_bid_id is not null do nothing
      returning id into v_new;
      -- the copied comments and history were already emailed about on the original
      if v_new is not null then
        insert into wf_notify (bid_id, through) values (v_new::text, v_now)
        on conflict (bid_id) do update set through = greatest(wf_notify.through, excluded.through);
      end if;
    exception when others then
      raise warning 'wf_apply_space_rules skipped a rule: %', sqlerrm;
    end;
  end loop;
  return NEW;
end;
$fn$;

-- Copies made before this: don't email about what's filled in below.
insert into wf_notify (bid_id, through) select c.id::text, (extract(epoch from now()) * 1000)::bigint from wf_bids c where c.origin_bid_id is not null on conflict (bid_id) do update set through = greatest(wf_notify.through, excluded.through);

-- Copies made before this: fill in what they're missing from the bid they came from (nothing already on the copy is overwritten; comments and history are merged).
update wf_bids c set assignee = coalesce(c.assignee, o.assignee), reporter = case when coalesce(c.reporter, '') in ('', 'Automation') then o.reporter else c.reporter end, descr = case when coalesce(c.descr, '') = '' then o.descr else c.descr end, labels = case when jsonb_array_length(coalesce(c.labels, '[]'::jsonb)) = 0 then o.labels else c.labels end, due = coalesce(c.due, o.due), due_time = coalesce(c.due_time, o.due_time), created = least(c.created, o.created), fields = coalesce(o.fields, '{}'::jsonb) || coalesce(c.fields, '{}'::jsonb), attachments = case when jsonb_array_length(coalesce(c.attachments, '[]'::jsonb)) = 0 then coalesce(o.attachments, '[]'::jsonb) else c.attachments end, comments = (select coalesce(jsonb_agg(x order by coalesce((x ->> 'at')::numeric, 0)), '[]'::jsonb) from (select distinct on (coalesce(x ->> 'id', x::text)) x from jsonb_array_elements(coalesce(o.comments, '[]'::jsonb) || coalesce(c.comments, '[]'::jsonb)) x) s), history = (select coalesce(jsonb_agg(x order by coalesce((x ->> 'at')::numeric, 0)), '[]'::jsonb) from (select distinct x from jsonb_array_elements(coalesce(o.history, '[]'::jsonb) || coalesce(c.history, '[]'::jsonb)) x) s), updated_at = now() from wf_bids o where c.origin_bid_id = o.id;

notify pgrst, 'reload schema';

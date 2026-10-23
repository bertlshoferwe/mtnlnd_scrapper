-- Automations that hand the copy to a role (see 014): when the bid's team has more than one person
-- with that role, they take turns. wf_teams.rotation remembers who got the last one, per role.
-- Safe to run more than once.
alter table wf_teams add column if not exists rotation jsonb not null default '{}'::jsonb;
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
  v_team wf_teams;
  v_pid text;
  v_pname text;
  v_asg text;
  v_rep text;
  v_hist jsonb;
  v_cands text[];
  v_i int;
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
      v_asg := NEW.assignee;
      v_rep := NEW.reporter;
      v_hist := coalesce(NEW.history, '[]'::jsonb) || jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', v_now, 'field', 'created'));
      -- "assign": hand the copy to the person on the bid's team with that role (several take turns,
      -- remembered in wf_teams.rotation); no team or nobody with the role leaves both empty
      if coalesce(r ->> 'assign', '') != '' then
        v_team := (select t from wf_teams t where t.division_id = NEW.division_id and t.id::text in (select value from jsonb_each_text(coalesce(NEW.fields, '{}'::jsonb))) order by t.name limit 1);
        v_pid := null;
        if v_team.id is not null then
          perform 1 from wf_teams where id = v_team.id for update;   -- serialise the turn-taking
          v_team := (select t from wf_teams t where t.id = v_team.id);
          v_cands := array(select p.id::text from wf_people p where p.id::text in (select jsonb_array_elements_text(coalesce(v_team.members, '[]'::jsonb))) and lower(coalesce(p.role, '')) = lower(r ->> 'assign') order by p.name, p.id);
          if coalesce(array_length(v_cands, 1), 0) > 0 then
            v_i := array_position(v_cands, coalesce(v_team.rotation, '{}'::jsonb) ->> lower(r ->> 'assign'));
            v_pid := v_cands[case when v_i is null then 1 else v_i % array_length(v_cands, 1) + 1 end];
            update wf_teams set rotation = coalesce(rotation, '{}'::jsonb) || jsonb_build_object(lower(r ->> 'assign'), v_pid) where id = v_team.id;
          end if;
        end if;
        v_pname := (select p.name from wf_people p where p.id::text = v_pid);
        -- logged just after the copy's creation, so the new person is emailed
        if coalesce(v_pid, '') is distinct from coalesce(NEW.assignee, '') then
          v_hist := v_hist || jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', v_now + 1, 'field', 'Assignee', 'from', coalesce(NEW.assignee, ''), 'to', coalesce(v_pid, '')));
        end if;
        if coalesce(v_pid, '') is distinct from coalesce(NEW.reporter, '') then
          v_hist := v_hist || jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', v_now + 1, 'field', 'Reporter', 'from', coalesce((select p.name from wf_people p where p.id::text = NEW.reporter), nullif(NEW.reporter, ''), 'None'), 'to', coalesce(v_pname, 'None'), 'toId', coalesce(v_pid, '')));
        end if;
        if v_pid is null then
          v_hist := v_hist || jsonb_build_array(jsonb_build_object('by', 'Automation', 'at', v_now + 1, 'field', 'note', 'text', case when v_team.id is null then 'No team is set on this bid, so the assignee and reporter are unknown.' else 'Nobody on ' || v_team.name || ' has the role ' || (r ->> 'assign') || ', so the assignee and reporter are unknown.' end));
        end if;
        v_asg := v_pid;
        v_rep := coalesce(v_pid, '');
      end if;
      v_new := null;
      insert into wf_bids (space_id, division_id, num, rank, type, title, descr, status, priority, assignee, reporter,
                           labels, due, due_time, created, fields, attachments, comments, history, source_url, origin_bid_id)
      values (v_target.id, v_target.division_id, v_num, v_rank, NEW.type, NEW.title, NEW.descr, v_col, NEW.priority,
              v_asg, v_rep, NEW.labels, NEW.due, NEW.due_time, NEW.created, NEW.fields, NEW.attachments,
              NEW.comments,
              v_hist,
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

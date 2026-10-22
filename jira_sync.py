"""Copy a division's Jira project onto its bid board, and keep it in step.

Runs in the scanner container (it holds the Jira login, and is one process).
Every Jira issue becomes a board bid in the division's main space, with a new
board number; wf_bids.jira_key remembers which issue it came from, so running
again updates instead of duplicating. A bid Bid Scout already put on the board
(Add to Board files the Jira issue too) is matched through project_flags.jira_key.

What comes across: summary, description, status (by column name), priority,
labels, created/due date, assignee/reporter, the custom fields whose names match
the space's field setup (Ticket #, Team, Co-Assignee, Quoted Amount, ...),
comments (author, date, @mentions), attachments (copied into board storage) and
subtasks under their parent.

People are matched to the division's board members by email, then by first
name; a match picks up the Jira last name and email. Anyone with no match is
added as a new member.

Direction: Jira -> board only. On a re-run, a bid whose Jira issue changed since
the last sync is updated from Jira (Jira wins for the fields above); new
comments and attachments are added; nothing on the board is deleted.

run(division_id, dry=True) returns a report and writes nothing.
"""

import re
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

import supabase_store

BID_FILES_BUCKET = supabase_store.BID_FILES_BUCKET
PRIORITIES = ["Highest", "High", "Medium", "Low"]


class JiraError(Exception):
    pass


# ---------------------------------------------------------------------------
# Jira API
# ---------------------------------------------------------------------------

def _cfg():
    import os
    base = (os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/")
    email = (os.environ.get("JIRA_EMAIL") or "").strip()
    token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    if not (base and email and token):
        raise JiraError("Jira isn't configured — set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN.")
    return base, (email, token)


def _jget(path, **params):
    base, auth = _cfg()
    r = requests.get(base + path, params=params, auth=auth, timeout=30)
    if not r.ok:
        raise JiraError(f"Jira {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def _jpost(path, body):
    base, auth = _cfg()
    r = requests.post(base + path, json=body, auth=auth, timeout=60)
    if not r.ok:
        raise JiraError(f"Jira {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def project_key(division_id):
    import os
    return (supabase_store.get_division_settings(division_id).get("jira_project_key")
            or (os.environ.get("JIRA_PROJECT_KEY") or "").strip().upper())


def _search_all(jql, fields):
    out, token = [], None
    while True:
        body = {"jql": jql, "fields": fields, "maxResults": 100}
        if token:
            body["nextPageToken"] = token
        page = _jpost("/rest/api/3/search/jql", body)
        out.extend(page.get("issues") or [])
        token = page.get("nextPageToken")
        if page.get("isLast", True) or not token:
            return out


def _all_comments(issue):
    c = (issue["fields"].get("comment") or {})
    got = c.get("comments") or []
    if (c.get("total") or 0) > len(got):
        got = _jget(f"/rest/api/3/issue/{issue['key']}/comment", maxResults=5000).get("comments") or got
    return got


# ---------------------------------------------------------------------------
# Jira values -> board values
# ---------------------------------------------------------------------------

def adf_text(node):
    """Atlassian document format -> plain text (paragraphs, lists, mentions)."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    kids = node.get("content") or []
    if t == "text":
        return node.get("text") or ""
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return "@" + ((node.get("attrs") or {}).get("text") or "").lstrip("@")
    if t in ("emoji",):
        return (node.get("attrs") or {}).get("text") or ""
    if t in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url") or ""
    inner = "".join(adf_text(k) for k in kids)
    if t == "listItem":
        return "- " + inner.strip() + "\n"
    if t in ("paragraph", "heading", "blockquote", "codeBlock"):
        return inner.rstrip() + "\n\n"
    return inner


def _mention_ids(node, out):
    if isinstance(node, dict):
        if node.get("type") == "mention":
            aid = (node.get("attrs") or {}).get("id")
            if aid:
                out.append(aid)
        for k in node.get("content") or []:
            _mention_ids(k, out)
    return out


def _ms(iso):
    if not iso:
        return int(time.time() * 1000)
    try:
        return int(datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                   .timestamp() * 1000 - _tz_offset_ms(iso))
    except ValueError:
        return int(time.time() * 1000)


def _tz_offset_ms(iso):
    m = re.search(r"([+-])(\d\d):?(\d\d)$", iso)
    if not m:
        return 0
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3))) * 60000


def _plain(v):
    """A Jira custom field value as text (option, user, number, list...)."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return ("%f" % v).rstrip("0").rstrip(".")
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ", ".join(x for x in (_plain(i) for i in v) if x)
    if isinstance(v, dict):
        if v.get("type") == "doc":
            return adf_text(v).strip()
        for k in ("value", "name", "title", "displayName", "key"):
            if v.get(k):
                return str(v[k])
    return str(v)


class People:
    """Jira users -> the division's board members (match, update, add)."""

    def __init__(self, division_id, dry):
        self.division_id, self.dry = division_id, dry
        self.rows = (supabase_store.get_client().table("wf_people").select("*")
                     .eq("division_id", division_id).execute().data or [])
        self.by_account = {}
        self.changes = []   # human-readable lines for the report

    def _first(self, s):
        return (s or "").strip().split(" ")[0].lower()

    def person_id(self, user):
        """Board person id for a Jira user object (or None)."""
        if not user:
            return None
        aid = user.get("accountId")
        if aid in self.by_account:
            return self.by_account[aid]
        name = (user.get("displayName") or "").strip()
        email = (user.get("emailAddress") or "").strip()
        parts = name.split(" ", 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else "")
        row = (next((r for r in self.rows if email and (r.get("email") or "").lower() == email.lower()), None)
               or next((r for r in self.rows if self._first(r.get("first_name") or r.get("name")) == first.lower()), None))
        if row:
            patch = {}
            if last and (row.get("last_name") or "") != last:
                patch["last_name"] = last
            if email and (row.get("email") or "") != email:
                patch["email"] = email
            if patch:
                patch["first_name"] = row.get("first_name") or first
                patch["name"] = f"{patch['first_name']} {patch.get('last_name') or row.get('last_name') or ''}".strip()
                self.changes.append(f"Update {row.get('name')} → {patch['name']}"
                                    + (f" ({patch['email']})" if patch.get("email") else ""))
                if not self.dry:
                    supabase_store.get_client().table("wf_people").update(patch).eq("id", row["id"]).execute()
                row.update(patch)
        else:
            row = {"id": str(uuid.uuid4()), "division_id": self.division_id, "name": name or "Someone",
                   "first_name": first or None, "last_name": last or None, "email": email or None}
            self.changes.append(f"Add {row['name']}" + (f" ({email})" if email else "") + " — no board member by that first name")
            if not self.dry:
                supabase_store.get_client().table("wf_people").insert(row).execute()
            self.rows.append(row)
        self.by_account[aid] = row["id"]
        return row["id"]


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------

def run(division_id, dry=True, log=print):
    """Import / sync one division. Returns a report dict."""
    sb = supabase_store.get_client()
    pkey = project_key(division_id)
    if not pkey:
        raise JiraError("No Jira project is picked for this division.")

    try:
        sb.table("wf_bids").select("jira_key,jira_updated").limit(1).execute()
    except Exception:
        raise JiraError("The bid board's database needs one update first — run workflow/sql/010_jira_sync.sql in Supabase.")
    jfields = _jget("/rest/api/3/field")
    by_name = {}
    for f in jfields:
        by_name.setdefault((f.get("name") or "").strip().lower(), f)

    space = supabase_store._workflow_space(division_id)
    statuses = space.get("statuses") or []
    board_fields = supabase_store._space_fields(space["id"])
    teams = (sb.table("wf_teams").select("id,name").eq("division_id", division_id).execute().data or [])

    # board field -> Jira field, by name
    fmap = []
    for bf in board_fields:
        jf = by_name.get((bf.get("name") or "").strip().lower())
        if jf:
            fmap.append((bf, jf))
    want = ["summary", "description", "status", "priority", "labels", "created", "updated", "duedate",
            "assignee", "reporter", "issuetype", "parent", "comment", "attachment"] + [jf["id"] for _, jf in fmap]
    issues = _search_all(f"project = {pkey} ORDER BY created ASC", want)
    log(f"[jira] {pkey}: {len(issues)} issue(s)")

    people = People(division_id, dry)
    existing = (sb.table("wf_bids").select("id,num,jira_key,jira_updated,source_project_id,comments,attachments,history")
                .eq("division_id", division_id).execute().data or [])
    by_jira = {b["jira_key"]: b for b in existing if b.get("jira_key")}
    by_source = {b["source_project_id"]: b for b in existing if b.get("source_project_id")}
    project_of = {v: k for k, v in supabase_store.load_project_jira_keys(division_id).items() if v}

    nums = [b["num"] for b in existing if b.get("num")]
    next_num = max(nums + [0, (space.get("next") or 1) - 1]) + 1
    space_bids = sb.table("wf_bids").select("rank").eq("space_id", space["id"]).execute().data or []
    next_rank = max([b.get("rank") or 0 for b in space_bids] + [0]) + 1000

    report = {"project": pkey, "space": space.get("name") or space.get("key"), "dry": dry, "issues": [], "new": 0, "updated": 0,
              "unchanged": 0, "attachments": 0, "attachment_bytes": 0,
              "unmatched_statuses": {}, "unmatched_teams": {}, "people": people.changes,
              "fields": [bf["name"] for bf, _ in fmap],
              "board_fields_missing": [bf["name"] for bf in board_fields if bf not in [x for x, _ in fmap]],
              "jira_fields_unused": []}
    used_jira = {jf["id"] for _, jf in fmap}
    seen_unused = set()
    key_to_bid = {}
    pending_parents = []
    now_ms = int(time.time() * 1000)

    for iss in issues:
        f, key = iss["fields"], iss["key"]
        bid = by_jira.get(key)
        linked_project = project_of.get(key)
        if not bid and linked_project:
            bid = by_source.get(linked_project)
        is_new = bid is None
        if not is_new and bid.get("jira_key") == key and (bid.get("jira_updated") or "") >= (f.get("updated") or ""):
            report["unchanged"] += 1
            key_to_bid[key] = bid["id"]
            report["issues"].append({"key": key, "title": f.get("summary"), "action": "unchanged",
                                     "board": None})
            continue

        st_name = ((f.get("status") or {}).get("name") or "").strip()
        st = next((s for s in statuses if (s.get("name") or "").strip().lower() == st_name.lower()), None)
        if not st:
            report["unmatched_statuses"][st_name] = report["unmatched_statuses"].get(st_name, 0) + 1
            st = statuses[0] if statuses else {"id": None}
        pri = ((f.get("priority") or {}).get("name") or "Medium")
        pri = pri if pri in PRIORITIES else ("Low" if pri.lower() == "lowest" else "Medium")

        custom = {}
        for bf, jf in fmap:
            v = f.get(jf["id"])
            if v in (None, "", []):
                continue
            t = bf.get("type")
            if t == "person":
                pid = people.person_id(v[0] if isinstance(v, list) else v)
                if pid:
                    custom[bf["id"]] = pid
            elif t == "team":
                tn = _plain(v)
                tm = next((x for x in teams if x["name"].strip().lower() == tn.strip().lower()), None)
                if tm:
                    custom[bf["id"]] = tm["id"]
                else:
                    report["unmatched_teams"][tn] = report["unmatched_teams"].get(tn, 0) + 1
            elif t == "date":
                custom[bf["id"]] = _plain(v)[:10]
            elif t in ("money", "number"):
                try:
                    custom[bf["id"]] = str(float(_plain(v).replace("$", "").replace(",", "")))
                except ValueError:
                    pass
            else:
                custom[bf["id"]] = _plain(v)
        for jid, v in f.items():
            if jid.startswith("customfield_") and jid not in used_jira and v not in (None, "", []) and jid not in seen_unused:
                seen_unused.add(jid)
                nm = next((x.get("name") for x in jfields if x.get("id") == jid), jid)
                report["jira_fields_unused"].append(nm)

        row = {
            "title": (f.get("summary") or key).strip(),
            "descr": adf_text(f.get("description")).strip(),
            "status": st.get("id"),
            "priority": pri,
            "labels": f.get("labels") or [],
            "due": f.get("duedate") or None,
            "assignee": people.person_id(f.get("assignee")),
            "reporter": people.person_id(f.get("reporter")) or "",
            "type": "Subtask" if (f.get("issuetype") or {}).get("subtask") else "Task",
            "jira_key": key,
            "jira_updated": f.get("updated") or "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        bid_id = bid["id"] if bid else str(uuid.uuid4())
        key_to_bid[key] = bid_id

        # comments: add the ones not already on the bid
        have_c = {c.get("id") for c in ((bid or {}).get("comments") or [])}
        comments = list((bid or {}).get("comments") or [])
        for c in _all_comments(iss):
            cid = "jira-" + str(c.get("id"))
            if cid in have_c:
                continue
            mention_people = [people.person_id({"accountId": a, "displayName": ""})
                              for a in _mention_ids(c.get("body"), []) if a in people.by_account]
            rec = {"id": cid, "by": people.person_id(c.get("author")) or "Jira", "at": _ms(c.get("created")),
                   "text": adf_text(c.get("body")).strip()}
            if mention_people:
                rec["mentions"] = mention_people
            comments.append(rec)
        row["comments"] = sorted(comments, key=lambda c: c.get("at") or 0)

        # attachments: copy the ones not already on the bid
        have_a = {a.get("id") for a in ((bid or {}).get("attachments") or [])}
        atts = list((bid or {}).get("attachments") or [])
        for a in f.get("attachment") or []:
            aid = "jira-" + str(a.get("id"))
            if aid in have_a:
                continue
            report["attachments"] += 1
            report["attachment_bytes"] += a.get("size") or 0
            if dry:
                continue
            try:
                base, auth = _cfg()
                r = requests.get(a["content"], auth=auth, timeout=120)
                r.raise_for_status()
                name = a.get("filename") or "file"
                path = f"{bid_id}/{uuid.uuid4()}-" + re.sub(r"[^\w.\-]+", "_", name)
                sb.storage.from_(BID_FILES_BUCKET).upload(
                    path, r.content, {"content-type": a.get("mimeType") or "application/octet-stream"})
                atts.append({"id": aid, "name": name, "path": path, "size": a.get("size") or len(r.content),
                             "type": a.get("mimeType") or "", "at": _ms(a.get("created")),
                             "by": people.person_id(a.get("author")) or "Jira"})
            except Exception as e:
                log(f"[jira] {key}: couldn't copy attachment {a.get('filename')}: {e}")
        row["attachments"] = atts

        if is_new:
            row.update({
                "id": bid_id, "space_id": space["id"], "division_id": division_id,
                "num": next_num, "rank": next_rank, "created": (f.get("created") or "")[:10] or None,
                "fields": custom,
                "history": [{"by": "Jira", "at": now_ms, "field": "Jira issue", "from": "None", "to": key}],
            })
            if linked_project and linked_project not in by_source:
                row["source_project_id"] = linked_project
                by_source[linked_project] = row
            next_num += 1
            next_rank += 1000
            report["new"] += 1
            action = "new"
        else:
            merged = dict(sb.table("wf_bids").select("fields").eq("id", bid_id).execute().data[0].get("fields") or {})
            merged.update(custom)
            row["fields"] = merged
            hist = list(bid.get("history") or [])
            if not bid.get("jira_key"):
                hist.append({"by": "Jira", "at": now_ms, "field": "Jira issue", "from": "None", "to": key})
            row["history"] = hist
            report["updated"] += 1
            action = "update"

        parent_key = ((f.get("parent") or {}).get("key")) if row["type"] == "Subtask" else None
        if parent_key:
            pending_parents.append((bid_id, parent_key))
        report["issues"].append({
            "key": key, "title": row["title"], "action": action,
            "board": (f"{space.get('key')}-{row.get('num') or bid.get('num')}"),
            "status": st_name, "column": st.get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "comments": len(row["comments"]) - len((bid or {}).get("comments") or []),
            "attachments": len(f.get("attachment") or []),
        })
        if not dry:
            if is_new:
                sb.table("wf_bids").insert(row).execute()
            else:
                sb.table("wf_bids").update(row).eq("id", bid_id).execute()

    if not dry:
        for bid_id, pk in pending_parents:
            if pk in key_to_bid:
                sb.table("wf_bids").update({"parent_id": key_to_bid[pk]}).eq("id", bid_id).execute()
        if next_num > (space.get("next") or 1):
            sb.table("wf_spaces").update({"next": next_num}).eq("id", space["id"]).execute()
    log(f"[jira] {pkey}: {report['new']} new, {report['updated']} updated, {report['unchanged']} unchanged"
        + (" (preview)" if dry else ""))
    return report


# ---------------------------------------------------------------------------
# Runner (scanner_service): one job at a time, plus a 15-minute sync for
# divisions that have been imported (setting jira_sync).
# ---------------------------------------------------------------------------

import threading  # noqa: E402

STATE = {"running": False, "division_id": None, "dry": None, "report": None, "error": None,
         "started": None, "finished": None, "last_sync": {}}
_lock = threading.Lock()
SYNC_EVERY_S = 15 * 60


def start(division_id, dry):
    with _lock:
        if STATE["running"]:
            return False
        STATE.update(running=True, division_id=division_id, dry=dry, report=None, error=None,
                     started=datetime.now(timezone.utc).isoformat(), finished=None)

    def work():
        try:
            rep = run(division_id, dry=dry)
            if not dry:
                supabase_store.save_division_settings(division_id, {"jira_sync": True})
                STATE["last_sync"][division_id] = datetime.now(timezone.utc).isoformat()
            STATE["report"] = rep
        except Exception as e:
            STATE["error"] = str(e)
            print(f"[jira] {division_id}: {e}", file=sys.stderr, flush=True)
        finally:
            STATE["running"] = False
            STATE["finished"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=work, daemon=True).start()
    return True


def loop():
    time.sleep(90)
    while True:
        try:
            for d in supabase_store.load_divisions():
                if not supabase_store.get_division_settings(d["id"]).get("jira_sync"):
                    continue
                with _lock:
                    if STATE["running"]:
                        continue
                    STATE["running"] = True
                try:
                    run(d["id"], dry=False)
                    STATE["last_sync"][d["id"]] = datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    print(f"[jira] sync {d['id']}: {e}", file=sys.stderr, flush=True)
                finally:
                    STATE["running"] = False
        except Exception as e:
            print(f"[jira] sync loop: {e}", file=sys.stderr, flush=True)
        time.sleep(SYNC_EVERY_S)

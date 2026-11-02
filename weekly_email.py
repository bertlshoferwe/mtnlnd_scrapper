"""The Monday email draft (bid board Settings → Division → Monday email).

Every Monday at 6:00 AM Mountain time the scanner container builds, for each
division, the list of bids in its main board space (e.g. Geo Bids) that are due
that Monday through Sunday, grouped by team (alphabetical, "No team set" last),
each team with its Outside Sales people. Teams with no bids are listed too. Bids with no bid date that aren't in a
done column are listed under their team too. A bid counts as quoted when it is
in a done column.

The draft is saved in divisions.settings["weekly_email"] and stays the same
until the next Monday (or until someone presses Rebuild now). Nothing is sent —
the board copies it so it can be pasted into Outlook, opening line and all.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import supabase_store

TZ = ZoneInfo("America/Denver")
BUILD_HOUR = 6
TICK_S = 10 * 60


def week_start(now=None):
    """The Monday (a date) of the week `now` falls in, Mountain time."""
    d = (now or datetime.now(TZ)).astimezone(TZ).date()
    return d - timedelta(days=d.weekday())


def build(division_id, now=None):
    """Build and save the division's draft for the current week. Returns it."""
    c = supabase_store.get_client()
    monday = week_start(now)
    sunday = monday + timedelta(days=6)
    draft = {"week": monday.isoformat(), "end": sunday.isoformat(),
             "built_at": datetime.now(timezone.utc).isoformat(), "teams": []}

    space_id = supabase_store._main_space_id(division_id)
    if space_id:
        sp = c.table("wf_spaces").select("statuses").eq("id", space_id).limit(1).execute().data
        done = {s.get("id") for s in (sp[0].get("statuses") if sp else None) or [] if s.get("cat") == "done"}
        team_field = next((f["id"] for f in supabase_store._space_fields(space_id)
                           if f.get("type") == "team" and f.get("id")), None)
        teams = {t["id"]: t for t in c.table("wf_teams").select("id,name,members")
                 .eq("division_id", division_id).execute().data or []}
        sales = {p["id"]: p for p in c.table("wf_people").select("id,name,email,role").execute().data or []
                 if (p.get("role") or "").strip().lower() == "outside sales"}
        bids = (c.table("wf_bids").select("title,type,status,due,due_time,fields")
                .eq("space_id", space_id).execute().data or [])

        groups = {}
        for b in bids:
            if b.get("type") == "Subtask":
                continue
            due = b.get("due") or ""
            quoted = b.get("status") in done
            if due:
                if not monday.isoformat() <= due[:10] <= sunday.isoformat():
                    continue
            elif quoted:
                continue
            tid = ((b.get("fields") or {}).get(team_field) if team_field else None) or ""
            if tid not in teams:
                tid = ""
            groups.setdefault(tid, []).append({
                "title": b.get("title") or "", "due": due[:10],
                "time": str(b.get("due_time") or "")[:5], "quoted": quoted})

        for tid in teams:   # every team is listed, even with nothing due this week
            groups.setdefault(tid, [])
        for tid, rows in groups.items():
            rows.sort(key=lambda r: (r["due"] or "9999", r["time"] or "99:99", r["title"].lower()))
            t = teams.get(tid)
            people = sorted((sales[m] for m in (t or {}).get("members") or [] if m in sales),
                            key=lambda p: p.get("name") or "")
            draft["teams"].append({
                "name": t["name"] if t else "No team set",
                "sales": [{"name": p.get("name") or "", "email": (p.get("email") or "").strip()} for p in people],
                "bids": rows})
        draft["teams"].sort(key=lambda g: (g["name"] == "No team set", g["name"].lower()))

    supabase_store.save_division_extra(division_id, {"weekly_email": draft})
    return draft


def tick(now=None):
    """Build this week's draft for every division that doesn't have it yet,
    once it is Monday 6:00 AM or later (so a restart mid-week still catches up)."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    monday = week_start(now)
    if now < datetime(monday.year, monday.month, monday.day, BUILD_HOUR, tzinfo=TZ):
        return
    for d in supabase_store.load_divisions() or []:
        saved = (d.get("settings") or {}).get("weekly_email") or {}
        if saved.get("week") == monday.isoformat():
            continue
        try:
            dr = build(d["id"], now)
            print(f"[monday email] {d.get('name')}: built for the week of {dr['week']} "
                  f"({sum(len(g['bids']) for g in dr['teams'])} bid(s))", flush=True)
        except Exception as e:
            print(f"[monday email] {d.get('name')}: {e}", file=sys.stderr, flush=True)


def loop():
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[monday email] {e}", file=sys.stderr, flush=True)
        time.sleep(TICK_S)

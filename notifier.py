"""Bid board email notifications.

Runs inside the scanner container (one process, so no duplicate emails).
Every minute it looks at recently saved bids for three kinds of events:

  - the status changed                 → assignee, reporter, person fields
  - someone was put in a person field  → that person
  - a new comment                      → the same people, earlier commenters,
                                         and anyone @mentioned

Events are bundled per bid: nothing is sent until a bid has been quiet for
QUIET_S (or its oldest unsent event is MAX_WAIT_S old), then each person gets
one email listing everything. Nobody is emailed about their own changes.
wf_notify remembers, per bid, the newest event already handled; the "*" row
is when notifications were switched on, so older history is never emailed.

Off until SMTP_HOST is set (see .env.example).
"""

import html
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import supabase_store

SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = int((os.environ.get("SMTP_PORT") or "587").strip())
SMTP_USER = (os.environ.get("SMTP_USER") or "").strip()
SMTP_PASSWORD = (os.environ.get("SMTP_PASSWORD") or "").strip()
SMTP_FROM = (os.environ.get("SMTP_FROM") or "").strip() or SMTP_USER
BOARD_URL = ((os.environ.get("BOARD_URL") or "").strip()
             or "https://bidscout.westonb.com/workflow/").rstrip("/") + "/"

QUIET_S = 5 * 60
MAX_WAIT_S = 30 * 60
TICK_S = 60
LOOKBACK_S = 3 * 24 * 3600   # only bids saved in the last few days are checked

ROLE_FIELDS = {"Assignee": "the assignee", "Reporter": "the reporter"}


def enabled():
    return bool(SMTP_HOST and SMTP_FROM)


def _now_ms():
    return int(time.time() * 1000)


def _load_state(c):
    rows = c.table("wf_notify").select("bid_id,through").execute().data or []
    return {r["bid_id"]: int(r["through"]) for r in rows}


def _save_through(c, bid_id, through):
    c.table("wf_notify").upsert({
        "bid_id": bid_id, "through": int(through),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _person_fields(c):
    """{field id: field name} for every person-type custom field (e.g. Co-Assignee)."""
    out = {}
    for t in c.table("wf_templates").select("fields").execute().data or []:
        for f in t.get("fields") or []:
            if f.get("type") == "person" and f.get("id"):
                out[f["id"]] = f.get("name") or "a person field"
    return out


def _bid_events(bid, through, person_fields):
    """The bid's unsent events, oldest first: (at, kind, entry)."""
    names = set(ROLE_FIELDS) | set(person_fields.values())
    ev = []
    for h in bid.get("history") or []:
        at = int(h.get("at") or 0)
        if at <= through:
            continue
        if h.get("field") == "Status":
            ev.append((at, "status", h))
        elif h.get("field") in names:
            # Assignee keeps the id in "to"; others (newer entries) carry toId
            pid = h.get("toId") or (h.get("to") if h.get("field") == "Assignee" else "")
            if pid:
                ev.append((at, "role", dict(h, toId=pid)))
    for cm in bid.get("comments") or []:
        at = int(cm.get("at") or 0)
        if at > through and (cm.get("text") or "").strip():
            ev.append((at, "comment", cm))
    ev.sort(key=lambda e: e[0])
    return ev


def _involved(bid, person_fields):
    ids = {bid.get("assignee"), bid.get("reporter")}
    fields = bid.get("fields") or {}
    ids.update(fields.get(fid) for fid in person_fields)
    return {i for i in ids if i}


def _still_holds(bid, e, person_fields):
    """Is the person this event put in a role still in it?"""
    field, pid = e.get("field"), e.get("toId")
    if field == "Assignee":
        return bid.get("assignee") == pid
    if field == "Reporter":
        return bid.get("reporter") == pid
    fields = bid.get("fields") or {}
    return any(fields.get(fid) == pid for fid, name in person_fields.items() if name == field)


def _lines_for(bid, events, people, person_fields):
    """{person id: [line, ...]} — what each person should hear about."""
    involved = _involved(bid, person_fields)
    commenters = {cm.get("by") for cm in bid.get("comments") or [] if cm.get("by")}

    def who(pid):
        p = people.get(pid)
        return p["name"] if p else (pid if pid and len(pid) < 40 else "Someone")

    out = {}

    def add(pid, line, by):
        if pid and pid != by and pid in people:
            out.setdefault(pid, []).append(line)

    for _, kind, e in events:
        by = e.get("by") or ""
        if kind == "status":
            line = {"text": f"{who(by)} moved it from {e.get('from') or 'None'} to {e.get('to') or 'None'}"}
            for pid in involved:
                add(pid, line, by)
        elif kind == "role":
            if not _still_holds(bid, e, person_fields):
                continue   # someone else got it before the email went out (e.g. an automation's pick was changed)
            role = ROLE_FIELDS.get(e.get("field")) or e.get("field")
            add(e["toId"], {"text": f"{who(by)} made you {role}"}, by)
        else:
            mentions = set(e.get("mentions") or [])
            for pid in involved | commenters | mentions:
                verb = "mentioned you in a comment" if pid in mentions else "commented"
                add(pid, {"text": f"{who(by)} {verb}:", "quote": e.get("text") or ""}, by)
    return out


def _summary(lines):
    if len(lines) == 1:
        return lines[0]["text"].rstrip(":")
    return f"{len(lines)} updates"


def _send(to_addr, to_name, key, title, lines, link):
    subject = f"{key} {title}: {_summary(lines)}"
    text = [f"Hi {to_name.split(' ')[0]},", "", f"{key} {title}", ""]
    for ln in lines:
        text.append("- " + ln["text"])
        if ln.get("quote"):
            text.extend("    " + q for q in ln["quote"].splitlines())
    text += ["", f"Open the bid: {link}"]

    items = ""
    for ln in lines:
        q = ""
        if ln.get("quote"):
            q = ('<div style="margin:6px 0 0;padding:8px 12px;border-left:3px solid #c9d2e3;'
                 'color:#333;white-space:pre-wrap">' + html.escape(ln["quote"]) + "</div>")
        items += f'<li style="margin:0 0 10px">{html.escape(ln["text"])}{q}</li>'
    body = (
        '<div style="font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d2433;max-width:560px">'
        f'<p style="margin:0 0 4px;color:#5b6475;font-size:13px">{html.escape(key)}</p>'
        f'<h2 style="margin:0 0 16px;font-size:18px">{html.escape(title)}</h2>'
        f'<ul style="padding-left:18px;margin:0 0 20px">{items}</ul>'
        f'<a href="{html.escape(link)}" style="display:inline-block;background:#1f3a68;color:#fff;'
        'text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:600">Open the bid</a>'
        '<p style="margin:24px 0 0;color:#8a92a3;font-size:12px">You get these because you are on this bid '
        'or were mentioned in it.</p></div>'
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = f"{to_name} <{to_addr}>"
    msg.set_content("\n".join(text))
    msg.add_alternative(body, subtype="html")
    if SMTP_PORT == 465:
        conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    with conn as s:
        if SMTP_PORT != 465:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def tick():
    c = supabase_store.get_client()
    state = _load_state(c)
    now = _now_ms()
    baseline = state.get("*")
    if baseline is None:
        # First run: start from now, so existing history is never emailed.
        _save_through(c, "*", now)
        print("[notify] switched on — emailing changes from now on", flush=True)
        return

    since = max(baseline, now - LOOKBACK_S * 1000) - 60_000
    bids = (c.table("wf_bids")
            .select("id,space_id,num,title,assignee,reporter,fields,comments,history,updated_at")
            .gte("updated_at", datetime.fromtimestamp(since / 1000, timezone.utc).isoformat())
            .execute().data or [])
    if not bids:
        return
    person_fields = _person_fields(c)
    people = {p["id"]: p for p in c.table("wf_people").select("id,name,email").execute().data or []}
    keys = {s["id"]: s["key"] for s in c.table("wf_spaces").select("id,key").execute().data or []}

    for bid in bids:
        through = state.get(bid["id"], baseline)
        events = _bid_events(bid, through, person_fields)
        if not events:
            continue
        oldest, newest = events[0][0], events[-1][0]
        if now - newest < QUIET_S * 1000 and now - oldest < MAX_WAIT_S * 1000:
            continue   # still being worked on — bundle more

        key = f"{keys.get(bid['space_id'], 'BID')}-{bid['num']}"
        link = BOARD_URL + "?bid=" + bid["id"]
        for pid, lines in _lines_for(bid, events, people, person_fields).items():
            p = people[pid]
            if not (p.get("email") or "").strip():
                continue
            try:
                _send(p["email"].strip(), p["name"], key, bid["title"], lines, link)
                print(f"[notify] {key}: emailed {p['name']} ({len(lines)} update(s))", flush=True)
            except Exception as e:
                print(f"[notify] {key}: couldn't email {p['name']}: {e}", file=sys.stderr, flush=True)
        _save_through(c, bid["id"], newest)


def loop():
    if not enabled():
        print("[notify] off — set SMTP_HOST (and SMTP_USER / SMTP_PASSWORD) to email bid updates", flush=True)
        return
    print(f"[notify] on — sending from {SMTP_FROM} via {SMTP_HOST}:{SMTP_PORT}", flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[notify] {e}", file=sys.stderr, flush=True)
        time.sleep(TICK_S)

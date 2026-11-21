"""
Vendors' tables (migrations/006_vendors.sql), for the app server
(vendors_api.py) and the scan worker (vendor_catalog.py). Everything goes
through the service key — the browser never reads these tables itself.
"""

import re
from datetime import datetime, timezone

import supabase_store

BUCKET = "vendor-files"
STATES = ("UT", "ID", "CO", "NV", "WY", "AZ", "NM", "MT")
HINT = "run migrations/006_vendors.sql in Supabase"


def _db():
    return supabase_store.get_client()


def _now():
    return datetime.now(timezone.utc).isoformat()


def audit(actor, action, table, row_id=None, diff=None):
    try:
        _db().table("vendor_audit_log").insert({"actor": actor, "action": action, "table_name": table,
                                                "row_id": str(row_id) if row_id else None, "diff": diff}).execute()
    except Exception as e:
        print(f"  ! vendor audit log: {e}")


# ---------------------------------------------------------------------------
# Normalizing what people type
# ---------------------------------------------------------------------------

def norm_phone(text):
    """'801.555.0173' -> '(801) 555-0173', '801-555-0173 x12' -> '(801) 555-0173 ext. 12';
    anything that isn't a US number is kept as typed."""
    t = (text or "").strip()
    m = re.match(r"^(.*?)\s*(?:ext\.?|extension|x|#)\s*(\d{1,6})$", t, re.I)
    main, ext = (m.group(1), m.group(2)) if m else (t, "")
    d = re.sub(r"\D", "", main)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) != 10:
        return t
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" + (f" ext. {ext}" if ext else "")


def norm_email(text):
    return (text or "").strip().lower() or None


def norm_website(text):
    """'https://www.Example.com/' -> 'example.com/' style: no scheme, no www, no trailing slash."""
    t = (text or "").strip()
    t = re.sub(r"^https?://", "", t, flags=re.I)
    t = re.sub(r"^www\.", "", t, flags=re.I)
    return t.rstrip("/") or None


def domain_of(text):
    t = norm_website(text) or ""
    return t.split("/")[0].lower()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def search_ids(division_id, words):
    try:
        res = _db().rpc("vendor_search", {"p_division": division_id, "p_words": words}).execute()
    except Exception as e:
        raise ValueError(f"couldn't search vendors — {HINT}") from e
    return [r if isinstance(r, str) else r.get("vendor_search") for r in res.data or []]


def _in(table, col, ids, select="*"):
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 200):
        out += _db().table(table).select(select).in_(col, ids[i:i + 200]).execute().data or []
    return out


def bundle(vendor_ids, division_id):
    """{vendor_id: vendor row + "contacts" (each with "areas") + "items" (this
    division's active ones)} for these vendors."""
    if not vendor_ids:
        return {}
    vendors = {v["id"]: dict(v, contacts=[], items=[])
               for v in _in("vendors", "id", vendor_ids) if not v.get("deleted_at")}
    contacts = [c for c in _in("vendor_contacts", "vendor_id", vendors) if not c.get("deleted_at")]
    areas = {}
    for a in _in("vendor_contact_areas", "contact_id", [c["id"] for c in contacts]):
        areas.setdefault(a["contact_id"], []).append(a)
    for c in sorted(contacts, key=lambda c: (c["name"] or "").lower()):
        c["areas"] = _areas_out(areas.get(c["id"], []))
        c["primary"] = c["id"] == vendors[c["vendor_id"]].get("primary_contact_id")
        vendors[c["vendor_id"]]["contacts"].append(c)
    for v in vendors.values():   # the main contact (★) first
        v["contacts"].sort(key=lambda c: not c["primary"])
    for it in _in("vendor_items", "vendor_id", vendors):
        if it["division_id"] == division_id and it["active"]:
            vendors[it["vendor_id"]]["items"].append(it)
    for v in vendors.values():
        v["items"].sort(key=lambda i: i["name"].lower())
    return vendors


def _areas_out(rows):
    """Area rows -> [{"state": "UT", "regions": ["Wasatch Front"]}], states in the usual order."""
    by = {}
    for a in rows:
        by.setdefault(a["state_code"], [])
        if a.get("region_label"):
            by[a["state_code"]].append(a["region_label"])
    order = {s: i for i, s in enumerate(STATES)}
    return [{"state": s, "regions": by[s]} for s in sorted(by, key=lambda s: (order.get(s, 99), s))]


def get_vendor(vendor_id):
    r = _db().table("vendors").select("*").eq("id", vendor_id).limit(1).execute().data
    return r[0] if r and not r[0].get("deleted_at") else None


def all_vendors():
    return [v for v in _db().table("vendors").select("*").order("name").execute().data or [] if not v.get("deleted_at")]


def vendor_names():
    """[{"id", "name", "website"}] of every vendor, for the contact form's vendor box."""
    return [{"id": v["id"], "name": v["name"], "website": v.get("website")} for v in all_vendors()]


def find_vendor(name=None, website=None):
    """An existing vendor by exact name (any case), else by web domain."""
    vs = all_vendors()
    n = (name or "").strip().lower()
    hit = next((v for v in vs if n and v["name"].strip().lower() == n), None)
    d = domain_of(website)
    return hit or next((v for v in vs if d and domain_of(v.get("website")) == d), None)


def contact_with_email(email, not_id=None):
    e = norm_email(email)
    if not e:
        return None
    rows = _db().table("vendor_contacts").select("id,name,vendor_id").eq("email", e).is_("deleted_at", "null").execute().data
    return next((r for r in rows if r["id"] != not_id), None)


def _name_key(name):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (name or "").lower())).strip()


def find_duplicate(name=None, email=None, vendor_id=None, not_id=None):
    """The contact this one would duplicate, or None: anyone with the same email
    (at any vendor), or the same name at the same vendor. {"id", "name",
    "vendor_id", "why": "email" | "name"}. Phones aren't compared — people at
    one company often share a main line with different extensions."""
    hit = contact_with_email(email, not_id)
    if hit:
        return dict(hit, why="email")
    key = _name_key(name)
    if key and vendor_id:
        rows = _db().table("vendor_contacts").select("id,name,vendor_id").eq("vendor_id", vendor_id) \
            .is_("deleted_at", "null").execute().data or []
        hit = next((r for r in rows if r["id"] != not_id and _name_key(r["name"]) == key), None)
        if hit:
            return dict(hit, why="name")
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def add_to_division(vendor_id, division_id):
    _db().table("vendor_divisions").upsert({"vendor_id": vendor_id, "division_id": division_id},
                                           on_conflict="vendor_id,division_id").execute()


def create_vendor(name, website=None, division_id=None, actor=None):
    row = _db().table("vendors").insert({"name": name.strip(), "website": norm_website(website)}).execute().data[0]
    if division_id:
        add_to_division(row["id"], division_id)
    audit(actor, "create", "vendors", row["id"], {"name": row["name"], "website": row["website"]})
    return row


def update_vendor(vendor_id, patch, actor=None):
    if "website" in patch:
        patch["website"] = norm_website(patch["website"])
    patch["updated_at"] = _now()
    row = _db().table("vendors").update(patch).eq("id", vendor_id).execute().data[0]
    audit(actor, "update", "vendors", vendor_id, {k: v for k, v in patch.items() if k != "updated_at"})
    return row


def delete_vendor(vendor_id, actor=None):
    """Soft-deletes the vendor and its contacts; its items and pending changes go."""
    now = _now()
    _db().table("vendors").update({"deleted_at": now}).eq("id", vendor_id).execute()
    _db().table("vendor_contacts").update({"deleted_at": now}).eq("vendor_id", vendor_id).execute()
    _db().table("vendor_items").delete().eq("vendor_id", vendor_id).execute()
    _db().table("vendor_catalog_changes").delete().eq("vendor_id", vendor_id).execute()
    audit(actor, "delete", "vendors", vendor_id)


def _set_areas(contact_id, nationwide, areas):
    _db().table("vendor_contact_areas").delete().eq("contact_id", contact_id).execute()
    if nationwide:
        return
    rows = []
    for a in areas or []:
        st = str(a.get("state") or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", st):
            continue
        regions = [r.strip() for r in a.get("regions") or [] if str(r).strip()]
        rows += [{"contact_id": contact_id, "state_code": st, "region_label": r[:80]} for r in regions] or \
                [{"contact_id": contact_id, "state_code": st, "region_label": None}]
    if rows:
        _db().table("vendor_contact_areas").insert(rows).execute()


def _write_contact(contact_id, row):
    t = _db().table("vendor_contacts")
    return (t.update(row).eq("id", contact_id) if contact_id else t.insert(row)).execute().data[0]


def save_contact(contact_id, vendor_id, fields, actor=None):
    """Create (contact_id None) or update a contact. fields: name, title,
    phone, alt_phone, email, nationwide, areas, card_image_path."""
    row = {"vendor_id": vendor_id, "name": fields["name"].strip()[:120],
           "title": (fields.get("title") or "").strip()[:120] or None,
           "phone": norm_phone(fields.get("phone"))[:40] or None,
           "alt_phone": norm_phone(fields.get("alt_phone"))[:40] or None,
           "email": norm_email(fields.get("email")),
           "nationwide": bool(fields.get("nationwide")), "updated_at": _now()}
    if fields.get("card_image_path"):
        row["card_image_path"] = fields["card_image_path"]
    if not contact_id:
        row["created_by"] = actor
    try:
        saved = _write_contact(contact_id, row)
    except Exception as e:
        if "alt_phone" not in str(e):
            raise
        print("  ! vendor_contacts has no alt_phone yet — run migrations/007_vendor_alt_phone.sql")
        saved = _write_contact(contact_id, {k: v for k, v in row.items() if k != "alt_phone"})
    _set_areas(saved["id"], row["nationwide"], fields.get("areas"))
    audit(actor, "update" if contact_id else "create", "vendor_contacts", saved["id"],
          {k: v for k, v in row.items() if k != "updated_at"} | {"areas": fields.get("areas") or []})
    return saved


def get_contact(contact_id):
    r = _db().table("vendor_contacts").select("*").eq("id", contact_id).limit(1).execute().data
    return r[0] if r and not r[0].get("deleted_at") else None


def set_contact_card(contact_id, path, actor=None):
    """Swap a contact's card photo (a re-crop), removing the old one."""
    old = (get_contact(contact_id) or {}).get("card_image_path")
    _db().table("vendor_contacts").update({"card_image_path": path, "updated_at": _now()}).eq("id", contact_id).execute()
    if old and old != path:
        remove_file(old)
    audit(actor, "update", "vendor_contacts", contact_id, {"card_image_path": path})


def set_main_contact(vendor_id, contact_id, actor=None):
    """The vendor's main contact (★), or None for none."""
    try:
        _db().table("vendors").update({"primary_contact_id": contact_id}).eq("id", vendor_id).execute()
    except Exception as e:
        raise ValueError("couldn't save the main contact — run migrations/008_vendor_main_contact.sql in Supabase") from e
    audit(actor, "update", "vendors", vendor_id, {"primary_contact_id": contact_id})


def delete_contact(contact_id, actor=None):
    _db().table("vendor_contacts").update({"deleted_at": _now()}).eq("id", contact_id).execute()
    try:   # a deleted contact can't be anyone's main contact (only soft-deleted, so the foreign key doesn't see it)
        _db().table("vendors").update({"primary_contact_id": None}).eq("primary_contact_id", contact_id).execute()
    except Exception:
        pass
    audit(actor, "delete", "vendor_contacts", contact_id)


def add_item(vendor_id, division_id, name, source="manual", actor=None, log=True):
    """Adds (or brings back) an item; returns the row. log=False skips the
    audit log (an approved review is logged once, as a whole)."""
    name = re.sub(r"\s+", " ", name).strip()[:200]
    hit = [i for i in _db().table("vendor_items").select("*").eq("vendor_id", vendor_id)
           .eq("division_id", division_id).execute().data if i["name"].lower() == name.lower()]
    if hit:
        row = _db().table("vendor_items").update({"active": True, "last_seen_at": _now()}).eq("id", hit[0]["id"]).execute().data[0]
    else:
        row = _db().table("vendor_items").insert({"vendor_id": vendor_id, "division_id": division_id,
                                                  "name": name, "source": source}).execute().data[0]
    add_to_division(vendor_id, division_id)
    if log:
        audit(actor, "create", "vendor_items", row["id"], {"name": name, "division_id": division_id, "source": source})
    return row


def remove_item(item_id, actor=None):
    """Takes an item off the list (kept inactive, so a later pull that finds it
    again goes through review rather than slipping straight back)."""
    row = _db().table("vendor_items").update({"active": False}).eq("id", item_id).execute().data
    audit(actor, "delete", "vendor_items", item_id, {"name": row[0]["name"]} if row else None)


def items_of(vendor_id, division_id=None):
    q = _db().table("vendor_items").select("*").eq("vendor_id", vendor_id)
    if division_id:
        q = q.eq("division_id", division_id)
    return q.execute().data or []


def divisions_of(vendor_id):
    return sorted({r["division_id"] for r in _db().table("vendor_divisions").select("division_id")
                  .eq("vendor_id", vendor_id).execute().data or []}
                  | {i["division_id"] for i in items_of(vendor_id)})


# ---------------------------------------------------------------------------
# Files (vendor-files bucket)
# ---------------------------------------------------------------------------

def put_file(path, data, content_type):
    _db().storage.from_(BUCKET).upload(path, data, file_options={"content-type": content_type, "upsert": "true"})
    return path


def get_file(path):
    try:
        return _db().storage.from_(BUCKET).download(path)
    except Exception:
        return None


def remove_file(path):
    try:
        _db().storage.from_(BUCKET).remove([path])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pulls and the review queue
# ---------------------------------------------------------------------------

def start_run(vendor_id):
    return _db().table("vendor_scrape_runs").insert({"vendor_id": vendor_id, "status": "running"}).execute().data[0]


def finish_run(run_id, ok, method=None, reason=None):
    _db().table("vendor_scrape_runs").update({"status": "ok" if ok else "failed", "method": method,
                                              "failure_reason": None if ok else (reason or "unknown")[:500],
                                              "finished_at": _now()}).eq("id", run_id).execute()


def fail_running(reason, vendor_id=None, skip=()):
    """Mark pulls still 'running' as failed (one vendor's, or every one), except
    the vendors in skip — those really are in progress. For when the worker
    starts (nothing can be running then) and for Stop on a pull nothing's doing."""
    q = _db().table("vendor_scrape_runs").select("id,vendor_id").eq("status", "running")
    if vendor_id:
        q = q.eq("vendor_id", vendor_id)
    ids = [r["id"] for r in q.execute().data or [] if r["vendor_id"] not in skip]
    for i in range(0, len(ids), 200):
        _db().table("vendor_scrape_runs").update({"status": "failed", "failure_reason": reason, "finished_at": _now()}) \
            .in_("id", ids[i:i + 200]).execute()
    return len(ids)


def last_runs():
    """{vendor_id: its most recent pull}."""
    out = {}
    for r in _db().table("vendor_scrape_runs").select("*").order("started_at", desc=True).limit(2000).execute().data or []:
        out.setdefault(r["vendor_id"], r)
    return out


def had_good_run(vendor_id, before_run_id):
    return any(r["id"] != before_run_id and r["status"] == "ok"
               for r in _db().table("vendor_scrape_runs").select("id,status").eq("vendor_id", vendor_id).execute().data or [])


def queue_changes(vendor_id, division_id, run_id, added, removed):
    """Pending review rows; a name already waiting for this vendor/division isn't queued twice."""
    waiting = {(c["kind"], c["item_name"].lower()) for c in _db().table("vendor_catalog_changes").select("kind,item_name")
               .eq("vendor_id", vendor_id).eq("division_id", division_id).eq("status", "pending").execute().data or []}
    rows = [{"vendor_id": vendor_id, "division_id": division_id, "kind": k, "item_name": n[:200], "run_id": run_id}
            for k, names in (("added", added), ("removed", removed)) for n in names if (k, n.lower()) not in waiting]
    if rows:
        _db().table("vendor_catalog_changes").insert(rows).execute()
    return len(rows)


def pending_changes():
    return _db().table("vendor_catalog_changes").select("*").eq("status", "pending").order("created_at").execute().data or []


def decide_changes(vendor_id, division_id, approve, actor=None):
    """Approve (apply to vendor_items) or reject every pending change for a vendor in a division."""
    rows = [c for c in pending_changes() if c["vendor_id"] == vendor_id and c["division_id"] == division_id]
    if approve:
        for c in rows:
            if c["kind"] == "added":
                add_item(vendor_id, division_id, c["item_name"], source="scrape", log=False)
            else:
                for i in items_of(vendor_id, division_id):
                    if i["name"].lower() == c["item_name"].lower() and i["source"] != "manual":
                        _db().table("vendor_items").update({"active": False}).eq("id", i["id"]).execute()
    ids = [c["id"] for c in rows]
    for i in range(0, len(ids), 200):
        _db().table("vendor_catalog_changes").update({"status": "approved" if approve else "rejected"}).in_("id", ids[i:i + 200]).execute()
    audit(actor, "approve" if approve else "reject", "vendor_catalog_changes", vendor_id,
          {"division_id": division_id, "changes": [[c["kind"], c["item_name"]] for c in rows]})
    return len(rows)

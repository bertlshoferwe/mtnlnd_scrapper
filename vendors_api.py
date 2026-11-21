"""
Vendors: the page at /vendors/ (vendors/index.html) and its API under
/api/vendors. A directory of vendors and their salesmen ("contacts"), shared
by every division; what a vendor sells is kept per division.

  GET  /api/vendors?division=&q=&state=&with_contact=1   the list / search
  GET  /api/vendors/names                                every vendor's name and web address (the contact form)
  GET/PATCH/DELETE /api/vendors/<id>                     one vendor (GET takes ?division=)
  GET/POST/DELETE  /api/vendors/<id>/logo                its logo (POST: an upload, or {"pull": true})
  POST /api/vendors/<id>/items, DELETE /api/vendors/items/<item_id>
  POST /api/vendors/contacts, PATCH/DELETE /api/vendors/contacts/<id>
  GET  /api/vendors/contacts/duplicate?name=&email=&vendor=&not=   who this contact would duplicate
  POST /api/vendors/cards                                a scanned card's photo (kept with the contact)
  POST /api/vendors/contacts/<id>/card                   a re-cropped photo for a saved contact's card
  GET  /api/vendors/cards/<contact_id>                   that photo
  /api/vendors/admin/...                                 catalog review, failed pulls, vendor websites

There's no sign-in yet (as on the rest of the app). Every write asks
_can_edit() and every admin route _is_admin(); when sign-in comes, those two
are the only places that change.
"""

import os
import re
import uuid

import requests
from flask import Blueprint, Response, jsonify, redirect, request, send_from_directory

import supabase_store
import vendors_store as vs

bp = Blueprint("vendors", __name__)

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDORS_DIR = os.path.join(_REPO_ROOT, "vendors")
MAX_IMAGE = 8 * 1024 * 1024
MAX_PDF = 40 * 1024 * 1024
_IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg"}


def _can_edit():
    return True


def _is_admin():
    return True


def _actor():
    return (request.headers.get("X-Vendors-User") or "").strip()[:120] or None


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _forbidden():
    return _err("you don't have access to that", 403)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def page_html(config_js):
    with open(os.path.join(VENDORS_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    return page.replace('<script src="config.js"></script>', "<script>" + config_js + "</script>", 1)


@bp.route("/vendors")
def vendors_redirect():
    qs = request.query_string.decode()
    return redirect("/vendors/" + ("?" + qs if qs else ""), code=301)


@bp.route("/vendors/manifest.webmanifest")
def vendors_manifest():
    resp = send_from_directory(VENDORS_DIR, "manifest.webmanifest", mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/vendors/sw.js")
def vendors_sw():
    resp = send_from_directory(VENDORS_DIR, "sw.js", mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/vendors/"
    return resp


@bp.route("/vendors/icons/<path:filename>")
def vendors_icon(filename):
    return send_from_directory(os.path.join(VENDORS_DIR, "icons"), filename, max_age=86400)


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------

def _logo_url(v):
    if not v.get("logo_path"):
        return None
    stamp = re.sub(r"\D", "", str(v.get("logo_pulled_at") or v.get("updated_at") or ""))[:14]
    return f"/api/vendors/{v['id']}/logo?v={stamp}"


def _covers(c, state):
    return c["nationwide"] or any(a["state"] == state for a in c["areas"])


def _unset(c):
    return not c["nationwide"] and not c["areas"]


def _area_text(c):
    if c["nationwide"]:
        return "Nationwide"
    if _unset(c):
        return "Coverage not set"
    return ", ".join(a["state"] + (f" ({', '.join(a['regions'])})" if a["regions"] else "") for a in c["areas"])


def _contact_out(c):
    return {"id": c["id"], "name": c["name"], "title": c.get("title") or "", "phone": c.get("phone") or "",
            "alt_phone": c.get("alt_phone") or "", "email": c.get("email") or "", "nationwide": c["nationwide"], "areas": c["areas"],
            "unset": _unset(c), "area_text": _area_text(c), "has_card": bool(c.get("card_image_path")),
            "card_v": re.sub(r"\D", "", str(c.get("updated_at") or ""))[:14]}


def _vendor_out(v):
    return {"id": v["id"], "name": v["name"], "website": v.get("website") or "", "logo": _logo_url(v),
            "logo_source": v.get("logo_source"), "logo_pulled_at": v.get("logo_pulled_at")}


@bp.route("/api/vendors", methods=["GET"])
def api_vendor_list():
    """The vendors in a division, filtered and searched. Each word of q must
    match somewhere in the vendor (the database does that, vendor_search()).
    state hides the contacts who don't cover it, except ones with no coverage
    set, who stay (marked) after the rest; nationwide contacts always match."""
    division = (request.args.get("division") or "").strip()
    if not division or not supabase_store.get_division(division):
        return _err("pick a division", 404)
    words = [w for w in re.split(r"\s+", (request.args.get("q") or "").strip().lower()) if w][:8]
    state = (request.args.get("state") or "").strip().upper()
    state = state if re.fullmatch(r"[A-Z]{2}", state) else ""
    with_contact = request.args.get("with_contact") in ("1", "true")
    try:
        ids = vs.search_ids(division, words)
        vendors = vs.bundle(ids, division)
    except ValueError as e:
        return _err(str(e), 500)

    def hay(c):
        return " ".join([c["name"], c.get("title") or "", c.get("email") or "", c.get("phone") or "",
                         c.get("alt_phone") or "", _area_text(c)]).lower()

    out = []
    for v in vendors.values():
        cs = v["contacts"]
        show = [c for c in cs if not state or _covers(c, state)] + [c for c in cs if state and _unset(c)]
        if with_contact and not show:
            continue
        if state and cs and not show:
            continue
        matched = [c for c in show if any(w in hay(c) for w in words)] if words else []
        rest = [c for c in show if c not in matched]
        ordered = matched + [c for c in rest if not _unset(c) or not state] + [c for c in rest if _unset(c) and state]
        items = [i["name"] for i in v["items"] if any(w in i["name"].lower() for w in words)] if words else []
        states = sorted({a["state"] for c in cs for a in c["areas"]} | ({"All"} if any(c["nationwide"] for c in cs) else set()))
        out.append(dict(_vendor_out(v), contacts=[_contact_out(c) for c in ordered], contact_count=len(cs),
                        matched_items=items, item_count=len(v["items"]), states=states))
    out.sort(key=lambda r: r["name"].lower())
    return jsonify({"vendors": out})


@bp.route("/api/vendors/names", methods=["GET"])
def api_vendor_names():
    return jsonify(vs.vendor_names())


@bp.route("/api/vendors/<vendor_id>", methods=["GET"])
def api_vendor_get(vendor_id):
    division = (request.args.get("division") or "").strip()
    v = vs.bundle([vendor_id], division).get(vendor_id)
    if not v:
        return _err("that vendor is gone", 404)
    runs = vs.last_runs()
    return jsonify(dict(_vendor_out(v), contacts=[_contact_out(c) for c in v["contacts"]],
                        items=[{"id": i["id"], "name": i["name"], "source": i["source"]} for i in v["items"]],
                        last_pull=runs.get(vendor_id)))


@bp.route("/api/vendors/<vendor_id>", methods=["PATCH"])
def api_vendor_update(vendor_id):
    if not _can_edit():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    patch = {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return _err("the vendor needs a name")
        patch["name"] = name[:120]
    if "website" in data:
        patch["website"] = (data.get("website") or "").strip()[:200]
    if not vs.get_vendor(vendor_id):
        return _err("that vendor is gone", 404)
    return jsonify({"ok": True, "vendor": _vendor_out(vs.update_vendor(vendor_id, patch, _actor()))})


@bp.route("/api/vendors/<vendor_id>", methods=["DELETE"])
def api_vendor_delete(vendor_id):
    if not _can_edit():
        return _forbidden()
    if not vs.get_vendor(vendor_id):
        return _err("that vendor is gone", 404)
    vs.delete_vendor(vendor_id, _actor())
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Logos
# ---------------------------------------------------------------------------

@bp.route("/api/vendors/<vendor_id>/logo", methods=["GET"])
def api_vendor_logo(vendor_id):
    v = vs.get_vendor(vendor_id)
    data = vs.get_file(v["logo_path"]) if v and v.get("logo_path") else None
    if not data:
        return _err("no logo", 404)
    ext = v["logo_path"].rsplit(".", 1)[-1]
    mime = next((m for m, e in _IMAGE_TYPES.items() if e == ext), "application/octet-stream")
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=604800"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"   # an uploaded SVG can't run script
    return resp


def _save_logo(vendor_id, data, mime, source):
    ext = _IMAGE_TYPES.get(mime)
    if not ext:
        raise ValueError("that isn't a picture (PNG, JPEG, WebP, GIF or SVG)")
    if len(data) > MAX_IMAGE:
        raise ValueError("that picture is too big (8 MB at most)")
    v = vs.get_vendor(vendor_id)
    path = f"logos/{vendor_id}-{uuid.uuid4().hex[:8]}.{ext}"
    vs.put_file(path, data, mime)
    if v.get("logo_path"):
        vs.remove_file(v["logo_path"])
    return vs.update_vendor(vendor_id, {"logo_path": path, "logo_source": source,
                                        "logo_pulled_at": vs._now()}, _actor())


@bp.route("/api/vendors/<vendor_id>/logo", methods=["POST"])
def api_vendor_logo_set(vendor_id):
    """A file upload (logo_source manual), or {"pull": true}: ask the scan worker
    to pull it from their website now (never replaces an uploaded one)."""
    if not _can_edit():
        return _forbidden()
    if not vs.get_vendor(vendor_id):
        return _err("that vendor is gone", 404)
    f = request.files.get("file")
    if f is None:
        data = request.get_json(force=True, silent=True) or {}
        if data.get("pull"):
            return _scanner("/vendor-pull", {"vendor_id": vendor_id, "what": "logo", "force": True}, wait=True)
        return _err("send a picture")
    try:
        v = _save_logo(vendor_id, f.read(MAX_IMAGE + 1), f.mimetype, "manual")
    except ValueError as e:
        return _err(str(e))
    return jsonify({"ok": True, "vendor": _vendor_out(v)})


@bp.route("/api/vendors/<vendor_id>/logo", methods=["DELETE"])
def api_vendor_logo_clear(vendor_id):
    if not _can_edit():
        return _forbidden()
    v = vs.get_vendor(vendor_id)
    if not v:
        return _err("that vendor is gone", 404)
    if v.get("logo_path"):
        vs.remove_file(v["logo_path"])
    return jsonify({"ok": True, "vendor": _vendor_out(vs.update_vendor(vendor_id, {
        "logo_path": None, "logo_source": None, "logo_pulled_at": None}, _actor()))})


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@bp.route("/api/vendors/<vendor_id>/items", methods=["POST"])
def api_item_add(vendor_id):
    if not _can_edit():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    division, name = (data.get("division") or "").strip(), (data.get("name") or "").strip()
    if not name:
        return _err("type the item's name")
    if not division or not supabase_store.get_division(division) or not vs.get_vendor(vendor_id):
        return _err("that vendor or division is gone", 404)
    row = vs.add_item(vendor_id, division, name, source="manual", actor=_actor())
    return jsonify({"ok": True, "item": {"id": row["id"], "name": row["name"], "source": row["source"]}})


@bp.route("/api/vendors/items/<item_id>", methods=["DELETE"])
def api_item_remove(item_id):
    if not _can_edit():
        return _forbidden()
    vs.remove_item(item_id, _actor())
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def _contact_fields(data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("add a name first")
    email = vs.norm_email(data.get("email"))
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("that email doesn't look right")
    areas = [{"state": str(a.get("state") or "").upper(), "regions": [str(r) for r in a.get("regions") or []]}
             for a in data.get("areas") or [] if isinstance(a, dict)]
    return {"name": name, "title": data.get("title"), "phone": data.get("phone"), "alt_phone": data.get("alt_phone"), "email": email,
            "nationwide": bool(data.get("nationwide")), "areas": areas}


def _vendor_for(data, division):
    """The contact's vendor: by id, else an existing one by name, else a new one."""
    if data.get("vendor_id"):
        v = vs.get_vendor(data["vendor_id"])
        if v:
            return v
    name = (data.get("vendor") or "").strip()
    if not name:
        raise ValueError("pick or type a vendor")
    return vs.find_vendor(name=name) or vs.create_vendor(name[:120], data.get("vendor_website"), division, _actor())


def _card_logo(vendor, data):
    """A logo picked out of a scanned card, used when the vendor has none."""
    path = data.get("card_logo_path")
    if path and str(path).startswith("cards/") and not vendor.get("logo_path"):
        vs.update_vendor(vendor["id"], {"logo_path": path, "logo_source": "card", "logo_pulled_at": vs._now()}, _actor())


def _existing_vendor(data):
    """The contact's vendor if it already exists (by id or name), without making one."""
    v = vs.get_vendor(data["vendor_id"]) if data.get("vendor_id") else None
    return v or vs.find_vendor(name=(data.get("vendor") or "").strip() or None)


def _duplicate(fields, data, not_id=None):
    """A 409 answer when this contact already exists, or None. The page shows
    who it is and opens them instead of adding a second entry."""
    v = _existing_vendor(data)
    hit = vs.find_duplicate(fields["name"], fields.get("email"), v["id"] if v else None, not_id)
    if not hit:
        return None
    hv = vs.get_vendor(hit["vendor_id"])
    out = {"id": hit["id"], "name": hit["name"], "vendor_id": hit["vendor_id"], "vendor": hv["name"] if hv else "", "why": hit["why"]}
    msg = (f"{hit['name']}{' at ' + out['vendor'] if out['vendor'] else ''} already has this email"
           if hit["why"] == "email" else f"{hit['name']} is already a contact at {out['vendor']}")
    return jsonify({"error": msg + " — not added twice", "duplicate": out}), 409


@bp.route("/api/vendors/contacts", methods=["POST"])
def api_contact_create():
    if not _can_edit():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    division = (data.get("division") or "").strip()
    if not division or not supabase_store.get_division(division):
        return _err("pick a division", 404)
    try:
        fields = _contact_fields(data)
        dup = _duplicate(fields, data)
        if dup:
            return dup
        vendor = _vendor_for(data, division)
    except ValueError as e:
        return _err(str(e))
    card = data.get("card_image_path")
    if card and str(card).startswith("cards/"):
        fields["card_image_path"] = card
    vs.add_to_division(vendor["id"], division)
    _card_logo(vendor, data)
    c = vs.save_contact(None, vendor["id"], fields, _actor())
    return jsonify({"ok": True, "contact_id": c["id"], "vendor_id": vendor["id"]})


@bp.route("/api/vendors/contacts/<contact_id>", methods=["PATCH"])
def api_contact_update(contact_id):
    if not _can_edit():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    division = (data.get("division") or "").strip()
    if not vs.get_contact(contact_id):
        return _err("that contact is gone", 404)
    try:
        fields = _contact_fields(data)
        dup = _duplicate(fields, data, not_id=contact_id)
        if dup:
            return dup
        vendor = _vendor_for(data, division)
    except ValueError as e:
        return _err(str(e))
    if division:
        vs.add_to_division(vendor["id"], division)
    vs.save_contact(contact_id, vendor["id"], fields, _actor())
    return jsonify({"ok": True, "contact_id": contact_id, "vendor_id": vendor["id"]})


@bp.route("/api/vendors/contacts/<contact_id>", methods=["DELETE"])
def api_contact_delete(contact_id):
    if not _can_edit():
        return _forbidden()
    vs.delete_contact(contact_id, _actor())
    return jsonify({"ok": True})


@bp.route("/api/vendors/contacts/duplicate", methods=["GET"])
def api_contact_duplicate():
    """?name=&email=&vendor_id=|vendor=&not= — who this contact would duplicate (see vs.find_duplicate)."""
    a = request.args
    v = _existing_vendor({"vendor_id": a.get("vendor_id"), "vendor": a.get("vendor")})
    hit = vs.find_duplicate(a.get("name"), a.get("email"), v["id"] if v else None, a.get("not") or None)
    if not hit:
        return jsonify({"match": None})
    hv = vs.get_vendor(hit["vendor_id"])
    return jsonify({"match": {"id": hit["id"], "name": hit["name"], "vendor_id": hit["vendor_id"],
                              "vendor": hv["name"] if hv else "", "why": hit["why"]}})


# ---------------------------------------------------------------------------
# Business cards
# ---------------------------------------------------------------------------

@bp.route("/api/vendors/cards", methods=["POST"])
def api_card_upload():
    """The card photo (already shrunk by the page) and, optionally, a logo cut
    from it ("logo"). Kept in the private bucket; the saved contact points at
    them. The reading itself happens in the page (see CARD_READER there)."""
    if not _can_edit():
        return _forbidden()
    out = {}
    for field, prefix in (("file", "card"), ("logo", "logo")):
        f = request.files.get(field)
        if f is None:
            continue
        data = f.read(MAX_IMAGE + 1)
        ext = _IMAGE_TYPES.get(f.mimetype)
        if not ext or ext == "svg" or len(data) > MAX_IMAGE:
            return _err("send a photo (JPEG or PNG, 8 MB at most)")
        out[field] = vs.put_file(f"cards/{prefix}-{uuid.uuid4().hex}.{ext}", data, f.mimetype)
    if "file" not in out:
        return _err("send a photo")
    return jsonify({"ok": True, "path": out["file"], "logo_path": out.get("logo")})


@bp.route("/api/vendors/contacts/<contact_id>/card", methods=["POST"])
def api_contact_card(contact_id):
    """A new photo for a contact's saved card (cropped on the page)."""
    if not _can_edit():
        return _forbidden()
    if not vs.get_contact(contact_id):
        return _err("that contact is gone", 404)
    f = request.files.get("file")
    data = f.read(MAX_IMAGE + 1) if f else b""
    ext = _IMAGE_TYPES.get(f.mimetype) if f else None
    if not ext or ext == "svg" or not data or len(data) > MAX_IMAGE:
        return _err("send a photo (JPEG or PNG, 8 MB at most)")
    path = vs.put_file(f"cards/card-{uuid.uuid4().hex}.{ext}", data, f.mimetype)
    vs.set_contact_card(contact_id, path, _actor())
    return jsonify({"ok": True})


@bp.route("/api/vendors/cards/<contact_id>", methods=["GET"])
def api_card_get(contact_id):
    """The contact's scanned card. Sent through here rather than a signed
    storage link: Supabase's own address (http://kong:8000 on the server)
    isn't one a browser can reach."""
    c = vs.get_contact(contact_id)
    data = vs.get_file(c["card_image_path"]) if c and c.get("card_image_path") else None
    if not data:
        return _err("no card for this contact", 404)
    ext = c["card_image_path"].rsplit(".", 1)[-1]
    resp = Response(data, mimetype=next((m for m, e in _IMAGE_TYPES.items() if e == ext), "image/jpeg"))
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---------------------------------------------------------------------------
# Admin: catalog review, failed pulls, vendor websites
# ---------------------------------------------------------------------------

def _scanner(path, body, wait=False):
    base = (os.environ.get("SCANNER_URL") or "").rstrip("/")
    if not base:
        return _err("SCANNER_URL isn't set, so pulls can't run", 500)
    try:
        resp = requests.post(base + path, json=body, timeout=120 if wait else 15)
    except requests.RequestException as e:
        return _err(f"couldn't reach the scan worker: {e}", 424)
    return Response(resp.content, status=resp.status_code, mimetype="application/json")


def _division_names():
    return {d["id"]: d["name"] for d in supabase_store.load_divisions()}


@bp.route("/api/vendors/admin/review", methods=["GET"])
def api_admin_review():
    """Pending changes, one group per vendor and division."""
    if not _is_admin():
        return _forbidden()
    names, groups = _division_names(), {}
    for c in vs.pending_changes():
        if c["division_id"] not in names:
            continue   # a division since deleted in Bid Scout
        g = groups.setdefault((c["vendor_id"], c["division_id"]), {"added": [], "removed": [], "at": c["created_at"]})
        g[c["kind"]].append(c["item_name"])
    vendors = {v["id"]: v for v in vs.all_vendors()}
    out = [dict(_vendor_out(vendors[vid]), division_id=div, division=names[div], **g)
           for (vid, div), g in groups.items() if vid in vendors]
    return jsonify(sorted(out, key=lambda r: (r["name"].lower(), r["division"])))


@bp.route("/api/vendors/admin/review", methods=["POST"])
def api_admin_decide():
    if not _is_admin():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    n = vs.decide_changes(data.get("vendor_id"), data.get("division_id"), bool(data.get("approve")), _actor())
    return jsonify({"ok": True, "changes": n})


@bp.route("/api/vendors/admin/vendors", methods=["GET"])
def api_admin_vendors():
    """Every vendor with its last pull — the Vendors and Failed pulls tabs."""
    if not _is_admin():
        return _forbidden()
    runs = vs.last_runs()
    return jsonify([dict(_vendor_out(v), last_pull=runs.get(v["id"])) for v in vs.all_vendors()])


@bp.route("/api/vendors/admin/vendors", methods=["POST"])
def api_admin_add_vendor():
    """{"name", "website", "division"}: a vendor added by its website; its first pull starts now."""
    if not _is_admin():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _err("add a vendor name")
    division = (data.get("division") or "").strip() or None
    if vs.find_vendor(name=name):
        return _err(f"{name} is already in Vendors")
    v = vs.create_vendor(name[:120], data.get("website"), division, _actor())
    if v.get("website"):
        _scanner("/vendor-pull", {"vendor_id": v["id"], "what": "all"})
    return jsonify({"ok": True, "vendor": _vendor_out(v)})


@bp.route("/api/vendors/admin/pull", methods=["POST"])
def api_admin_pull():
    """{"vendor_id"}: pull the vendor's items (and logo) now."""
    if not _is_admin():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    if not vs.get_vendor(data.get("vendor_id") or ""):
        return _err("that vendor is gone", 404)
    return _scanner("/vendor-pull", {"vendor_id": data["vendor_id"], "what": "all"})


@bp.route("/api/vendors/admin/pull-all", methods=["POST"])
def api_admin_pull_all():
    """Pull now: every vendor with a website, whenever they were last pulled."""
    if not _is_admin():
        return _forbidden()
    return _scanner("/vendor-pull-all", {})


@bp.route("/api/vendors/admin/stop", methods=["POST"])
def api_admin_stop():
    """{"vendor_id"}: stop that vendor's pull; {}: stop every pull going."""
    if not _is_admin():
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    return _scanner("/vendor-stop", {"vendor_id": data.get("vendor_id") or None})


@bp.route("/api/vendors/admin/pdf", methods=["POST"])
def api_admin_pdf():
    """A PDF catalog for a vendor (form: vendor_id, file). The scan worker reads
    it into proposed items, which go to To review."""
    if not _is_admin():
        return _forbidden()
    vid = request.form.get("vendor_id") or ""
    f = request.files.get("file")
    if not vs.get_vendor(vid) or f is None:
        return _err("send a vendor and a PDF")
    data = f.read(MAX_PDF + 1)
    if len(data) > MAX_PDF or not data.startswith(b"%PDF"):
        return _err("send a PDF (40 MB at most)")
    path = vs.put_file(f"catalogs/{vid}-{uuid.uuid4().hex[:8]}.pdf", data, "application/pdf")
    return _scanner("/vendor-pull", {"vendor_id": vid, "what": "pdf", "pdf_path": path})

"""
Vendors' rules, with the database swapped for fakes (vendor_search() itself
is SQL, in migrations/006_vendors.sql, and isn't run here).

  SUPABASE_URL=http://x SUPABASE_SERVICE_KEY=x venv/bin/python -m unittest tests.test_vendors
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "http://x")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

import vendor_catalog  # noqa: E402
import vendors_api  # noqa: E402
import vendors_store  # noqa: E402
from api.index import app  # noqa: E402


def contact(cid, name, nationwide=False, areas=(), **kw):
    return dict({"id": cid, "name": name, "title": "", "phone": "", "email": "", "nationwide": nationwide,
                 "areas": [{"state": s, "regions": list(r)} for s, r in areas]}, **kw)


VENDORS = {
    "v1": {"id": "v1", "name": "Wasatch Pipe Supply", "website": "wasatchpipe.example",
           "contacts": [contact("c1", "Dale Hutchins", areas=[("UT", ["Wasatch Front"])]),
                        contact("c2", "Marisol Vega", areas=[("ID", [])]),
                        contact("c3", "Tom Brandt")],                       # coverage not set
           "items": [{"id": "i1", "name": "HDPE pipe 12-inch", "source": "scrape"},
                     {"id": "i2", "name": "Gate valve 6-inch", "source": "scrape"}]},
    "v2": {"id": "v2", "name": "Summit Rebar", "website": "",
           "contacts": [contact("c5", "Colby Reed", nationwide=True)], "items": []},
    "v3": {"id": "v3", "name": "Red Rock Culvert", "website": "",
           "contacts": [contact("c7", "Sam Whitaker", areas=[("NV", [])])], "items": []},
    "v4": {"id": "v4", "name": "Basin Aggregate", "website": "", "contacts": [], "items": []},
}


class ListTests(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()
        patches = [mock.patch.object(vendors_api.supabase_store, "get_division", return_value={"id": "geo"}),
                   mock.patch.object(vs_mod(), "search_ids", return_value=list(VENDORS)),
                   mock.patch.object(vs_mod(), "bundle", side_effect=lambda ids, d: {k: VENDORS[k] for k in ids})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def get(self, **q):
        r = self.c.get("/api/vendors", query_string=dict({"division": "geo"}, **q))
        self.assertEqual(r.status_code, 200, r.data)
        return {v["id"]: v for v in r.get_json()["vendors"]}

    def test_no_state_shows_everyone(self):
        got = self.get()
        self.assertEqual(set(got), {"v1", "v2", "v3", "v4"})
        self.assertEqual([c["name"] for c in got["v1"]["contacts"]], ["Dale Hutchins", "Marisol Vega", "Tom Brandt"])

    def test_state_hides_others_but_keeps_coverage_not_set_last(self):
        got = self.get(state="UT")
        names = [c["name"] for c in got["v1"]["contacts"]]
        self.assertEqual(names, ["Dale Hutchins", "Tom Brandt"])        # Marisol (ID only) hidden
        self.assertTrue(got["v1"]["contacts"][-1]["unset"])
        self.assertEqual(got["v1"]["contacts"][-1]["area_text"], "Coverage not set")

    def test_nationwide_always_matches(self):
        self.assertIn("v2", self.get(state="WY"))

    def test_vendor_whose_contacts_all_miss_the_state_is_hidden(self):
        got = self.get(state="UT")
        self.assertNotIn("v3", got)                                   # only an NV contact
        self.assertIn("v4", got)                                      # no contacts at all: still listed

    def test_only_vendors_with_a_contact(self):
        self.assertNotIn("v4", self.get(with_contact="1"))

    def test_matched_contacts_first_and_matched_items(self):
        got = self.get(q="vega")
        self.assertEqual(got["v1"]["contacts"][0]["name"], "Marisol Vega")
        got = self.get(q="hdpe")
        self.assertEqual(got["v1"]["matched_items"], ["HDPE pipe 12-inch"])

    def test_words_are_sent_lowercased_to_the_database(self):
        self.get(q="  HDPE  Pipe ")
        vs_mod().search_ids.assert_called_with("geo", ["hdpe", "pipe"])


def vs_mod():
    return vendors_api.vs


class EmailAndAdminTests(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def test_duplicate_check_names_who_it_is(self):
        with mock.patch.object(vs_mod(), "find_vendor", return_value=None), \
             mock.patch.object(vs_mod(), "get_vendor", return_value={"name": "Skaps"}), \
             mock.patch.object(vs_mod(), "find_duplicate", return_value={"id": "c1", "name": "Anurag Shah", "vendor_id": "v1", "why": "email"}):
            r = self.c.get("/api/vendors/contacts/duplicate", query_string={"email": "anurag@skaps.com"})
        self.assertEqual(r.get_json()["match"], {"id": "c1", "name": "Anurag Shah", "vendor_id": "v1", "vendor": "Skaps", "why": "email"})
        with mock.patch.object(vs_mod(), "find_vendor", return_value=None), mock.patch.object(vs_mod(), "find_duplicate", return_value=None):
            self.assertIsNone(self.c.get("/api/vendors/contacts/duplicate?email=new@x.com").get_json()["match"])

    def test_a_duplicate_is_refused_and_nothing_is_made(self):
        body = {"division": "geo", "name": "Anurag Shah", "email": "anurag@skaps.com", "vendor": "Brand New Co"}
        with mock.patch.object(vendors_api.supabase_store, "get_division", return_value={"id": "geo"}), \
             mock.patch.object(vs_mod(), "find_vendor", return_value=None), \
             mock.patch.object(vs_mod(), "get_vendor", return_value={"name": "Skaps"}), \
             mock.patch.object(vs_mod(), "find_duplicate", return_value={"id": "c1", "name": "Anurag Shah", "vendor_id": "v1", "why": "email"}), \
             mock.patch.object(vs_mod(), "create_vendor") as made_vendor, mock.patch.object(vs_mod(), "save_contact") as saved:
            r = self.c.post("/api/vendors/contacts", json=body)
            self.assertEqual(r.status_code, 409)
            self.assertIn("already has this email", r.get_json()["error"])
            made_vendor.assert_not_called()                           # no stray "Brand New Co" either
            saved.assert_not_called()

    def test_same_name_at_the_same_vendor_counts(self):
        rows = [{"id": "c1", "name": "Anurag  Shah", "vendor_id": "v1"}]
        q = mock.MagicMock()
        q.select.return_value = q; q.eq.return_value = q; q.is_.return_value = q
        q.execute.return_value = mock.Mock(data=rows)
        with mock.patch.object(vendors_store, "_db") as db, mock.patch.object(vendors_store, "contact_with_email", return_value=None):
            db.return_value.table.return_value = q
            self.assertEqual(vendors_store.find_duplicate("anurag shah", None, "v1")["why"], "name")
            self.assertIsNone(vendors_store.find_duplicate("anurag shah", None, "v1", not_id="c1"))   # editing themselves
            self.assertIsNone(vendors_store.find_duplicate("Kourosh Sabzevari", None, "v1"))

    def test_admin_routes_refuse_a_non_admin(self):
        with mock.patch.object(vendors_api, "_is_admin", return_value=False):
            for method, path in (("get", "/api/vendors/admin/review"), ("post", "/api/vendors/admin/review"),
                                 ("get", "/api/vendors/admin/vendors"), ("post", "/api/vendors/admin/vendors"),
                                 ("post", "/api/vendors/admin/pull"), ("post", "/api/vendors/admin/pdf")):
                self.assertEqual(getattr(self.c, method)(path, json={}).status_code, 403, path)

    def test_writes_refuse_a_viewer(self):
        with mock.patch.object(vendors_api, "_can_edit", return_value=False):
            for method, path in (("post", "/api/vendors/contacts"), ("patch", "/api/vendors/contacts/x"),
                                 ("delete", "/api/vendors/contacts/x"), ("patch", "/api/vendors/x"),
                                 ("delete", "/api/vendors/x"), ("post", "/api/vendors/x/items"),
                                 ("delete", "/api/vendors/items/x"), ("post", "/api/vendors/cards")):
                self.assertEqual(getattr(self.c, method)(path, json={}).status_code, 403, path)


class ItemMergeTests(unittest.TestCase):
    def test_easy_duplicates_merge_but_different_items_dont(self):
        k = vendors_api._item_key
        self.assertEqual(k("Silt Fences"), k("silt fence"))
        self.assertEqual(k("Straw wattles"), k("Straw  wattle"))
        self.assertEqual(k("Geocomposites"), k("Geocomposite"))
        self.assertEqual(k("Bulk-bags"), k("Bulk bags"))
        self.assertNotEqual(k("Wired silt fence"), k("Silt fence"))
        self.assertNotEqual(k("Geogrid"), k("Biaxial geogrid"))
        self.assertEqual(k("Glass"), "glass")                         # 'ss' isn't a plural


class MainContactTests(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def test_only_a_contact_at_that_vendor_can_be_the_main_one(self):
        with mock.patch.object(vs_mod(), "get_vendor", return_value={"id": "v1"}), \
             mock.patch.object(vs_mod(), "get_contact", return_value={"id": "c9", "vendor_id": "v2"}), \
             mock.patch.object(vs_mod(), "set_main_contact") as setm:
            r = self.c.post("/api/vendors/v1/main-contact", json={"contact_id": "c9"})
        self.assertEqual(r.status_code, 400)
        setm.assert_not_called()

    def test_set_and_clear(self):
        with mock.patch.object(vs_mod(), "get_vendor", return_value={"id": "v1"}), \
             mock.patch.object(vs_mod(), "get_contact", return_value={"id": "c1", "vendor_id": "v1"}), \
             mock.patch.object(vs_mod(), "set_main_contact") as setm:
            self.assertEqual(self.c.post("/api/vendors/v1/main-contact", json={"contact_id": "c1"}).status_code, 200)
            setm.assert_called_with("v1", "c1", None)
            self.assertEqual(self.c.post("/api/vendors/v1/main-contact", json={"contact_id": None}).status_code, 200)
            setm.assert_called_with("v1", None, None)

    def test_a_viewer_cant_star(self):
        with mock.patch.object(vendors_api, "_can_edit", return_value=False):
            self.assertEqual(self.c.post("/api/vendors/v1/main-contact", json={"contact_id": "c1"}).status_code, 403)


class NormalizeTests(unittest.TestCase):
    def test_phone_email_website(self):
        self.assertEqual(vendors_store.norm_phone("801.555.0173"), "(801) 555-0173")
        self.assertEqual(vendors_store.norm_phone("+1 801 555 0173"), "(801) 555-0173")
        self.assertEqual(vendors_store.norm_phone("ext 12"), "ext 12")
        self.assertEqual(vendors_store.norm_phone("801-555-0173 x12"), "(801) 555-0173 ext. 12")
        self.assertEqual(vendors_store.norm_phone("(801) 555-0173 ext. 4"), "(801) 555-0173 ext. 4")
        self.assertEqual(vendors_store.norm_email(" RMackay@GranitePeak.com "), "rmackay@granitepeak.com")
        self.assertEqual(vendors_store.norm_website("https://www.Example.com/"), "Example.com")
        self.assertEqual(vendors_store.domain_of("https://www.Example.com/shop"), "example.com")


class PullTests(unittest.TestCase):
    """What a pull does with what it found: the first goes straight in, later
    ones are queued for review, and items added by hand are never removed."""

    def run_pull(self, found, have, first):
        vs = vendor_catalog.vs
        with mock.patch.object(vs, "get_vendor", return_value={"id": "v1", "name": "W", "website": "w.example"}), \
             mock.patch.object(vendor_catalog, "pull_logo", return_value=False), \
             mock.patch.object(vendor_catalog.supabase_store, "load_divisions", return_value=[{"id": "geo"}]), \
             mock.patch.object(vendor_catalog.supabase_store, "load_keywords", return_value=[]), \
             mock.patch.object(vs, "divisions_of", return_value=["geo"]), \
             mock.patch.object(vs, "start_run", return_value={"id": "r1"}), \
             mock.patch.object(vs, "finish_run") as finish, \
             mock.patch.object(vs, "had_good_run", return_value=not first), \
             mock.patch.object(vs, "items_of", return_value=have), \
             mock.patch.object(vs, "add_item") as add, \
             mock.patch.object(vs, "queue_changes", return_value=1) as queue, \
             mock.patch.object(vendor_catalog, "_from_site", return_value=(found, "schema_org")):
            result = vendor_catalog.pull("v1")
        return result, add, queue, finish

    def test_first_pull_goes_straight_in(self):
        result, add, queue, finish = self.run_pull(["Silt fence", "Geogrid"], [], first=True)
        self.assertTrue(result["ok"])
        self.assertEqual([c.args[2] for c in add.call_args_list], ["Silt fence", "Geogrid"])
        queue.assert_not_called()
        finish.assert_called_with("r1", True, "schema_org")

    def test_later_pull_queues_added_and_removed_but_never_manual(self):
        have = [{"name": "Silt fence", "source": "scrape", "active": True},
                {"name": "Tackifier", "source": "scrape", "active": True},
                {"name": "Straw wattle", "source": "manual", "active": True}]
        _r, add, queue, _f = self.run_pull(["Silt fence", "Geocell 4-inch"], have, first=False)
        add.assert_not_called()
        queue.assert_called_once_with("v1", "geo", "r1", ["Geocell 4-inch"], ["Tackifier"])

    def test_failed_pull_is_recorded(self):
        with mock.patch.object(vendor_catalog, "_from_site", side_effect=vendor_catalog.Failed("Site blocks automated requests")):
            vs = vendor_catalog.vs
            with mock.patch.object(vs, "get_vendor", return_value={"id": "v1", "name": "W", "website": "w"}), \
                 mock.patch.object(vendor_catalog, "pull_logo", return_value=False), \
                 mock.patch.object(vendor_catalog.supabase_store, "load_divisions", return_value=[]), \
                 mock.patch.object(vs, "divisions_of", return_value=[]), \
                 mock.patch.object(vs, "start_run", return_value={"id": "r1"}), \
                 mock.patch.object(vs, "finish_run") as finish:
                result = vendor_catalog.pull("v1")
        self.assertFalse(result["ok"])
        finish.assert_called_with("r1", False, None, "Site blocks automated requests")

    def test_stop_ends_a_pull_as_stopped_by_hand(self):
        def site(*_a):
            vendor_catalog.stop("v1")       # pressed while the site is being read
            vendor_catalog._check()         # what every request does first
            return ["never"], "feed"
        vs = vendor_catalog.vs
        with mock.patch.object(vs, "get_vendor", return_value={"id": "v1", "name": "W", "website": "w"}), \
             mock.patch.object(vendor_catalog, "pull_logo", return_value=False), \
             mock.patch.object(vendor_catalog.supabase_store, "load_divisions", return_value=[]), \
             mock.patch.object(vs, "divisions_of", return_value=[]), \
             mock.patch.object(vs, "start_run", return_value={"id": "r1"}), \
             mock.patch.object(vs, "fail_running", return_value=0), \
             mock.patch.object(vs, "add_item") as add, \
             mock.patch.object(vs, "finish_run") as finish, \
             mock.patch.object(vendor_catalog, "_from_site", side_effect=site):
            result = vendor_catalog.pull("v1")
        self.assertEqual(result["message"], "Stopped by hand")
        finish.assert_called_with("r1", False, None, "Stopped by hand")
        add.assert_not_called()
        self.assertNotIn("v1", vendor_catalog._stop)   # cleared, so the next pull runs

    def test_the_weekly_pull_is_sunday_2am_mountain(self):
        from datetime import datetime
        tz = vendor_catalog.TZ
        at = lambda *a: datetime(*a, tzinfo=tz)
        self.assertEqual(vendor_catalog.week_slot(at(2026, 11, 25, 15, 0)), at(2026, 11, 22, 2, 0))   # Wednesday -> last Sunday
        self.assertEqual(vendor_catalog.week_slot(at(2026, 11, 22, 1, 59)), at(2026, 11, 15, 2, 0))   # Sunday, before 2 AM
        self.assertEqual(vendor_catalog.week_slot(at(2026, 11, 22, 2, 0)), at(2026, 11, 22, 2, 0))    # on the dot

    def test_division_keywords_pick_items_without_ai(self):
        with mock.patch.object(vendor_catalog.ai_provider, "get_provider", return_value=None):
            got = vendor_catalog._assign(["Geotextile fabric 6 oz", "Anchor bolts", "Silt fences"],
                                         {"geo": ["geotextile", "silt fence"], "civil": []})
        self.assertEqual(got["geo"], ["Geotextile fabric 6 oz", "Silt fences"])
        self.assertEqual(len(got["civil"]), 3)                        # no keywords: everything


if __name__ == "__main__":
    unittest.main()

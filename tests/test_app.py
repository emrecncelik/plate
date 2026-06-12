"""
Plate test suite. Run before every deployment / feature change:

    python -m unittest discover -s tests        # or: python -m pytest tests

Covers parsing, the auth gate, per-user data isolation, logging, friends /
nicknames, day totals, and the SQLite migration. Uses an isolated temp database
and never touches your real data/ directory or the network.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

# Point the app at a throwaway database and neutralise deploy-only env BEFORE
# importing app (app.py reads these and runs init_db() at import time).
_TMP = tempfile.mkdtemp(prefix="plate_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
os.environ["COOKIE_SECURE"] = ""   # so the test client keeps the session cookie
os.environ["WARM_ASR"] = ""        # don't spin up the whisper model in tests

import app  # noqa: E402

app.app.testing = True
app.app.config["SESSION_COOKIE_SECURE"] = False


# A reference table fixture for the pure-parser tests (no DB needed).
REF = [
    {"name": "Cherry tomatoes", "cal": 0.18, "protein": 0.01, "unit": "g", "aliases": ["cherry tomatoes", "tomatoes", "tomato"]},
    {"name": "Chicken thigh", "cal": 2.3, "protein": 0.27, "unit": "g", "aliases": ["chicken thigh", "chicken"]},
    {"name": "Olive oil", "cal": 8.84, "protein": 0, "unit": "g", "aliases": ["olive oil"]},
    {"name": "Canola oil", "cal": 8.84, "protein": 0, "unit": "g", "aliases": ["canola oil", "canola"]},
    {"name": "Light cheese", "cal": 1.51, "protein": 0.2, "unit": "g", "aliases": ["light cheese"]},
    {"name": "Milk A", "cal": 0.45, "protein": 0.03, "unit": "g", "aliases": ["milk"]},
    {"name": "Milk B", "cal": 0.61, "protein": 0.03, "unit": "g", "aliases": ["milk"]},
]


def reset_db():
    conn = app.get_db()
    try:
        for t in ("log", "reference", "friendships", "users"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()


def login(client, uid):
    with client.session_transaction() as s:
        s["uid"] = uid


class ParsingTests(unittest.TestCase):
    def test_parse_qty(self):
        self.assertEqual(app.parse_qty("chicken 80 g"), 80.0)
        self.assertEqual(app.parse_qty("rice 1.5"), 1.5)
        self.assertEqual(app.parse_qty("rice 1,5"), 1.5)      # comma decimal
        self.assertEqual(app.parse_qty("two eggs"), 2.0)       # number word
        self.assertIsNone(app.parse_qty("chicken"))            # no quantity

    def test_exact_longest_alias_wins(self):
        items = app.match_items("light cheese 50 g", REF)
        self.assertEqual([i["name"] for i in items], ["Light cheese"])

    def test_ambiguous_name_returns_all(self):
        items = app.match_items("milk 200", REF)
        self.assertEqual(sorted(i["name"] for i in items), ["Milk A", "Milk B"])

    def test_partial_fallback(self):
        # "oil" is not a full alias of anything; partial should surface both oils
        items = app.match_items("oil 20 g", REF)
        self.assertEqual(sorted(i["name"] for i in items), ["Canola oil", "Olive oil"])

    def test_no_match(self):
        self.assertEqual(app.match_items("pizza", REF), [])

    def test_parse_input_single_with_protein(self):
        out = app.parse_input("chicken 90 g", REF)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertTrue(e["ok"])
        self.assertEqual(e["name"], "Chicken thigh")
        self.assertEqual(e["cal"], 207)            # round(90 * 2.3)
        self.assertEqual(e["protein"], 24.3)       # round(90 * 0.27, 1)
        self.assertNotIn("alts", e)

    def test_parse_input_ambiguous_has_alts(self):
        out = app.parse_input("milk 100", REF)
        self.assertEqual(len(out), 1)
        self.assertIn("alts", out[0])
        self.assertEqual(len(out[0]["alts"]), 2)
        for alt in out[0]["alts"]:
            self.assertIn("protein", alt)

    def test_parse_input_splitting_and_miss(self):
        out = app.parse_input("tomatoes 80 g and chicken 90 g, pizza 100 g", REF)
        self.assertEqual(len(out), 3)
        self.assertTrue(out[0]["ok"] and out[1]["ok"])
        self.assertFalse(out[2]["ok"])             # pizza has no reference


class AuthTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.c = app.app.test_client()

    def test_config_public(self):
        self.assertEqual(self.c.get("/api/config").get_json()["google_client_id"], "test-client-id")

    def test_me_anonymous(self):
        self.assertIsNone(self.c.get("/api/auth/me").get_json()["user"])

    def test_protected_routes_require_login(self):
        for path in ("/api/reference", "/api/log/2026-06-13", "/api/friends"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
        self.assertEqual(self.c.post("/api/transcribe").status_code, 401)

    def test_google_login_flow(self):
        fake = {"sub": "user-x", "email": "x@example.com", "name": "Xavier"}
        with mock.patch("app.id_token.verify_oauth2_token", return_value=fake):
            r = self.c.post("/api/auth/google", json={"credential": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["user"]["email"], "x@example.com")
        # session now active
        self.assertEqual(self.c.get("/api/auth/me").get_json()["user"]["id"], "user-x")
        # logout clears it
        self.c.post("/api/auth/logout")
        self.assertIsNone(self.c.get("/api/auth/me").get_json()["user"])

    def test_google_login_rejects_bad_token(self):
        with mock.patch("app.id_token.verify_oauth2_token", side_effect=ValueError("bad")):
            r = self.c.post("/api/auth/google", json={"credential": "tok"})
        self.assertEqual(r.status_code, 401)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        app.upsert_user("A", "a@x.com", "Ada")
        app.upsert_user("B", "b@x.com", "Bo")
        self.c = app.app.test_client()
        login(self.c, "A")

    def test_starts_empty(self):
        self.assertEqual(self.c.get("/api/reference").get_json(), [])

    def test_add_with_protein_and_delete(self):
        self.c.post("/api/reference", json={"name": "Rice", "cal": 2, "protein": 0.25, "unit": "g"})
        ref = self.c.get("/api/reference").get_json()
        self.assertEqual(len(ref), 1)
        self.assertEqual(ref[0]["protein"], 0.25)
        self.c.delete("/api/reference/" + str(ref[0]["id"]))
        self.assertEqual(self.c.get("/api/reference").get_json(), [])

    def test_protein_optional(self):
        self.c.post("/api/reference", json={"name": "Water", "cal": 0, "unit": "g"})
        self.assertEqual(self.c.get("/api/reference").get_json()[0]["protein"], 0)

    def test_requires_name_and_cal(self):
        self.assertEqual(self.c.post("/api/reference", json={"name": "X"}).status_code, 400)

    def test_isolation_between_users(self):
        self.c.post("/api/reference", json={"name": "Rice", "cal": 2, "unit": "g"})
        cb = app.app.test_client()
        login(cb, "B")
        self.assertEqual(cb.get("/api/reference").get_json(), [])

    def test_edit_updates_item(self):
        self.c.post("/api/reference", json={"name": "Rice", "cal": 2, "protein": 0.2, "unit": "g"})
        rid = self.c.get("/api/reference").get_json()[0]["id"]
        self.c.put(f"/api/reference/{rid}", json={"name": "Basmati", "cal": 1.3, "protein": 0.27, "unit": "g"})
        ref = self.c.get("/api/reference").get_json()[0]
        self.assertEqual((ref["name"], ref["cal"], ref["protein"]), ("Basmati", 1.3, 0.27))

    def test_edit_propagates_to_existing_logs(self):
        self.c.post("/api/reference", json={"name": "Chicken thigh", "cal": 2.3, "protein": 0.27, "unit": "g"})
        rid = self.c.get("/api/reference").get_json()[0]["id"]
        self.c.post("/api/log/2026-06-13", json={"meal": "Lunch", "text": "chicken 100 g"})
        # edit the reference -> the already-logged entry must be recomputed
        self.c.put(f"/api/reference/{rid}", json={"name": "Chicken breast", "cal": 1.65, "protein": 0.31, "unit": "g"})
        entry = self.c.get("/api/log/2026-06-13").get_json()["Lunch"][0]
        self.assertEqual(entry["name"], "Chicken breast")
        self.assertEqual(entry["cal"], 165)       # round(100 * 1.65)
        self.assertEqual(entry["protein"], 31.0)  # round(100 * 0.31, 1)

    def test_edit_does_not_touch_other_users_logs(self):
        # both users have an identically named food + log entry
        self.c.post("/api/reference", json={"name": "Rice", "cal": 2, "unit": "g"})
        self.c.post("/api/log/2026-06-13", json={"meal": "Lunch", "text": "rice 100 g"})
        cb = app.app.test_client(); login(cb, "B")
        cb.post("/api/reference", json={"name": "Rice", "cal": 2, "unit": "g"})
        cb.post("/api/log/2026-06-13", json={"meal": "Lunch", "text": "rice 100 g"})
        # A edits A's Rice
        rid = self.c.get("/api/reference").get_json()[0]["id"]
        self.c.put(f"/api/reference/{rid}", json={"name": "White rice", "cal": 3, "unit": "g"})
        # B's entry is untouched
        b_entry = cb.get("/api/log/2026-06-13").get_json()["Lunch"][0]
        self.assertEqual(b_entry["name"], "Rice")
        self.assertEqual(b_entry["cal"], 200)

    def test_edit_missing_returns_404(self):
        self.assertEqual(self.c.put("/api/reference/9999", json={"name": "X", "cal": 1}).status_code, 404)


class LogTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        app.upsert_user("A", "a@x.com", "Ada")
        self.c = app.app.test_client()
        login(self.c, "A")
        self.c.post("/api/reference", json={"name": "Chicken thigh", "cal": 2.3, "protein": 0.27, "unit": "g"})

    def test_add_via_text(self):
        r = self.c.post("/api/log/2026-06-13", json={"meal": "Lunch", "text": "chicken 100 g"})
        day = r.get_json()["day"]
        self.assertEqual(len(day["Lunch"]), 1)
        self.assertEqual(day["Lunch"][0]["cal"], 230)
        self.assertEqual(day["Lunch"][0]["protein"], 27.0)

    def test_add_via_items(self):
        item = {"name": "Chicken thigh", "qty": 50, "unit": "g", "cal": 115, "protein": 13.5, "ok": True}
        r = self.c.post("/api/log/2026-06-13", json={"meal": "Dinner", "items": [item]})
        entry = r.get_json()["day"]["Dinner"][0]
        self.assertEqual(entry["cal"], 115)
        self.assertEqual(entry["protein"], 13.5)

    def test_invalid_meal_rejected(self):
        r = self.c.post("/api/log/2026-06-13", json={"meal": "Brunch", "text": "chicken 100 g"})
        self.assertEqual(r.status_code, 400)

    def test_delete_entry(self):
        self.c.post("/api/log/2026-06-13", json={"meal": "Lunch", "text": "chicken 100 g"})
        eid = self.c.get("/api/log/2026-06-13").get_json()["Lunch"][0]["id"]
        self.c.delete(f"/api/log/2026-06-13/Lunch/{eid}")
        self.assertEqual(self.c.get("/api/log/2026-06-13").get_json()["Lunch"], [])

    def test_empty_day_shape(self):
        day = self.c.get("/api/log/2020-01-01").get_json()
        self.assertEqual(set(day.keys()), {"Breakfast", "Lunch", "Dinner", "Snacks"})


class FriendTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        for uid, em, nm in [("A", "ada@x.com", "Ada"), ("B", "bo@x.com", "Bo"), ("C", "cy@x.com", "Cy")]:
            app.upsert_user(uid, em, nm)
        self.ca = app.app.test_client(); login(self.ca, "A")
        self.cb = app.app.test_client(); login(self.cb, "B")

    def _befriend(self):
        self.ca.post("/api/friends/request", json={"email": "bo@x.com"})
        fid = self.cb.get("/api/friends").get_json()["incoming"][0]["fid"]
        self.cb.post(f"/api/friends/{fid}/accept")
        return fid

    def test_request_accept_mutual(self):
        self.ca.post("/api/friends/request", json={"email": "BO@X.COM"})  # case-insensitive
        self.assertEqual(len(self.ca.get("/api/friends").get_json()["outgoing"]), 1)
        fid = self.cb.get("/api/friends").get_json()["incoming"][0]["fid"]
        self.cb.post(f"/api/friends/{fid}/accept")
        self.assertEqual([f["name"] for f in self.ca.get("/api/friends").get_json()["friends"]], ["Bo"])
        self.assertEqual([f["name"] for f in self.cb.get("/api/friends").get_json()["friends"]], ["Ada"])

    def test_totals_daily_only(self):
        self._befriend()
        app.add_log_entries("B", "2026-06-13", "Lunch", [{"name": "Rice", "qty": 100, "unit": "g", "cal": 200, "protein": 20}])
        totals = self.ca.get("/api/friends/totals/2026-06-13").get_json()
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["cal"], 200)
        self.assertEqual(totals[0]["protein"], 20.0)
        # daily totals ONLY -- no items or meal breakdown leak through
        self.assertEqual(set(totals[0].keys()), {"id", "name", "email", "cal", "protein"})

    def test_reverse_request_auto_accepts(self):
        self.ca.post("/api/friends/request", json={"email": "cy@x.com"})
        cc = app.app.test_client(); login(cc, "C")
        r = cc.post("/api/friends/request", json={"email": "ada@x.com"})
        self.assertEqual(r.get_json()["status"], "accepted")
        self.assertIn("Cy", [f["name"] for f in self.ca.get("/api/friends").get_json()["friends"]])

    def test_guards(self):
        self.assertEqual(self.ca.post("/api/friends/request", json={"email": "ada@x.com"}).status_code, 400)   # self
        self.assertEqual(self.ca.post("/api/friends/request", json={"email": "ghost@x.com"}).status_code, 404)  # unknown
        self.ca.post("/api/friends/request", json={"email": "bo@x.com"})
        self.assertEqual(self.ca.post("/api/friends/request", json={"email": "bo@x.com"}).status_code, 409)     # dup

    def test_remove(self):
        fid = self._befriend()
        self.ca.delete(f"/api/friends/{fid}")
        self.assertEqual(self.ca.get("/api/friends").get_json()["friends"], [])
        self.assertEqual(self.cb.get("/api/friends").get_json()["friends"], [])

    def test_nickname_per_direction(self):
        fid = self._befriend()
        self.ca.post(f"/api/friends/{fid}/nick", json={"nick": "Bobby"})
        self.assertEqual(self.ca.get("/api/friends").get_json()["friends"][0]["nick"], "Bobby")
        # B is unaffected by A's nickname
        self.assertIsNone(self.cb.get("/api/friends").get_json()["friends"][0]["nick"])
        # clearing works
        self.ca.post(f"/api/friends/{fid}/nick", json={"nick": ""})
        self.assertIsNone(self.ca.get("/api/friends").get_json()["friends"][0]["nick"])

    def test_nickname_non_participant_blocked(self):
        fid = self._befriend()
        cc = app.app.test_client(); login(cc, "C")
        cc.post(f"/api/friends/{fid}/nick", json={"nick": "hax"})
        self.assertIsNone(self.ca.get("/api/friends").get_json()["friends"][0]["nick"])


class MigrationTests(unittest.TestCase):
    """A pre-protein / pre-nickname database must upgrade cleanly on startup."""
    def test_old_db_gains_new_columns(self):
        old_dir = tempfile.mkdtemp(prefix="plate_old_")
        old_db = os.path.join(old_dir, "plate.db")
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT, name TEXT, created_at TEXT);
            CREATE TABLE reference(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, name TEXT, cal REAL, unit TEXT, aliases TEXT);
            CREATE TABLE log(id TEXT PRIMARY KEY, user_id TEXT, date TEXT, meal TEXT, name TEXT, qty REAL, unit TEXT, cal INTEGER, created_at TEXT);
            CREATE TABLE friendships(id INTEGER PRIMARY KEY AUTOINCREMENT, requester_id TEXT, addressee_id TEXT, status TEXT, created_at TEXT, UNIQUE(requester_id, addressee_id));
            INSERT INTO users VALUES('u1','a@x.com','A','t');
            INSERT INTO reference(user_id,name,cal,unit,aliases) VALUES('u1','Old',1.0,'g','["old"]');
            INSERT INTO log VALUES('e1','u1','2026-06-13','Lunch','Old',50,'g',50,'t');
            """
        )
        conn.commit()
        conn.close()

        original = app.DB_FILE
        try:
            app.DB_FILE = old_db
            app.init_db()  # should ALTER TABLE the missing columns in
            ref = app.user_reference("u1")
            self.assertEqual(ref[0]["protein"], 0)
            day = app.user_day("u1", "2026-06-13")
            self.assertEqual(day["Lunch"][0]["protein"], 0)
            # nickname columns now exist (set/read without error)
            app.upsert_user("u2", "b@x.com", "B")
            app.request_friend("u1", "b@x.com")
            self.assertEqual(app.set_friend_nick("u1", app.get_relationships("u1")["outgoing"][0]["fid"], "x"), True)
        finally:
            app.DB_FILE = original


if __name__ == "__main__":
    unittest.main(verbosity=2)

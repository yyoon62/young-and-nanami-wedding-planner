from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from test_dashboard import running


class HouseholdInventory(unittest.TestCase):
    def test_legacy_inventory_through_two_starts(self):
        # Schema from e4f98a0, not a DB produced by the new initialization code.
        legacy = (17, "Fictional co-op", "Units", "Custom units", "asset", "JPY", "7", "2024-02-29")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
                    kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
                    currency TEXT NOT NULL, balance TEXT NOT NULL, asof TEXT NOT NULL)""")
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)", legacy)

            def overview(request, url, ids, totals):
                status, body = request(url)
                self.assertEqual(status, 200)
                self.assertCountEqual(re.findall(r'href="/edit/(\d+)"', body), list(map(str, ids)))
                self.assertCountEqual(re.findall(r'<section class="card[^"]*">.*?<strong>([^<]+)</strong>', body, re.S), totals)
                return body

            def stored():
                with closing(sqlite3.connect(path)) as db:
                    return db.execute("SELECT * FROM accounts ORDER BY id").fetchall()

            with running(path) as request:
                self.assertEqual(stored(), [(*legacy, "Unspecified", "Manual entry", 0, 0, "")])
                overview(request, "/?view=Young", [], [])
                form = request("/new")[1]
                self.assertIn("<option selected>Unspecified</option>", form)
                self.assertIn('name="update_method" value="Manual entry"', form)
                token = re.search(r'name="token" value="([^"]+)"', form)[1]
                fields = dict(institution="Example", nickname="Everyday", currency="USD", side="asset",
                              kind="Checking", asof="2024-03-01", update_method="Check latest statement", token=token)
                # Same nickname on separate accounts must not collapse distinct IDs.
                for owner, side, balance in [("Young", "asset", "10"), ("Nanami", "debt", "2"),
                                             ("Joint", "asset", "3")]:
                    self.assertEqual(request("/new", dict(fields, owner=owner, side=side, balance=balance))[0], 303)
                household = overview(request, "/", [17, 18, 19, 20], ["USD 11.00", "JPY 7"])
                self.assertIn("Assets: USD 13.00 · Debts: USD 2.00", household)
                self.assertIn('href="/?view=Young"', household)
                self.assertIn('href="/?view=Nanami"', household)
                for person, ids, total in [("Young", [18, 20], "USD 13.00"), ("Nanami", [19, 20], "USD 1.00")]:
                    self.assertNotIn("JPY", overview(request, f"/?view={person}", ids, [total]))

                edit = request("/edit/17")[1]
                self.assertIn("<option selected>Unspecified</option>", edit)
                self.assertIn('value="2024-02-29"', edit)
                keys = ("institution", "nickname", "kind", "side", "currency", "balance", "asof")
                fields = dict(zip(keys, legacy[1:]), token=token, owner="Nanami",
                              update_method="Read co-op statement")
                snapshot = stored()
                for invalid in (dict(owner="Someone"), dict(update_method=" "), dict(update_method="x"*101)):
                    self.assertEqual(request("/edit/17", dict(fields, **invalid))[0], 422)
                self.assertEqual(stored(), snapshot)
                self.assertEqual(request("/edit/17", fields)[0], 303)

            with running(path) as request:
                self.assertEqual(stored(), [(*legacy, "Nanami", "Read co-op statement", 0, 0, ""), *snapshot[1:]])
                overview(request, "/", [17, 18, 19, 20], ["USD 11.00", "JPY 7"])
                overview(request, "/?view=Young", [18, 20], ["USD 13.00"])
                body = overview(request, "/?view=Nanami", [17, 19, 20], ["USD 1.00", "JPY 7"])
                for text in ("2024-02-29", "Custom units", "Fictional co-op"):
                    self.assertIn(text, body)
                edit17 = request("/edit/17")[1]
                self.assertIn("<option selected>Nanami</option>", edit17)
                self.assertIn('value="Read co-op statement"', edit17)
                self.assertEqual(request("/?view=unknown")[0], 400)
                # Inline independent Young + Nanami toggles (both selected = Joint)
                token = re.search(r'name="token" value="([^"]+)"', body)[1]
                # id=18 starts as Young; clicking Nanami makes it Joint (both active)
                self.assertEqual(request("/owner/18", dict(token=token, owner="Nanami", view="Household"))[0], 303)
                overview(request, "/?view=Nanami", [17, 18, 19, 20], ["USD 11.00", "JPY 7"])
                # Clicking Young turns Young off, leaving Nanami
                self.assertEqual(request("/owner/18", dict(token=token, owner="Young", view="Household"))[0], 303)
                overview(request, "/?view=Young", [20], ["USD 3.00"])
                overview(request, "/?view=Nanami", [17, 18, 19, 20], ["USD 11.00", "JPY 7"])
                # Clicking Nanami turns Nanami off too -> Unspecified
                self.assertEqual(request("/owner/18", dict(token=token, owner="Nanami", view="Household"))[0], 303)
                overview(request, "/?view=Nanami", [17, 19, 20], ["USD 1.00", "JPY 7"])
                # Inline rename and section move
                self.assertEqual(request("/rename/18", dict(token=token, nickname="Renamed Checking", view="Household"))[0], 303)
                self.assertEqual(request("/section/18", dict(token=token, section="Retirement", view="Household"))[0], 303)
                home = request("/")[1]
                self.assertIn("Renamed Checking", home)
                self.assertIn('data-section="Retirement"', home)
                # Excluding removes from totals without deleting
                self.assertEqual(request("/exclude/19", dict(token=token, excluded="1", view="Household"))[0], 303)
                after_ex = overview(request, "/", [17, 18, 19, 20], ["USD 13.00", "JPY 7"])
                self.assertIn("Excluded accounts (1)", after_ex)
                self.assertEqual(request("/exclude/19", dict(token=token, excluded="0", view="Household"))[0], 303)
                overview(request, "/", [17, 18, 19, 20], ["USD 11.00", "JPY 7"])

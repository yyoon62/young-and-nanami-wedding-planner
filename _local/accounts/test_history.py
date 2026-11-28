from contextlib import closing, contextmanager
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
import re
import sqlite3
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlencode

from server import init_db, serve


DAY = "2024-11-"
BASE = dict(institution="Fictional Harbor", nickname="Harbor savings", kind="Savings",
            owner="Young", update_method="Manual entry", side="asset", balance="100",
            currency="USD", asof=DAY + "01")


@contextmanager
def running(path, provider=None):
    with serve(path, 0, provider, logo_dir=path.parent / "logos") as server:
        # Exercise the local logo path only; never reach an institution.
        server.logo_fetcher = None
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, path="/", **form):
    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as conn:
        if form:
            conn.request("POST", path, urlencode(dict(token=server.token) | form))
        else:
            conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode()


def populate(server):
    actions = [
        ("/new", BASE),
        ("/new", dict(BASE, institution="Fictional Elm", nickname="Elm card", kind="Credit card",
                      owner="Joint", side="debt", balance="20", asof=DAY + "03")),
        ("/new", dict(BASE, institution="Fictional Pine", nickname="Pine savings",
                      owner="Nanami", currency="JPY", balance="500", asof=DAY + "03")),
        ("/new", dict(BASE, nickname="Excluded reserve", balance="1000", asof=DAY + "04")),
        ("/exclude/4", {"excluded": "1"}),
        ("/edit/1", dict(BASE, balance="120", asof=DAY + "05")),
    ]
    for path, data in actions:
        status, body = request(server, path, **data)
        if status != 303:
            raise AssertionError(f"{path}: {status}, {body}")


def table(html, currency):
    return re.findall(r"<tr><th scope=\"row\"><time>([^<]+)</time></th><td>([^<]+)</td></tr>",
                      html.partition(f"<caption>{currency} net worth by saved date</caption>")[2]
                      .partition("</table>")[0])


class StubBridge:
    label = "Fictional provider"
    namespace = "fictional-history"

    def __init__(self):
        self.row = {"conn_id": "example", "id": "savings", "name": "Fictional sync",
                    "currency": "USD", "balance": "10.01", "balance-date": self.stamp(3)}

    @staticmethod
    def stamp(day, hour=12):
        return int(datetime(2024, 11, day, hour).timestamp())

    def fetch(self):
        return dict(accounts=[self.row.copy()], connections=[dict(conn_id="example", name="Fictional Bank")],
                    errlist=[])


class HistoryFlow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "synthetic.sqlite"

    def sql(self, statement):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(statement).fetchall()

    def snapshots(self):
        return self.sql("SELECT account_id, asof, balance, currency, side FROM balance_history ORDER BY 1, 2")

    def test_legacy_backfill_idempotent_edit_flip_and_delete(self):
        self.sql("""CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
            kind TEXT NOT NULL, side TEXT NOT NULL, currency TEXT NOT NULL, balance TEXT NOT NULL, asof TEXT NOT NULL)""")
        self.sql("""INSERT INTO accounts VALUES
            (1, 'Legacy fictional', 'Harbor savings', 'Savings', 'asset', 'USD', '10.01', '2024-11-01'),
            (2, 'Legacy fictional', 'Reserve', 'Savings', 'asset', 'JPY', '500', '2024-11-03')""")
        init_db(self.path)
        self.assertEqual(self.snapshots(), [(1, DAY + "01", "10.01", "USD", "asset"),
                                            (2, DAY + "03", "500", "JPY", "asset")])
        self.assertTrue(all(stamp.endswith("Z") for (stamp,) in self.sql("SELECT recorded_at FROM balance_history")))
        # A preexisting same-date point is not rewritten by either restart.
        self.sql("UPDATE balance_history SET balance='10.02' WHERE account_id=1")
        before = self.sql("SELECT * FROM balance_history ORDER BY 1, 2")
        for _ in range(2):
            init_db(self.path)
            self.assertEqual(self.sql("SELECT * FROM balance_history ORDER BY 1, 2"), before)
        with running(self.path) as server:
            self.assertEqual(request(server, "/edit/1", **dict(BASE, inline="1", balance="10.03"))[0], 303)
            self.assertEqual(request(server, "/side/1", side="debt")[0], 303)
            self.assertEqual(self.snapshots(), [(1, DAY + "01", "10.03", "USD", "debt"),
                                                (2, DAY + "03", "500", "JPY", "asset")])
            self.assertEqual(request(server, "/edit/1", **dict(BASE, asof=DAY + "05", balance="12.34"))[0], 303)
            self.assertEqual(table(request(server)[1], "USD"),
                             [(DAY + d, v) for d, v in [("01", "USD -10.03"), ("03", "USD -10.03"),
                                                        ("05", "USD 12.34")]])
            self.assertEqual(request(server, "/remove/1", view="Household")[0], 303)
            self.assertEqual(self.snapshots(), [(2, DAY + "03", "500", "JPY", "asset")])
            self.assertNotIn("USD", request(server)[1])

    def test_sync_replaces_same_day_and_holds_malformed_older_and_changed_currency(self):
        bridge = StubBridge()
        with running(self.path, bridge) as server:
            self.assertEqual(request(server, "/sync", view="Household")[0], 303)
            self.assertEqual(self.snapshots(), [(1, DAY + "03", "10.01", "USD", "asset")])
            bridge.row.update(balance="-20.11", **{"balance-date": bridge.stamp(3, 13)})
            for _ in range(2):
                self.assertEqual(request(server, "/sync", view="Household")[0], 303)
            self.assertEqual(self.snapshots(), [(1, DAY + "03", "20.11", "USD", "debt")])
            bridge.row.update(balance="30.12", **{"balance-date": bridge.stamp(5)})
            self.assertEqual(request(server, "/sync", view="Household")[0], 303)
            expected = [(1, DAY + "03", "20.11", "USD", "debt"), (1, DAY + "05", "30.12", "USD", "asset")]
            self.assertEqual(self.snapshots(), expected)
            for stamp, balance, currency in [
                (bridge.stamp(3), "88", "USD"), (bridge.stamp(6), "1e3", "USD"),
                (bridge.stamp(6), "700", "JPY"),
            ]:
                bridge.row.update(balance=balance, currency=currency, **{"balance-date": stamp})
                self.assertEqual(request(server, "/sync", view="Household")[0], 303)
                self.assertEqual(self.snapshots(), expected)
            self.assertEqual(request(server, "/remove/1", view="Household")[0], 303)
            bridge.row.update(balance="99", currency="USD")
            self.assertEqual(request(server, "/sync", view="Household")[0], 303)
            self.assertEqual(self.snapshots(), [])
            self.assertIn("No dated balances", request(server)[1])

    def test_owner_and_exclusion_scope_at_view_observed_dates(self):
        with running(self.path) as server:
            self.assertIn("No dated balances", request(server)[1])
            populate(server)
            for view, expected in {
                "/": ([("01", "USD 100.00"), ("03", "USD 80.00"), ("05", "USD 100.00")],
                      [("03", "JPY 500"), ("05", "JPY 500")]),
                "/?view=Young": ([("01", "USD 100.00"), ("03", "USD 80.00"), ("05", "USD 100.00")], []),
                "/?view=Nanami": ([("03", "USD -20.00")], [("03", "JPY 500")]),
            }.items():
                status, html = request(server, view)
                self.assertEqual(status, 200)
                self.assertLess(html.index('id="history-heading"'), html.index('class="acct-table"'))
                self.assertNotIn('class="history-backdated"', html)
                for currency, points in zip(("USD", "JPY"), expected):
                    self.assertEqual(table(html, currency), [(DAY + d, v) for d, v in points])
                self.assertEqual(html.count('class="history-plot"'), sum(len(points) >= 2 for points in expected))
            # Today's owner reassignment is retroactive; ownerless rows only appear in Household.
            self.assertEqual(request(server, "/owner/1", owner="Young")[0], 303)
            self.assertEqual(table(request(server, "/?view=Young")[1], "USD"), [(DAY + "03", "USD -20.00")])
            self.assertEqual(table(request(server)[1], "USD")[0], (DAY + "01", "USD 100.00"))
            self.assertEqual(request(server, "/exclude/1", excluded="1")[0], 303)
            self.assertEqual(table(request(server)[1], "USD"), [(DAY + "03", "USD -20.00")])

    def test_backdated_note_only_for_views_that_include_the_edited_account(self):
        with running(self.path) as server:
            populate(server)
            self.assertEqual(request(server, "/edit/1", **dict(BASE, inline="1", balance="90"))[0], 303)
            for view in ("/", "/?view=Young"):
                html = request(server, view)[1]
                self.assertIn('class="history-backdated"', html)
                self.assertIn("USD 70.00</strong>", html)  # Account table totals use 90 - 20.
                self.assertEqual(table(html, "USD"), [(DAY + "01", "USD 90.00"),
                                                      (DAY + "03", "USD 70.00"),
                                                      (DAY + "05", "USD 100.00")])
            html = request(server, "/?view=Nanami")[1]
            self.assertNotIn('class="history-backdated"', html)
            self.assertEqual(table(html, "USD"), [(DAY + "03", "USD -20.00")])
            self.assertEqual(request(server, "/exclude/1", excluded="1")[0], 303)
            self.assertNotIn('class="history-backdated"', request(server)[1])

    def test_currency_turnover_absence_reappearance_and_exact_amounts(self):
        with running(self.path) as server:
            huge = "999999999999999.99"
            self.assertEqual(request(server, "/new", **dict(BASE, balance=huge))[0], 303)
            self.assertEqual(request(server, "/edit/1", **dict(BASE, asof=DAY + "03", currency="JPY",
                                                              side="debt", balance="0"))[0], 303)
            html = request(server)[1]
            self.assertEqual(table(html, "USD"), [(DAY + "01", "USD 999,999,999,999,999.99"),
                                                  (DAY + "03", "No included balance")])
            self.assertEqual(table(html, "JPY"), [(DAY + "03", "JPY 0")])  # zero isn't absence
            self.assertIn("No included balance on <time>2024-11-03</time>", html)
            self.assertNotIn('class="history-plot"', html)
            self.assertEqual(request(server, "/edit/1", **dict(BASE, asof=DAY + "05", balance=huge))[0], 303)
            self.assertEqual(request(server, "/new", **dict(BASE, asof=DAY + "05", side="debt",
                                                           balance="999999999999999.98"))[0], 303)
            html = request(server)[1]
            self.assertEqual(table(html, "USD")[-1], (DAY + "05", "USD 0.01"))
            self.assertEqual(table(html, "JPY")[-1], (DAY + "05", "No included balance"))
            self.assertIn('d="M6.00 10.00 H300.00 M594.00 118.00"', html)
            # Backdated manual save changes current snapshot, not the retained later point.
            self.assertEqual(request(server, "/edit/1", **dict(BASE, asof=DAY + "01", balance="10"))[0], 303)
            html = request(server)[1]
            self.assertIn("USD -999,999,999,999,989.98</strong>", html)
            self.assertEqual(table(html, "USD")[-1], (DAY + "05", "USD 0.01"))

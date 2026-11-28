"""L29: Projection folded into Accounts. Fictional DBs only; the clock is pinned; no network."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode
from unittest import mock

import history
import l29_golden
import projections_ui
import server
from l29_fixture import fetch, running, seed

GOLDEN = json.loads((Path(__file__).with_name("l29_golden.json")).read_text())


def projection_url(view, currency):
    """Where the Projection panel is served. Baseline 043c5a9: its own page."""
    return "/projections?" + urlencode({"view": view, "currency": currency})


class Base(unittest.TestCase):
    backdated = False

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="fictional-l29-", dir=Path(tempfile.gettempdir()).resolve()))
        self.path = Path(tmp) / "accounts.sqlite3"
        self.ids = seed(self.path, backdated=self.backdated)
        self.srv = self.enterContext(running(self.path))

    def get(self, target, **kw):
        return fetch(self.srv, target, **kw)

    def post(self, target, data, **kw):
        return fetch(self.srv, target, data, **kw)

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, args).fetchall()


def accounts_url(view, currency):
    query = {"view": view} if view != "Household" else {}
    return "/?" + urlencode({**query, "currency": currency, "chart": "projection"})


class Golden(Base):
    def test_example_9_numbers_equal_the_baseline_projections_page(self):
        """Pinned at 043c5a9 from GET /projections on this fixture: numbers, not layout."""
        self.check(projection_url)

    def test_example_9_same_numbers_in_the_accounts_projection_tab(self):
        self.check(accounts_url)

    def check(self, url):
        got = l29_golden.collect(self.srv, fetch, url)
        self.assertEqual(sorted(got), sorted(GOLDEN))
        for case in GOLDEN:
            for region in GOLDEN[case]:
                with self.subTest(case=case, region=region):
                    want, have = GOLDEN[case][region], json.loads(json.dumps(got[case][region]))
                    # The suggestion card now shows the averages first and the months table inside a collapsed ℹ️.
                    self.assertEqual(sorted(have) if region == "suggest" else have,
                                     sorted(want) if region == "suggest" else want)

    def test_golden_is_not_vacuous(self):
        household = GOLDEN["Household-USD"]
        for region in ("cards", "annual", "budget", "notes", "suggest", "debt", "asset", "form"):
            self.assertTrue(household[region], region)
        self.assertIn(["Net worth", "Start: USD 980.00", "Year 10: USD 262,935.95"], household["cards"])


class Panel(Base):
    """L29-2: the Projection panel on the Accounts page, built only when that tab is open."""

    def test_default_path_never_builds_the_projection_panel(self):
        with mock.patch.object(projections_ui, "embed", side_effect=AssertionError("built on History")):
            for target in ("/", "/?view=Nanami&currency=EUR", "/?chart=history"):
                self.assertEqual(self.get(target)[0], 200, target)
        html = self.get("/")[2]
        self.assertNotIn("projection-heading", html)

    def test_projection_tab_is_marked_and_keeps_view_and_currency(self):
        _, _, html = self.get("/?view=Nanami&currency=EUR&chart=projection")
        nav = lambda label: re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)</a>',
                                       re.search(rf'aria-label="{label}">(.*?)</nav>', html, re.S)[1])
        self.assertEqual([(t[2], bool(t[1])) for t in nav("Net worth chart")], [("History", False), ("Projection", True)])
        self.assertEqual(nav("Net worth chart")[0][0], "/?view=Nanami&amp;currency=EUR#net-worth")
        self.assertEqual([(t[2], bool(t[1])) for t in nav("Currency")], [("USD", False), ("EUR", True)])  # no "All" here
        self.assertIn('name="currency" value="EUR"', html)
        self.assertIn("Nanami EUR Checking", html)                      # the account table is still on the page
        self.assertEqual(html.count('aria-label="Currency"'), 1)        # one control set, no second picker
        self.assertNotIn("projection-selector", html)

    def test_default_currency_is_the_view_default_and_only_the_default_chip_is_current(self):
        _, _, html = self.get("/?chart=projection")
        self.assertIn('name="currency" value="USD"', html)
        self.assertRegex(html, r'<a href="/\?currency=USD&amp;chart=projection#net-worth" aria-current="page">USD</a>')

    def test_all_projection_pieces_are_in_the_panel_in_the_requested_order(self):
        html = self.get("/?chart=projection")[2]
        order = [html.index(m) for m in ('id="net-worth"', 'class="net-chart"', 'id="projection-suggestion"',
                                         'class="cards projection-cards"', 'Show annual breakdown', 'id="debt-heading"',
                                         'id="asset-heading"', 'id="projection-assumptions"')]
        self.assertEqual(order, sorted(order))
        for needle in ("Advanced: targets, rates, display units", "Avalanche vs snowball", "rate missing", "Rate missing for",
                       "Planned only:", "With extra:", "Saved balance dates", "stale", "How this projection works"):
            self.assertIn(needle, html, needle)

    def test_hosted_inputs_hide_suggestions_and_observed_payments(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            token = dict(token="t", section_for=server.section_for, places=server.places,
                         today=server.date.today(), suggestions=False)
            html = projections_ui.embed(db, "Household", "USD", **token)
        self.assertNotIn("projection-suggestion", html)
        self.assertNotIn("Observed:", html)
        self.assertIn("projection-assumptions", html)

    def test_no_duplicate_element_ids_on_the_page(self):
        for target in ("/", "/?chart=projection", "/?view=Nanami&currency=EUR&chart=projection"):
            ids = re.findall(r'\bid="([^"]+)"', self.get(target)[2])
            self.assertEqual([i for i in set(ids) if ids.count(i) > 1], [], target)


class Scoping(Base):
    """L29-1: chart/currency parameters, the sub-tab bar, currency chips, owner links."""

    def links(self, html, label):
        nav = re.search(rf'<nav class="[^"]*" aria-label="{label}">(.*?)</nav>', html, re.S)
        return re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)</a>', nav[1]) if nav else []

    def test_default_is_history_with_a_tab_bar_above_the_unchanged_history_panel(self):
        status, _, html = self.get("/")
        self.assertEqual(status, 200)
        tabs = self.links(html, "Net worth chart")
        self.assertEqual([(t[2], bool(t[1])) for t in tabs], [("History", True), ("Projection", False)])
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            points = db.execute("""SELECT h.*, a.asof AS current_asof FROM balance_history h JOIN accounts a
                ON a.id=h.account_id WHERE a.excluded=0 ORDER BY h.asof, h.account_id""").fetchall()
        expected = history.history_panel(history.net_worth(points), server.money,
                                         has_backdated=any(p["asof"] > p["current_asof"] for p in points))
        self.assertIn(expected, html)
        self.assertLess(html.index('aria-label="Net worth chart"'), html.index('id="history-heading"'))

    def test_bad_chart_or_currency_is_400_and_unknown_parameters_are_ignored(self):
        for target in ("/?chart=bogus", "/?chart=history&chart=history", "/?currency=usd", "/?currency=EUR&currency=USD",
                       "/?currency=JPY", "/?view=Young&currency=EUR", "/?view=Nobody"):
            with self.subTest(target=target):
                self.assertEqual(self.get(target)[0], 400)
        self.assertEqual(self.get("/?utm=1&chart=history&currency=EUR")[0], 200)

    def test_chips_only_when_the_view_has_two_currencies(self):
        chips = lambda target: [(t[2], bool(t[1])) for t in self.links(self.get(target)[2], "Currency")]
        self.assertEqual(chips("/"), [("All", True), ("USD", False), ("EUR", False)])
        self.assertEqual(chips("/?currency=EUR"), [("All", False), ("USD", False), ("EUR", True)])
        self.assertEqual(chips("/?view=Nanami"), [("All", True), ("USD", False), ("EUR", False)])
        self.assertEqual(chips("/?view=Young"), [])

    def test_a_chosen_currency_shows_only_that_history_series_and_all_shows_both(self):
        both = self.get("/")[2]
        self.assertIn("<h3>EUR net worth</h3>", both)
        self.assertIn("<h3>USD net worth</h3>", both)
        eur = self.get("/?currency=EUR")[2]
        self.assertIn("<h3>EUR net worth</h3>", eur)
        self.assertNotIn("<h3>USD net worth</h3>", eur)

    def test_owner_links_keep_the_currency_only_where_the_target_view_has_it(self):
        _, _, html = self.get("/?view=Nanami&currency=EUR")
        owners = {label: href for href, _, label in self.links(html, "Inventory views")}
        self.assertEqual(owners["Household"], "/?currency=EUR#net-worth")
        self.assertEqual(owners["Nanami"], "/?view=Nanami&amp;currency=EUR#net-worth")
        self.assertEqual(owners["Young"], "/?view=Young")           # Young has no EUR account: no 400 waiting there
        for href in owners.values():
            self.assertEqual(self.get(href.replace("&amp;", "&").partition("#")[0])[0], 200, href)
        plain = {label: href for href, _, label in self.links(self.get("/")[2], "Inventory views")}
        self.assertEqual(plain, {"Household": "/", "Young": "/?view=Young", "Nanami": "/?view=Nanami"})


if __name__ == "__main__":
    unittest.main()

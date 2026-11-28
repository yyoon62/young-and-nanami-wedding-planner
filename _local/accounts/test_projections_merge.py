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
    def test_example_9_the_accounts_projection_tab_shows_the_numbers_of_the_baseline_projections_page(self):
        """Pinned at 043c5a9 (l29_golden.json, generated from GET /projections on this fixture): numbers, not layout."""
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


class Routes(Base):
    """L29-4: no Projections tab; GET /projections is a validated permanent redirect."""

    NAV = ["Accounts", "Transactions", "Add account", "Statements", "Federal preview"]

    def nav(self, html):
        nav = re.search(r'<nav class="sitenav"[^>]*>(.*?)</nav>', html, re.S)[1]
        return re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)</a>', nav)

    def test_example_1_nav_has_no_projections_and_marks_the_active_page(self):
        pages = {"/": "Accounts", "/?chart=projection": "Accounts", "/?view=Young&chart=projection": "Accounts",
                 "/new": "Add account", "/servicers": "Statements", "/transactions": "Transactions",
                 f"/edit/{self.ids['young-chk']}": "Accounts"}
        for target, active in pages.items():
            with self.subTest(target=target):
                status, _, html = self.get(target)
                self.assertEqual(status, 200)
                nav = self.nav(html)
                self.assertEqual([label for _, _, label in nav], self.NAV)
                self.assertEqual([label for _, current, label in nav if current], [active])
                self.assertNotIn("Projections", html.partition("</nav>")[0])

    def test_example_2_old_links_redirect_permanently_to_the_projection_tab(self):
        for old, new in (("/projections?view=Nanami&currency=EUR", "/?view=Nanami&currency=EUR&chart=projection#net-worth"),
                         ("/projections", "/?chart=projection#net-worth"),
                         ("/projections?view=Young", "/?view=Young&chart=projection#net-worth"),
                         ("/projections?currency=USD", "/?currency=USD&chart=projection#net-worth"),
                         ("/projections?view=Household&currency=EUR", "/?currency=EUR&chart=projection#net-worth")):
            with self.subTest(old=old):
                status, headers, body = self.get(old)
                self.assertEqual((status, headers["Location"], body), (308, new, ""))
                self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, html = self.get("/?view=Nanami&currency=EUR&chart=projection")
        self.assertIn('name="currency" value="EUR"', html)
        status, _, html = self.get("/?chart=projection")                         # bare: Household, default currency
        self.assertIn('name="view" value="Household"', html)
        self.assertIn('name="currency" value="USD"', html)

    def test_example_2_bad_links_are_a_400_not_a_redirect_to_nowhere(self):
        for target in ("/projections?view=Mallory", "/projections?view=Young&currency=EUR", "/projections?currency=JPY",
                       "/projections?view=Young&view=Nanami", "/projections?currency=USD&currency=EUR",
                       "/projections?currency=usd"):
            with self.subTest(target=target):
                status, headers, body = self.get(target)
                self.assertEqual(status, 400)
                self.assertNotIn("Location", headers)
                self.assertNotIn('id="net-worth"', body)


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


FORM = dict(years="10", income="4000", spending="2500", debt_target="", retirement_target="", investment_target="",
            checking_apy="0", hysa_apy="4", investment_return="7", retirement_return="7", debt_apr="6",
            inflation="2.5", mode="nominal")


class Saving(Base):
    """L29-3: POST /projections goes back to (or stays in) the Projection tab; account-table forms keep tab + currency."""

    def saved(self):
        return self.rows("SELECT view, currency, assumptions FROM projection_assumptions ORDER BY 1, 2")

    def test_example_7_save_returns_to_the_same_view_and_currency_tab_and_survives_restart(self):
        before = dict((row[:2], row[2]) for row in self.saved())
        status, headers, _ = self.post("/projections", dict(FORM, view="Young", currency="USD", years="7"))
        self.assertEqual((status, headers["Location"]), (303, "/?view=Young&currency=USD&chart=projection#net-worth"))
        status, headers, _ = self.post("/projections", dict(FORM, view="Household", currency="USD", years="9"))
        self.assertEqual(headers["Location"], "/?currency=USD&chart=projection#net-worth")
        after = dict((row[:2], row[2]) for row in self.saved())
        self.assertEqual(sorted(after), [("Household", "USD"), ("Young", "USD")])
        self.assertEqual(json.loads(after[("Household", "USD")])["years"], "9")
        self.assertEqual(json.loads(after[("Young", "USD")])["years"], "7")
        self.assertNotIn(("Nanami", "EUR"), after)
        html = self.get("/?view=Young&currency=USD&chart=projection")[2]
        self.assertIn('name="years" inputmode="numeric" value="7"', html)
        self.assertIn("Year 7", html)
        self.assertEqual(self.get("/?view=Nanami&currency=EUR&chart=projection")[2].count('name="years" inputmode="numeric" value="10"'), 1)
        self.srv.shutdown()   # a restart reads the same database
        with running(self.path) as again:
            self.assertIn('name="years" inputmode="numeric" value="7"',
                          fetch(again, "/?view=Young&currency=USD&chart=projection")[2])
        self.assertEqual(sorted(before), [("Household", "USD")])           # only the fixture's own row existed

    def test_example_8_invalid_values_render_inside_the_projection_tab_and_save_nothing(self):
        self.post("/projections", dict(FORM, view="Nanami", currency="EUR", years="5"))
        saved = self.saved()
        status, _, html = self.post("/projections", dict(FORM, view="Nanami", currency="EUR", years="-1", income="12,5"))
        self.assertEqual(status, 422)
        self.assertEqual(self.saved(), saved)
        self.assertIn('id="net-worth"', html)                              # the Accounts page, not a standalone page
        self.assertIn('class="acct-table"', html)
        self.assertRegex(html, r'<a href="[^"]*chart=projection[^"]*" aria-current="page">Projection</a>')
        self.assertIn('name="years" inputmode="numeric" value="-1" aria-invalid="true"', html)   # attempted value kept
        self.assertIn('value="12,5"', html)
        self.assertIn('id="error-years"', html)
        self.assertIn("Not saved", html)
        self.assertIn('Start: EUR 9,000.00', html)                         # results still use the previous assumptions
        self.assertIn('Year 5', html)
        self.assertIn('action="/projections#projection-assumptions"', html)

    def test_scope_errors_and_suggestion_refusals_stay_standalone_and_save_nothing(self):
        before = self.saved()
        for data in (dict(FORM, view="Mallory", currency="USD"), dict(FORM, view="Young", currency="EUR"),
                     dict(FORM, view="Young", currency="USD", apply="suggestion", suggest_income="1", suggest_spending="1")):
            with self.subTest(data=data):
                status, _, html = self.post("/projections", data)
                self.assertEqual(status, 422)
                self.assertNotIn('id="net-worth"', html)
        self.assertEqual(self.saved(), before)

    def test_applying_the_shown_suggestion_saves_income_and_spending_and_returns_to_the_tab(self):
        html = self.get("/?chart=projection")[2]
        income, spending = (re.search(rf'name="suggest_{k}" value="([^"]+)"', html)[1] for k in ("income", "spending"))
        status, headers, _ = self.post("/projections", dict(view="Household", currency="USD", apply="suggestion",
                                                           suggest_income=income, suggest_spending=spending))
        self.assertEqual((status, headers["Location"]), (303, "/?currency=USD&chart=projection#net-worth"))
        saved = json.loads(dict((r[:2], r[2]) for r in self.saved())[("Household", "USD")])
        self.assertEqual((saved["income"], saved["spending"]), (income, spending))
        self.assertIn("Already saved as your income and spending.", self.get("/?chart=projection")[2])

    def test_account_table_forms_carry_tab_and_currency_through_every_action(self):
        html = self.get("/?view=Nanami&currency=EUR&chart=projection")[2]
        table = html[html.index('<table class="acct-table"'):]
        for action in ("owner", "rename", "section", "side", "exclude", "edit"):
            form = re.search(rf'<form method="post" action="/{action}/\d+"[^>]*>(.*?)</form>', table, re.S)[1]
            self.assertIn('name="view" value="Nanami"', form, action)
            self.assertIn('name="nw_chart" value="projection"', form, action)
            self.assertIn('name="nw_currency" value="EUR"', form, action)
        self.assertIn('<input required name="currency" maxlength="3" value="EUR">', table)   # the account's own field
        plain = self.get("/")[2]
        self.assertNotIn("nw_chart", plain)
        self.assertNotIn("nw_currency", plain)

    def test_example_14_each_account_action_returns_to_the_same_tab_view_and_currency(self):
        eur, card = self.ids["nanami-eur"], self.ids["nanami-card"]
        ctx = dict(view="Nanami", nw_chart="projection", nw_currency="EUR")
        here = "/?view=Nanami&currency=EUR&chart=projection"
        for path, data in ((f"/owner/{eur}", dict(owner="Young")), (f"/rename/{eur}", dict(nickname="Renamed EUR")),
                           (f"/section/{eur}", dict(section="Savings")), (f"/side/{card}", dict(side="asset")),
                           (f"/exclude/{card}", dict(excluded="1")),
                           (f"/edit/{eur}", dict(institution="Maple Trust", nickname="Renamed EUR", kind="Savings",
                                                 owner="Nanami", side="asset", currency="EUR", balance="3000.00",
                                                 asof="2026-11-25", update_method="Manual entry", inline="1"))):
            with self.subTest(path=path):
                status, headers, _ = self.post(path, dict(data, **ctx))
                self.assertEqual((status, headers["Location"]), (303, here))
        status, headers, _ = self.post(f"/owner/{eur}", dict(owner="Nanami", view="Nanami"))   # no context: as before
        self.assertEqual(headers["Location"], "/?view=Nanami")
        status, headers, _ = self.post(f"/owner/{eur}", dict(owner="Nanami", view="Nanami", nw_chart="evil", nw_currency="eur"))
        self.assertEqual(headers["Location"], "/?view=Nanami")

    def test_a_redirect_never_points_at_a_currency_the_view_just_lost(self):
        ctx = dict(view="Nanami", nw_chart="projection", nw_currency="EUR")
        for key in ("nanami-eur", "nanami-eur-sav"):
            status, headers, _ = self.post(f"/exclude/{self.ids[key]}", dict(excluded="1", **ctx))
        self.assertEqual((status, headers["Location"]), (303, "/?view=Nanami&chart=projection"))
        self.assertEqual(self.get(headers["Location"])[0], 200)
        self.assertEqual(self.get("/?view=Nanami&currency=EUR&chart=projection")[0], 400)   # a hand-typed link still 400s


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

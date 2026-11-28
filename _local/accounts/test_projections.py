"""L20 acceptance using fictional balances, temporary SQLite, and in-memory HTTP."""
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from http.client import HTTPResponse
import inspect
import io
from pathlib import Path
import re
import sqlite3
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode

import server
from hosted_auth import Auth, Config, digest
from projections import (
    Assumptions, Balances, available_currencies, eligible_accounts, install_projections,
    project, starting_balances, validate_assumptions,
)
from projections_ui import projection_get, projection_post


D = Decimal
TODAY = date(2026, 11, 27)
FLAT = Assumptions(years=1, hysa_apy=D(0), investment_return=D(0), retirement_return=D(0), debt_apr=D(0))
ORIGIN = "https://accounts.example.test"
CONFIG = Config.load(dict(
    GOOGLE_CLIENT_ID="12345-fictional.apps.googleusercontent.com", LOANS_ORIGIN=ORIGIN,
    LOANS_SESSION_KEY="de" * 32, LOANS_MEMBERS="alpha@gmail.com,beta@example.test",
))


def hooked_handler():
    """Rehearse the exact dispatch hooks in memory before the parent wires server.py.

    Both routes still traverse the real Handler access checks and response writer.
    Once wired, exercise the actual methods instead of adding the hooks twice.
    """
    namespace = {**vars(server), "projection_get": projection_get, "projection_post": projection_post}
    sources = []
    for name, called, anchor, hook in (
        ("route_get", "projection_get", '        if path == "/federal-preview":', '''
if path == "/projections":
    status, body, location = projection_get(
        self.server.db, self.path, page=page, token=self.token, people=PEOPLE,
        section_for=section_for, places=places, today=date.today())
    return self.send(status, body, location=location)
'''),
        ("route_post", "projection_post", "        target = self.path", '''
if self.path == "/projections":
    if not self.server.auth:
        try:
            data = self.read_form(8192)
            self.require_csrf(data)
        except Rejected as exc:
            return self.reject(exc)
    status, body, location = projection_post(
        self.server.db, data, page=page, token=self.token, people=PEOPLE,
        section_for=section_for, places=places, today=date.today())
    return self.send(status, body, location=location)
'''),
    ):
        method = getattr(server.Handler, name)
        if called in method.__code__.co_names:
            continue
        source = inspect.getsource(method)
        if source.count("\n" + anchor) != 1:
            raise AssertionError(f"Review projection hook placement in {name}.")
        sources.append(source.replace("\n" + anchor, "\n" + textwrap.indent(hook.strip() + "\n", "        ") + anchor))
    exec(compile("class ProjectionHandler(Handler):\n    pass\n" + "\n".join(sources),
                 "<L20 dispatch hooks>", "exec"), namespace)
    return namespace["ProjectionHandler"]


class InMemorySocket:
    """Just the subset StreamRequestHandler and HTTPResponse need."""
    def __init__(self, raw):
        self.raw = raw
        self.sent = bytearray()

    def makefile(self, *_):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, _):
        pass


def request(handler, fixture, target, data=None, *, headers=()):
    body = b"" if data is None else urlencode(data).encode()
    raw = f'{"GET" if data is None else "POST"} {target} HTTP/1.1\r\nConnection: close\r\n'
    if not any(key.lower() == "host" for key, _ in headers):
        raw += "Host: localhost:8765\r\n"
    if data is not None:
        raw += f"Content-Length: {len(body)}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
    raw += "".join(f"{key}: {value}\r\n" for key, value in headers) + "\r\n"
    socket = InMemorySocket(raw.encode() + body)
    handler(socket, ("127.0.0.1", 12345), fixture)
    with closing(HTTPResponse(InMemorySocket(bytes(socket.sent)))) as response:
        response.begin()
        return response.status, response.headers, response.read().decode()


class ModelTests(unittest.TestCase):
    def test_given_surplus_when_debt_pays_off_after_interest_then_reallocates_without_new_money(self):
        initial = Balances(debt=D(50))
        points = project(initial, replace(FLAT, income=D(125), spending=D(25), debt_apr=D(12),
                                          debt_target=D(80), retirement_target=D(20), investment_target=D(10)))
        first, second = points[1].balances, points[2].balances
        self.assertEqual(first, Balances(hysa=D("19.50"), retirement=D(20), investments=D(10)))
        self.assertEqual(first.net_worth - initial.net_worth, D("99.50"))
        self.assertEqual(second, Balances(hysa=D("89.50"), retirement=D(40), investments=D(20)))
        self.assertEqual(points[12].balances.net_worth, D("1149.50"))

    def test_given_deficit_when_cash_and_then_hysa_drawn_then_unfunded_gap_is_not_a_loan(self):
        points = project(Balances(cash=D(25), hysa=D(25), investments=D(100)),
                         replace(FLAT, spending=D(20)))
        self.assertEqual(points[1].balances, Balances(cash=D(5), hysa=D(25), investments=D(100)))
        self.assertEqual(points[2].balances, Balances(hysa=D(10), investments=D(100)))
        self.assertEqual(points[2].cumulative_gap, 0)
        self.assertEqual(points[3].balances, Balances(investments=D(100)))
        self.assertEqual(points[3].cumulative_gap, 10)
        self.assertEqual(points[12].cumulative_gap, 190)
        self.assertEqual(points[12].balances.net_worth, 100)

    def test_given_annual_rates_when_twelve_months_then_geometric_assets_and_nominal_debt(self):
        initial = Balances(cash=D(1000), hysa=D(1000), investments=D(1000), retirement=D(1000), debt=D(1000))
        a = replace(FLAT, checking_apy=D(3), hysa_apy=D(4), investment_return=D(7),
                    retirement_return=D(-12), debt_apr=D(12))
        end = project(initial, a)[12].balances
        with localcontext() as ctx:
            ctx.prec = 64
            debt = D(1000) * D("1.01") ** 12
        for actual, expected in ((end.cash, 1030), (end.hysa, 1040), (end.investments, 1070),
                                 (end.retirement, 880), (end.debt, debt)):
            self.assertLess(abs(actual - expected), D("1e-56"))
        # A month-end deposit does not get that month's interest.
        funded = project(initial, replace(a, income=D("12.34")))
        first_without_deposit = project(initial, a)[1].balances
        self.assertAlmostEqual(funded[1].balances.hysa - first_without_deposit.hysa, D("12.34"), places=25)

    def test_given_inflation_when_display_toggled_then_start_and_nominal_flows_are_unchanged(self):
        start = Balances(debt=D(1000), unclassified=D(10))
        a = replace(FLAT, income=D(10), inflation=D(25), years=2)
        nominal = project(start, a)
        real = project(start, replace(a, mode="real"))
        self.assertEqual(real[0].display, start)
        self.assertEqual([p.balances for p in real], [p.balances for p in nominal])
        self.assertEqual(nominal[-1].display.net_worth, -750)
        self.assertLess(abs(real[-1].display.net_worth - (D(-750) / D("1.25") ** 2)), D("1e-56"))
        self.assertLess(abs(real[12].display.net_worth - (D(-870) / D("1.25"))), D("1e-56"))

    def test_currency_order_and_bounds(self):
        rows = [("", "", currency) for currency in ("JPY", "USD", "EUR", "JPY", "USD", "EUR")]
        self.assertEqual(available_currencies(rows), ["USD", "EUR", "JPY"])
        self.assertEqual(available_currencies(rows + [("", "", "JPY")]), ["JPY", "USD", "EUR"])
        self.assertEqual(available_currencies(rows[:1] + rows[2:3]), ["EUR", "JPY"])
        self.assertEqual(available_currencies([]), ["USD"])
        defaults = Assumptions().form_values()
        valid, errors = validate_assumptions(dict(defaults, years="40", income="999999999999999.1234",
                                                  investment_return="-99", inflation="100", debt_target=""), 4)
        self.assertFalse(errors)
        self.assertEqual(len(project(Balances(), valid)), 481)
        for key, value in (
            ("years", "0"), ("years", "41"), ("years", "1.0"), ("income", "1000000000000000"),
            ("income", "-1"), ("income", "1,000"), ("income", "١"), ("investment_return", "NaN"),
            ("investment_return", "-99.0001"), ("debt_apr", "-1"), ("hysa_apy", "101"),
            ("inflation", "2.12345"), ("spending", "1e2"), ("investment_target", "-1"),
        ):
            with self.subTest(key=key, value=value):
                self.assertIn(key, validate_assumptions(dict(defaults, **{key: value}), 2)[1])


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = hooked_handler()
        cls.wired = "projection_get" in server.Handler.route_get.__code__.co_names

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp) / "fictional.sqlite3"
        self.restart()

    def restart(self):
        server.init_db(self.path)
        if not self.wired:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                install_projections(db)
        self.fixture = SimpleNamespace(db=self.path, token="local-fixture-token", auth=None, server_port=8765,
                                       batch=None, logo_dir=None, notices=[])

    @staticmethod
    def add(db, kind, owner, amount, *, side="asset", currency="USD", asof="2026-10-01", excluded=0):
        db.execute("""INSERT INTO accounts (institution, nickname, kind, side, currency,
            balance, asof, owner, excluded) VALUES ('Example', 'Fictional', ?, ?, ?, ?, ?, ?, ?)""",
                   (kind, side, currency, amount, asof, owner, excluded))

    def populate(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            def add(*args, **kwargs):
                self.add(db, *args, **kwargs)
            add("Checking", "Young", "100.10")
            add("Savings", "Joint", "10.20", asof="2026-11-26")
            add("Brokerage", "Nanami", "15.30")
            add("IRA", "Unspecified", "20.40")
            add("Custom", "Unspecified", "2.01")
            add("Retirement", "Young", "250.55", side="debt")
            add("Student loan", "Young", "30.10", side="debt")
            add("Credit card", "Nanami", "40.20", side="debt", asof="2026-10-28")
            add("Other", "Joint", "5.03", side="debt")
            add("Mortgage", "Unspecified", "3.05")  # asset in Loans
            add("Checking", "Young", "777.77", asof="2026-01-01", excluded=1)
            add("Checking", "Unspecified", "999999", currency="JPY")

    def call(self, path="/projections", data=None, **kwargs):
        return request(self.handler, self.fixture, path, data, **kwargs)

    def form(self, **params):
        return dict(Assumptions().form_values(), **{
            "view": "Household", "currency": "USD", "token": self.fixture.token, **params,
        })

    def saved(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT * FROM projection_assumptions ORDER BY view, currency").fetchall()

    def test_given_mixed_accounts_then_exact_buckets_and_joint_once_by_view(self):
        self.populate()
        with closing(sqlite3.connect(self.path)) as db:
            def seed(view, currency="USD"):
                return starting_balances(eligible_accounts(db, view), currency, server.section_for, TODAY)
            household = seed("Household")
            self.assertEqual(household.count, 10)
            self.assertEqual(household.balances, Balances(D("102.11"), D("10.20"), D("15.30"), D("20.40"),
                                                         D("325.88"), D("3.05")))
            self.assertEqual(household.balances.net_worth, D("-174.82"))
            self.assertEqual((household.oldest, household.newest, household.stale_count, household.unclassified_count),
                             (date(2026, 10, 1), date(2026, 11, 26), 8, 1))
            young, nanami, jpy = seed("Young"), seed("Nanami"), seed("Household", "JPY")
            self.assertEqual((young.count, young.balances.debt, young.balances.net_worth),
                             (5, D("285.68"), D("-175.38")))
            self.assertEqual((nanami.count, nanami.balances.debt, nanami.balances.net_worth),
                             (4, D("45.23"), D("-19.73")))
            self.assertEqual((jpy.count, jpy.oldest, jpy.newest, jpy.balances),
                             (1, date(2026, 10, 1), date(2026, 10, 1), Balances(cash=D(999999))))
        status, _, body = self.call()
        self.assertEqual(status, 200)
        for label in ("Start: USD -174.82", "Start: USD 325.88",
                      "<time>2026-10-01</time> to <time>2026-11-26</time> (mixed dates)",
                      "not treated as spendable cash", "Dashed line: zero.",
                      'role="img"', "Show annual breakdown", "does not refresh"):
            self.assertIn(label, body)
        self.assertNotIn("USD 777.77", body)
        self.assertNotIn("999,999", body)
        body = self.call("/projections?currency=JPY")[2]
        self.assertIn("Start: JPY 999,999", body)
        self.assertNotIn("USD -174.82", body)
        body = self.call("/projections?view=Nanami")[2]
        self.assertNotIn('<option value="JPY"', body)
        self.assertIn("Start: USD -19.73", body)
        for query in ("currency=EUR", "view=Joint", "view=Young&view=Nanami", "currency=USD&currency=JPY"):
            self.assertEqual(self.call("/projections?" + query)[0], 400)

    def test_given_a_card_with_no_terms_then_it_is_assumed_paid_in_full_and_held_flat(self):
        """L23 (by design): the old blended 6% accrual and debt payment no longer apply to a credit card."""
        with closing(sqlite3.connect(self.path)) as db, db:
            self.add(db, "Credit card", "Unspecified", "100.00", side="debt")
            self.add(db, "Checking", "Unspecified", "200.00")
        self.assertEqual(self.call(data=self.form(years="1", income="40", spending="35", debt_target="80"))[0], 303)
        body = self.call()[2]
        self.assertIn("1 card(s) assumed paid in full:", body)
        self.assertIn("balance USD 100.00 held flat, no interest", body)
        self.assertIn("<small>Debt owed</small><strong>Start: USD 100.00</strong><div>Year 1: USD 100.00", body)
        self.assertNotIn("No debt payment is modeled:", body)        # there is no payable model debt
        self.assertNotIn("Rate missing for", body)

    def test_given_debt_then_result_warns_about_no_payment_or_an_underfunded_target(self):
        # A debt with no rate (a personal loan here; before L23 this test used a credit card) keeps the blended path.
        with closing(sqlite3.connect(self.path)) as db, db:
            self.add(db, "Personal loan", "Unspecified", "100.00", side="debt")
            self.add(db, "Checking", "Unspecified", "200.00")
        no_payment = "No debt payment is modeled:"
        underfunded = "Debt payments can fall below the target:"
        service = "Debt service included in spending does not pay down model debt."
        body = self.call()[2]
        for text in (no_payment, "the debt payment target is zero", "Unpaid debt can grow with interest", service,
                     "Monthly net savings before modeled allocations (income minus spending)",
                     "not financial advice or a forecast"):
            self.assertIn(text, body)
        self.assertIn("<small>Debt owed</small><strong>Start: USD 100.00</strong><div>Year 10: USD 181.94", body)
        self.assertNotIn(underfunded, body)

        for income in ("34", "35"):
            self.assertEqual(self.call(data=self.form(years="1", income=income, spending="35", debt_target="80"))[0], 303)
            body = self.call()[2]
            self.assertIn(no_payment, body)
            self.assertIn("monthly net savings are zero or negative", body)
            self.assertNotIn(underfunded, body)

        params = dict(years="1", income="40", spending="35", debt_target="80", debt_apr="0")
        self.assertEqual(self.call(data=self.form(**params))[0], 303)
        body = self.call()[2]
        for text in (underfunded, "only USD 5.00 is left each month", "compared with a USD 80.00 target",
                     service, "not saved cash or HYSA",
                     "<small>Debt owed</small><strong>Start: USD 100.00</strong><div>Year 1: USD 40.00",
                     "<small>Cash + HYSA</small><strong>Start: USD 200.00</strong><div>Year 1: USD 200.00"):
            self.assertIn(text, body)
        self.assertNotIn(no_payment, body)

        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE accounts SET balance = '0.00' WHERE side = 'debt'")
        body = self.call()[2]
        for text in (no_payment, underfunded, service):
            self.assertNotIn(text, body)
        self.assertEqual(self.call(data=self.form())[0], 303)
        self.assertNotIn(no_payment, self.call()[2])

    def test_given_deficit_then_annual_rows_show_cash_depleted_before_savings(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.add(db, "Checking", "Unspecified", "25.00")
            self.add(db, "Savings", "Unspecified", "50.00")
        self.assertEqual(self.call(data=self.form(years="1", spending="5", hysa_apy="0"))[0], 303)
        body = self.call()[2]
        self.assertIn("<small>Cash + HYSA</small><strong>Start: USD 75.00</strong><div>Year 1: USD 15.00", body)
        annual = body.split("<summary>Show annual breakdown</summary>", 1)[1]
        self.assertIn('<th scope="col" class="num">Cash</th><th scope="col" class="num">Savings / HYSA</th>', annual)
        self.assertNotIn("Cash + HYSA", annual)
        for year, balances in (("Start", ("75.00", "25.00", "50.00")), ("1", ("15.00", "0.00", "15.00"))):
            row_start = f'<tr><th scope="row">{year}</th>'
            self.assertIn(row_start + "".join(f'<td class="num">USD {v}</td>' for v in balances), annual)
        self.assertNotIn("Unfunded spending begins", body)

    def test_given_no_balances_then_no_assumptions_row_until_valid_save_and_gap_visible(self):
        status, headers, body = self.call()
        self.assertEqual(status, 200)
        self.assertIn("No saved balances", body)
        self.assertIn("starts from zero", body)
        self.assertIn("Start: USD 0.00", body)
        self.assertIn('value="10"', body)
        self.assertIn("Illustrative projection only", body)
        if self.wired:
            self.assertIn('<a href="/projections" aria-current="page">Projections</a>', body)
        self.assertEqual(self.saved(), [])
        self.assert_policy(headers, body)
        self.assertEqual(self.call(data=self.form(years="1", spending="1.23"))[0], 303)
        body = self.call()[2]
        self.assertIn("Unfunded spending begins in month 1", body)
        self.assertIn("USD 14.76 cumulative", body)
        self.assertIn("not financed", body)

    def test_given_saved_assumptions_when_restarted_or_invalid_then_scoped_persistence_and_no_mutation(self):
        self.populate()
        for scope in (dict(years="2"), dict(currency="JPY", years="3"), dict(view="Young", years="4")):
            status, headers, _ = self.call(data=self.form(**scope))
            self.assertEqual(status, 303)
            self.assertIn("chart=projection#net-worth", headers["Location"])          # L29: back to the Accounts tab
            self.assertIn("Year " + scope["years"], self.call(headers["Location"].partition("#")[0])[2])
        expected = self.saved()
        self.assertEqual(len(expected), 3)
        self.restart()
        for query, year in (("", 2), ("?currency=JPY", 3), ("?view=Young", 4), ("?view=Nanami", 10)):
            self.assertIn(f"Year {year}:", self.call("/projections" + query)[2])
        attack = '"><svg onload="alert(1)">'
        status, _, body = self.call(data=self.form(years="41", income=attack, mode=attack))
        self.assertEqual(status, 422)
        self.assertIn("&quot;&gt;&lt;svg onload=&quot;alert(1)&quot;&gt;", body)
        self.assertNotIn(attack, body)
        self.assertIn('aria-describedby="error-income"', body)
        self.assertIn("Year 2:", body)
        self.assertEqual(self.call(data=self.form(currency="JPY", spending="1.01"))[0], 422)
        self.assertEqual(self.call(data=self.form(view="Nanami", currency="JPY"))[0], 422)
        self.assertEqual(self.saved(), expected)

    def assert_policy(self, headers, body):
        self.assertEqual(headers["Content-Security-Policy"],
                         "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("<script", body)
        self.assertNotIn(" style=", body)

    def test_local_dispatch_checks_host_origin_and_csrf_before_save(self):
        self.assertEqual(self.call(headers=(("Host", "outside.example"),))[0], 403)
        self.assertEqual(self.call(data=self.form(), headers=(("Origin", ORIGIN),))[0], 403)
        self.assertEqual(self.call(data=self.form(token="bad"))[0], 403)
        self.assertEqual(self.saved(), [])
        same_origin = (("Origin", "http://localhost:8765"),)
        self.assertEqual(self.call(data=self.form(), headers=same_origin)[0], 303)

    def test_hosted_dispatch_checks_signed_session_exact_origin_and_csrf_without_double_read(self):
        auth = Auth(self.path, CONFIG, keys=lambda _: self.fail("No verification/key lookup expected"), clock=lambda: 1)
        sid, token = "a" * 43, "b" * 43
        with auth.db() as db:
            email = CONFIG.members[0]
            db.execute("INSERT INTO hosted_identities VALUES (?,?)", (email, "10001"))
            db.execute("INSERT INTO hosted_sessions VALUES (?,?,?,?)", (digest(sid), email, token, 2))
        self.fixture.auth = auth
        cookie = (("Cookie", f"{CONFIG.name('session')}={sid}.{auth.signature(sid)}"),)
        origin = (("Origin", ORIGIN),)
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(self.call(data=self.form(token=token), headers=origin)[0], 401)
        status, headers, body = self.call(headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(re.search(r'name="token" value="([^"]+)"', body)[1], token)
        self.assert_policy(headers, body)
        self.assertEqual(self.call(data=self.form(token=token), headers=cookie)[0], 403)
        self.assertEqual(self.call(data=self.form(token=token), headers=cookie + origin + origin)[0], 403)
        self.assertEqual(self.call(data=self.form(token=token), headers=cookie + (("Origin", ORIGIN + "/"),))[0], 403)
        self.assertEqual(self.call(data=self.form(token="bad"), headers=cookie + origin)[0], 403)
        self.assertEqual(self.saved(), [])
        self.assertEqual(self.call(data=self.form(token=token, debt_target=""), headers=cookie + origin)[0], 303)
        self.assertEqual(len(self.saved()), 1)

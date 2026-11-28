"""Slice C: projection suggestions from transactions (dated basis, covered months only), Advanced, apply."""
from contextlib import closing
from datetime import date
from decimal import Decimal
import re
import sqlite3
from types import SimpleNamespace
import unittest

import server
from hosted_auth import Auth, digest
from projections import Assumptions, load_assumptions, save_assumptions
from projections_ui import projection_get, projection_post
from test_projections import CONFIG, ORIGIN, request
from tx_fixture import Fixture, cover, put_row, source_of
from tx_periods import suggest_monthly

TODAY = date(2026, 11, 28)
D = Decimal


def page(content, current=None, heading=""):
    return server.page(content, current=current, heading=heading)


class Suggest(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.chk = self.account("Checking")
        self.sav = self.account("Savings", "Savings")
        self.card = self.account("Card", "Credit card")
        self.brk = self.account("Brokerage", "Brokerage")
        self.eur = self.account("Euro", currency="EUR")
        self.jpy = self.account("Yen", currency="JPY")
        self.ids = dict(CHK=self.chk, SAV=self.sav, CARD=self.card, BRK=self.brk, EUR=self.eur, JPY=self.jpy)
        self.cur = dict(EUR="EUR", JPY="JPY")

    def put(self, which, day, amount, desc, **kw):
        a = self.ids[which]
        put_row(self.db, a, source_of(self.db, a), self.cur.get(which, "USD"), day, amount, desc, **kw)

    def acceptance_months(self):
        for month, income, spend in (("2026-08", "3000", ("1200", "800")), ("2026-09", "2900", ("1500", "700")),
                                     ("2026-10", "3100", ("1000", "800"))):
            self.put("CHK", f"{month}-01", income, "Payroll Fictional Co")
            self.put("CHK", f"{month}-05", "-" + spend[0], "Landlord Rent")
            self.put("CARD", f"{month}-12", "-" + spend[1], "Green Market")
        # Marked own-account transfer (both legs) is excluded without comment.
        self.put("CHK", "2026-08-20", "-500", "Move money", transfer=True)
        self.put("SAV", "2026-08-20", "500", "Move money back", transfer=True)
        # Investment-account activity is not cash flow for this purpose.
        self.put("BRK", "2026-09-02", "-4000", "Buy fictional fund")
        self.put("BRK", "2026-09-03", "4000", "Sell fictional fund")
        # Other currencies never mix in.
        self.put("EUR", "2026-09-04", "-999", "Euro rent")
        cover(self.db, "2026-08-01", "2026-11-29")

    def test_three_full_months_average_with_dated_basis_and_transfers_excluded(self):
        self.acceptance_months()
        s = suggest_monthly(self.db, "Household", "USD", TODAY)
        self.assertEqual((s.income, s.spending), (D("3000.00"), D("2000.00")))
        self.assertEqual(s.basis, "avg of Aug–Oct 2026, 3 full months, USD")
        self.assertEqual([(m, c, sp) for m, c, sp in s.monthly],
                         [("2026-08", D(3000), D(2000)), ("2026-09", D(2900), D(2200)), ("2026-10", D(3100), D(1800))])
        self.assertEqual((s.possible_transfers, s.wide, s.reason), (0, (), ""))
        euro = suggest_monthly(self.db, "Household", "EUR", TODAY)
        self.assertEqual(euro.spending, D("333.00"))  # EUR is its own figure, from EUR rows only
        self.assertEqual(euro.income, D("0.00"))
        self.assertEqual(euro.wide, ("spending",))  # one month holds all of it

    def test_unconfirmed_transfer_pair_is_left_out_and_counted(self):
        self.acceptance_months()
        self.put("CHK", "2026-09-20", "-700", "Transfer to savings")
        self.put("SAV", "2026-09-20", "700", "Transfer from checking")
        s = suggest_monthly(self.db, "Household", "USD", TODAY)
        self.assertEqual((s.income, s.spending, s.possible_transfers), (D("3000.00"), D("2000.00"), 2))  # two legs
        # A lone card payment from checking is real spending until marked; the page says so.
        html = self.get().replace("&amp;", "&")
        self.assertIn("possible internal transfer leg(s) were left out", html)
        self.assertIn("still count as spending unless marked Transfer", html)

    def test_needs_three_full_covered_months_and_says_why(self):
        self.acceptance_months()
        cover(self.db, "2026-08-30", "2026-11-29")  # the 90-day first sync: August has 2 of 31 days
        s = suggest_monthly(self.db, "Household", "USD", TODAY)
        self.assertIsNone(s.income)
        self.assertIn("2 of 3", s.reason)
        html = self.get()
        self.assertIn("No suggestion yet: Needs 3 full months of fetched USD transactions", html)
        self.assertNotIn("Use these as my income and spending", html)
        cover(self.db, "2026-08-01", "2026-09-30")  # September not fully fetched either
        self.assertIn("1 of 3", suggest_monthly(self.db, "Household", "USD", TODAY).reason)
        self.assertIn("No linked cash or credit-card accounts", suggest_monthly(self.db, "Nanami", "GBP", TODAY).reason)

    def test_rounding_half_up_per_currency_and_wide_warning(self):
        for month, amount in (("2026-08", "1000"), ("2026-09", "1001"), ("2026-10", "1001")):
            self.put("JPY", f"{month}-02", "-" + amount, "Yen rent")
        cover(self.db, "2026-08-01", "2026-11-29")
        self.assertEqual(suggest_monthly(self.db, "Household", "JPY", TODAY).spending, D("1001"))  # 1000.67, 0 places
        for month, amount in (("2026-08", "2000.01"), ("2026-09", "2200.00"), ("2026-10", "1800.00")):
            self.put("CHK", f"{month}-02", "-" + amount, "Rent")
        self.put("CHK", "2026-10-03", "9000", "Tax refund")
        cover(self.db, "2026-08-01", "2026-11-29")
        s = suggest_monthly(self.db, "Household", "USD", TODAY)
        self.assertEqual(s.spending, D("2000.00"))  # 2000.0033 -> 2000.00 exactly
        self.assertEqual(s.income, D("3000.00"))
        self.assertEqual(s.wide, ("credits",))
        self.assertIn("one-off amounts", self.get())

    def test_unlinked_cash_account_is_disclosed(self):
        self.acceptance_months()
        self.account("Manual checking", linked=False)
        self.assertEqual(suggest_monthly(self.db, "Household", "USD", TODAY).unlinked, 1)
        self.assertIn("no transaction feed", self.get())

    # ---- page and apply ----
    def get(self, query="", **kw):
        return projection_get(self.path, "/projections" + query, page=page, token="t0ken", people=server.PEOPLE,
                              section_for=server.section_for, places=server.places, today=TODAY,
                              **({"suggestions": True} | kw))[1]

    def post(self, **fields):
        suggestions = fields.pop("suggestions", True)
        data = dict(Assumptions().form_values(), view="Household", currency="USD", token="t0ken", **fields)
        return projection_post(self.path, data, page=page, token="t0ken", people=server.PEOPLE,
                               section_for=server.section_for, places=server.places, today=TODAY,
                               suggestions=suggestions)

    def saved(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT view, currency, assumptions FROM projection_assumptions").fetchall()

    def test_page_shows_basis_and_months_and_saves_nothing(self):
        self.acceptance_months()
        html = self.get()
        for text in ("avg of Aug–Oct 2026, 3 full months, USD", "USD 3,000.00", "USD 2,000.00",
                     "average monthly credits (not verified income)".capitalize(), "Not applied until you choose to",
                     "Use these as my income and spending", "cash and credit-card accounts only"):
            self.assertIn(text, html)
        self.assertEqual(self.saved(), [])  # viewing is read-only
        self.assertNotIn("<script", html)
        self.assertNotIn(" style=", html)

    def test_apply_is_one_post_recomputed_server_side_changing_only_income_and_spending(self):
        self.acceptance_months()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            save_assumptions(db, "Household", "USD", Assumptions(years=7, hysa_apy=D("5"), debt_target=D("10")))
        status, body, location = self.post(apply="suggestion", suggest_income="3000.00", suggest_spending="2000.00")
        self.assertEqual((status, location), (303, "/projections?view=Household&currency=USD"))
        with closing(sqlite3.connect(self.path)) as db:
            saved = load_assumptions(db, "Household", "USD", 2)
        self.assertEqual((saved.income, saved.spending), (D("3000.00"), D("2000.00")))
        self.assertEqual((saved.years, saved.hysa_apy, saved.debt_target), (7, D("5"), D("10")))  # untouched
        self.assertIn("Already saved as your income and spending", self.get())
        # What the user saw must be what is applied; stale or forged numbers are refused.
        before = self.saved()
        self.post(apply="suggestion", suggest_income="1", suggest_spending="1")
        for fields in (dict(suggest_income="3000.00", suggest_spending="1.00"), dict(suggest_income="x"),
                       dict(suggest_income="3000.00")):
            status, body, _ = self.post(apply="suggestion", **fields)
            self.assertEqual(status, 422)
            self.assertIn("No assumptions were saved", body)
        self.assertEqual(self.saved(), before)

    def test_apply_refused_without_enough_months_or_when_suggestions_are_off(self):
        self.acceptance_months()
        self.assertEqual(self.post(apply="suggestion", suggest_income="3000.00", suggest_spending="2000.00",
                                   suggestions=False)[0], 422)
        cover(self.db, "2026-09-16", "2026-11-29")
        status, body, _ = self.post(apply="suggestion", suggest_income="3000.00", suggest_spending="2000.00")
        self.assertEqual(status, 422)
        self.assertIn("no suggestion to apply", body)
        self.assertEqual(self.saved(), [])

    def test_advanced_hides_rates_until_needed_and_opens_on_error_or_non_default(self):
        html = self.get(suggestions=False)
        form_start = html.index('class="edit-form projection-form"')
        advanced = html.index('<details class="advanced">')
        self.assertNotIn(" open>", html[advanced:advanced + 40])
        for name in ("years", "income", "spending"):
            self.assertLess(html.index(f'name="{name}"', form_start), advanced)
        for name in ("hysa_apy", "inflation", "debt_apr", "investment_return", "retirement_target", "mode"):
            self.assertGreater(html.index(f'name="{name}"', form_start), advanced)
        self.assertNotIn("Suggested from your transactions", html)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            save_assumptions(db, "Household", "USD", Assumptions(hysa_apy=D("5")))
        self.assertIn('<details class="advanced" open>', self.get(suggestions=False))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            save_assumptions(db, "Household", "USD", Assumptions(years=3, income=D("100")))  # basic changes only
        self.assertNotIn('<details class="advanced" open>', self.get(suggestions=False))
        status, body, _ = self.post(inflation="101")  # an error inside Advanced must be visible
        self.assertEqual(status, 422)
        self.assertIn('<details class="advanced" open>', body)
        self.assertIn('aria-describedby="error-inflation"', body)

    # ---- hosted ----
    def test_hosted_serves_no_suggestion_and_refuses_apply(self):
        self.acceptance_months()
        auth = Auth(self.path, CONFIG, keys=lambda _: self.fail("no key lookup"), clock=lambda: 1)
        sid, token = "a" * 43, "b" * 43
        with auth.db() as db:
            email = CONFIG.members[0]
            db.execute("INSERT INTO hosted_identities VALUES (?,?)", (email, "10001"))
            db.execute("INSERT INTO hosted_sessions VALUES (?,?,?,?)", (digest(sid), email, token, 2))
        fixture = SimpleNamespace(db=self.path, token=None, auth=auth, server_port=8765, batch=None, logo_dir=None,
                                  notices=[])
        cookie = (("Cookie", f"{CONFIG.name('session')}={sid}.{auth.signature(sid)}"),)
        status, _, body = request(server.Handler, fixture, "/?chart=projection", headers=cookie)   # L29: a tab on Accounts
        self.assertEqual(status, 200)
        self.assertIn('id="projection-assumptions"', body)
        self.assertNotIn("Suggested from your transactions", body)
        self.assertNotIn("USD 3,000.00", body)
        form = dict(Assumptions().form_values(), view="Household", currency="USD", token=token, apply="suggestion",
                    suggest_income="3000.00", suggest_spending="2000.00")
        status, _, body = request(server.Handler, fixture, "/projections", form,
                                  headers=cookie + (("Origin", ORIGIN),))
        self.assertEqual(status, 422)
        self.assertIn("not available", body)
        self.assertEqual(self.saved(), [])
        self.assertEqual(request(server.Handler, fixture, "/transactions", headers=cookie)[0], 404)


if __name__ == "__main__":
    unittest.main()

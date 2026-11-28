"""L23 slice B1: the Projections page shows per-debt schedules from the same engine as the chart (fictional data)."""
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

import server
from account_terms import save_terms
from projections import Assumptions, save_assumptions
from projections_ui import projection_get

TODAY = date(2026, 11, 28)
D = Decimal


def page(content, current=None, heading=""):
    return server.page(content, current=current, heading=heading)


class DebtPage(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp).resolve() / "fictional.sqlite3"
        server.init_db(self.path)
        self.ids = {}

    def account(self, name, kind, side, balance, *, currency="USD", asof="2026-11-20", owner="Joint", excluded=0, **terms):
        with closing(sqlite3.connect(self.path)) as db, db:
            rid = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,excluded)
                VALUES ('Fictional Bank',?,?,?,?,?,?,?,?)""", (name, kind, side, currency, balance, asof, owner, excluded)).lastrowid
            if terms:
                save_terms(db, rid, terms)
        self.ids[name] = rid
        return rid

    def assume(self, currency="USD", **kw):
        base = dict(years=3, hysa_apy=D(0), checking_apy=D(0), investment_return=D(0), retirement_return=D(0),
                    inflation=D(0), debt_apr=D(6), income=D(1000), spending=D(400))
        with closing(sqlite3.connect(self.path)) as db, db:
            save_assumptions(db, "Household", currency, replace(Assumptions(), **(base | kw)))

    def get(self, currency="USD"):
        status, body, _ = projection_get(self.path, f"/projections?view=Household&currency={currency}", page=page,
                                         token="t", people=server.PEOPLE, section_for=server.section_for,
                                         places=server.places, today=TODAY, suggestions=False)
        self.assertEqual(status, 200)
        return body

    def section(self, body):
        return body[body.index('id="debt-heading"'):]

    def test_acceptance_1_payoff_interest_final_payment_and_monthly_cost(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12.000", planned_payment="100.00")
        self.assume()
        s = self.section(self.get())
        self.assertRegex(s, r"Sep 2027 <span class=\"acct-sub\">\(month 11\)</span>")
        self.assertIn('<td class="num">USD 58.98</td>', s)                      # total interest
        self.assertIn("Current monthly interest cost of these debts: <strong>USD 10.00</strong>", s)
        self.assertIn('<td class="num">USD 58.98</td><td class="num">USD 0.00</td>', s)   # final payment = remainder
        self.assertIn("12% APR · fixed", s)
        self.assertIn("(planned)", s)
        self.assertIn("as of <time>2026-11-20</time>", s)

    def test_acceptance_2_promo_shows_expiry_in_the_summary_line_and_the_rate_change_in_month_7(self):
        self.account("Cedar Card", "Credit card", "debt", "1000.00", rate="12", promo_rate="0", promo_end="2027-04",
                     planned_payment="100.00", paid_in_full=0)
        self.assume()
        s = self.section(self.get())
        self.assertIn("0% promo through Apr 2027, then 12%", s)
        self.assertIn('<td class="num">USD 10.30</td>', s)
        self.assertIn("Month 7: rate changes to 12% (promo ended Apr 2027)", s)
        self.assertIn("Sep 2027", s)

    def test_acceptance_3_missing_rate_uses_blended_and_adding_a_rate_removes_the_flag(self):
        self.account("Birch Loan", "Student loan", "debt", "1000.00")
        self.assume(debt_target=D(100))
        body = self.get()
        self.assertIn("Rate missing for 1 debt(s):", body)
        self.assertIn("blended 6% APR", body)
        self.assertNotIn("<th scope=\"col\">Payoff</th>", body)                 # no per-debt table for it
        with closing(sqlite3.connect(self.path)) as db, db:
            save_terms(db, self.ids["Birch Loan"], dict(rate="4.500", min_payment="50.00"))
        body = self.get()
        self.assertNotIn("Rate missing for", body)
        self.assertIn("4.5% APR · fixed", body)

    def test_acceptance_4_payment_below_interest_shows_the_flag_and_no_date(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12", planned_payment="5.00")
        self.assume()
        s = self.section(self.get())
        self.assertIn("payment does not cover interest", s)
        self.assertIn("<strong>No payoff date</strong>", s)
        self.assertIn("interest USD 10.00 a month", s)
        self.assertNotRegex(s, r"\(month \d+\)")

    def test_payment_missing_is_flagged_and_gives_no_date(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12")
        self.assume()
        s = self.section(self.get())
        self.assertIn('<span class="rate-flag">payment missing</span>', s)
        self.assertIn("<strong>No payoff date</strong>", s)

    def test_acceptance_5_card_default_paid_in_full_has_no_payoff_row_and_is_labelled(self):
        self.account("Cedar Card", "Credit card", "debt", "400.00")
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12", planned_payment="100.00")
        self.assume()
        body = self.get()
        self.assertIn("1 card(s) assumed paid in full:", body)
        self.assertIn("balance USD 400.00 held flat, no interest (Cedar Card)", body)
        table = body[body.index('aria-label="Scrollable debt table"'):body.index("Current monthly interest cost")]
        self.assertNotIn("Cedar Card", table)
        self.assertIn("Birch Loan", table)
        self.assertIn("Current monthly interest cost of these debts: <strong>USD 10.00</strong>", body)  # card adds none
        # Turning "paid in full" off with a rate puts the card in the table with interest.
        with closing(sqlite3.connect(self.path)) as db, db:
            save_terms(db, self.ids["Cedar Card"], dict(rate="20", paid_in_full=0, min_payment="40.00"))
        body = self.get()
        self.assertNotIn("card(s) assumed paid in full", body)
        self.assertIn("Cedar Card", body[body.index('aria-label="Scrollable debt table"'):])

    def test_acceptance_12_dated_snapshot_with_a_stale_label(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", asof="2026-09-01", rate="12", planned_payment="100.00")
        self.assume()
        s = self.section(self.get())
        self.assertIn("as of <time>2026-09-01</time> <span class=\"stale-pill\">Stale</span>", s)
        self.assertIn("Balance is stale; the schedule starts from it anyway.", s)
        self.assertIn("nothing here refreshes them", s)

    def test_acceptance_9_currencies_never_mix(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12", planned_payment="100.00")
        self.account("Seoul Loan", "Personal loan", "debt", "1000000", currency="KRW", rate="5", planned_payment="100000")
        self.account("Seoul Savings", "Savings", "asset", "5000000", currency="KRW")
        self.assume("USD")
        self.assume("KRW", income=D(3000000), spending=D(2000000))
        usd, krw = self.get("USD"), self.get("KRW")
        self.assertIn("Birch Loan", usd)
        self.assertNotIn("Seoul Loan", usd)
        self.assertNotIn("KRW", self.section(usd))
        self.assertIn("Seoul Loan", krw)
        self.assertNotIn("Birch Loan", krw)
        self.assertNotRegex(self.section(krw), r"USD ")
        # KRW has no decimals: 1,000,000 * 5% / 12 = 4166.67 -> 4,167 this month, whole won throughout.
        self.assertIn("KRW 4,167", self.section(krw))
        self.assertNotRegex(self.section(krw), r"KRW [\d,]+\.\d")

    def test_excluded_and_other_owner_accounts_follow_the_view_scope(self):
        self.account("Hidden Loan", "Personal loan", "debt", "1000.00", excluded=1, rate="12", planned_payment="100.00")
        self.account("Nanami Loan", "Personal loan", "debt", "1000.00", owner="Nanami", rate="12", planned_payment="100.00")
        self.assume()
        self.assertNotIn("Hidden Loan", self.get())
        with closing(sqlite3.connect(self.path)) as db, db:
            save_assumptions(db, "Young", "USD", Assumptions())
        status, body, _ = projection_get(self.path, "/projections?view=Young&currency=USD", page=page, token="t",
                                         people=server.PEOPLE, section_for=server.section_for, places=server.places,
                                         today=TODAY)
        self.assertNotIn("Nanami Loan", body)

    def test_chart_cards_and_the_debt_table_agree(self):
        """Headline Debt owed at year 1 equals the sum of what the table's schedules leave after month 12."""
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12", planned_payment="100.00")
        self.account("Cedar Loan", "Personal loan", "debt", "600.00", rate="0", planned_payment="25.00")
        self.account("Maple Card", "Credit card", "debt", "200.00")
        self.assume(years=1)
        body = self.get()
        # Birch: paid off in month 11; Cedar: 600 - 12*25 = 300; card float 200 -> 500.00 at year 1.
        self.assertIn("<small>Debt owed</small><strong>Start: USD 1,800.00</strong><div>Year 1: USD 500.00", body)
        self.assertNotIn("Rate missing", body)

    def test_planned_payments_above_net_savings_are_flagged(self):
        self.account("Birch Loan", "Personal loan", "debt", "1000.00", rate="12", planned_payment="900.00")
        self.assume(income=D(1000), spending=D(500))
        self.assertIn("Planned payments (USD 900.00 a month) exceed monthly net savings (USD 500.00).", self.get())


if __name__ == "__main__":
    unittest.main()

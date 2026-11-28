"""L23 slice C-observed: observed payments beside planned ones (transaction-derived, so absent when hosted)."""
from datetime import date
from decimal import Decimal
import re
import unittest

import server
from account_terms import save_terms
from projections_ui import projection_get
from tx_fixture import Fixture, cover, put_row, source_of

TODAY = date(2026, 11, 28)
D = Decimal


def page(content, current=None, heading=""):
    return server.page(content, current=current, heading=heading)


class ObservedPayments(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.chk = self.account("Checking")
        self.loan = self.debt("Student loan", "Student loan", "1000.00", rate="4.500", planned_payment="120.00")

    def debt(self, key, kind, balance, currency="USD", linked=True, **terms):
        rid = self.account(key, kind, currency=currency, linked=linked)
        with self.db:
            self.db.execute("UPDATE accounts SET side='debt', balance=?, asof='2026-11-20' WHERE id=?", (balance, rid))
            save_terms(self.db, rid, terms)
        return rid

    def pay(self, account, day, amount, desc="Loan payment", currency="USD", **kw):
        put_row(self.db, account, source_of(self.db, account), currency, day, amount, desc, **kw)

    def body(self, suggestions=True, currency="USD"):
        return projection_get(self.path, f"/projections?view=Household&currency={currency}", page=page, token="t",
                              people=server.PEOPLE, section_for=server.section_for, places=server.places, today=TODAY,
                              suggestions=suggestions)[1]

    def debt_cell(self, body, name="Student loan"):
        i = body.index(f'<th scope="row">{name}<')
        return body[i:body.index("</tr>", i)]

    def three_months(self, account=None):
        account = account or self.loan
        for month, amount in (("2026-08", "90.00"), ("2026-09", "100.00"), ("2026-10", "110.00")):
            self.pay(account, f"{month}-10", amount, transfer=True)
        cover(self.db, "2026-08-01", "2026-11-29")

    def test_acceptance_6_observed_average_beside_the_planned_payment_which_alone_drives_the_projection(self):
        self.three_months()
        cell = self.debt_cell(self.body())
        self.assertIn("USD 120.00 <span class=\"acct-sub\">(planned)</span>", cell)
        self.assertIn("Observed: USD 100.00 <strong>avg of 3 months</strong>", cell)
        self.assertIn("Aug USD 90.00 · Sep USD 100.00 · Oct USD 110.00", cell)
        self.assertIn("not used in the projection", cell)
        plain = self.debt_cell(self.body(suggestions=False))
        strip = lambda text: re.sub(r'<div class="acct-sub">Observed:.*?</div>', "", text)
        self.assertEqual(strip(cell), plain)            # payoff date and interest are identical with or without it

    def test_suggested_or_unflagged_credits_are_not_counted_but_disclosed(self):
        self.three_months()
        self.pay(self.loan, "2026-10-20", "95.00", "Online payment")                  # not marked, not matched
        cell = self.debt_cell(self.body())
        self.assertIn("avg of 3 months", cell)
        self.assertIn("USD 100.00", cell)
        self.assertIn("1 payment-like credit(s) not confirmed", cell)

    def test_pending_negative_and_other_account_rows_do_not_count(self):
        self.three_months()
        self.pay(self.loan, "2026-09-12", "-50.00", "Reversal", transfer=True)          # wrong direction
        self.pay(self.chk, "2026-09-10", "500.00", "Some deposit", transfer=True)       # another account's feed
        with self.db:
            self.db.execute("""INSERT INTO account_transactions (account_id, tx_id, source, posted_date, pending_date, amount,
                currency, description, payee, merchant, pending, pending_active, is_transfer, transfer_manual)
                VALUES (?,?,?,NULL,'2026-11-26','999.00','USD','Pending payment','','pending payment',1,1,1,1)""",
                            (self.loan, "p1", source_of(self.db, self.loan)))
        cover(self.db, "2026-08-01", "2026-11-29")
        self.assertIn("Observed: USD 100.00 <strong>avg of 3 months</strong>", self.debt_cell(self.body()))

    def test_an_auto_matched_card_payment_counts_as_confirmed(self):
        card = self.debt("Visa card", "Credit card", "500.00", rate="19.990", paid_in_full=0, min_payment="25.00")
        self.pay(self.chk, "2026-10-05", "-75.00", "Credit card payment")
        self.pay(card, "2026-10-06", "75.00", "Payment thank you")
        cover(self.db, "2026-08-01", "2026-11-29")
        cell = self.debt_cell(self.body(), "Visa card")
        self.assertIn("Observed: USD 25.00 <strong>avg of 3 months</strong>", cell)          # (0 + 0 + 75) / 3
        self.assertIn("Aug USD 0.00 · Sep USD 0.00 · Oct USD 75.00", cell)

    def test_only_months_this_accounts_own_feed_covers_are_used(self):
        self.three_months()
        cover(self.db, "2026-09-01", "2026-11-29")                                       # Aug is not covered
        cell = self.debt_cell(self.body())
        self.assertIn("Observed: USD 105.00 <strong>avg of 2 months</strong>", cell)
        self.assertNotIn("Aug", cell.split("Observed")[1])

    def test_none_found_and_no_feed_say_so_plainly(self):
        cover(self.db, "2026-08-01", "2026-11-29")
        self.assertIn("no observed payments found (loan payments are only counted when marked as transfers)",
                      self.debt_cell(self.body()))
        self.debt("Other loan", "Personal loan", "300.00", linked=False, rate="9", planned_payment="30.00")
        self.assertIn("Observed: No transaction feed for this account.", self.debt_cell(self.body(), "Other loan"))

    def test_acceptance_9_currency_never_mixes(self):
        won = self.debt("Seoul loan", "Personal loan", "1000000", currency="KRW", rate="5", planned_payment="100000")
        self.three_months()
        self.pay(won, "2026-09-10", "100000", currency="KRW", transfer=True)
        cover(self.db, "2026-08-01", "2026-11-29")
        usd = self.body()
        self.assertIn("avg of 3 months", self.debt_cell(usd))
        self.assertNotIn("Seoul loan", usd)
        krw = self.body(currency="KRW")
        self.assertIn("Observed: KRW 33,333 <strong>avg of 3 months</strong>", self.debt_cell(krw, "Seoul loan"))
        self.assertNotIn("USD", self.debt_cell(krw, "Seoul loan"))

    def test_acceptance_13_hosted_pages_show_terms_and_schedules_but_no_observed_data(self):
        for month, amount in (("2026-08", "333.30"), ("2026-09", "333.33"), ("2026-10", "333.36")):
            self.pay(self.loan, f"{month}-10", amount, transfer=True)
        cover(self.db, "2026-08-01", "2026-11-29")
        local, hosted = self.body(), self.body(suggestions=False)
        self.assertIn("333.33", local)
        self.assertNotIn("333", hosted)
        self.assertNotIn("observed", hosted.lower())
        self.assertIn("Student loan", hosted)
        self.assertIn("4.5% APR", hosted)
        self.assertIn("(planned)", hosted)


if __name__ == "__main__":
    unittest.main()

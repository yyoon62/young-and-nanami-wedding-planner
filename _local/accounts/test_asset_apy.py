"""L23 slice B2: per-account APY for cash and savings (own rate where set, blended pool otherwise)."""
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

import server
from account_terms import save_terms
from asset_inputs import AssetAcct
from projections import Assumptions, Balances, project, save_assumptions
from projections_ui import projection_get

D = Decimal
TODAY = date(2026, 11, 28)
A = replace(Assumptions(), years=2, hysa_apy=D(0), checking_apy=D(0), investment_return=D(0), retirement_return=D(0),
            inflation=D(0), debt_apr=D(0))


def acct(key, bucket, balance, apy):
    return AssetAcct(key, f"Acct {key}", "Fictional Bank", bucket, D(balance), "2026-11-20", False, None if apy is None else D(apy))


class Model(unittest.TestCase):
    def test_acceptance_8_ten_thousand_at_four_percent_apy_is_10400_after_a_year(self):
        points = project(Balances(hysa=D(10000)), A, None, [acct(1, "hysa", 10000, 4)])
        self.assertLess(abs(points[12].balances.hysa - 10000 * 104 // 100), D("0.01"))     # independent: 10,000 x 1.04
        self.assertLess(abs(points[12].balances.hysa - D(10400)), D("1e-50"))
        self.assertEqual(dict(points[12].own).keys(), {1})
        self.assertEqual(points[0].balances.hysa, D(10000))

    def test_without_apy_the_blended_pool_applies_exactly_as_before(self):
        a = replace(A, hysa_apy=D(4), income=D(100), spending=D(40))
        start = Balances(cash=D(50), hysa=D(10000))
        legacy = project(start, a)
        for assets in ((), [acct(1, "hysa", 10000, None), acct(2, "cash", 50, None)]):
            self.assertEqual([p.balances for p in project(start, a, None, assets)], [p.balances for p in legacy])

    def test_each_account_grows_at_its_own_rate_and_new_savings_earn_the_blended_rate(self):
        a = replace(A, hysa_apy=D(2), income=D(100), spending=D(0), years=1)
        start = Balances(hysa=D(11000))
        points = project(start, a, None, [acct(1, "hysa", 10000, 4), acct(2, "hysa", 1000, 1)])
        own = dict(points[12].own)
        self.assertLess(abs(own[1] - D(10400)), D("1e-50"))
        self.assertLess(abs(own[2] - D(1010)), D("1e-50"))                       # contributions never touch them
        pool = points[12].balances.hysa - own[1] - own[2]
        expected = sum(D(100) * D("1.02") ** (D(11 - m) / 12) for m in range(12))   # month-end deposits, 2% effective
        self.assertLess(abs(pool - expected), D("1e-40"))

    def test_withdrawals_take_the_lowest_apy_first_within_a_bucket_and_cash_before_savings(self):
        a = replace(A, income=D(0), spending=D(100), years=1)
        points = project(Balances(hysa=D(2000)), a, None, [acct(1, "hysa", 1000, 4), acct(2, "hysa", 1000, 1)])
        own = dict(points[1].own)
        self.assertLess(abs(own[1] - D(1000) * D("1.04") ** (D(1) / 12)), D("1e-20"))   # the 4% account is untouched
        self.assertLess(abs(own[2] - (D(1000) * D("1.01") ** (D(1) / 12) - 100)), D("1e-20"))
        mixed = project(Balances(cash=D(30), hysa=D(100)), a, None, [acct(1, "cash", 30, 0), acct(2, "hysa", 100, 0)])
        self.assertEqual((mixed[1].balances.cash, mixed[1].balances.hysa), (D(0), D(30)))     # cash first, then savings
        self.assertEqual(mixed[2].cumulative_gap, D(70))                                         # then unfunded
        self.assertEqual(mixed[2].balances.hysa, D(0))


class Page(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp).resolve() / "f.sqlite3"
        server.init_db(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            for name, kind, balance, terms in (("Maple Savings", "Savings", "10000.00", dict(rate="4.000")),
                                               ("Cedar Savings", "Savings", "2000.00", {}),
                                               ("Oak Checking", "Checking", "500.00", {}),
                                               ("Brokerage", "Brokerage", "900.00", dict(rate="9"))):
                rid = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank',?,?,'asset','USD',?,'2026-11-20','Joint')""", (name, kind, balance)).lastrowid
                if terms:
                    save_terms(db, rid, terms)
            save_assumptions(db, "Household", "USD", replace(A, years=1, hysa_apy=D(3)))

    def test_projections_shows_each_account_rate_flags_unrated_savings_and_ignores_other_asset_types(self):
        body = projection_get(self.path, "/projections", page=lambda c, **k: server.page(c, **k), token="t",
                              people=server.PEOPLE, section_for=server.section_for, places=server.places, today=TODAY)[1]
        s = body[body.index('id="asset-heading"'):]
        s = s[:s.index("</section>")]
        self.assertIn("Maple Savings", s)
        self.assertIn("4% APY", s)
        self.assertIn("USD 10,400.00", s)                     # after 1 year
        self.assertIn("Cedar Savings", s)
        self.assertIn('<span class="rate-flag">rate missing</span>', s)
        self.assertIn("blended 3% assumed", s)
        self.assertIn("Oak Checking", s)
        self.assertNotIn("Brokerage", s)                      # investments keep the blended return assumption
        self.assertIn("pooled", s)


if __name__ == "__main__":
    unittest.main()

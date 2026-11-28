"""L23 slice C (avalanche vs snowball). The reference simulation is integer cents only and shares no code with the app."""
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
from debt_engine import Debt, DebtPlan, compare_orders, run_plan
from projections import Assumptions, save_assumptions
from projections_ui import projection_get

D = Decimal
START = "2026-11"
TODAY = date(2026, 11, 28)


def ref_multi(debts, extra_c, order, cap=480):
    """debts: list of (balance_c, apr_bp, promo_bp|None, promo_months, pay_c). Returns (payoff months, interest cents)."""
    bal = [d[0] for d in debts]
    paid_off, interest_total = [None] * len(debts), [0] * len(debts)
    budget = sum(d[4] for d in debts) + extra_c
    for month in range(1, cap + 1):
        bps = [(d[2] if (d[2] is not None and month <= d[3]) else d[1]) for d in debts]
        due = []
        for i, d in enumerate(debts):
            interest = (2 * bal[i] * bps[i] + 120000) // 240000 if bal[i] > 0 else 0
            interest_total[i] += interest
            due.append(bal[i] + interest)
        spent = 0
        for i, d in enumerate(debts):
            p = min(d[4], due[i])
            due[i] -= p
            spent += p
        if extra_c > 0:
            left = budget - spent
            live = [i for i in range(len(debts)) if due[i] > 0]
            live.sort(key=(lambda i: (due[i], i)) if order == "snowball" else (lambda i: (-bps[i], due[i], i)))
            for i in live:
                q = min(left, due[i])
                due[i] -= q
                left -= q
        bal = due
        for i in range(len(debts)):
            if paid_off[i] is None and bal[i] == 0:
                paid_off[i] = month
        if all(b == 0 for b in bal):
            break
    return paid_off, interest_total


def plan_of(specs):
    debts = []
    for i, (bal, bp, promo, months, pay) in enumerate(specs, 1):
        promo_end = "" if promo is None else f"{2026 + (9 + months) // 12}-{(9 + months) % 12 + 1:02d}"
        debts.append(Debt(i, f"Debt {i}", D(bal) / 100, "2026-11-01", D(bp) / 100,
                          promo_rate=None if promo is None else D(promo) / 100, promo_end=promo_end, payment=D(pay) / 100))
    return DebtPlan(tuple(debts), places=2, start_month=START)


def assumptions(target_c, income=100000, spending=10000):
    return replace(Assumptions(), years=10, hysa_apy=D(0), checking_apy=D(0), investment_return=D(0),
                   retirement_return=D(0), inflation=D(0), income=D(income), spending=D(spending), debt_target=D(target_c) / 100)


# Acceptance 7: a small low-rate debt and a large high-rate debt, 200.00 extra.
FIXED = [(50000, 500, None, 0, 2500), (300000, 2000, None, 0, 10000)]
# Promo: a 0% promo card that jumps to 24% in month 7, versus a steady 12% debt, equal balances.
PROMO = [(150000, 2400, 0, 6, 5000), (150000, 1200, None, 0, 5000)]


# (fixture, order) -> (payoff month per debt, total interest cents per debt); hand-checked for the first snowball debt:
# 500.00 at 5% with 225.00 paid a month: interest 2.08 + 1.15 + 0.22 = 3.45 in 3 months. Note the promo fixture:
# snowball is CHEAPER there (11,993 vs 12,298 cents), so "avalanche is always lowest" would be wrong.
LITERAL = {('fixed', 'avalanche'): ([12, 12], [1861, 30913]), ('fixed', 'snowball'): ([3, 12], [345, 37392]),
           ('promo', 'avalanche'): ([11, 8], [6811, 5487]), ('promo', 'snowball'): ([6, 11], [0, 11993])}


class Reference(unittest.TestCase):
    def check(self, specs, extra_c):
        plan = plan_of(specs)
        target = sum(s[4] for s in specs) + extra_c
        a = assumptions(target)
        for order in ("avalanche", "snowball"):
            run = run_plan(plan, a, order, extra=D(extra_c) / 100, with_pool=False)
            months, interest = ref_multi(specs, extra_c, order)
            self.assertEqual([run.outcomes[d.key].payoff_month for d in plan.debts], months, (order, specs))
            self.assertEqual([int(run.outcomes[d.key].total_interest * 100) for d in plan.debts], interest, (order, specs))
        return plan, a

    def test_fixed_rate_fixture_matches_the_integer_reference_in_both_orders(self):
        self.check(FIXED, 20000)

    def test_promo_expiry_fixture_matches_the_integer_reference_in_both_orders(self):
        self.check(PROMO, 20000)

    def test_literal_results_for_the_fixtures(self):
        """Frozen outcomes (payoff months per debt, total interest cents) of the integer reference."""
        got = {(k, order): ref_multi(spec, 20000, order) for k, spec in (("fixed", FIXED), ("promo", PROMO))
               for order in ("avalanche", "snowball")}
        self.assertEqual(got, LITERAL)

    def test_assorted_cases_match_the_reference(self):
        for specs, extra in (([(100000, 1500, None, 0, 3000), (20000, 2999, None, 0, 1000), (80000, 800, 0, 4, 2500)], 5000),
                             ([(10000, 1200, None, 0, 1000), (10000, 1200, None, 0, 1000)], 1000),
                             ([(250000, 650, None, 0, 4000), (90000, 650, None, 0, 2000), (1234, 100, None, 0, 100)], 777)):
            self.check(specs, extra)




class Comparing(unittest.TestCase):
    def test_equal_everything_is_no_difference_and_no_winner(self):
        plan = plan_of([(10000, 1200, None, 0, 1000), (10000, 1200, None, 0, 1000)])
        c = compare_orders(plan, assumptions(2000 + 1000))
        self.assertTrue(c.comparable)
        self.assertEqual((c.winner, c.interest_difference), ("", D(0)))
        self.assertEqual(c.avalanche.last_payoff_month, c.snowball.last_payoff_month)

    def test_withheld_reasons(self):
        a = assumptions(40000)
        one = plan_of([(50000, 500, None, 0, 2500)])
        self.assertIn("two or more", compare_orders(one, a).reason)
        pooled = replace(plan_of(FIXED), pool=D("100.00"), pool_names=("Mystery loan",))
        self.assertIn("1 debt(s) have no rate", compare_orders(pooled, a).reason)
        self.assertIn("No extra payment", compare_orders(plan_of(FIXED), assumptions(12500)).reason)
        self.assertIn("No extra payment", compare_orders(plan_of(FIXED), assumptions(40000, income=400, spending=300)).reason)

    def test_winner_is_whatever_the_engine_and_reference_compute_never_assumed(self):
        for specs in (FIXED, PROMO):
            plan = plan_of(specs)
            c = compare_orders(plan, assumptions(sum(s[4] for s in specs) + 20000))
            ava = sum(ref_multi(specs, 20000, "avalanche")[1])
            snow = sum(ref_multi(specs, 20000, "snowball")[1])
            self.assertEqual(int(c.avalanche.total_interest * 100), ava)
            self.assertEqual(int(c.snowball.total_interest * 100), snow)
            self.assertEqual(c.winner, "avalanche" if snow > ava else "snowball" if ava > snow else "")
            self.assertEqual(int(c.interest_difference * 100), abs(snow - ava))
            self.assertLess(c.avalanche.total_interest, c.planned_only.total_interest)   # extra money always helps
        promo = compare_orders(plan_of(PROMO), assumptions(sum(s[4] for s in PROMO) + 20000))
        self.assertEqual((promo.winner, promo.interest_difference), ("snowball", D("3.05")))   # avalanche is not always cheaper
        fixed = compare_orders(plan_of(FIXED), assumptions(sum(s[4] for s in FIXED) + 20000))
        self.assertEqual((fixed.winner, fixed.interest_difference), ("avalanche", D("49.63")))


class Page(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp).resolve() / "f.sqlite3"
        server.init_db(self.path)

    def seed(self, specs, target, **kw):
        with closing(sqlite3.connect(self.path)) as db, db:
            for i, (bal, bp, promo, months, pay) in enumerate(specs, 1):
                rid = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank',?,'Personal loan','debt','USD',?,'2026-11-20','Joint')""",
                                 (f"Loan {i}", format(D(bal) / 100, "f"))).lastrowid
                terms = dict(rate=format(D(bp) / 100, "f"), planned_payment=format(D(pay) / 100, "f"))
                if promo is not None:
                    terms |= dict(promo_rate=format(D(promo) / 100, "f"), promo_end=f"{2026 + (9 + months) // 12}-{(9 + months) % 12 + 1:02d}")
                save_terms(db, rid, terms | kw)
            save_assumptions(db, "Household", "USD", assumptions(target))

    def body(self):
        return projection_get(self.path, "/projections", page=lambda c, **k: server.page(c, **k), token="t",
                              people=server.PEOPLE, section_for=server.section_for, places=server.places, today=TODAY)[1]

    def test_both_orders_dates_interest_and_a_labelled_difference(self):
        self.seed(FIXED, 32500)
        body = self.body()
        s = body[body.index("Avalanche vs snowball"):]
        for text in ("<th scope=\"row\">Avalanche</th>", "<th scope=\"row\">Snowball</th>", "Planned payments only (no extra)",
                     "Extra payment USD 200.00 a month", "Neither order is assumed to win"):
            self.assertIn(text, s)
        ava, snow = (sum(ref_multi(FIXED, 20000, o)[1]) for o in ("avalanche", "snowball"))
        if snow != ava:
            lower, diff = ("avalanche", snow - ava) if snow > ava else ("snowball", ava - snow)
            self.assertIn(f"Lower total interest: {lower}, by USD {diff // 100:,}.{diff % 100:02d}", s)
        self.assertIn(f"USD {ava // 100:,}.{ava % 100:02d}", s)

    def test_tie_makes_no_winner_claim_and_unrated_debts_withhold_the_comparison(self):
        self.seed([(10000, 1200, None, 0, 1000), (10000, 1200, None, 0, 1000)], 3000)
        body = self.body()
        self.assertIn("No difference:", body)
        self.assertNotIn("Lower total interest", body)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                VALUES ('Fictional Bank','Mystery','Personal loan','debt','USD','100.00','2026-11-20','Joint')""")
        body = self.body()
        self.assertIn("Withheld: 1 debt(s) have no rate", body)
        self.assertNotIn("No difference:", body)


if __name__ == "__main__":
    unittest.main()

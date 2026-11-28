"""L23 slice B1: per-debt amortization. The reference below is INDEPENDENT of the app: integer cents only, no
Decimal, no app imports for the arithmetic, written as a different loop, plus literal rows worked out by hand."""
from dataclasses import replace
from decimal import Decimal, localcontext
import unittest

from debt_engine import Book, Debt, DebtPlan, run_plan, schedule
from projections import Assumptions, Balances, project

D = Decimal
START = "2026-11"          # schedule month 1 = November 2026


def reference(balance_c, apr_bp, pay_c, promo_bp=None, promo_months=0, cap=480, minor=2):
    """Integer minor units. apr_bp = APR in basis points (12% -> 1200). Interest = round-half-up(bal*bp/120000)."""
    rows, month, bal = [], 0, balance_c
    scale = 10 ** (2 - minor)
    while bal > 0 and month < cap:
        month += 1
        bp = promo_bp if (promo_bp is not None and month <= promo_months) else apr_bp
        num, den = bal * bp, 120000
        interest = (2 * num + den) // (2 * den)
        if scale != 1:                                  # zero-decimal currencies round to whole units
            unit = 10 ** (2 - minor) if minor < 2 else 1
            interest = ((2 * (bal * bp) + 120000 * unit) // (2 * 120000 * unit)) * unit
        due = bal + interest
        paid = min(pay_c, due)
        bal = due - paid
        rows.append((month, interest, paid, bal))
    return rows


def cents(value):
    return int((value * 100).to_integral_value())


class Literal(unittest.TestCase):
    """Rows worked out by hand (and by the integer reference), then frozen here as literals (cents)."""
    PLAIN = [(1, 1000, 10000, 91000), (2, 910, 10000, 81910), (3, 819, 10000, 72729), (4, 727, 10000, 63456),
             (5, 635, 10000, 54091), (6, 541, 10000, 44632), (7, 446, 10000, 35078), (8, 351, 10000, 25429),
             (9, 254, 10000, 15683), (10, 157, 10000, 5840), (11, 58, 5898, 0)]
    PROMO = [(1, 0, 10000, 90000), (2, 0, 10000, 80000), (3, 0, 10000, 70000), (4, 0, 10000, 60000),
             (5, 0, 10000, 50000), (6, 0, 10000, 40000), (7, 400, 10000, 30400), (8, 304, 10000, 20704),
             (9, 207, 10000, 10911), (10, 109, 10000, 1020), (11, 10, 1030, 0)]

    def rows(self, schedule_):
        return [(r.month, cents(r.interest), cents(r.payment), cents(r.balance)) for r in schedule_.rows]

    def test_acceptance_1_amortization_month_11_interest_58_98_final_payment_is_the_remainder(self):
        s = schedule(Debt(1, "Loan", D("1000.00"), "2026-11-01", D(12), payment=D("100.00")), START, 2)
        self.assertEqual(self.rows(s), self.PLAIN)
        self.assertEqual((s.status, s.payoff_month, s.total_interest), ("paid_off", 11, D("58.98")))
        self.assertEqual(s.rows[-1].payment, D("58.98"))               # remainder, not 100.00
        self.assertEqual(self.PLAIN, reference(100000, 1200, 10000))     # the literal agrees with the reference

    def test_acceptance_2_promo_through_month_6_then_12_percent(self):
        # promo_end 2027-04 is the LAST promo month: Nov, Dec, Jan, Feb, Mar, Apr = months 1..6.
        debt = Debt(1, "Card", D("1000.00"), "2026-11-01", D(12), promo_rate=D(0), promo_end="2027-04", payment=D("100.00"))
        s = schedule(debt, START, 2)
        self.assertEqual(self.rows(s), self.PROMO)
        self.assertEqual((s.status, s.payoff_month, s.total_interest), ("paid_off", 11, D("10.30")))
        self.assertEqual([r.rate for r in s.rows][5:7], [D(0), D(12)])
        self.assertEqual(s.promo_ended_month, 7)                         # the rate change is visible in month 7
        self.assertEqual(self.PROMO, reference(100000, 1200, 10000, 0, 6))

    def test_acceptance_4_payment_below_interest_has_no_payoff_date(self):
        s = schedule(Debt(1, "Loan", D("1000.00"), "2026-11-01", D(12), payment=D("5.00")), START, 2)
        self.assertEqual((s.status, s.payoff_month, s.total_interest, s.stalled_from), ("does_not_cover", None, None, 1))
        self.assertEqual(s.first_interest, D("10.00"))                   # the monthly interest cost
        self.assertTrue(all(r.balance > D("1000.00") for r in s.rows))

    def test_payment_equal_to_interest_never_reduces_the_balance(self):
        s = schedule(Debt(1, "Loan", D("1000.00"), "2026-11-01", D(12), payment=D("10.00")), START, 2)
        self.assertEqual((s.status, s.payoff_month), ("does_not_cover", None))

    def test_a_promo_can_cover_the_payment_until_the_regular_rate_does_not(self):
        debt = Debt(1, "Card", D("1000.00"), "2026-11-01", D(12), promo_rate=D(0), promo_end="2027-04", payment=D("5.00"))
        s = schedule(debt, START, 2)
        self.assertEqual((s.status, s.stalled_from), ("does_not_cover", 7))   # month 7: 12% interest 9.70 > 5.00

    def test_missing_payment_and_cap_and_ended_promo(self):
        s = schedule(Debt(1, "Loan", D("1000.00"), "2026-11-01", D(12), payment=D(0)), START, 2)
        self.assertEqual((s.status, s.payoff_month, s.first_interest), ("no_payment", None, D("10.00")))
        slow = schedule(Debt(1, "Mortgage", D("1000000.00"), "2026-11-01", D(1), payment=D("834.00")), START, 2)
        self.assertEqual((slow.status, slow.payoff_month, len(slow.rows)), ("beyond_cap", None, 480))
        ended = Debt(1, "Card", D("1000.00"), "2026-11-01", D(12), promo_rate=D(0), promo_end="2026-06", payment=D("100.00"))
        old = schedule(ended, START, 2)
        self.assertEqual((old.rows[0].rate, old.total_interest, old.promo_ended_month), (D(12), D("58.98"), None))


class Reference(unittest.TestCase):
    CASES = [(100000, 1200, 10000, None, 0), (123456, 599, 5000, None, 0), (25000, 2499, 2000, 0, 12),
             (999999, 450, 20000, None, 0), (50050, 799, 1234, 199, 3), (1, 1200, 100, None, 0), (333333, 0, 7777, None, 0),
             (10000, 1000, 837, None, 0), (987654, 6530, 15000, 0, 24), (75, 2999, 25, None, 0)]

    def test_engine_matches_the_integer_reference_row_by_row(self):
        for balance, bp, pay, promo, months in self.CASES:
            promo_end = "" if promo is None else f"{int(START[:4]) + (9 + months) // 12}-{(9 + months) % 12 + 1:02d}"
            debt = Debt(1, "x", D(balance) / 100, "2026-11-01", D(bp) / 100, promo_rate=None if promo is None else D(promo) / 100,
                        promo_end=promo_end, payment=D(pay) / 100)
            got = schedule(debt, START, 2)
            expected = reference(balance, bp, pay, promo, months)
            if got.status == "paid_off":
                self.assertEqual([(r.month, cents(r.interest), cents(r.payment), cents(r.balance)) for r in got.rows],
                                 expected, (balance, bp, pay, promo))
                self.assertEqual(cents(got.total_interest), sum(row[1] for row in expected))
            else:   # the reference has no stall rule: it must simply never reach zero within the rows shown
                self.assertNotEqual(expected[len(got.rows) - 1][3], 0, (balance, bp, pay))

    def test_zero_decimal_currency_rounds_half_up_to_whole_units(self):
        one = lambda bal: schedule(Debt(1, "w", D(bal), "2026-11-01", D(6), payment=D(10000)), START, 0).rows[0].interest
        self.assertEqual((one(1005), one(1010), one(1100), one(1090)), (D(5), D(5), D(6), D(5)))   # 5.025, 5.05, 5.5, 5.45
        # 100000 KRW at 6% = 500 exactly; 1.1% hand value below
        self.assertEqual(one(100000), D(500))


class BookAndProjection(unittest.TestCase):
    A = replace(Assumptions(), years=2, hysa_apy=D(0), checking_apy=D(0), investment_return=D(0),
                retirement_return=D(0), inflation=D(0), debt_apr=D(6))

    def debts(self):
        return (Debt(1, "Loan A", D("1000.00"), "2026-11-01", D(12), payment=D("100.00")),
                Debt(2, "Card B", D("500.00"), "2026-11-01", D(0), promo_rate=D(0), promo_end="2027-02", payment=D("50.00")))

    def test_without_extra_the_book_is_exactly_each_standalone_schedule(self):
        plan = DebtPlan(self.debts(), places=2, start_month=START)
        a = replace(self.A, income=D(1000), spending=D(400), debt_target=D(150))      # target == planned: no extra
        run = run_plan(plan, a)
        for d in plan.debts:
            s = schedule(d, START, 2)
            self.assertEqual(run.outcomes[d.key].payoff_month, s.payoff_month)
            self.assertEqual(run.outcomes[d.key].total_interest, s.total_interest)
            self.assertEqual(run.outcomes[d.key].balances[1:len(s.rows) + 1], tuple(r.balance for r in s.rows))

    def test_chart_debt_equals_pool_plus_each_schedule_plus_float_every_month(self):
        plan = DebtPlan(self.debts(), pool=D("300.00"), float_debt=D("200.00"), places=2, start_month=START)
        a = replace(self.A, income=D(1000), spending=D(400), debt_target=D(150))
        start = Balances(hysa=D(100), debt=D("2000.00"))
        points = project(start, a, plan)
        run = run_plan(plan, a)
        self.assertEqual(points[0].balances.debt, D("2000.00"))
        for month in range(1, 25):
            with localcontext() as ctx:
                ctx.prec = 64                                  # the model's own working precision
                parts = sum((run.outcomes[d.key].balances[month] for d in plan.debts), D(0))
                expected = run.pool_balances[month] + parts + D("200.00")
            self.assertEqual(points[month].balances.debt, expected, month)
            self.assertEqual(dict(points[month].modeled), {d.key: run.outcomes[d.key].balances[month] for d in plan.debts})
        self.assertEqual(points[11].balances.debt - points[11].pool, D("200.00") + sum(
            (v for _, v in points[11].modeled), D(0)))
        self.assertEqual(dict(points[11].modeled)[1], D("0.00"))               # Loan A paid off in month 11

    def test_no_rate_plan_is_bit_identical_to_the_blended_path_and_float_only_adds_a_constant(self):
        a = replace(self.A, income=D(500), spending=D(300), debt_target=D(120), retirement_target=D(10))
        start = Balances(cash=D(10), hysa=D(20), investments=D(30), retirement=D(40), debt=D("700.00"), unclassified=D(5))
        legacy = project(start, a)
        pooled = project(start, a, DebtPlan(pool=D("700.00"), places=2, start_month=START))
        self.assertEqual([p.balances for p in pooled], [p.balances for p in legacy])
        flat = project(replace(start, debt=D("900.00")), a, DebtPlan(pool=D("700.00"), float_debt=D("200.00"),
                                                                       places=2, start_month=START))
        self.assertEqual([p.balances.debt for p in flat], [p.balances.debt + D(200) for p in legacy])
        self.assertEqual([p.balances.net_worth for p in flat], [p.balances.net_worth - D(200) for p in legacy])

    def test_planned_payments_are_obligations_a_shortfall_draws_cash_then_savings_and_is_never_skipped(self):
        plan = DebtPlan((Debt(1, "Loan", D("1000.00"), "2026-11-01", D(0), payment=D("100.00")),), places=2, start_month=START)
        a = replace(self.A, income=D(300), spending=D(300), debt_target=D(500), years=1)   # surplus 0, planned 100
        points = project(Balances(cash=D(30), hysa=D(150), debt=D("1000.00")), a, plan)
        self.assertEqual(points[1].balances.debt, D("900.00"))
        self.assertEqual((points[1].balances.cash, points[1].balances.hysa), (D(0), D(80)))   # cash first, then savings
        self.assertEqual(points[1].cumulative_gap, 0)
        self.assertEqual((points[2].balances.hysa, points[2].cumulative_gap), (D(0), D(20)))   # 80 left, 100 due: 20 unfunded
        self.assertEqual(points[3].cumulative_gap, D(120))         # the payment is still made; the shortfall is flagged
        self.assertEqual(points[10].balances.debt, D(0))

    def test_extra_is_target_minus_planned_limited_by_positive_net_savings(self):
        from debt_engine import extra_payment
        plan = DebtPlan(self.debts(), places=2, start_month=START)
        for target, income, expected in ((150, 1000, 0), (400, 1000, 250), (400, 560, 10), (400, 540, 0), (0, 1000, 0)):
            a = replace(self.A, income=D(income), spending=D(400), debt_target=D(target))
            self.assertEqual(extra_payment(plan, a), D(expected), (target, income))


if __name__ == "__main__":
    unittest.main()

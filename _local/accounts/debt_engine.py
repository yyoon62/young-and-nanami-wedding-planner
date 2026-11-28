"""Per-debt amortization for Projections (L23). Pure functions, exact Decimal, no I/O.

Conventions (all documented on the Projections page):
* Monthly rate = APR / 1200. Interest = opening balance x rate, rounded half-up to the currency's decimal
  places each month, then the payment is applied at month-end (the same order as `projections.project`).
* The final payment is the remainder (balance + that month's interest) when smaller than the planned payment.
* Schedule month 1 is the current calendar month; a promo "YYYY-MM" is the LAST month at the promo rate.
* A payment that cannot cover the month's interest once no further rate change is ahead is reported as
  "payment does not cover interest": no payoff date is ever shown for it.
* Horizon cap: 480 months (40 years, the longest horizon the page allows).
Debts without a rate stay in one legacy aggregate (`pool`, unrounded, blended APR) so that path is unchanged.
Paid-in-full cards are a flat `float_debt`: no interest, no payoff, no payment.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext

ZERO = Decimal(0)
CAP = 480
PRECISION = 64


def month_add(month, delta):
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def round_half_up(value, places):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Debt:
    key: int
    name: str
    balance: Decimal
    asof: str
    rate: Decimal                      # APR, percentage points
    rate_kind: str = "fixed"
    promo_rate: Decimal | None = None
    promo_end: str = ""                # last calendar month (YYYY-MM) at promo_rate
    payment: Decimal = ZERO            # planned payment, else minimum, else 0 (payment missing)
    stale: bool = False


def rate_for(debt, calendar_month):
    if debt.promo_rate is not None and debt.promo_end and calendar_month <= debt.promo_end:
        return debt.promo_rate
    return debt.rate


def monthly_interest(balance, rate, places):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return round_half_up(balance * rate / 1200, places)


@dataclass(frozen=True)
class Row:
    month: int
    calendar: str
    rate: Decimal
    interest: Decimal
    payment: Decimal
    balance: Decimal


@dataclass(frozen=True)
class Schedule:
    rows: tuple
    status: str                        # paid_off | does_not_cover | no_payment | beyond_cap
    payoff_month: int | None = None
    total_interest: Decimal | None = None
    stalled_from: int | None = None
    first_interest: Decimal = ZERO     # interest cost this month at the current rate
    promo_ended_month: int | None = None   # first schedule month at the regular rate after a promo


def schedule(debt, start_month, places, cap=CAP):
    """One debt paid only by its own planned payment. Standalone and independent of cash flow."""
    balance, rows, total = debt.balance, [], ZERO
    first = monthly_interest(balance, rate_for(debt, start_month), places)
    promo_ended = None
    if debt.promo_rate is not None and debt.promo_end and debt.promo_end >= start_month:
        promo_ended = _month_number(start_month, debt.promo_end) + 1
    if balance <= 0:
        return Schedule((), "paid_off", 0, ZERO, None, ZERO, promo_ended)
    if debt.payment <= 0:
        return Schedule((), "no_payment", None, None, None, first, promo_ended)
    for month in range(1, cap + 1):
        calendar = month_add(start_month, month - 1)
        rate = rate_for(debt, calendar)
        interest = monthly_interest(balance, rate, places)
        later_change = (debt.promo_rate is not None and debt.promo_end and calendar < debt.promo_end)
        if interest > 0 and debt.payment <= interest and not later_change:
            rows.append(Row(month, calendar, rate, interest, debt.payment, balance + interest - debt.payment))
            return Schedule(tuple(rows), "does_not_cover", None, None, month, first, promo_ended)
        due = balance + interest
        paid = min(debt.payment, due)
        balance = due - paid
        total += interest
        rows.append(Row(month, calendar, rate, interest, paid, balance))
        if balance == 0:
            return Schedule(tuple(rows), "paid_off", month, total, None, first, promo_ended)
    return Schedule(tuple(rows), "beyond_cap", None, None, None, first, promo_ended)


def _month_number(start_month, calendar):
    """1-based schedule month of a calendar month."""
    return (int(calendar[:4]) - int(start_month[:4])) * 12 + int(calendar[5:7]) - int(start_month[5:7]) + 1


@dataclass(frozen=True)
class DebtPlan:
    """Everything the model needs about debts in ONE currency, besides the assumptions."""
    debts: tuple = ()                  # rate-known, not paid in full
    pool: Decimal = ZERO               # debts without a rate (legacy blended aggregate)
    float_debt: Decimal = ZERO         # paid-in-full cards, held flat
    float_names: tuple = ()
    start_month: str = "1970-01"
    places: int = 2
    pool_names: tuple = ()

    @property
    def planned_total(self):
        return sum((d.payment for d in self.debts), ZERO)


def extra_payment(plan, assumptions):
    """Monthly extra beyond planned payments: the debt target limited by positive monthly net savings."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return max(ZERO, min(assumptions.debt_target, assumptions.surplus) - plan.planned_total)


def _order(debts, due, calendar, order):
    live = [d for d in debts if due[d.key] > 0]
    if order == "snowball":
        return sorted(live, key=lambda d: (due[d.key], d.key))
    return sorted(live, key=lambda d: (-rate_for(d, calendar), due[d.key], d.key))


class Book:
    """Month-by-month state of the pool and the rate-known debts. Shared by the chart and the tables."""

    def __init__(self, plan, assumptions, order="avalanche", extra=None):
        self.plan, self.order = plan, order
        self.extra = extra_payment(plan, assumptions) if extra is None else extra
        with localcontext() as ctx:
            ctx.prec = PRECISION
            self.debt_factor = 1 + assumptions.debt_apr / 1200
        self.pool = plan.pool
        self.balance = {d.key: d.balance for d in plan.debts}
        self.month = 0

    @property
    def modeled_total(self):
        return sum(self.balance.values(), ZERO)

    def step(self):
        """Accrue, then pay at month-end. Returns the total paid this month (cash that left the household)."""
        plan = self.plan
        self.month += 1
        calendar = month_add(plan.start_month, self.month - 1)
        with localcontext() as ctx:
            ctx.prec = PRECISION             # the same precision `project` uses, wherever the Book runs
            self.pool *= self.debt_factor
        due = {}
        for d in plan.debts:
            bal = self.balance[d.key]
            due[d.key] = bal + monthly_interest(bal, rate_for(d, calendar), plan.places) if bal > 0 else ZERO
        paid_planned = ZERO
        for d in plan.debts:
            pay = min(d.payment, due[d.key])
            due[d.key] -= pay
            paid_planned += pay
        paid_extra = ZERO
        if self.extra > 0:
            # Constant budget: a paid-off debt's planned payment rolls over to the next target.
            leftover = plan.planned_total + self.extra - paid_planned
            x = min(self.pool, leftover)           # debts with no rate first, exactly as before L23
            self.pool -= x
            paid_extra += x
            leftover -= x
            for d in _order(plan.debts, due, calendar, self.order):
                q = min(leftover, due[d.key])
                due[d.key] -= q
                leftover -= q
                paid_extra += q
        self.balance = dict(due)
        return paid_planned + paid_extra

    def debt_total(self):
        return self.pool + self.modeled_total + self.plan.float_debt


@dataclass(frozen=True)
class Outcome:
    payoff_month: int | None
    total_interest: Decimal
    balances: tuple                    # month 0..n


@dataclass
class PlanRun:
    order: str
    extra: Decimal
    outcomes: dict = field(default_factory=dict)     # key -> Outcome
    pool_balances: tuple = ()
    months: int = 0
    last_payoff_month: int | None = None
    total_interest: Decimal = ZERO


def run_plan(plan, assumptions, order="avalanche", cap=CAP, extra=None, with_pool=True):
    """Run the plan to the cap (or until nothing is owed). Same `Book.step` the projection uses."""
    book = Book(plan, assumptions, order, extra)
    balances = {d.key: [d.balance] for d in plan.debts}
    interest = {d.key: ZERO for d in plan.debts}
    payoff = {d.key: (0 if d.balance <= 0 else None) for d in plan.debts}
    pool = [book.pool]
    for month in range(1, cap + 1):
        calendar = month_add(plan.start_month, month - 1)
        for d in plan.debts:                       # interest charged this month, for the totals
            bal = book.balance[d.key]
            if bal > 0:
                interest[d.key] += monthly_interest(bal, rate_for(d, calendar), plan.places)
        book.step()
        for d in plan.debts:
            balances[d.key].append(book.balance[d.key])
            if payoff[d.key] is None and book.balance[d.key] == 0:
                payoff[d.key] = month
        pool.append(book.pool)
        if book.modeled_total == 0 and (book.pool == 0 or not with_pool):
            break
    run = PlanRun(order, book.extra, pool_balances=tuple(pool), months=book.month)
    for d in plan.debts:
        run.outcomes[d.key] = Outcome(payoff[d.key], interest[d.key] if payoff[d.key] is not None else interest[d.key],
                                      tuple(balances[d.key]))
    done = [o.payoff_month for o in run.outcomes.values()]
    run.last_payoff_month = max(done) if done and None not in done else None
    run.total_interest = sum((o.total_interest for o in run.outcomes.values()), ZERO)
    return run

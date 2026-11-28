"""One-currency scenarios. SQLite stores base-10 text, never binary floats."""
from collections import Counter
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, localcontext
import json
import re

from debt_engine import Book, DebtPlan


ZERO = Decimal(0)
PRECISION = 64
STALE_DAYS = 30  # Match the Accounts dashboard.
BUCKETS = {
    "Checking": "cash", "Other": "cash", "Savings": "hysa",
    "Investments": "investments", "Retirement": "retirement",
}


@dataclass(frozen=True)
class Balances:
    cash: Decimal = ZERO
    hysa: Decimal = ZERO
    investments: Decimal = ZERO
    retirement: Decimal = ZERO
    debt: Decimal = ZERO
    unclassified: Decimal = ZERO

    @property
    def net_worth(self):
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self.liquid + self.investments + self.retirement + self.unclassified - self.debt

    @property
    def liquid(self):
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self.cash + self.hysa


@dataclass(frozen=True)
class Seed:
    balances: Balances
    count: int
    oldest: date | None
    newest: date | None
    stale_count: int
    unclassified_count: int


def eligible_accounts(db, view):
    """Same owner scope as Accounts; a Joint row is selected just once."""
    return db.execute("""SELECT kind, side, currency, balance, asof FROM accounts
        WHERE excluded = 0 AND (? = 'Household' OR owner IN (?, 'Joint'))""", (view, view)).fetchall()


def available_currencies(rows):
    """Default first: most accounts, then USD, then alphabetical. No FX."""
    counts = Counter(row[2] for row in rows)
    return sorted(counts, key=lambda code: (-counts[code], code != "USD", code)) or ["USD"]


def starting_balances(rows, currency, section_for, today):
    totals = {field.name: ZERO for field in fields(Balances)}
    dates, unclassified = [], 0
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for kind, side, code, amount, asof in rows:
            if code != currency:
                continue
            bucket = "debt" if side == "debt" else BUCKETS.get(section_for(kind), "unclassified")
            totals[bucket] += Decimal(amount)
            unclassified += bucket == "unclassified"
            dates.append(date.fromisoformat(asof))
    return Seed(Balances(**totals), len(dates), min(dates, default=None), max(dates, default=None),
                sum((today - day).days > STALE_DAYS for day in dates), unclassified)


@dataclass(frozen=True)
class Assumptions:
    years: int = 10
    income: Decimal = ZERO
    spending: Decimal = ZERO
    debt_target: Decimal = ZERO
    retirement_target: Decimal = ZERO
    investment_target: Decimal = ZERO
    checking_apy: Decimal = ZERO
    hysa_apy: Decimal = Decimal(4)
    investment_return: Decimal = Decimal(7)
    retirement_return: Decimal = Decimal(7)
    debt_apr: Decimal = Decimal(6)
    inflation: Decimal = Decimal("2.5")
    mode: str = "nominal"

    @property
    def surplus(self):
        return self.income - self.spending

    def form_values(self):
        return {field.name: (format(value, "f") if isinstance(value, Decimal) else str(value))
                for field in fields(self) for value in (getattr(self, field.name),)}


AMOUNTS = {
    "income": "Monthly take-home income",
    "spending": "Monthly spending",
    "debt_target": "Monthly debt payment target",
    "retirement_target": "Monthly retirement contribution target",
    "investment_target": "Monthly investment contribution target",
}
RATES = {
    "checking_apy": "Checking / cash APY",
    "hysa_apy": "Savings / HYSA APY",
    "investment_return": "Investment return (annual effective)",
    "retirement_return": "Retirement return (annual effective)",
    "debt_apr": "Blended debt APR, for debts without a rate (nominal)",
    "inflation": "Inflation (annual effective)",
}
OPTIONAL = frozenset(("debt_target", "retirement_target", "investment_target"))
MODES = {"nominal": "Nominal dollars", "real": "Today’s dollars (inflation adjusted)"}


def validate_assumptions(raw, places):
    """Return (Assumptions or None, field errors). Rates are percentage points."""
    values, errors = {}, {}
    years = raw.get("years", "").strip()
    if re.fullmatch(r"[0-9]{1,2}", years) and 1 <= int(years) <= 40:
        values["years"] = int(years)
    else:
        errors["years"] = "Horizon: enter a whole number from 1 to 40."
    amount_pattern = r"[0-9]{1,15}" + (rf"(?:\.[0-9]{{1,{places}}})?" if places else "")
    for name, label in AMOUNTS.items():
        value = raw.get(name, "").strip()
        if not value and name in OPTIONAL:
            values[name] = ZERO
        elif re.fullmatch(amount_pattern, value):
            values[name] = Decimal(value)
        else:
            errors[name] = (f"{label}: enter a nonnegative amount (up to 15 whole digits "
                            f"and {places} decimal places, no commas).")
    for name, label in RATES.items():
        value = raw.get(name, "").strip()
        low = 0 if name in ("debt_apr", "inflation") else -99
        if re.fullmatch(r"-?[0-9]{1,3}(?:\.[0-9]{1,4})?", value) and low <= Decimal(value) <= 100:
            values[name] = Decimal(value)
        else:
            errors[name] = f"{label}: enter a percentage from {low} to 100 (up to 4 decimal places)."
    if raw.get("mode") in MODES:
        values["mode"] = raw["mode"]
    else:
        errors["mode"] = "Display units: choose nominal or today’s dollars."
    return (None, errors) if errors else (Assumptions(**values), {})


def install_projections(db):
    """Run inside the accounts schema migration; do not create scenario rows."""
    db.execute("""CREATE TABLE IF NOT EXISTS projection_assumptions (
        view TEXT NOT NULL, currency TEXT NOT NULL, assumptions TEXT NOT NULL,
        PRIMARY KEY(view, currency))""")


def load_assumptions(db, view, currency, places):
    row = db.execute("SELECT assumptions FROM projection_assumptions WHERE view=? AND currency=?",
                     (view, currency)).fetchone()
    if row is None:
        return Assumptions()
    result, errors = validate_assumptions(json.loads(row[0]), places)
    if errors:
        raise ValueError("Invalid saved projection assumptions.")
    return result


def save_assumptions(db, view, currency, assumptions):
    """Write a validated scenario in the caller's transaction."""
    db.execute("""INSERT INTO projection_assumptions VALUES (?, ?, ?)
        ON CONFLICT(view, currency) DO UPDATE SET assumptions=excluded.assumptions""",
               (view, currency, json.dumps(assumptions.form_values())))


@dataclass(frozen=True)
class Point:
    month: int
    balances: Balances                 # Nominal account balances.
    display: Balances                  # Deflated if requested; no impact on the simulation.
    cumulative_gap: Decimal            # Nominal unfunded spending, NOT new debt.
    pool: Decimal | None = None        # Debts without a rate (blended aggregate); only when a debt plan is used.
    modeled: tuple = ()                # ((account id, balance), ...) of rate-known debts; only with a debt plan.


def project(start, assumptions, plan=None):
    """Accrue first, then apply constant nominal cash flow at each month-end.

    Asset growth and inflation use geometric effective rates. Debt uses
    APR / 12. Keep Decimal precision through all months, with rounding only
    at display. Unclassified assets are held flat and are not available for
    spending. No future borrowing, forced sales, taxes, or automatic catch-up
    on unmet spending is modeled.

    With a `plan` (L23) rate-known debts follow their own schedules through `debt_engine.Book`:
    planned payments are obligations paid every month (a shortfall draws cash, then savings),
    extra payments come only from positive net savings, and paid-in-full cards are held flat.
    Debts without a plan entry keep the blended path above exactly.
    """
    a = assumptions
    with localcontext() as ctx:
        ctx.prec = PRECISION

        def effective_factor(rate):
            return (1 + rate / 100) ** (Decimal(1) / 12)

        factors = tuple(effective_factor(rate) for rate in
                        (a.checking_apy, a.hysa_apy, a.investment_return, a.retirement_return))
        debt_factor = 1 + a.debt_apr / 1200
        inflation_factor = effective_factor(a.inflation) if a.mode == "real" else Decimal(1)
        cash, hysa, inv, ret, debt, other = (getattr(start, field.name) for field in fields(Balances))
        gap = ZERO
        book = Book(plan, a) if plan is not None else None
        points = [Point(0, start, start, gap)]
        for month in range(1, a.years * 12 + 1):
            cash, hysa, inv, ret = (amount * factor for amount, factor in zip((cash, hysa, inv, ret), factors))
            if book is None:
                debt *= debt_factor
                remaining = a.surplus
            else:
                remaining = a.surplus - book.step()
                debt = book.debt_total()
            if remaining >= 0:
                if book is None:
                    paid = min(debt, a.debt_target, remaining)
                    debt -= paid
                    remaining -= paid
                contribution = min(a.retirement_target, remaining)
                ret += contribution
                remaining -= contribution
                contribution = min(a.investment_target, remaining)
                inv += contribution
                hysa += remaining - contribution
            else:
                deficit = -remaining
                from_cash = min(cash, deficit)
                cash -= from_cash
                deficit -= from_cash
                from_savings = min(hysa, deficit)
                hysa -= from_savings
                gap += deficit - from_savings
            amounts = (cash, hysa, inv, ret, debt, other)
            balances = Balances(*amounts)
            deflator = inflation_factor ** month
            display = balances if deflator == 1 else Balances(*(value / deflator for value in amounts))
            points.append(Point(month, balances, display, gap) if book is None else
                          Point(month, balances, display, gap, book.pool, tuple(book.balance.items())))
    return points

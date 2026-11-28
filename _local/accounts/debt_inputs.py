"""Turn saved accounts + terms into a `DebtPlan` for one view and currency (L23)."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from account_terms import is_paid_in_full
from debt_engine import ZERO, Debt, DebtPlan
from projections import STALE_DAYS


@dataclass(frozen=True)
class Entry:
    kind: str                  # "modeled" (rate known) | "float" (paid-in-full card) | "pool" (rate missing)
    key: int
    name: str
    institution: str
    balance: Decimal
    asof: str
    stale: bool
    terms: object
    debt: Debt | None = None


def debt_rows(db, view, currency):
    return db.execute("""SELECT id, institution, nickname, kind, balance, asof FROM accounts
        WHERE excluded = 0 AND side = 'debt' AND currency = ? AND (? = 'Household' OR owner IN (?, 'Joint'))
        ORDER BY institution COLLATE NOCASE, nickname COLLATE NOCASE, id""", (currency, view, view)).fetchall()


def make_plan(rows, terms_by_id, section_for, places, today):
    """-> (DebtPlan, [Entry]). Zero-balance accounts are left out."""
    entries, debts, pool, flat = [], [], ZERO, ZERO
    flat_names, pool_names = [], []
    for key, institution, nickname, kind, balance, asof in rows:
        amount = Decimal(balance)
        if amount <= 0:
            continue
        terms = terms_by_id.get(key)
        stale = (today - date.fromisoformat(asof)).days > STALE_DAYS
        name = f"{nickname}"
        if is_paid_in_full(terms, kind, "debt", section_for):
            flat += amount
            flat_names.append(name)
            entries.append(Entry("float", key, name, institution, amount, asof, stale, terms))
        elif terms is not None and terms.rate is not None:
            debt = Debt(key, name, amount, asof, terms.rate, terms.rate_kind, terms.promo_rate, terms.promo_end,
                        terms.payment or ZERO, stale)
            debts.append(debt)
            entries.append(Entry("modeled", key, name, institution, amount, asof, stale, terms, debt))
        else:
            pool += amount
            pool_names.append(name)
            entries.append(Entry("pool", key, name, institution, amount, asof, stale, terms))
    plan = DebtPlan(tuple(debts), pool, flat, tuple(flat_names), today.isoformat()[:7], places, tuple(pool_names))
    return plan, entries

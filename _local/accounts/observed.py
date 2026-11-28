"""Observed monthly payments into a debt account (L23). Derived from transactions, so never used when hosted.

Counted: posted rows in the debt account's own feed, amount > 0 (reduces the debt), same currency, whose flow is an
effective transfer (auto-matched by L18, or marked by you as a transfer / merchant rule). Not counted: pending rows,
`suggested_transfer` hints, card credits still under review, anything negative. The 3 most recent full months for
which THIS account's feed is fully fetched are used; a covered month with no payment counts as 0. It is shown
beside the planned payment and never feeds the projection.
"""
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
import re

from transactions import monthly_snapshot
from tx_periods import SUGGEST_MONTHS, month_add, month_covered, quantize

PAYMENT_LIKE = re.compile(r"\b(?:payments?|autopay|auto pay|pymt|pmt)\b")


@dataclass
class Observed:
    months: tuple = ()                       # covered months used, oldest first
    values: dict = field(default_factory=dict)   # month -> Decimal total
    average: Decimal | None = None
    unconfirmed: int = 0                     # payment-like positive credits that are not confirmed transfers
    reason: str = ""                         # why there is no average


def observed_payments(db, view, currency, today, account_ids, *, context=None):
    """{account id: Observed} for the given debt accounts of one currency."""
    ids = set(account_ids)
    if not ids:
        return {}
    window = tuple(month_add(today.isoformat()[:7], -n) for n in range(SUGGEST_MONTHS, 0, -1))
    feeds = {}
    for account, start, end in db.execute(f"""SELECT a.id, c.start_at, c.end_at FROM accounts a
        JOIN source_links l ON l.local=a.id
        LEFT JOIN transaction_coverage c ON c.source=l.source AND c.account_id=a.id AND c.currency=a.currency
        WHERE a.id IN ({",".join("?" * len(ids))})""", tuple(ids)):
        feeds[account] = (account, "debt", start, end)
    result = {i: Observed() for i in ids}
    snapshots = {}
    for i in ids:
        if i not in feeds:
            result[i].reason = "No transaction feed for this account."
            continue
        covered = tuple(m for m in window if month_covered([feeds[i]], m))
        result[i].months = covered
        if not covered:
            result[i].reason = (f"Needs a fully fetched month of transactions for this account "
                                f"(0 of {SUGGEST_MONTHS} recent months are complete).")
    for month in window:
        if not any(month in result[i].months for i in ids if i in feeds):
            continue
        snapshots[month] = monthly_snapshot(db, month, view, household_matching=True, context=context)
    with localcontext() as ctx:
        ctx.prec = 150
        for i in ids:
            if i not in feeds:
                continue
            totals = {m: Decimal(0) for m in result[i].months}
            for month, snap in snapshots.items():
                for r in snap.rows:
                    if r["account_id"] != i or r["currency"] != currency or r["pending"]:
                        continue
                    amount = Decimal(r["amount"])
                    if amount <= 0:
                        continue
                    if r["effective_transfer"]:
                        if month in totals:
                            totals[month] += amount
                    elif month in totals and (r["flow"] == "Review card credit" or r["suggested_transfer"]
                                              or PAYMENT_LIKE.search(" ".join((r["description"], r["payee"])).casefold())):
                        result[i].unconfirmed += 1
            result[i].values = totals
            if totals:
                result[i].average = quantize(sum(totals.values(), Decimal(0)) / len(totals), currency)
    return result

"""Month coverage, spending-by-category comparison and (slice C) projection suggestions.

Everything here reuses `monthly_snapshot`, so category bars and suggestions agree with the
page totals and its transfer handling. Currencies are never mixed.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from transactions import _midnight, month_dates, monthly_snapshot, utc_date

ZERO_PLACES = set("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split())
THREE_PLACES = set("BHD IQD JOD KWD LYD OMR TND".split())


def places(currency):
    return (0 if currency in ZERO_PLACES else 3 if currency in THREE_PLACES
            else 4 if currency in {"CLF", "UYW"} else 2)


def quantize(value, currency):
    """Half-up to the currency's decimal places; exact Decimal arithmetic."""
    with localcontext() as ctx:
        ctx.prec = 150
        return value.quantize(Decimal(1).scaleb(-places(currency)), rounding=ROUND_HALF_UP)


def money(value, currency):
    return f"{quantize(value, currency):,.{places(currency)}f}"


def month_add(month, delta):
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + mon - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(month):
    return date.fromisoformat(month + "-01").strftime("%b %Y")


def scope_accounts(db, view, currency, kinds=None):
    """Included linked accounts for a view/currency with their transaction coverage."""
    query = """SELECT a.id, a.kind, c.start_at, c.end_at FROM accounts a
        JOIN source_links l ON l.local=a.id
        LEFT JOIN transaction_coverage c ON c.source=l.source AND c.account_id=a.id AND c.currency=a.currency
        WHERE a.excluded=0 AND a.currency=? AND (?='Household' OR a.owner IN (?,'Joint'))"""
    rows = db.execute(query, (currency, view, view)).fetchall()
    return [r for r in rows if kinds is None or str(r[1]).lower() in kinds]


def unlinked_count(db, view, currency, kinds=None):
    rows = db.execute("""SELECT a.kind FROM accounts a WHERE a.excluded=0 AND a.currency=?
        AND (?='Household' OR a.owner IN (?,'Joint'))
        AND NOT EXISTS (SELECT 1 FROM source_links l WHERE l.local=a.id)""", (currency, view, view)).fetchall()
    return sum(1 for (kind,) in rows if kinds is None or str(kind).lower() in kinds)


def month_covered(accounts, month):
    """Every linked account in scope has fetched the whole calendar month (and at least one is linked)."""
    start, end = month_dates(month)
    lo, hi = _midnight(start), _midnight(end)
    return bool(accounts) and all(a[2] is not None and a[2] <= lo and a[3] >= hi for a in accounts)


@dataclass
class Comparison:
    currency: str
    month: str
    current: dict = field(default_factory=dict)
    prev_month: str = ""
    prev: dict | None = None
    avg: dict | None = None
    avg_months: tuple = ()
    partial: str = ""     # "" when the viewed month is complete and covered
    unlinked: int = 0


def _partial_label(accounts, month, today):
    start, end = month_dates(month)
    if str(start) <= str(today) < str(end):
        ends = [a[3] for a in accounts if a[3] is not None]
        through = min(today, utc_date(min(ends) - 1)) if ends and len(ends) == len(accounts) else today
        return f"so far, through {through.day} {through.strftime('%b')}"
    return "" if month_covered(accounts, month) else "partial coverage"


def spending_comparison(db, month, view, *, household_matching, today, context=None, snapshot=None):
    """{currency: Comparison} for the viewed month: previous covered month and 3-month mean.

    The mean covers the three full months BEFORE the viewed month, counting a covered month
    with no spend as zero and skipping months that are not fully covered; fewer than two
    covered months gives no average. Nothing is extrapolated for an in-progress month.
    """
    snapshot = snapshot or monthly_snapshot(db, month, view, household_matching=household_matching,
                                            context=context)
    baseline = [month_add(month, -n) for n in (1, 2, 3)]
    older = {m: monthly_snapshot(db, m, view, household_matching=household_matching, context=context)
             for m in baseline}
    result = {}
    for currency, totals in snapshot.totals.items():
        accounts = scope_accounts(db, view, currency)
        cmp = Comparison(currency, month, dict(totals.category_spending), prev_month=baseline[0],
                         partial=_partial_label(accounts, month, today),
                         unlinked=unlinked_count(db, view, currency))
        covered = [m for m in baseline if month_covered(accounts, m)]
        spend = {m: (older[m].totals[currency].category_spending if currency in older[m].totals else {})
                 for m in covered}
        if baseline[0] in spend:
            cmp.prev = dict(spend[baseline[0]])
        if len(covered) >= 2:
            names = {c for m in covered for c in spend[m]}
            cmp.avg = {c: quantize(sum((spend[m].get(c, Decimal(0)) for m in covered), Decimal(0))
                                   / len(covered), currency) for c in names}
            cmp.avg_months = tuple(covered)
        result[currency] = cmp
    return result


CASHLIKE = frozenset(("checking", "savings", "saving", "cash", "credit card", "credit cards"))
SUGGEST_MONTHS = 3
WIDE_RATIO = Decimal("1.5")


@dataclass
class Suggestion:
    currency: str
    months: tuple = ()
    monthly: tuple = ()          # ((month, credits, spending), ...)
    income: Decimal | None = None
    spending: Decimal | None = None
    reason: str = ""             # why there is no offer
    possible_transfers: int = 0  # suggested-but-unconfirmed transfer legs left out
    card_reviews: int = 0
    unlinked: int = 0
    wide: tuple = ()             # names of figures whose largest month exceeds 1.5x the smallest

    @property
    def basis(self):
        first, last = month_label(self.months[0]), month_label(self.months[-1])
        span = f"{first[:3]}–{last}" if first[4:] == last[4:] else f"{first}–{last}"
        return f"avg of {span}, {len(self.months)} full months, {self.currency}"


def suggest_monthly(db, view, currency, today, *, context=None):
    """Average monthly credits and spending over the 3 latest full, fully fetched months.

    Cash and credit-card accounts only. Left out: effective transfers, unconfirmed transfer
    suggestions (counted and disclosed), card credits under review and pending rows. Nothing
    is offered unless every one of the three months is covered for every linked cash/card
    account in the currency.
    """
    current = today.isoformat()[:7]
    window = tuple(month_add(current, -n) for n in range(SUGGEST_MONTHS, 0, -1))
    accounts = scope_accounts(db, view, currency, CASHLIKE)
    result = Suggestion(currency, unlinked=unlinked_count(db, view, currency, CASHLIKE))
    if not accounts:
        result.reason = f"No linked cash or credit-card accounts with transactions in {currency}."
        return result
    covered = [m for m in window if month_covered(accounts, m)]
    if len(covered) < SUGGEST_MONTHS:
        result.reason = (f"Needs {SUGGEST_MONTHS} full months of fetched {currency} transactions; "
                         f"{len(covered)} of {SUGGEST_MONTHS} recent months are complete so far.")
        return result
    monthly = []
    with localcontext() as ctx:
        ctx.prec = 150
        for month in window:
            snap = monthly_snapshot(db, month, view, household_matching=True, context=context)
            credits = spending = Decimal(0)
            for r in snap.rows:
                if r["currency"] != currency or r["pending"] or str(r["kind"]).lower() not in CASHLIKE:
                    continue
                if r["flow"] == "Review card credit":
                    result.card_reviews += 1
                    continue
                if r["effective_transfer"]:
                    continue
                if r["suggested_transfer"]:
                    result.possible_transfers += 1
                    continue
                amount = Decimal(r["amount"])
                credits += amount if amount > 0 else 0
                spending += -amount if amount < 0 else 0
            monthly.append((month, credits, spending))
        for name, index in (("credits", 1), ("spending", 2)):
            values = [row[index] for row in monthly]
            if max(values) and (min(values) == 0 or max(values) > WIDE_RATIO * min(values)):
                result.wide += (name,)
        result.income = quantize(sum((row[1] for row in monthly), Decimal(0)) / SUGGEST_MONTHS, currency)
        result.spending = quantize(sum((row[2] for row in monthly), Decimal(0)) / SUGGEST_MONTHS, currency)
    result.months, result.monthly = window, tuple(monthly)
    return result

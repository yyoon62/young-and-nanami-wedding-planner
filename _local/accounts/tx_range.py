"""Periods for /transactions (L26 part 14): 30d / 90d / 6m / YTD / 1y / All, or one calendar month (old `month=`).

A period is an inclusive run of UTC days ending today, held as [start, end) dates. The comparison is with the previous
period of the same length (a calendar month compares with the previous calendar month; All has none). Nothing is
extrapolated: where a linked account's fetched data starts after the period starts, the page says so in plain words
("📅 Data from 30 Aug only") and the comparison is dropped rather than invented.

Pure helpers plus one snapshot-based comparison; the UI module renders the results.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from transactions import _midnight, month_dates, period_snapshot, utc_date
from tx_periods import month_label, scope_accounts

PERIODS = ("30d", "90d", "6m", "ytd", "1y", "all")
TILES = {"30d": "30d", "90d": "90d", "6m": "6m", "ytd": "YTD", "1y": "1y", "all": "All"}
DEFAULT_PERIOD = "30d"
NAMES = {"30d": "the last 30 days", "90d": "the last 90 days", "6m": "the last 6 months", "ytd": "this year so far",
         "1y": "the last year", "all": "all synced data"}
PREVIOUS = {"30d": "the previous 30 days", "90d": "the previous 90 days", "6m": "the previous 6 months",
            "ytd": "the same number of days before January 1", "1y": "the previous year"}


@dataclass(frozen=True)
class Span:
    start: date
    end: date            # exclusive
    key: str             # one of PERIODS, or "month"
    label: str
    month: str = ""      # "YYYY-MM" for a calendar month

    @property
    def days(self):
        return (self.end - self.start).days


def add_months(day, n):
    """Calendar-month arithmetic with the day clipped to the target month's length."""
    index = day.year * 12 + day.month - 1 + n
    year, month = divmod(index, 12)
    month += 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def resolve_span(f, today, earliest=None):
    """The date range a page's filters ask for. `month=` (an old link) wins over `period=`."""
    if f.get("month"):
        start, end = month_dates(f["month"])
        return Span(start, end, "month", month_label(f["month"]), f["month"])
    key = f.get("period") or DEFAULT_PERIOD
    end = today + timedelta(days=1)
    if key == "30d":
        start = today - timedelta(days=29)
    elif key == "90d":
        start = today - timedelta(days=89)
    elif key == "6m":
        start = add_months(end, -6)
    elif key == "1y":
        start = add_months(end, -12)
    elif key == "ytd":
        start = date(today.year, 1, 1)
    else:
        start = min(earliest or today, today)
    return Span(start, end, key, NAMES[key])


def previous_span(span):
    """The period of the same length right before this one (None for All)."""
    if span.key == "all":
        return None
    if span.key == "month":
        before = month_dates(span.month)[0] - timedelta(days=1)
        month = before.isoformat()[:7]
        start, end = month_dates(month)
        return Span(start, end, "month", month_label(month), month)
    return Span(span.start - timedelta(days=span.days), span.start, span.key, PREVIOUS[span.key])


def earliest_date(db):
    row = db.execute("""SELECT min(coalesce(t.posted_date, t.pending_date)) FROM account_transactions t
                        JOIN accounts a ON a.id=t.account_id WHERE a.excluded=0""").fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def covered(accounts, span):
    """Every linked account in scope has fetched the whole of [span.start, span.end)."""
    lo, hi = _midnight(span.start), _midnight(span.end)
    return bool(accounts) and all(a[2] is not None and a[2] <= lo and a[3] >= hi for a in accounts)


def notes(accounts, span, today):
    """Plain-language reasons the numbers may not be the whole period; [] when nothing applies."""
    out = []
    if span.key == "month" and span.start <= today < span.end:
        out.append("📅 Month in progress")
    if not accounts:
        return out
    if any(a[2] is None for a in accounts):
        out.append("📅 Some accounts have no synced data yet")
        return out
    first = max(a[2] for a in accounts)
    if first > _midnight(span.start):
        out.append(f"📅 Data from {utc_date(first).day} {utc_date(first).strftime('%b')} only")
    last = utc_date(min(a[3] for a in accounts) - 1)
    if last < min(span.end - timedelta(days=1), today) - timedelta(days=1):
        out.append(f"📅 Data through {last.day} {last.strftime('%b')}")
    return out


@dataclass
class PeriodComparison:
    currency: str
    span: Span
    current: dict = field(default_factory=dict)
    prev: dict | None = None
    prev_span: Span | None = None
    notes: list = field(default_factory=list)
    prev_total: dict | None = None      # {"credits", "spending"} of the previous period when it is fully covered


def comparisons(db, span, view, *, household_matching, today, context=None, snapshot=None, show_pending=False):
    """{currency: PeriodComparison} for the period: current category spending, and the previous period of the
    same length when every linked account in the currency fetched all of it."""
    snap = snapshot or period_snapshot(db, span.start, span.end, view, household_matching=household_matching,
                                       context=context)
    before = previous_span(span)
    older = (period_snapshot(db, before.start, before.end, view, household_matching=household_matching,
                             context=context) if before else None)
    result = {}
    for currency, totals in snap.totals.items():
        accounts = scope_accounts(db, view, currency)
        cmp = PeriodComparison(currency, span, dict(totals.category_spending), notes=notes(accounts, span, today))
        if before and covered(accounts, before) and currency in older.totals:
            cmp.prev, cmp.prev_span = dict(older.totals[currency].category_spending), before
            cmp.prev_total = dict(credits=older.totals[currency].credits, spending=older.totals[currency].spending)
        result[currency] = cmp
    return result

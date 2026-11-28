"""The one collapsed "How these numbers work" box (L26 part 4).

Every explanatory sentence that used to sit above the list lives here (nothing dropped), grouped as short
bullets. The page shows at most two short notes plus this summary line.
"""
from html import escape

NOTES = ("💡 Income includes refunds. Net = income minus spending, not measured savings.",
         "🔁 Pending items, transfers and card payments are left out of the totals.")

_GROUPS = (
    ("Totals", (
        "💰 Income is money in, including refunds; it is not verified income.",
        "🟢 Net saved / 🔴 Net overspent is income minus spending for the period, not measured savings.",
        "Pending items, transfers between your own accounts and unmatched card payments are left out of the "
        "totals; a bank payment that is not matched or marked as a transfer stays in spending.",
        "⚠️ A card payment to confirm is a payment that arrived on a card but has no matching payment from your "
        "bank yet. Marking it Transfer clears it from the list, but the matching bank payment still counts as "
        "spending until it is matched or marked Transfer too.",
        "Automatic card matching requires corroboration on both available legs. Checking and savings matches are "
        "suggestions only.",
        "A manual transfer choice overrides a merchant rule, which overrides automatic classification.")),
    ("Coverage", (
        "📅 Data from 30 Aug only = a linked account has no data before that date, so the earlier days are not counted "
        "and no comparison is shown. 📅 Month in progress = the month is not over; nothing is extrapolated. "
        "📅 Data through D Mon = an account has not synced since then.",
        "Requested windows do not prove institution completeness or retention. Current-month figures are provisional. "
        "Excluded accounts do not contribute.")),
    ("Periods", (
        "30d, 90d, 6m, YTD and 1y are the days up to today (UTC); All starts at the first synced day. ▲▼ in a chip's "
        "tooltip compares with the previous period of the same length, only where every linked account has that whole "
        "period. An old month link shows that calendar month (✕ removes it). A long list shows the newest "
        "500 rows; totals use every row.",)),
    ("Owners", (
        "👥 Individual views include joint accounts and any transaction you assigned to that person with the "
        "Young / Nanami pills (↺ resets it). An override survives a sync; on a pending row it is lost if the bank "
        "changes the transaction id.",)),
    ("Lists and rules", (
        "List filters do not change totals.",
        "Merchant rules match the cleaned merchant name (dates, store and reference numbers removed) on the same "
        "account and currency; a category you set on one row by hand always wins over a rule.")),
    ("Bars and dates", (
        "📊 The bar splits this month's spending by category; transfers, pending and unmatched card payments are "
        "excluded. Hover a chip for the comparison with last month (where fully fetched) and the average of up to 3 "
        "full months before it (needs at least two fully fetched months). Click a chip to filter the list.",
        "📅 Dates are the bank's posted date (UTC): an evening transaction in a western timezone, or a "
        "local-midnight stamp east of UTC, can show a day off, and period and month filters use the same date.")),
)
SYNC_NOTE = "🔄 Sync refreshes all linked accounts and shares the balance sync quota."


def notes_html():
    return '<ul class="tx-notes">' + "".join(f"<li>{escape(n)}</li>" for n in NOTES) + "</ul>"


def info_box(coverage_items, *, can_sync=False, categories_html=""):
    """coverage_items: already-escaped <li> strings for the per-account coverage list."""
    body = "".join(f"<h4>{escape(title)}</h4><ul>" + "".join(f"<li>{escape(t)}</li>" for t in items) + "</ul>"
                   for title, items in _GROUPS)
    if can_sync:
        body += f"<h4>Sync</h4><ul><li>{escape(SYNC_NOTE)}</li></ul>"
    body += categories_html
    body += ("<h4>Accounts and coverage</h4><ul>" + "".join(coverage_items) + "</ul>" if coverage_items
             else "<p>No included accounts in this view.</p>")
    return f'<details class="tx-info"><summary>ℹ️ How these numbers work</summary><div class="tx-info-body">{body}</div></details>'

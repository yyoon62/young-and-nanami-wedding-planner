"""The one collapsed "How these numbers work" box (L26 part 4).

Every explanatory sentence that used to sit above the list lives here (nothing dropped), grouped as short
bullets. The page shows at most two short notes plus this summary line.
"""
from html import escape

NOTES = ("💡 Income includes refunds; Saved / Overspent is cash flow, not savings.",
         "🔁 Pending, transfers and unmatched card payments are left out of the totals.")

_GROUPS = (
    ("Totals", (
        "💰 Income is credits including refunds; it is not verified income.",
        "🟢 Saved / 🔴 Overspent is signed cash flow (income minus spending), not measured savings.",
        "Pending, transfers and explicit unmatched card-payment credits are outside totals; unmatched bank "
        "debits remain spending.",
        "⚠️ A card payment to confirm is a card-account credit that looks like a payment but has no matching bank "
        "debit yet. Marking it Transfer clears it from the list, but the matching bank debit still counts as "
        "spending until it is matched or marked Transfer too.",
        "Automatic card matching requires corroboration on both available legs. Checking and savings matches are "
        "suggestions only.",
        "A manual transfer choice overrides a merchant rule, which overrides automatic classification.")),
    ("Coverage", (
        "partial = an account has no or late data for this month. “so far, through D Mon” = the month is still in "
        "progress; nothing is extrapolated.",
        "Requested windows do not prove institution completeness or retention. Current-month figures are "
        "provisional. Excluded accounts do not contribute.")),
    ("Owners", (
        "👥 Individual views include joint accounts and any transaction you assigned to that person with the "
        "Young / Nanami pills (↺ resets it). An override survives a sync; on a pending row it is lost if the bank "
        "changes the transaction id.",)),
    ("Lists and rules", (
        "List filters do not change totals.",
        "Merchant rules match the cleaned merchant name (dates, store and reference numbers removed) on the same "
        "account and currency; a category you set on one row by hand always wins over a rule.")),
    ("Bars and dates", (
        "📊 Bars: the bar is this month; ticks are last month and the average of up to 3 full months before it. "
        "Transfers, pending and card credits under review are excluded. Click a category to filter the list.",
        "📅 Dates are the bank's posted date (UTC): an evening transaction in a western timezone, or a "
        "local-midnight stamp east of UTC, can show a day off, and month filters use the same date.")),
)
SYNC_NOTE = "🔄 Sync refreshes all linked accounts and shares the balance sync quota."


def notes_html():
    return '<ul class="tx-notes">' + "".join(f"<li>{escape(n)}</li>" for n in NOTES) + "</ul>"


def info_box(coverage_items, *, can_sync=False):
    """coverage_items: already-escaped <li> strings for the per-account coverage list."""
    body = "".join(f"<h4>{escape(title)}</h4><ul>" + "".join(f"<li>{escape(t)}</li>" for t in items) + "</ul>"
                   for title, items in _GROUPS)
    if can_sync:
        body += f"<h4>Sync</h4><ul><li>{escape(SYNC_NOTE)}</li></ul>"
    body += ("<h4>Accounts and coverage</h4><ul>" + "".join(coverage_items) + "</ul>" if coverage_items
             else "<p>No included accounts in this view.</p>")
    return f'<details class="tx-info"><summary>ℹ️ How these numbers work</summary><div class="tx-info-body">{body}</div></details>'

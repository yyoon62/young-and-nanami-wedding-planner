"""Click targets for the /transactions filters (L26 part 11): plain links in the Accounts `.owner-tabs` look, no
dropdowns, every state in the query string. Pure functions: `loc(filters)` builds a URL, `hidden(filters)` the hidden
inputs of the one small search form."""
from html import escape

from transactions import VIEWS
from tx_periods import month_label
from tx_range import NAMES, PERIODS, TILES

PEOPLE = ("Young", "Nanami")


def _tab(label, href, active, title=""):
    """An Accounts-style tab: `.owner-tabs a`, the active one marked aria-current."""
    return (f'<a href="{escape(href)}"{" aria-current=" + chr(34) + "page" + chr(34) if active else ""}'
            f'{" title=" + chr(34) + escape(title) + chr(34) if title else ""}>{label}</a>')


def view_after_click(view, person):
    """The Accounts rule for the person pills: a click toggles that person; both or neither = Household."""
    chosen = {view} & set(PEOPLE) ^ {person}
    return next(iter(chosen)) if len(chosen) == 1 else "Household"


def view_tabs(f, allowed_views, loc):
    """Young / Nanami. Household is neither pill active (or both toggled on). A pill the viewer may not open is
    shown but inert."""
    tabs = []
    for person in PEOPLE:
        target = view_after_click(f["view"], person)
        title = (f"Show only {person}'s transactions (joint accounts included)" if f["view"] != person
                 else "Back to the whole household")
        if target in allowed_views:
            tabs.append(_tab(person, loc(dict(f, view=target)), f["view"] == person, title))
        elif f["view"] == person:        # the viewer may not leave this view: show it as selected, not clickable
            tabs.append(f'<span class="cur" aria-current="page">{person}</span>')
        else:
            tabs.append(f'<span class="off" aria-disabled="true">{person}</span>')
    return f'<nav class="owner-tabs tx-tabs tx-views" aria-label="Whose transactions">{"".join(tabs)}</nav>'


def currency_tabs(f, currencies, loc):
    """Only when the data has more than one currency; each tab narrows the list (and its card) to that currency."""
    if len(currencies) < 2:
        return ""
    tabs = [_tab("All", loc(dict(f, currency="")), not f["currency"], "Every currency")]
    tabs += [_tab(escape(c), loc(dict(f, currency=c)), f["currency"] == c, f"Only {c} transactions") for c in currencies]
    return f'<nav class="owner-tabs tx-tabs tx-cur" aria-label="Currency">{"".join(tabs)}</nav>'


def pending_toggle(f, loc):
    on = f["pending"] == "1"
    return (f'<nav class="owner-tabs tx-tabs" aria-label="Pending">'
            + _tab("⏳ Pending", loc(dict(f, pending="0" if on else "1")), on,
                   "Showing pending transactions, if fetched. Click to hide" if on
                   else "Show pending transactions, if fetched") + '</nav>')


def find_form(f, action, hidden):
    keep = {k: v for k, v in f.items() if k != "q"}
    return (f'<form method="get" action="{action}" class="tx-find" data-autofilter role="search">{hidden(keep)}'
            f'<input type="search" name="q" value="{escape(f["q"])}" maxlength="100" placeholder="🔎 Find" '
            'aria-label="Find a merchant, account or category" autocomplete="off"></form>')


def period_tabs(f, loc):
    """30d · 90d · 6m · YTD · 1y · All as links. With an old `month=` link no tile is lit and a removable chip names
    the month (the stepper above it walks months)."""
    tabs = "".join(_tab(TILES[key], loc(dict(f, period=key, month="")), not f["month"] and f["period"] == key,
                        NAMES[key].capitalize()) for key in PERIODS)
    chip = ""
    if f["month"]:
        chip = (f'<a class="filter-chip tx-monthchip" href="{escape(loc(dict(f, month="")))}" '
                f'title="Back to the rolling periods">📅 {escape(month_label(f["month"]))} ✕</a>')
    return f'<nav class="owner-tabs tx-tabs tx-periods" aria-label="Period">{tabs}</nav>{chip}'

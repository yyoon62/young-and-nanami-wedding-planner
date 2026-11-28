"""Accounts net worth block: History | Projection sub-tabs, currency chips, and the scope carried through links/forms.

Plain server-rendered links; no JavaScript. `Scope` is a `str` (the owner view, so existing code keeps working) that also
carries which sub-tab is open and which currency (if any) was chosen explicitly.
"""
import re
from html import escape
from urllib.parse import parse_qs, urlencode

from history import history_panel
import projections_ui
from projections import available_currencies, eligible_accounts

CHARTS = ("history", "projection")
ANCHOR = "#net-worth"
KEEP = object()


class Scope(str):
    """view + chart + currency. `str(scope)` is the view, so `scope == "Young"` and `f"{scope}"` behave as before."""

    def __new__(cls, view="Household", chart="history", currency=None):
        scope = super().__new__(cls, view)
        scope.chart, scope.currency = chart, currency
        return scope

    def but(self, *, view=None, chart=None, currency=KEEP):
        return Scope(str(self) if view is None else view, self.chart if chart is None else chart,
                     self.currency if currency is KEEP else currency)

    def url(self, anchor=False):
        query = {}
        if self != "Household":
            query["view"] = str(self)
        if self.currency:
            query["currency"] = self.currency
        if self.chart != "history":
            query["chart"] = self.chart
        return "/" + ("?" + urlencode(query) if query else "") + (ANCHOR if anchor else "")


def parse(query, people):
    """Scope from a query string, or ValueError(message) for a 400. Unknown parameters are ignored."""
    values = parse_qs(query, keep_blank_values=True)
    view, chart = values.get("view", ["Household"]), values.get("chart", ["history"])
    if len(view) != 1 or view[0] not in ("Household", *people):
        raise ValueError("Unknown inventory view.")
    if len(chart) != 1 or chart[0] not in CHARTS:
        raise ValueError("Unknown chart.")
    currency = values.get("currency")
    if currency is not None and (len(currency) != 1 or not re.fullmatch("[A-Z]{3}", currency[0])):
        raise ValueError("Unknown currency.")
    return Scope(view[0], chart[0], currency[0] if currency else None)


def projection_href(scope):
    return scope.url(anchor=True)


def _tabs(scope):
    def link(label, chart):
        target = scope.but(chart=chart)
        return (f'<a href="{escape(target.url(anchor=True) if chart == "history" else projection_href(target))}"'
                + (' aria-current="page"' if scope.chart == chart else "") + f'>{label}</a>')
    return (f'<nav class="owner-tabs nw-tabs" aria-label="Net worth chart">{link("History", "history")}'
            f'{link("Projection", "projection")}</nav>')


def _chips(scope, currencies):
    if len(currencies) < 2:
        return ""
    history = scope.chart == "history"
    chosen = scope.currency if history else (scope.currency or currencies[0])
    items = ([("All", None)] if history else []) + [(code, code) for code in currencies]
    links = "".join(
        f'<a href="{escape(scope.but(currency=code).url(anchor=True))}"'
        + (' aria-current="page"' if code == chosen else "") + f'>{escape(label)}</a>' for label, code in items)
    return f'<nav class="owner-tabs nw-chips" aria-label="Currency">{links}</nav>'


def owner_links(db, scope, people):
    """Owner tabs: keep the chart; keep the currency only where the target view has it."""
    out = []
    for label in ("Household", *people):
        code = scope.currency if scope.currency in available_currencies(eligible_accounts(db, label)) else None
        target = scope.but(view=label, currency=code)
        out.append(f'<a href="{escape(target.url(anchor=scope.chart != "history" or bool(code)))}"'
                   + (' aria-current="page"' if label == scope else "") + f'>{label}</a>')
    return "".join(out)


class Net:
    def __init__(self, links, html, currencies):
        self.links, self.html, self.currencies = links, html, currencies


def build(db, scope, people, *, timeline, money, has_backdated, projection=None, form_state=None):
    """Everything the Accounts page needs above the table. ValueError(message) if the currency is not in this view.

    The Projection panel is built only when that tab is open. `projection` = the server-side inputs it needs
    (token, section_for, places, today, suggestions); `form_state` = (attempted values, errors) after a refused save."""
    currencies = available_currencies(eligible_accounts(db, scope))
    if scope.currency and scope.currency not in currencies:
        raise ValueError("Currency is unavailable in this view.")
    if scope.chart == "projection":
        attempted, errors = form_state or (None, None)
        panel = projections_ui.embed(db, str(scope), scope.currency or currencies[0], attempted=attempted, errors=errors,
                                     history=(timeline, has_backdated), **projection)
    else:
        panel = history_panel(timeline, money, has_backdated=has_backdated, currency=scope.currency)
    html = (f'<div id="net-worth" class="nw"><div class="nw-bar">{_tabs(scope)}{_chips(scope, currencies)}</div>'
            f'{panel}</div>')
    return Net(owner_links(db, scope, people), html, currencies)

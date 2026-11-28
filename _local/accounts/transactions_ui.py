"""Server-rendered /transactions fragments and form dispatch, not an HTTP server.

Parent MUST authenticate/authorize all three routes, pass a trusted allowed_views
set and session token, and retain Host/Origin, CSP, no-store and request-size
guards. Owner tabs are not account ACLs; do not enable this in hosted mode
without real per-account authorization. For POST require exact path (no
query), application/x-www-form-urlencoded and strictly bounded raw bytes. Do not
use the baseline dict of first form values; duplicates must be rejected.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import re
import secrets
from urllib.parse import parse_qsl, urlencode

from html import escape as _esc  # noqa: F401
from decimal import Decimal
from tx_periods import month_add, month_label, money, spending_comparison
from logos import cached_logo, domain_for, fallback_svg
from transactions import (CURRENCY, VIEWS, category_context, edit_transaction, manual_index, month_dates,
                          monthly_snapshot)
from tx_categories import list_categories


TRANSACTIONS = "/transactions"
EDIT = TRANSACTIONS + "/edit"
SYNC = TRANSACTIONS + "/sync"
_FILTERS = ("month", "view", "currency", "q", "pending", "cat")


class RouteProblem(Exception):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.body = body  # Optional full rendered fragment (e.g. the page with the drawer kept open).


def problem_body(exc):
    """HTML fragment for a RouteProblem: the kept-open form page when there is one."""
    return exc.body or f'<p class="notice" role="alert">{escape(str(exc))}</p>'


def _params(raw, keys):
    if len(raw) > 8192 or re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise RouteProblem(400, "Invalid transaction form or query.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=len(keys))
    except ValueError:
        raise RouteProblem(400, "Invalid transaction form or query.") from None
    data = dict(pairs)
    if len(data) != len(pairs) or data.keys() - keys:
        raise RouteProblem(400, "Unexpected or duplicate transaction field.")
    return data


def _filters(data, allowed_views, default_month):
    view = data.get("view", next((v for v in VIEWS if v in allowed_views), "Household"))
    if view not in VIEWS:
        raise RouteProblem(400, "Unknown account view.")
    if view not in allowed_views:
        raise RouteProblem(403, "Account view not authorized.")
    month = data.get("month", default_month)
    try:
        month_dates(month)
    except ValueError as exc:
        raise RouteProblem(400, str(exc)) from None
    currency, q, pending, category = (data.get(k, default) for k, default in
                                      (("currency", ""), ("q", ""), ("pending", "0"), ("cat", "")))
    if ((currency and not CURRENCY.fullmatch(currency)) or len(q) > 100
            or (q and not q.isprintable()) or pending not in ("0", "1")
            or len(category) > 48 or (category and not category.isprintable())):
        raise RouteProblem(400, "Invalid transaction filter.")
    return dict(month=month, view=view, currency=currency, q=q, pending=pending, cat=category)


def _hidden(data, **extra):
    return "".join(f'<input type="hidden" name="{name}" value="{escape(str(value))}">'
                   for name, value in (data | extra).items())


def _option(value, label, chosen):
    return f'<option value="{escape(value)}"{" selected" if value == chosen else ""}>{escape(label)}</option>'


def _location(filters):
    return TRANSACTIONS + "?" + urlencode(filters)


def _stamp(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_SOURCES = {"manual": ' <span class="acct-sub">(set by you)</span>',
            "rule": ' <span class="acct-sub">(merchant rule)</span>',
            "stamp": ' <span class="acct-sub">(saved rule)</span>'}


def _drawer(r, f, token, categories, manual, attempt, error):
    """One edit drawer per row: a single form and a single Save."""
    chosen = attempt.get("category", r["category"])
    names = list(categories) + ([chosen] if chosen and chosen not in categories else [])
    cats = _option("", "Uncategorized", chosen) + "".join(_option(c, c, chosen) for c in names)
    transfer = attempt.get("transfer") or (("yes" if r["is_transfer"] else "no") if r["transfer_manual"] else "auto")
    choices = "".join(_option(value, label, transfer) for value, label in
                      (("auto", "Use merchant rule or automatic"), ("yes", "Transfer"), ("no", "Not a transfer")))
    can_rule = not r["pending"] and bool(r["merchant"])
    rules = ""
    if can_rule:
        key = r["clean_key"]
        others = manual.get((r["account_id"], r["source"], r["currency"], key or r["merchant"]), 0) \
            - (1 if r["category_manual"] else 0)
        scope = f' matching “{escape(key)}”' if key else " with this exact description"
        note = (f" {others} other row(s) with a category you set by hand will not change." if others > 0 else "")
        rule_choice = attempt.get("merchant_transfer") or {None: "none", 1: "yes", 0: "no"}[r["merchant_transfer"]]
        rule_choices = "".join(_option(value, label, rule_choice) for value, label in
                               (("none", "No rule"), ("yes", "Transfer"), ("no", "Not a transfer")))
        rules = (f'<label class="check"><input type="checkbox" name="apply" value="1"'
                 f'{" checked" if attempt.get("apply") == "1" else ""}> Apply to this merchant on this account '
                 f'({escape(r["currency"])}){scope}.</label><small>{note.strip()}</small>'
                 f'<label>Merchant transfer rule<select name="merchant_transfer">{rule_choices}</select></label>')
    problem = f'<p class="notice" role="alert">{escape(error)}</p>' if error else ""
    return (
        f'<details class="inline-pop tx-drawer"{" open" if error else ""}><summary class="sec-chip">Edit</summary>'
        f'<form method="post" action="{EDIT}" class="drawer-form tx-form">{problem}'
        + _hidden(f, token=token, row=r["row_id"])
        + f'<div class="tx-grid"><label>Category<select name="category">{cats}</select></label>'
        f'<label>New category<input name="new_category" maxlength="48" autocomplete="off" '
        f'value="{escape(attempt.get("new_category", ""))}" placeholder="e.g. Pets"></label>'
        '<button class="button secondary" name="action" value="add_category">Add category</button>'
        f'<label>Transfer<select name="transfer">{choices}</select></label></div>'
        + rules + '<div class="drawer-left"><button name="action" value="save">Save</button></div>'
        '</form></details>')


def _bars(cmp, f, totals):
    """Spending by category for the viewed month vs last month and the 3-month mean."""
    c = cmp.currency
    if not cmp.current:
        return '<div>No posted spending reported.</div>'
    zero = Decimal(0)
    peak = max([*cmp.current.values(), *(cmp.prev or {}).values(), *(cmp.avg or {}).values()] or [Decimal(1)])
    rows = []
    for name, amount in sorted(cmp.current.items(), key=lambda kv: (-kv[1], kv[0].casefold())):
        prev = None if cmp.prev is None else cmp.prev.get(name, zero)
        avg = None if cmp.avg is None else cmp.avg.get(name, zero)
        scale = lambda value: float(min(Decimal(100), value * 100 / peak)) if peak else 0.0
        marks = "".join(f'<line class="{cls}" x1="{scale(v):.2f}" x2="{scale(v):.2f}" y1="0" y2="10"/>'
                        for cls, v in (("tick-prev", prev), ("tick-avg", avg)) if v is not None)
        active = f["cat"] == name and f["currency"] == c
        href = _location(dict(f, cat=name, currency=c))
        rows.append(
            f'<li><a class="bar-row{" is-active" if active else ""}" href="{escape(href)}"'
            f'{" aria-current=\"true\"" if active else ""} title="Show only {escape(name)} ({escape(c)})">'
            f'<span class="bar-name">{escape(name)}</span>'
            f'<svg class="bar-svg" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="'
            f'{escape(name)}: {money(amount, c)}"><rect class="bar-fill" width="{scale(amount):.2f}" height="10"/>'
            f'{marks}</svg>'
            f'<span class="bar-nums"><strong>{money(amount, c)}</strong>'
            f' <span class="bar-cmp">last month {money(prev, c) if prev is not None else "—"}'
            f' · {len(cmp.avg_months) or ""}{"-mo avg" if cmp.avg is not None else "3-mo avg"} '
            f'{money(avg, c) if avg is not None else "—"}</span></span></a></li>')
    basis = (f'Compared with {escape(month_label(cmp.prev_month))} and the average of '
             f'{len(cmp.avg_months)} full month(s) before {escape(month_label(cmp.month))}. ' if cmp.avg is not None
             else f'Compared with {escape(month_label(cmp.prev_month))} where fully fetched; averages need at least '
                  'two fully fetched months before this one. ')
    flag = f' <span class="partial-flag">{escape(cmp.partial)}</span>' if cmp.partial else ""
    clear = ""
    if f["cat"] and f["currency"] == c:
        clear = (f'<p class="filter-chip">Showing {escape(f["cat"])} only · '
                 f'<a href="{escape(_location(dict(f, cat="")))}">Clear filter</a></p>')
    return (f'<section class="bars" aria-label="Spending by category in {escape(c)}">'
            f'<h3>Spending by category{flag}</h3>{clear}<ul class="bar-list">{"".join(rows)}</ul>'
            f'<p class="acct-sub">{basis}Bar = this month; ticks = last month and average. Transfers, pending '
            'and card credits under review are excluded. Click a category to filter the list.</p></section>')


def _amount(r):
    value = Decimal(r["amount"])
    if value == 0:
        return f'<span class="amt-zero">{escape(format(value, ",f"))}</span>'
    shown = escape(format(value, "+,f"))
    if r["effective_transfer"] or r["pending"]:
        return f'<span class="amt-neutral">{shown}</span>'
    return f'<span class="{"amt-in" if value > 0 else "amt-out"}">{shown}</span>'


def _logo(r, logo_dir):
    domain = domain_for(r["institution"], r["logo_domain"]) if logo_dir else ""
    if domain and cached_logo(logo_dir, domain, fetcher=None):  # cache only: no fetch at render
        return (f'<span class="logo-frame"><img class="inst-logo-img" src="/logo/{escape(domain)}" '
                'width="22" height="22" alt=""></span>')
    return fallback_svg(r["institution"])


def render_transactions(db, query, *, token, allowed_views, can_sync=False, today=None,
                        open_row=None, attempt=None, error=None, logo_dir=None):
    """Return fragment for parent page()/send(); read DB in a single parent snapshot."""
    today = today or datetime.now(timezone.utc).date()
    f = _filters(_params(query, set(_FILTERS)), allowed_views, today.isoformat()[:7])
    context = category_context(db)
    snap = monthly_snapshot(db, f["month"], f["view"], show_pending=f["pending"] == "1",
                            household_matching="Household" in allowed_views, context=context)
    categories, manual = list_categories(db), manual_index(db)
    attempt = attempt or {}
    views = "".join(_option(v, v, f["view"]) for v in VIEWS if v in allowed_views)
    currencies = sorted({r["currency"] for r in snap.coverage} | ({f["currency"]} - {""}))
    currencies = _option("", "All currencies", f["currency"]) + "".join(
        _option(c, c, f["currency"]) for c in currencies)
    current_month = today.isoformat()[:7]
    nav_args = lambda month: _location(dict(f, month=month))
    next_link = (f'<a class="month-step" href="{escape(nav_args(month_add(f["month"], 1)))}" rel="next">'
                 f'{escape(month_label(month_add(f["month"], 1)))} →</a>' if f["month"] < current_month
                 else '<span class="month-step off">Current month</span>')
    names = "".join(_option(c, c, f["cat"]) for c in list(categories) + ["Uncategorized"]
                    if c) + ("" if f["cat"] in categories or f["cat"] in ("", "Uncategorized")
                             else _option(f["cat"], f["cat"], f["cat"]))
    search = (
        f'<nav class="month-nav" aria-label="Month"><a class="month-step" href="{escape(nav_args(month_add(f["month"], -1)))}" '
        f'rel="prev">← {escape(month_label(month_add(f["month"], -1)))}</a>'
        f'<strong>{escape(month_label(f["month"]))}</strong>{next_link}</nav>'
        f'<form method="get" action="{TRANSACTIONS}" class="drawer-form" data-autofilter><div class="drawer-grid">'
        f'<label>Month<input type="month" name="month" value="{f["month"]}" required></label>'
        f'<label>View<select name="view">{views}</select></label>'
        f'<label>Currency<select name="currency">{currencies}</select></label>'
        f'<label>Category<select name="cat">{_option("", "All categories", f["cat"])}{names}</select></label>'
        f'<label>Find<input name="q" maxlength="100" value="{escape(f["q"])}"'
        ' placeholder="Merchant, account, category" autocomplete="off"></label>'
        f'<label>Pending<select name="pending">{_option("0", "Hide", f["pending"])}'
        f'{_option("1", "Show, if fetched", f["pending"])}</select></label>'
        '<noscript><div class="drawer-left"><button>Apply filters</button></div></noscript></div></form>')
    sync = ""
    if can_sync and "Household" in allowed_views:
        sync = (f'<form method="post" action="{SYNC}" class="drawer-form">'
                + _hidden(f, token=token)
                + '<div class="drawer-left"><button>Sync transactions</button>'
                '<label><input type="checkbox" name="include_pending" value="1">'
                ' Request pending as well</label></div></form>'
                '<p class="acct-sub">Refreshes all linked accounts. Shares the balance sync quota.</p>')
    coverage = []
    for r in snap.coverage:
        if r["end_at"] is not None:
            state = ("Window requested" if r["covered"] else "Partial or outside requested window")
            state += f' · {_stamp(r["start_at"])} through {_stamp(r["end_at"])} (end exclusive)'
        else:
            state = "Not yet fetched" if r["linked"] else "Not linked; transaction data unavailable"
        coverage.append(f'<li>{escape(r["institution"])} · {escape(r["nickname"])}'
                        f' ({escape(r["currency"])}) — {state}</li>')
    comparisons = spending_comparison(db, f["month"], f["view"], household_matching="Household" in allowed_views,
                                      today=today, context=context, snapshot=snap)
    totals = []
    for currency, t in sorted(snap.totals.items()):
        qualifier = ("" if all(r["covered"] for r in snap.coverage if r["currency"] == currency)
                     else " · partial coverage")
        totals.append(f'<article class="card"><small>{escape(currency)}{qualifier}</small>'
                      f'<strong>{t.net:f}</strong><div>Net saved estimate · signed cash flow</div>'
                      f'<div>Income / credits {t.credits:f} · Spending {t.spending:f}</div>'
                      + (f'<div>Card credits under review: {t.review:f}</div>' if t.review else "")
                      + _bars(comparisons[currency], f, t) + '</article>')
    if not totals:
        summary = "<p>No posted data with sufficient coverage to report a zero.</p>"
    else:
        summary = ('<div class="cards">' + "".join(totals) + "</div>"
                   '<p>Income / credits includes refunds; it is not verified income. '
                   'The net saved estimate is signed cash flow, not measured savings.</p>')
    if not any(not r["pending"] for r in snap.rows) and totals:
        summary += "<p>No posted transactions reported for this month in this view.</p>"

    rows = []
    q = f["q"].casefold().strip()
    for r in snap.rows:
        if f["currency"] and f["currency"] != r["currency"]:
            continue
        if q and not any(q in r[k].casefold() for k in
                         ("description", "payee", "category", "nickname", "institution", "flow")):
            continue
        if f["cat"] and (r["category"] or "Uncategorized").casefold() != f["cat"].casefold():
            continue
        category = escape(r["category"])
        drawer = _drawer(r, f, token, categories, manual, attempt if open_row == r["row_id"] else {},
                         error if open_row == r["row_id"] else None)
        when = escape(r["posted_date"] or r["pending_date"])
        label = "Pending · provider or observed date" if r["pending"] else "Posted"
        payee = f'<div class="acct-sub">Payee: {escape(r["payee"])}</div>' if r["payee"] else ""
        suggestion = ('<div class="acct-sub">Possible internal transfer · still counted. '
                      'If confirmed, mark each leg as Transfer.</div>' if r["suggested_transfer"] else "")
        origin = _SOURCES.get(r["category_source"], "")
        rows.append(
            f'<tr class="acct-row" id="tx-{r["row_id"]}"><td class="date-cell"><time datetime="{when}">{when}</time>'
            f'<div class="acct-sub">{label}</div></td><th scope="row" class="acct-cell">'
            f'<div class="acct-with-logo">{_logo(r, logo_dir)}<div class="acct-info">'
            f'<span class="acct-name">{escape(r["description"] or "No description")}</span>{payee}'
            f'<div class="acct-sub">SimpleFIN · {escape(r["institution"])} · {escape(r["nickname"])}</div>'
            f'</div></div></th>'
            f'<td class="num"><span class="cur">{escape(r["currency"])}</span> {_amount(r)}</td>'
            f'<td>{category or "Uncategorized"}{origin} · {escape(r["flow"])}{suggestion} {drawer}</td></tr>')
    table = (
        '<div class="table-wrap"><table class="acct-table"><thead><tr><th>Date</th><th>Transaction</th>'
        '<th class="num">Amount</th><th>Category</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div>" if rows else "<p>No transactions match this list.</p>")
    alert = f'<p class="notice" role="alert">{escape(error)}</p>' if error else ""
    return (
        '<script defer src="/dashboard.js"></script>'
        '<section class="history" aria-labelledby="cash-flow"><h2 id="cash-flow">Monthly cash flow</h2>'
        + alert + search + sync + summary
        + '<p>Pending, transfers and explicit unmatched card-payment credits are outside totals; unmatched bank '
        'debits remain spending. Automatic card matching requires corroboration on both available legs. '
        'Checking and savings matches are suggestions only. A manual transfer choice overrides a merchant '
        'rule, which overrides automatic classification.</p>'
        '<details><summary>Coverage and dates</summary><p>Requested windows do not prove institution '
        'completeness or retention. Current-month figures are provisional. Excluded accounts do not contribute. '
        'Individual views include joint accounts. List filters do not change totals. Merchant rules match the '
        'cleaned merchant name (dates, store and reference numbers removed) on the same account and currency; '
        'a category you set on one row by hand always wins over a rule. Dates are the bank\'s posted date (UTC): '
        'an evening transaction in a western timezone, or a local-midnight stamp east of UTC, can show a day '
        'off, and month filters use the same date.</p>'
        + ("<ul>" + "".join(coverage) + "</ul>" if coverage else "<p>No included accounts in this view.</p>")
        + '</details></section><section><h2>Transactions</h2>' + table + '</section>')


@dataclass(frozen=True)
class PostResult:
    location: str
    sync: bool = False
    include_pending: bool = False


def post_transactions(db, path, body, *, token, allowed_views, can_sync=False):
    """Accept exact path and raw UTF-8 form bytes after parent HTTP guards.

    Owns edit BEGIN IMMEDIATE/commit; use a fresh connection. Sync only returns
    authorization + arguments; parent starts sync with no open DB transaction.
    """
    if path not in (EDIT, SYNC):
        raise RouteProblem(404, "Unknown transaction route.")
    if len(body) > 8192:
        raise RouteProblem(413, "Transaction form too large.")
    try:
        raw = body.decode("utf-8", "strict")
    except UnicodeError:
        raise RouteProblem(400, "Invalid transaction form.") from None
    keys = set(_FILTERS) | {"token"} | ({"row", "category", "new_category", "apply", "transfer",
                                         "merchant_transfer", "action"}
                                        if path == EDIT else {"include_pending"})
    data = _params(raw, keys)
    if not token or not secrets.compare_digest(data.get("token", "").encode("utf-8"), token.encode("utf-8")):
        raise RouteProblem(403, "Reload the transaction form and try again.")
    if "view" not in data:
        raise RouteProblem(400, "Choose an account view.")
    f = _filters(data, allowed_views, None)
    if path == SYNC:
        if not can_sync or "Household" not in allowed_views:
            raise RouteProblem(403, "Transaction sync not authorized or unavailable.")
        pending = data.get("include_pending", "0")
        if pending not in ("0", "1"):
            raise RouteProblem(400, "Invalid pending option.")
        return PostResult(_location(f), True, pending == "1")
    row = data.get("row", "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", row) or int(row) > 2**63 - 1:
        raise RouteProblem(400, "Invalid transaction row.")
    action = data.get("action")
    if ((action in ("category", "rule") and "category" not in data)
            or (action == "transfer" and "transfer" not in data)
            or (action == "transfer_rule" and "merchant_transfer" not in data)
            or data.get("apply", "1") != "1"):
        raise RouteProblem(400, "Incomplete transaction edit.")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not edit_transaction(db, int(row), f["view"], f["month"], action=action,
                                    category=data.get("category", ""), transfer=data.get("transfer", ""),
                                    merchant_transfer=data.get("merchant_transfer", ""),
                                    new_category=data.get("new_category", ""), apply=data.get("apply") == "1"):
                raise RouteProblem(404, "Transaction not found in this view and month.")
    except ValueError as exc:
        message = str(exc) or "Enter a printable category of up to 48 characters."
        try:
            body = render_transactions(db, urlencode(f), token=token, allowed_views=allowed_views,
                                       can_sync=can_sync, open_row=int(row), attempt=data, error=message)
        except Exception:
            body = None
        raise RouteProblem(422, message, body=body) from None
    return PostResult(_location(f) + f"#tx-{row}")

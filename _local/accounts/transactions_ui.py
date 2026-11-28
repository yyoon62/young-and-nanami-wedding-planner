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
from tx_info import info_box, notes_html
from tx_money import fmt, sign_of
from tx_periods import month_add, month_label, spending_comparison
from logos import cached_logo, domain_for, fallback_svg
from transactions import (CURRENCY, VIEWS, _midnight, category_context, edit_transaction, manual_index,
                          month_dates, monthly_snapshot)
from pathlib import Path
from tx_categories import default_emoji, list_categories
from tx_owners import PEOPLE, effective, effective_owner_of
from tx_classifier import enabled as classifier_enabled, load_key, model_name, run_classifier, set_setting, stats as classifier_stats


TRANSACTIONS = "/transactions"
EDIT = TRANSACTIONS + "/edit"
SYNC = TRANSACTIONS + "/sync"
_FILTERS = ("month", "view", "currency", "q", "pending", "cat", "review")
_MOVED = re.compile(r"(Young|Nanami|Unspecified)-([1-9][0-9]{0,18})")   # one-shot GET note after an owner click, not a filter


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
    currency, q, pending, category, review = (data.get(k, default) for k, default in
                                              (("currency", ""), ("q", ""), ("pending", "0"), ("cat", ""), ("review", "")))
    if ((currency and not CURRENCY.fullmatch(currency)) or len(q) > 100
            or (q and not q.isprintable()) or pending not in ("0", "1") or review not in ("", "1")
            or len(category) > 48 or (category and not category.isprintable())):
        raise RouteProblem(400, "Invalid transaction filter.")
    return dict(month=month, view=view, currency=currency, q=q, pending=pending, cat=category, review=review)


def _hidden(data, **extra):
    return "".join(f'<input type="hidden" name="{name}" value="{escape(str(value))}">'
                   for name, value in (data | extra).items())


def _option(value, label, chosen):
    return f'<option value="{escape(value)}"{" selected" if value == chosen else ""}>{escape(label)}</option>'


def _location(filters):
    return TRANSACTIONS + "?" + urlencode(filters)


def _stamp(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_SOURCES = {"suggested": ' <span class="badge" title="Suggested by the classifier; not confirmed">suggested</span>',
            "manual": ' <span class="acct-sub">(set by you)</span>',
            "rule": ' <span class="acct-sub">(merchant rule)</span>',
            "stamp": ' <span class="acct-sub">(saved rule)</span>',
            "default": ' <span class="acct-sub">(built-in default)</span>'}


def _data_dir(db):
    row = db.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).parent if row and row[2] else None


def _classifier_panel(db, f, token):
    on, st = classifier_enabled(db), classifier_stats(db)
    key, source, problem = load_key(_data_dir(db))
    key_line = (f"Key found ({source})." if key else (problem or "No key found.") + " Nothing is sent without a key.")
    if not on:
        status = "Off."
    elif key is None:
        status = "On, but nothing is sent until a usable key is found."   # never next to a stale "last run" line
    else:
        status = st["status"] or "On, waiting for the next sync or Classify now."
    form = (f'<form method="post" action="{EDIT}" class="drawer-form classifier-form">'
            + _hidden(f, token=token)
            + '<label class="check"><input type="checkbox" name="enabled" value="1"'
            + (" checked" if on else "") + '> Suggest categories and an emoji with Claude Haiku. '
            '<strong>Merchant names are sent to Anthropic.</strong></label>'
            '<div class="drawer-left"><button name="action" value="classifier">Save</button></div></form>')
    # Its own form: sharing the checkbox form would post "enabled" with classify_now, which the route rejects.
    now_form = (f'<form method="post" action="{EDIT}" class="drawer-form classifier-form">'
                + _hidden(f, token=token)
                + '<div class="drawer-left"><button class="button secondary" name="action" '
                'value="classify_now">Classify now</button></div></form>' if on else "")
    form += now_form
    totals = f'{st["suggested"]} suggested · {st["review"]} need review · {st["pending"]} waiting'
    title = "Category suggestions · on" if on else "Category suggestions (off by default)"
    return (f'<details class="classifier"{" open" if on and key is None else ""}><summary>{title}</summary>'
            + form +
            f'<p class="acct-sub">{escape(key_line)} Model: {escape(model_name())}.<br>'
            f'{escape(status)}<br>Merchants so far: {totals}.</p>'
            '<p class="acct-sub">Only cleaned merchant names (no amounts, dates or accounts) are sent, once each.</p>'
            '<details class="classifier-details"><summary>Privacy details</summary>'
            '<p class="acct-sub">Only cleaned merchant text (for example “maple cafe”) leaves this computer, once per '
            'merchant: never amounts, dates, accounts, owners or ids. Descriptions that look like Zelle, Venmo, '
            'payroll, transfers or checks are not sent. A bare person’s name or an employer in a description '
            'cannot be recognised and would be sent. Suggestions are labelled, never override anything you set, '
            'and are not counted as confirmed. Not available when the dashboard is hosted.</p></details></details>')


def _drawer(r, f, token, categories, manual, attempt, error):
    """One edit drawer per row: a single form and a single Save."""
    chosen = attempt.get("category", r["category"])
    names = list(categories) + ([chosen] if chosen and chosen not in categories else [])
    cats = _option("", "Uncategorized", chosen) + "".join(_option(c, c, chosen) for c in names)
    transfer = attempt.get("transfer") or (("yes" if r["is_transfer"] else "no") if r["transfer_manual"] else "auto")
    choices = "".join(_option(value, label, transfer) for value, label in
                      (("auto", "Automatic"), ("yes", "Yes, a transfer"), ("no", "No, not a transfer")))
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
                               (("none", "No rule"), ("yes", "Yes, always a transfer"), ("no", "No, never a transfer")))
        who = escape((key or r["description"] or r["merchant"])[:40])
        rules = (f'<label class="check"><input type="checkbox" name="apply" value="1"'
                 f'{" checked" if attempt.get("apply") == "1" else ""}> Apply to this merchant on this account '
                 f'({escape(r["currency"])}){scope}.</label><small>{note.strip()}</small>'
                 f'<label>Always treat “{who}” on this account as a transfer'
                 f'<select name="merchant_transfer">{rule_choices}</select></label>')
    problem = f'<p class="notice" role="alert">{escape(error)}</p>' if error else ""
    return (
        f'<details class="inline-pop tx-drawer"{" open" if error else ""}><summary class="sec-chip">Edit</summary>'
        f'<form method="post" action="{EDIT}" class="drawer-form tx-form">{problem}'
        + _hidden(f, token=token, row=r["row_id"])
        + f'<div class="tx-grid"><label>Category<select name="category">{cats}</select></label>'
        f'<label>New category<input name="new_category" maxlength="48" autocomplete="off" '
        f'value="{escape(attempt.get("new_category", ""))}" placeholder="e.g. Pets"></label>'
        '<button class="button secondary" name="action" value="add_category">Add category</button>'
        f'<label>Is this transaction a transfer?<select name="transfer">{choices}</select></label></div>'
        '<small class="tx-hint">Transfer = money between your own accounts; not counted as spending.</small>'
        + rules + '<div class="drawer-left"><button name="action" value="save">Save</button></div>'
        '</form></details>')


def _summary_card(currency, t, cmp, incomplete, f):
    """Opening tag + compact per-currency summary; the caller appends the bars and </article>."""
    net = sign_of(t.net, currency)
    label = "🟢 Saved" if net > 0 else "🔴 Overspent" if net < 0 else "⚪ Even"
    chips = ""
    if cmp.partial.startswith("so far"):
        chips += (f' <span class="partial-flag" title="The month is still in progress; nothing is extrapolated">'
                  f'{escape(cmp.partial)}</span>')
    if incomplete:
        chips += (' <span class="partial-flag" title="Not every linked account has fetched this whole month, '
                  'so the totals may be incomplete">partial</span>')
    line = (f'<div class="tx-sum"><span title="Credits including refunds; not verified income">💰 Income '
            f'<strong>{fmt(t.credits, currency)}</strong></span> '
            f'<span title="Pending, transfers and card credits under review are excluded">💸 Spending '
            f'<strong>{fmt(t.spending, currency)}</strong></span> '
            f'<span title="Income minus spending: cash flow, not measured savings">{label} '
            f'<strong>{fmt(t.net, currency, absolute=True)}</strong></span></div>')
    confirm = ""
    if t.review_count:
        n = t.review_count
        active = f["review"] == "1" and f["currency"] == currency
        confirm = (f'<div class="tx-confirm"><a href="{escape(_location(dict(f, review="1", currency=currency)))}"'
                   f'{" aria-current=\"true\"" if active else ""} title="Unmatched card payment credits; '
                   f'click to list them">⚠️ {n} card payment{"" if n == 1 else "s"} to confirm · '
                   f'{fmt(t.review, currency)}</a></div>')
    return (f'<article class="card tx-card"><div class="tx-card-head"><small>{escape(currency)}</small>{chips}</div>'
            + line + confirm)


def _owner_cell(r, f, token):
    """Young / Nanami pills with the Accounts markup; both active = Joint. A click toggles this transaction only."""
    eff = r["eff_owner"]
    overridden = r["owner_override"] is not None and r["owner_override"] != r["owner"]
    pills = "".join(
        f'<button type="submit" name="owner" value="{p}" class="pill-btn{" active" if eff in (p, "Joint") else ""}"'
        f' aria-pressed="{"true" if eff in (p, "Joint") else "false"}"'
        f' title="Owner of this transaction (account owner: {escape(r["owner"])})">{p}</button>' for p in PEOPLE)
    cell = (f'<form method="post" action="{EDIT}" class="inline-form owner-pills">'
            + _hidden(f, token=token, row=r["row_id"], action="owner") + pills + '</form>')
    if overridden:
        cell += (f' <form method="post" action="{EDIT}" class="inline-form">'
                 + _hidden(f, token=token, row=r["row_id"], action="owner_reset")
                 + f'<button type="submit" class="icon-btn tx-reset" title="Overridden for this transaction. '
                 f'Reset to the account owner ({escape(r["owner"])})" aria-label="Reset owner to the account owner">'
                 '↺</button></form> <span class="acct-sub tx-ovr">overridden</span>')
    return f'<td class="owner-cell">{cell}</td>'


def _moved_note(moved, f, allowed_views):
    """One line after an owner click that took the row out of the current view (validated, fixed wording)."""
    found = _MOVED.fullmatch(moved or "")
    if not found or f["view"] == "Household" or found[1] == f["view"]:
        return ""
    show = (f' · <a href="{escape(_location(dict(f, view="Household")) + "#tx-" + found[2])}">Show in Household</a>'
            if "Household" in allowed_views else "")
    where = "Now unassigned (no owner)" if found[1] == "Unspecified" else f"Moved to {found[1]}\'s view"
    return f'<p class="notice" role="status">{where}{show}</p>'


def _bars(cmp, f, totals):
    """Spending by category for the viewed month vs last month and the 3-month mean."""
    c = cmp.currency
    if not cmp.current:
        return '<div class="acct-sub">📊 No posted spending reported.</div>'
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
            f'{escape(name)}: {fmt(amount, c)}"><rect class="bar-fill" width="{scale(amount):.2f}" height="10"/>'
            f'{marks}</svg>'
            f'<span class="bar-nums"><strong>{fmt(amount, c)}</strong>'
            f' <span class="bar-cmp">last month {fmt(prev, c) if prev is not None else "—"}'
            f' · {len(cmp.avg_months) or ""}{"-mo avg" if cmp.avg is not None else "3-mo avg"} '
            f'{fmt(avg, c) if avg is not None else "—"}'
            + (f' · includes {cmp.suggested[name]} suggested' if cmp.suggested.get(name) else "")
            + '</span></span></a></li>')
    basis = (f'Compared with {escape(month_label(cmp.prev_month))} and the average of '
             f'{len(cmp.avg_months)} full month(s) before {escape(month_label(cmp.month))}. ' if cmp.avg is not None
             else f'Compared with {escape(month_label(cmp.prev_month))} where fully fetched; averages need at least '
                  'two fully fetched months before this one. ')
    clear = ""
    if f["cat"] and f["currency"] == c:
        clear = (f'<p class="filter-chip">Showing {escape(f["cat"])} only ({escape(c)}) · '
                 f'<a href="{escape(_location(dict(f, cat="", currency="")))}">Clear filter</a></p>')
    top = "".join(f'<span>{escape(name)} <strong>{fmt(amount, c)}</strong></span> '
                  for name, amount in sorted(cmp.current.items(), key=lambda kv: (-kv[1], kv[0].casefold()))[:3])
    opened = bool(f["cat"]) and f["currency"] in ("", c)
    return (f'<section class="bars" aria-label="Spending by category in {escape(c)}"><details class="bars-det"'
            f'{" open" if opened else ""}><summary title="Bar = this month; ticks = last month and the average. '
            f'Click a category to filter the list."><span class="bars-title">📊 Spending by category</span> '
            f'<span class="bars-top">{top}</span></summary>{clear}'
            f'<ul class="bar-list">{"".join(rows)}</ul><p class="acct-sub">{basis.strip()}</p></details></section>')


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
    data = _params(query, set(_FILTERS) | {"moved"})
    f = _filters(data, allowed_views, today.isoformat()[:7])
    moved = _moved_note(data.get("moved"), f, allowed_views)
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
                + '<div class="drawer-left"><button title="Refreshes all linked accounts. Shares the balance sync '
                'quota.">Sync transactions</button>'
                '<label><input type="checkbox" name="include_pending" value="1">'
                ' Request pending as well</label></div></form>')
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
        rows = [r for r in snap.coverage if r["currency"] == currency]
        if comparisons[currency].partial.startswith("so far"):
            # An in-progress month is never "covered" (it is not over); that is what the dated chip says. "partial"
            # is for an account that has not fetched from the first of the month, or not at all.
            lo = _midnight(month_dates(f["month"])[0])
            incomplete = any(r["start_at"] is None or r["start_at"] > lo for r in rows)
        else:
            incomplete = not all(r["covered"] for r in rows)
        totals.append(_summary_card(currency, t, comparisons[currency], incomplete, f)
                      + _bars(comparisons[currency], f, t) + '</article>')
    if not totals:
        summary = "<p>No posted data with sufficient coverage to report a zero.</p>"
    else:
        summary = '<div class="cards">' + "".join(totals) + "</div>" + notes_html()
    if not any(not r["pending"] for r in snap.rows) and totals:
        summary += '<p class="acct-sub">No posted transactions reported for this month in this view.</p>'

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
        if f["review"] == "1" and r["flow"] != "Review card credit":
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
        # A transfer is not spending, so it needs no category; say so instead of "Needs review".
        label_cell = category or ("" if r["effective_transfer"] else
                                  "Needs review" if r["category_source"] == "review" else "Uncategorized")
        rows.append(
            f'<tr class="acct-row" id="tx-{r["row_id"]}"><td class="date-cell"><time datetime="{when}">{when}</time>'
            f'<div class="acct-sub">{label}</div></td><th scope="row" class="acct-cell">'
            f'<div class="acct-with-logo">{_logo(r, logo_dir)}<div class="acct-info">'
            f'<span class="acct-name"><span class="tx-emoji" aria-hidden="true">{escape(r["emoji"] or default_emoji(r["category"], r["effective_transfer"]))}</span> {escape(r["description"] or "No description")}</span>{payee}'
            f'<div class="acct-sub">SimpleFIN · {escape(r["institution"])} · {escape(r["nickname"])}</div>'
            f'</div></div></th>'
            + _owner_cell(r, f, token) +
            f'<td class="num"><span class="cur">{escape(r["currency"])}</span> {_amount(r)}</td>'
            f'<td>{label_cell + origin + " · " if label_cell else ""}{escape(r["flow"])}{suggestion} {drawer}</td></tr>')
    table = (
        '<div class="table-wrap"><table class="acct-table tx-table tx-compact"><thead><tr><th>Date</th><th>Transaction</th>'
        '<th>Owner</th><th class="num">Amount</th><th>Category</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div>" if rows else "<p>No transactions match this list.</p>")
    alert = f'<p class="notice" role="alert">{escape(error)}</p>' if error else ""
    review_chip = (f'<p class="filter-chip">Showing card payments to confirm only · '
                   f'<a href="{escape(_location(dict(f, review="", currency="")))}">Clear filter</a></p>'
                   if f["review"] == "1" else "")
    return (
        '<script defer src="/dashboard.js"></script>'
        '<section class="history" aria-labelledby="cash-flow"><h2 id="cash-flow">Monthly cash flow</h2>'
        + alert + moved + search + sync + _classifier_panel(db, f, token) + summary
        + info_box(coverage, can_sync=can_sync and "Household" in allowed_views)
        + '</section><section><h2>Transactions</h2>' + review_chip + table + '</section>')


@dataclass(frozen=True)
class PostResult:
    location: str
    sync: bool = False
    include_pending: bool = False


def _classifier_action(db, data, f):
    """Toggle or run the classifier. Local only: the parent never routes this in hosted mode."""
    if data.get("enabled", "1") not in ("0", "1") or ("enabled" in data and data["action"] != "classifier"):
        raise RouteProblem(400, "Invalid classifier setting.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if data["action"] == "classifier":
            turning_on = data.get("enabled") == "1"
            set_setting(db, "enabled", "1" if turning_on else "0")
            set_setting(db, "failures", 0)
            set_setting(db, "next_try_at", 0)
            set_setting(db, "status", "")
    if data["action"] == "classify_now":
        run_classifier(db.execute("PRAGMA database_list").fetchone()[2], hosted=False)
    return PostResult(_location(f))


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
                                         "merchant_transfer", "action", "enabled", "owner"}
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
    if data.get("action") in ("classifier", "classify_now"):
        return _classifier_action(db, data, f)
    row = data.get("row", "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", row) or int(row) > 2**63 - 1:
        raise RouteProblem(400, "Invalid transaction row.")
    action = data.get("action")
    if ((action in ("category", "rule") and "category" not in data)
            or (action == "transfer" and "transfer" not in data)
            or (action == "transfer_rule" and "merchant_transfer" not in data)
            or (action == "owner" and "owner" not in data)
            or data.get("apply", "1") != "1"):
        raise RouteProblem(400, "Incomplete transaction edit.")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not edit_transaction(db, int(row), f["view"], f["month"], action=action,
                                    category=data.get("category", ""), transfer=data.get("transfer", ""),
                                    merchant_transfer=data.get("merchant_transfer", ""),
                                    new_category=data.get("new_category", ""), apply=data.get("apply") == "1",
                                    owner=data.get("owner", "")):
                raise RouteProblem(404, "Transaction not found in this view and month.")
            moved = None
            if action in ("owner", "owner_reset") and f["view"] != "Household":
                now = effective_owner_of(db, int(row))
                if now not in (f["view"], "Joint"):
                    moved = f"&moved={now}-{row}"      # the row left this view: say so on the next page
    except ValueError as exc:
        message = str(exc) or "Enter a printable category of up to 48 characters."
        try:
            body = render_transactions(db, urlencode(f), token=token, allowed_views=allowed_views,
                                       can_sync=can_sync, open_row=int(row), attempt=data, error=message)
        except Exception:
            body = None
        raise RouteProblem(422, message, body=body) from None
    return PostResult(_location(f) + (moved or "") + f"#tx-{row}")

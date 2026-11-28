"""Server-rendered projection views; caller owns session, Origin and CSRF checks."""
from contextlib import closing
from decimal import Decimal, localcontext
from html import escape
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from dataclasses import replace

from projections import (
    AMOUNTS, MODES, OPTIONAL, PRECISION, RATES, STALE_DAYS, Assumptions, available_currencies,
    eligible_accounts, load_assumptions, project, save_assumptions, starting_balances,
    validate_assumptions,
)
from tx_periods import money as tx_money, suggest_monthly


PATH = "/projections"
NET_WORTH = ("net_worth", "Net worth")
OTHER_METRICS = (("investments", "Investments"), ("retirement", "Retirement"), ("debt", "Debt owed"))
CARD_METRICS = (NET_WORTH, ("liquid", "Cash + HYSA"), *OTHER_METRICS)
ANNUAL_METRICS = (NET_WORTH, ("cash", "Cash"), ("hysa", "Savings / HYSA"), *OTHER_METRICS)


def projection_url(view, currency=None):
    query = {"view": view}
    if currency is not None:
        query["currency"] = currency
    return PATH + "?" + urlencode(query)


def _page(page, content):
    return page(content, current=PATH, heading="Projected net worth")


def _scope_error(page, status, message, view="Household"):
    return (status, _page(page, f'<p class="notice" role="alert">{escape(message)}</p>'
                                 f'<p><a href="{escape(projection_url(view))}">Choose a projection</a></p>'), None)


def projection_get(db_path, target, *, page, token, people, section_for, places, today, suggestions=False):
    """After the server's GET access gate. Returns (status, text, location)."""
    try:
        query = parse_qs(urlsplit(target).query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return _scope_error(page, 400, "Invalid projection selector.")
    view = query.get("view", ["Household"])
    if len(view) != 1 or view[0] not in ("Household", *people):
        return _scope_error(page, 400, "Unknown projection view.")
    view = view[0]
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("BEGIN")  # Read accounts and assumptions from the same snapshot.
        rows = eligible_accounts(db, view)
        currencies = available_currencies(rows)
        currency = query.get("currency", currencies[:1])
        if len(currency) != 1 or currency[0] not in currencies:
            return _scope_error(page, 400, "Currency is unavailable in this view.", view)
        currency = currency[0]
        assumptions = load_assumptions(db, view, currency, places(currency))
        suggestion = suggest_monthly(db, view, currency, today) if suggestions else None
    seed = starting_balances(rows, currency, section_for, today)
    return 200, _page(page, _panel(view, currency, currencies, people, seed, assumptions,
                                  places(currency), token, suggestion=suggestion)), None


def projection_post(db_path, data, *, page, token, people, section_for, places, today, suggestions=False):
    """After the server's POST guards, using its already parsed and CSRF-checked data."""
    view, currency = data.get("view"), data.get("currency")
    if view not in ("Household", *people):
        return _scope_error(page, 422, "Unknown projection view. No assumptions were saved.")
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        rows = eligible_accounts(db, view)
        currencies = available_currencies(rows)
        if currency not in currencies:
            return _scope_error(page, 422, "Currency is unavailable in this view. No assumptions were saved.", view)
        scale = places(currency)
        if data.get("apply") == "suggestion":
            return _apply_suggestion(db, data, view, currency, scale, today, suggestions, page)
        assumptions, errors = validate_assumptions(data, scale)
        if not errors:
            save_assumptions(db, view, currency, assumptions)
            return 303, "", projection_url(view, currency)
        saved = load_assumptions(db, view, currency, scale)
        suggestion = suggest_monthly(db, view, currency, today) if suggestions else None
    seed = starting_balances(rows, currency, section_for, today)
    return 422, _page(page, _panel(view, currency, currencies, people, seed, saved, scale, token,
                                  attempted=data, errors=errors, suggestion=suggestion)), None


def _apply_suggestion(db, data, view, currency, scale, today, suggestions, page):
    """One POST: recompute server-side; save only income and spending, only if they match what was shown."""
    def refuse(message):
        return 422, _page(page, f'<p class="notice" role="alert">{escape(message)} No assumptions were saved.</p>'
                                f'<p><a href="{escape(projection_url(view, currency))}">Back to projections</a></p>'), None

    suggestion = suggest_monthly(db, view, currency, today) if suggestions else None
    if suggestion is None or suggestion.income is None:
        return refuse("There is no suggestion to apply." if suggestions else "Suggestions are not available here.")
    shown = (data.get("suggest_income"), data.get("suggest_spending"))
    if shown != (format(suggestion.income, "f"), format(suggestion.spending, "f")):
        return refuse("The suggestion changed since this page was loaded; reload to review it.")
    current = load_assumptions(db, view, currency, scale)
    updated, errors = validate_assumptions(
        replace(current, income=suggestion.income, spending=suggestion.spending).form_values(), scale)
    if errors:
        return refuse("The suggested amounts do not fit the allowed range.")
    save_assumptions(db, view, currency, updated)
    return 303, "", projection_url(view, currency)


def _suggestion_card(view, currency, token, suggestion, assumptions, scale):
    """Dated basis for the suggested income/spending; nothing here is saved by viewing it."""
    if suggestion is None:
        return ""
    money = lambda value: escape(currency) + " " + tx_money(value, currency)
    if suggestion.income is None:
        return ('<section aria-labelledby="projection-suggestion"><h2 id="projection-suggestion">'
                'Suggested from your transactions</h2>'
                f'<p class="notice" role="note">No suggestion yet: {escape(suggestion.reason)}</p></section>')
    rows = "".join(f'<tr><th scope="row">{escape(month)}</th><td class="num">{money(c)}</td>'
                   f'<td class="num">{money(sp)}</td></tr>' for month, c, sp in suggestion.monthly)
    notes = []
    if suggestion.wide:
        notes.append("The largest month is more than 1.5× the smallest for "
                     + " and ".join(suggestion.wide) + ": check for one-off amounts before applying.")
    if suggestion.possible_transfers:
        notes.append(f"{suggestion.possible_transfers} possible internal transfer leg(s) were left out; "
                     "confirm them on Transactions if they are not real spending or income.")
    if suggestion.card_reviews:
        notes.append(f"{suggestion.card_reviews} card credit(s) under review were left out.")
    if suggestion.unlinked:
        notes.append(f"{suggestion.unlinked} included cash/card account(s) have no transaction feed, "
                     "so their activity is missing.")
    notes.append("Loan and card payments leaving checking still count as spending unless marked Transfer. "
                 "Remove debt payments and contributions you model below to avoid counting them twice.")
    same = (assumptions.income == suggestion.income and assumptions.spending == suggestion.spending)
    return (
        '<section aria-labelledby="projection-suggestion"><h2 id="projection-suggestion">'
        'Suggested from your transactions</h2>'
        f'<p>Basis: <strong>{escape(suggestion.basis)}</strong> · cash and credit-card accounts only · '
        'transfers excluded. Not applied until you choose to.</p>'
        '<div class="table-wrap"><table class="acct-table"><thead><tr><th scope="col">Month</th>'
        '<th scope="col" class="num">Credits</th><th scope="col" class="num">Spending</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
        f'<p>Average monthly credits (not verified income): <strong>{money(suggestion.income)}</strong> · '
        f'Average monthly spending: <strong>{money(suggestion.spending)}</strong></p>'
        + "".join(f'<p class="history-range">{escape(n)}</p>' for n in notes)
        + (f'<p class="history-range">Already saved as your income and spending.</p>' if same else
           f'<form method="post" action="{PATH}"><input type="hidden" name="token" value="{escape(token)}">'
           f'<input type="hidden" name="view" value="{escape(view)}">'
           f'<input type="hidden" name="currency" value="{escape(currency)}">'
           f'<input type="hidden" name="apply" value="suggestion">'
           f'<input type="hidden" name="suggest_income" value="{format(suggestion.income, "f")}">'
           f'<input type="hidden" name="suggest_spending" value="{format(suggestion.spending, "f")}">'
           '<button>Use these as my income and spending</button></form>')
        + '</section>')


def _options(choices, selected):
    return "".join(f'<option value="{escape(key)}"{" selected" if key == selected else ""}>'
                   f'{escape(label)}</option>' for key, label in choices)


def _selector(view, currency, currencies, people):
    tabs = "".join(
        f'<a href="{escape(projection_url(owner))}"'
        + (' aria-current="page"' if owner == view else "") + f'>{escape(owner)}</a>'
        for owner in ("Household", *people)
    )
    choices = _options(((code, code) for code in sorted(currencies)), currency)
    return (f'<div class="top-bar"><nav class="owner-tabs" aria-label="Projection owner view">{tabs}</nav>'
            f'<form class="projection-selector" method="get" action="{PATH}">'
            f'<input type="hidden" name="view" value="{escape(view)}">'
            f'<label>Currency <select name="currency">{choices}</select></label>'
            '<button class="secondary">Show currency</button></form></div>'
            '<p>One currency at a time; no conversion. The default uses the most included accounts '
            '(ties: USD first, then alphabetical). Household includes Unspecified and counts Joint once; '
            'each individual view includes their own and Joint accounts.</p>')


def _form(view, currency, scale, token, values, errors):
    def attrs(name):
        return f' aria-invalid="true" aria-describedby="error-{name}"' if name in errors else ""

    def error(name):
        return f'<small id="error-{name}">{escape(errors[name])}</small>' if name in errors else ""

    def text_field(name, label, inputmode, placeholder=""):
        value = escape(values.get(name, ""))
        return (f'<label>{escape(label)}<input name="{name}" inputmode="{inputmode}" value="{value}"'
                f'{attrs(name)}{placeholder}>{error(name)}</label>')

    summary = ('<p class="notice" role="alert">No assumptions were saved. Fix the marked fields below. '
               'Results still use the previous assumptions.</p>') if errors else ""
    def amount(name, label):
        return text_field(name, label + (" (optional)" if name in OPTIONAL else ""),
                          "decimal" if scale else "numeric", ' placeholder="0"' if name in OPTIONAL else "")

    basic_amounts = "".join(amount(n, AMOUNTS[n]) for n in ("income", "spending"))
    advanced_amounts = "".join(amount(n, AMOUNTS[n]) for n in AMOUNTS if n not in ("income", "spending"))
    rate_fields = "".join(text_field(name, label + " (%)", "decimal") for name, label in RATES.items())
    mode = values.get("mode", "")
    modes = dict(MODES)
    if currency != "USD":
        modes = {"nominal": f"Nominal {currency}", "real": f"Today’s {currency} (inflation adjusted)"}
    if mode not in modes:
        modes = {mode: f"Invalid selection: {mode}", **modes}
    mode_field = (f'<label>Display units<select name="mode"{attrs("mode")}>{_options(modes.items(), mode)}'
                  f'</select>{error("mode")}</label>')
    advanced_names = [n for n in AMOUNTS if n not in ("income", "spending")] + list(RATES) + ["mode"]
    defaults = Assumptions().form_values()

    def non_default(name):
        value = str(values.get(name, "")).strip()
        if name in OPTIONAL:
            default = ""
        else:
            default = defaults[name]
        if value == default or value in ("", "0") and name in OPTIONAL:
            return False
        try:
            return Decimal(value) != Decimal(default or 0)
        except Exception:
            return value != default

    advanced_open = any(n in errors for n in advanced_names) or any(non_default(n) for n in advanced_names)
    return (
        f'<section aria-labelledby="projection-assumptions"><h2 id="projection-assumptions">Assumptions</h2>{summary}'
        f'<form class="edit-form projection-form" method="post" action="{PATH}">'
        f'<input type="hidden" name="token" value="{escape(token)}">'
        f'<input type="hidden" name="view" value="{escape(view)}">'
        f'<input type="hidden" name="currency" value="{escape(currency)}">'
        f'<h3>Monthly budget ({escape(currency)})</h3>'
        '<p>Enter take-home income and spending. Exclude the debt payments and contributions you model in '
        'Advanced from spending so they are not counted twice. Targets use only the surplus; they are not '
        'guaranteed payments, minimums or payroll contributions.</p>'
        f'<div class="projection-fields">{text_field("years", "Horizon in years (1–40)", "numeric")}'
        f'{basic_amounts}</div>'
        f'<p class="history-range">Amounts: nonnegative, up to 15 whole digits and {scale} decimal places, no commas. '
        'Leave optional targets blank for zero.</p>'
        f'<details class="advanced"{" open" if advanced_open else ""}><summary>Advanced: targets, rates, display units</summary>'
        f'<div class="projection-fields">{mode_field}{advanced_amounts}</div>'
        '<h3>Annual rates</h3>'
        '<p class="history-range">APYs and returns: −99% to 100%. Debt APR and inflation: 0% to 100%. '
        'Up to four decimal places.</p>'
        f'<div class="projection-fields">{rate_fields}</div></details>'
        '<button>Save assumptions</button></form></section>'
    )


def _money(value, currency, scale):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        rounded = value.quantize(Decimal(1).scaleb(-scale))
        return f"{escape(currency)} {(rounded if rounded else abs(rounded)):,.{scale}f}"


def _chart(points, money, years):
    values = [point.display.net_worth for point in points]
    low, high = min(values), max(values)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        bottom, top = min(low, 0), max(high, 0)

        def y(value):
            return Decimal(64) if top == bottom else 10 + 108 * (top - value) / (top - bottom)

        coords = " ".join(
            f'{6 + Decimal(588) * i / (len(points) - 1):.2f},{y(value):.2f}'
            for i, value in enumerate(values)
        )
        zero_y = y(0)
    return (
        f'<p class="history-range">Projected monthly range: {money(low)} to {money(high)}. '
        'Dashed line: zero.</p>'
        '<svg class="history-plot" viewBox="0 0 600 128" preserveAspectRatio="none" role="img" '
        'aria-labelledby="projection-chart-title" aria-describedby="projection-chart-desc">'
        '<title id="projection-chart-title">Projected monthly net worth</title>'
        f'<desc id="projection-chart-desc">Starts at {money(values[0])}, ends at {money(values[-1])}. '
        'Dashed line is zero. See annual breakdown for values.</desc>'
        f'<path class="history-grid projection-zero" vector-effect="non-scaling-stroke" d="M6 {zero_y:.2f}H594"/>'
        f'<polyline class="history-trace" vector-effect="non-scaling-stroke" points="{coords}"/>'
        f'</svg><div class="history-dates"><span>Start</span><span>Year {years}</span></div>'
    )


def _cards(start, end, years, metrics, money):
    return '<div class="cards projection-cards">' + "".join(
        f'<div class="card"><small>{label}</small><strong>Start: {money(getattr(start, field))}</strong>'
        f'<div>Year {years}: {money(getattr(end, field))}</div></div>' for field, label in metrics
    ) + '</div>'


def _annual(points, metrics, money, unit_label):
    rows = "".join(
        f'<tr><th scope="row">{"Start" if not p.month else str(p.month // 12)}</th>'
        + "".join(f'<td class="num">{money(getattr(p.display, field))}</td>' for field, _ in metrics)
        + f'<td class="num">{money(p.cumulative_gap)}</td></tr>' for p in points[::12]
    )
    return (
        '<details><summary>Show annual breakdown</summary>'
        '<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable annual breakdown">'
        f'<table class="acct-table"><caption>Year-end balances · {escape(unit_label)}.</caption>'
        '<thead><tr><th scope="col">Year</th>'
        + "".join(f'<th scope="col" class="num">{label}</th>' for _, label in metrics)
        + '<th scope="col" class="num">Unfunded to date (nominal)</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div></details>'
    )


def _debt_note(debt, target, surplus, money):
    if not debt:
        return ""
    service = ('Debt service included in spending does not pay down model debt. '
               'Modeled payments use only positive monthly net savings, not saved cash or HYSA.')
    if not target or surplus <= 0:
        reason = ("the debt payment target is zero" if not target
                  else "monthly net savings are zero or negative")
        return (f'<p class="notice" role="note"><strong>No debt payment is modeled:</strong> {reason}. '
                f'Unpaid debt can grow with interest. {service}</p>')
    if target > surplus:
        return (f'<p class="notice" role="note"><strong>Debt payments can fall below the target:</strong> '
                f'only {money(surplus)} is left each month after spending, before debt payments, '
                f'compared with a {money(target)} target. {service}</p>')
    return f'<p>{service}</p>'


def _panel(view, currency, currencies, people, seed, assumptions, scale, token, *, attempted=None, errors=None,
           suggestion=None):
    money = lambda value: _money(value, currency, scale)
    points = project(seed.balances, assumptions)
    extra_metrics = (("unclassified", "Unclassified assets"),) if seed.unclassified_count else ()
    if seed.count:
        date_range = f'<time>{seed.oldest}</time>'
        if seed.oldest != seed.newest:
            date_range += f' to <time>{seed.newest}</time> (mixed dates)'
        asof = f'<p>Saved balance dates: {date_range}. {seed.count} included account(s).</p>'
        if seed.stale_count:
            asof += (f'<p class="history-backdated">{seed.stale_count} included balance(s) are stale '
                     f'(older than {STALE_DAYS} days).</p>')
    else:
        asof = '<p class="history-backdated">No saved balances in this view. This USD scenario starts from zero.</p>'
    if seed.unclassified_count:
        asof += (f'<p class="history-backdated">{seed.unclassified_count} asset account(s) in sections without '
                 'a modeled asset rate are included in net worth, held flat in nominal terms, '
                 'and not treated as spendable cash.</p>')
    gap = ""
    if points[-1].cumulative_gap:
        first = next(point.month for point in points if point.cumulative_gap)
        gap = (f'<p class="notice" role="note"><strong>Unfunded spending begins in month {first}:</strong> '
               f'{money(points[-1].cumulative_gap)} cumulative by year {assumptions.years} (nominal). '
               'The projection shows account balances, not a funded plan. Unpaid spending is tracked separately '
               'and is not financed, carried forward as debt or deducted from net worth.</p>')
    unit_label = (f"Nominal {currency}" if assumptions.mode == "nominal"
                  else f"Inflation-adjusted {currency} in today’s purchasing power")
    values = assumptions.form_values()
    for name in OPTIONAL:
        if not getattr(assumptions, name):
            values[name] = ""
    surplus = assumptions.surplus
    budget = (f'<p>Monthly take-home income: <strong>{money(assumptions.income)}</strong> · Monthly spending: '
              f'<strong>{money(assumptions.spending)}</strong> · Monthly net savings before modeled allocations '
              f'(income minus spending): <strong>{money(surplus)}</strong> (nominal).</p>')
    debt_note = _debt_note(seed.balances.debt, assumptions.debt_target, surplus, money)
    return (
        _selector(view, currency, currencies, people)
        + '<p class="notice"><strong>Illustrative projection only:</strong> not financial advice or a forecast. '
          'The model uses saved balances; visiting this page does not refresh them.</p>'
        + '<section class="history" aria-labelledby="projection-heading"><h2 id="projection-heading">'
          'Net worth projection</h2>'
        + asof + budget + debt_note + gap + f'<p>{escape(unit_label)}.</p>'
        + _cards(points[0].display, points[-1].display, assumptions.years, CARD_METRICS + extra_metrics, money)
        + _chart(points, money, assumptions.years)
        + _annual(points, ANNUAL_METRICS + extra_metrics, money, unit_label)
        + '</section>'
        + _suggestion_card(view, currency, token, suggestion, assumptions, scale)
        + _form(view, currency, scale, token, attempted if attempted is not None else values, errors or {})
        + '<details><summary>How this projection works</summary>'
          '<p>Start is the sum of included saved balances, even when their dates differ; the scenario does not '
          'estimate intervening activity. The horizon is measured from this starting point.</p>'
          '<p>Checking and Other assets are modeled as cash. Savings-section assets use the HYSA rate. '
          'The model treats both as available to spend, even if a real account has restrictions. '
          'Every debt-side balance is debt, regardless of section.</p>'
          '<p>Each month, interest and growth accrue first (annual effective rates converted geometrically; '
          'debt APR divided by 12). At month-end, surplus goes to debt up to its balance and target, '
          'then retirement and investments up to their targets, then HYSA. Debt accrual ends when paid off. '
          'Deficits draw cash, then HYSA; any remainder is unfunded.</p>'
          '<p>Cash flows and targets are constant in nominal terms. Today’s purchasing power discounts each '
          'point by elapsed-month inflation without changing cash flows or start balances. No taxes, fees, '
          'new borrowing or automatic sales of other assets are modeled. Values round only for display.</p></details>'
    )

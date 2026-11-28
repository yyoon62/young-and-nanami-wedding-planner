"""Server-rendered projection views; caller owns session, Origin and CSRF checks."""
from contextlib import closing
from decimal import Decimal, localcontext
from html import escape
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from dataclasses import replace

from account_terms import VARIABLE_NOTE, load_terms
from debt_engine import compare_orders, month_add as plan_month_add, run_plan, schedule
from asset_inputs import asset_rows, make_assets
from debt_inputs import debt_rows, make_plan
from observed import observed_payments
from projections import (
    AMOUNTS, MODES, OPTIONAL, PRECISION, RATES, STALE_DAYS, Assumptions, available_currencies,
    eligible_accounts, load_assumptions, project, save_assumptions, starting_balances,
    validate_assumptions,
)
from tx_periods import money as tx_money, month_label, suggest_monthly


ZERO_D = Decimal(0)
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
        debts = (debt_rows(db, view, currency), load_terms(db), asset_rows(db, view, currency))
        observed = (observed_payments(db, view, currency, today, [r[0] for r in debts[0]])
                    if suggestions else None)     # transaction-derived: never when hosted
    seed = starting_balances(rows, currency, section_for, today)
    return 200, _page(page, _panel(view, currency, currencies, people, seed, assumptions,
                                  places(currency), token, suggestion=suggestion,
                                  debts=(debts[0], debts[1], debts[2], section_for, today), observed=observed)), None


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
        debts = (debt_rows(db, view, currency), load_terms(db), asset_rows(db, view, currency))
        observed = (observed_payments(db, view, currency, today, [r[0] for r in debts[0]])
                    if suggestions else None)
    seed = starting_balances(rows, currency, section_for, today)
    return 422, _page(page, _panel(view, currency, currencies, people, seed, saved, scale, token,
                                  attempted=data, errors=errors, suggestion=suggestion,
                                  debts=(debts[0], debts[1], debts[2], section_for, today), observed=observed)), None


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


def _pct(value):
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def _when(plan, month):
    return month_label(plan_month_add(plan.start_month, month - 1))


def _compare(plan, a, money):
    """Avalanche vs snowball for the extra payment. States both results; claims a winner only on a real difference."""
    c = compare_orders(plan, a)
    head = '<h3>Avalanche vs snowball</h3>'
    if not c.comparable:
        return f'<div class="compare">{head}<p class="history-range">{escape(c.reason)}</p></div>'

    def payoff_order(run):
        done = sorted(plan.debts, key=lambda d: (run.outcomes[d.key].payoff_month, d.key))
        return ", ".join(f"{escape(d.name)} ({escape(_when(plan, run.outcomes[d.key].payoff_month))})" for d in done)

    def row(label, run, note=""):
        return (f'<tr><th scope="row">{label}</th><td data-label="Debt-free"><div class="cell">'
                f'{escape(_when(plan, run.last_payoff_month))} <span class="acct-sub">(month {run.last_payoff_month})</span></div></td>'
                f'<td class="num" data-label="Total interest">{money(run.total_interest)}</td>'
                f'<td data-label="Payoff order"><div class="cell">{payoff_order(run)}{note}</div></td></tr>')

    ava, snow = c.avalanche, c.snowball
    if c.winner:
        other = "snowball" if c.winner == "avalanche" else "avalanche"
        verdict = (f'<p><strong>Lower total interest: {c.winner}, by {money(c.interest_difference)}</strong> '
                   f'(compared with {other}). Debt-free dates: avalanche {escape(_when(plan, ava.last_payoff_month))}, '
                   f'snowball {escape(_when(plan, snow.last_payoff_month))}.</p>')
    elif ava.last_payoff_month == snow.last_payoff_month:
        verdict = '<p><strong>No difference:</strong> same total interest and the same debt-free date.</p>'
    else:
        verdict = (f'<p><strong>Same total interest;</strong> the debt-free dates differ (avalanche '
                   f'{escape(_when(plan, ava.last_payoff_month))}, snowball {escape(_when(plan, snow.last_payoff_month))}).</p>')
    return (f'<div class="compare">{head}'
            f'<p class="history-range">Extra payment {money(ava.extra)} a month on top of planned payments, same in both '
            'orders. Avalanche: highest current rate first (re-picked each month, so a promo ending changes the target); '
            'snowball: smallest balance first. A paid-off debt’s planned payment rolls over. Neither order is assumed to '
            'win: both are computed.</p>'
            '<div class="table-wrap stack-table" tabindex="0" role="region" aria-label="Scrollable payoff order comparison">'
            '<table class="acct-table"><thead><tr><th scope="col">Order</th><th scope="col">Debt-free</th>'
            '<th scope="col" class="num">Total interest</th><th scope="col">Payoff order</th></tr></thead><tbody>'
            + row("Avalanche", ava) + row("Snowball", snow)
            + row("Planned payments only (no extra)", c.planned_only) + '</tbody></table></div>' + verdict + '</div>')


def _observed_line(obs, plan, money):
    """Observed payments beside the plan; only called when transaction-derived data is allowed (not hosted)."""
    if obs is None:
        return ""
    if obs.average is None:
        return f'<div class="acct-sub">Observed: {escape(obs.reason)}</div>'
    if not any(obs.values.values()):
        text = "no observed payments found (loan payments are only counted when marked as transfers)"
    else:
        by_month = " · ".join(f"{escape(month_label(m)[:3])} {money(v)}" for m, v in obs.values.items())
        text = (f'{money(obs.average)} <strong>avg of {len(obs.months)} month{"s" if len(obs.months) != 1 else ""}</strong>'
                f' ({by_month}); shown for comparison, not used in the projection')
    more = (f'; {obs.unconfirmed} payment-like credit(s) not confirmed' if obs.unconfirmed else "")
    return f'<div class="acct-sub">Observed: {text}{more}</div>'


def _asset_section(assets, points, a, currency, money):
    """Cash and savings accounts: own APY where set, "rate missing" (blended assumption) where not."""
    if not assets:
        return ""
    own = {x.key for x in assets if x.apy is not None}
    year1 = dict(points[min(12, len(points) - 1)].own)
    last = dict(points[-1].own)
    rows = []
    for x in assets:
        if x.apy is not None:
            note = "assumes today’s rate holds" if x.rate_kind == "variable" else "fixed"
            rate = f'{escape(_pct(x.apy))} APY <div class="acct-sub">{note}</div>'
            after1, afterN = money(year1[x.key]), money(last[x.key])
        else:
            blended = a.hysa_apy if x.bucket == "hysa" else a.checking_apy
            flag = ('<span class="rate-flag">rate missing</span>' if x.bucket == "hysa" else
                    '<span class="acct-sub">no rate set</span>')
            rate = f'{flag}<div class="acct-sub">blended {escape(_pct(blended))} assumed</div>'
            after1 = afterN = "pooled"
        stale = ' <span class="stale-pill">Stale</span>' if x.stale else ""
        rows.append(f'<tr><th scope="row">{escape(x.name)}<div class="acct-sub">{escape(x.institution)}</div></th>'
                    f'<td class="num" data-label="Balance"><div class="cell">{money(x.balance)}<div class="acct-sub">as of '
                    f'<time>{escape(x.asof)}</time>{stale}</div></div></td>'
                    f'<td data-label="Rate"><div class="cell">{rate}</div></td>'
                    f'<td class="num" data-label="After 1 year">{after1}</td>'
                    f'<td class="num" data-label="After {a.years} years">{afterN}</td></tr>')
    pooled = len(assets) - len(own)
    note = (f'<p class="history-range">Accounts with an APY grow at their own rate. {pooled} without one are pooled at the '
            'blended assumptions (Advanced); new savings from monthly net savings also earn the blended savings rate. '
            'A shortfall draws cash first, then savings, lowest APY first.</p>')
    return ('<section class="history" aria-labelledby="asset-heading"><h2 id="asset-heading">Cash and savings growth</h2>'
            + note + '<div class="table-wrap stack-table" tabindex="0" role="region" aria-label="Scrollable cash and savings table">'
            '<table class="acct-table"><thead><tr><th scope="col">Account</th><th scope="col" class="num">Balance</th>'
            '<th scope="col">Rate</th><th scope="col" class="num">After 1 year</th>'
            f'<th scope="col" class="num">After {a.years} years</th></tr></thead><tbody>' + "".join(rows)
            + '</tbody></table></div><p class="history-range">Nominal balances after growth and any withdrawals.</p></section>')


def _debt_section(plan, entries, run, a, currency, scale, money, observed=None):
    """Per-debt table, flags and schedules; every number comes from the same `debt_engine` as the chart."""
    if not entries:
        return ""
    parts = ['<section class="history" aria-labelledby="debt-heading"><h2 id="debt-heading">Debts</h2>',
             f'<p class="history-range">{escape(currency)} only. Interest is rounded to the cent each month, then the '
             f'payment is applied at month-end. {escape(VARIABLE_NOTE)} Balances are the saved, dated snapshots; '
             'nothing here refreshes them.</p>']
    flat = [e for e in entries if e.kind == "float"]
    pool = [e for e in entries if e.kind == "pool"]
    modeled = [e for e in entries if e.kind == "modeled"]
    if flat:
        names = ", ".join(escape(e.name) for e in flat)
        parts.append(f'<p class="notice" role="note"><strong>{len(flat)} card(s) assumed paid in full:</strong> balance '
                     f'{money(plan.float_debt)} held flat, no interest ({names}). Their monthly purchases are in your '
                     'spending; set a rate on Accounts and untick “paid in full” for a card that carries a balance.</p>')
    if pool:
        names = ", ".join(escape(e.name) for e in pool)
        parts.append(f'<p class="notice" role="note"><strong>Rate missing for {len(pool)} debt(s):</strong> {money(plan.pool)} '
                     f'({names}) uses the blended {escape(_pct(a.debt_apr))} APR and the debt payment target, as before. '
                     'Add the rate in the account’s edit drawer on Accounts.</p>')
    if plan.planned_total > a.surplus:
        parts.append(f'<p class="notice" role="note"><strong>Planned payments ({money(plan.planned_total)} a month) exceed '
                     f'monthly net savings ({money(a.surplus)}).</strong> They are still paid; the shortfall is drawn from '
                     'cash, then savings, and counted as unfunded when those run out.</p>')
    if modeled:
        rows, details = [], []
        interest_now = ZERO_D
        for e in modeled:
            d = e.debt
            sched = schedule(d, plan.start_month, plan.places)
            out = run.outcomes[d.key]
            interest_now += sched.first_interest
            stale = ' <span class="stale-pill">Stale</span>' if e.stale else ""
            rate = f'{escape(_pct(d.rate))} APR · {d.rate_kind}'
            if d.rate_kind == "variable":
                rate += " (assumes today’s rate holds)"
            if d.promo_rate is not None and d.promo_end:
                if d.promo_end < plan.start_month:
                    rate += f'<div class="acct-sub">Promo ended {escape(month_label(d.promo_end))}; regular rate applies.</div>'
                else:
                    rate += (f'<div class="acct-sub">{escape(_pct(d.promo_rate))} promo through '
                             f'{escape(month_label(d.promo_end))}, then {escape(_pct(d.rate))}</div>')
            source = ("planned" if e.terms.planned_payment is not None else "minimum")
            with_extra = run.extra > 0
            label = f"{source} + extra" if with_extra else f"({source})"
            pay = money(d.payment) + f' <span class="acct-sub">{label}</span>' if d.payment else (
                '<span class="rate-flag">payment missing</span>')
            if observed is not None:
                pay += _observed_line(observed.get(d.key), plan, money)
            if out.payoff_month:
                payoff = f'{escape(_when(plan, out.payoff_month))} <span class="acct-sub">(month {out.payoff_month})</span>'
                total = money(out.total_interest)
            elif sched.status in ("does_not_cover", "no_payment"):
                payoff, total = '<strong>No payoff date</strong>', "—"
            else:
                payoff, total = 'Not paid off within 40 years', "—"
            if with_extra:
                # The extra payment changes the date and the interest: show the planned-only figures beside them.
                if sched.status == "paid_off":
                    p_payoff = (f'{escape(_when(plan, sched.payoff_month))} '
                                f'<span class="acct-sub">(month {sched.payoff_month})</span>')
                    p_total = money(sched.total_interest)
                elif sched.status == "beyond_cap":
                    p_payoff, p_total = "Not paid off within 40 years", "—"
                else:
                    p_payoff, p_total = "<strong>No payoff date</strong>", "—"
                two = lambda with_, planned_: (f'<div class="pair"><span class="acct-sub">With extra:</span> {with_}</div>'
                                               f'<div class="pair"><span class="acct-sub">Planned only:</span> {planned_}</div>')
                payoff, total = two(payoff, p_payoff), two(total, p_total)
            notes = []
            if sched.status == "does_not_cover":
                notes.append(f'<span class="rate-flag">payment does not cover interest</span> '
                             f'<span class="acct-sub">(interest {money(sched.rows[-1].interest)} a month from month '
                             f'{sched.stalled_from} vs payment {money(d.payment)}; the balance grows)</span>')
            elif sched.status == "no_payment":
                notes.append('<span class="acct-sub">Add a planned or minimum payment on Accounts; only extra payments reduce it.</span>')
            if e.stale:
                notes.append('<span class="acct-sub">Balance is stale; the schedule starts from it anyway.</span>')
            note_html = "".join(f'<div class="acct-sub debt-note">{n}</div>' for n in notes)
            rows.append(
                f'<tr><th scope="row">{escape(e.name)}<div class="acct-sub">{escape(e.institution)}</div>{note_html}</th>'
                f'<td class="num" data-label="Balance"><div class="cell">{money(d.balance)}<div class="acct-sub">as of <time>{escape(e.asof)}</time>{stale}</div></div></td>'
                f'<td data-label="Rate"><div class="cell">{rate}</div></td><td class="pay" data-label="Payment"><div class="cell">{pay}</div></td>'
                f'<td data-label="Payoff"><div class="cell">{payoff}</div></td><td class="num" data-label="Total interest"><div class="cell">{total}</div></td>'
                f'<td class="num" data-label="Interest this month">{money(sched.first_interest)}</td></tr>')
            if sched.rows:
                shown = sched.rows[:120]
                cell = lambda v: f'<span class="cur">{escape(currency)} </span>{money(v).split(" ", 1)[1]}'
                body = "".join(
                    f'<tr><th scope="row">{r.month}<div class="acct-sub">{escape(month_label(r.calendar))}</div></th>'
                    f'<td class="num">{escape(_pct(r.rate))}</td><td class="num">{cell(r.interest)}</td>'
                    f'<td class="num">{cell(r.payment)}</td><td class="num">{cell(r.balance)}</td></tr>'
                    + (f'<tr class="rate-change"><td colspan="5">Month {r.month + 1}: rate changes to {escape(_pct(d.rate))} '
                       f'(promo ended {escape(month_label(d.promo_end))})</td></tr>'
                       if sched.promo_ended_month == r.month + 1 else "")
                    for r in shown)
                more = (f'<p class="history-range">First 120 of {len(sched.rows)} months shown.</p>'
                        if len(sched.rows) > len(shown) else "")
                details.append(
                    f'<details><summary>Schedule: {escape(e.name)} (planned payment only)</summary>'
                    '<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable debt schedule">'
                    f'<table class="acct-table sched"><caption>Amounts in {escape(currency)}; month 1 is the current month.</caption>'
                    '<thead><tr><th scope="col">Month</th>'
                    '<th scope="col" class="num">Rate</th><th scope="col" class="num">Interest</th>'
                    '<th scope="col" class="num">Payment</th><th scope="col" class="num">Balance after</th></tr></thead>'
                    f'<tbody>{body}</tbody></table></div>{more}</details>')
        extra = (f'<p class="history-range">Extra payments of {money(run.extra)} a month (debt target beyond planned '
                 'payments, from positive net savings) go to debts without a rate first, then the highest current rate; '
                 'a paid-off debt’s payment rolls over. The table above shows each debt with extra and planned only.</p>') if run.extra > 0 else ""
        parts.append(
            '<div class="table-wrap debt-table stack-table" tabindex="0" role="region" aria-label="Scrollable debt table">'
            '<table class="acct-table"><thead><tr><th scope="col">Account</th><th scope="col" class="num">Balance</th>'
            '<th scope="col">Rate</th><th scope="col" class="pay">Payment</th><th scope="col">Payoff</th>'
            '<th scope="col" class="num">Total interest</th><th scope="col" class="num">Interest this month</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            f'<p>Current monthly interest cost of these debts: <strong>{money(interest_now)}</strong> '
            '(at today’s rates, before payments).</p>' + extra + _compare(plan, a, money) + "".join(details))
    parts.append('</section>')
    return "".join(parts)


def _panel(view, currency, currencies, people, seed, assumptions, scale, token, *, attempted=None, errors=None,
           suggestion=None, debts=None, observed=None):
    money = lambda value: _money(value, currency, scale)
    plan = entries = run = None
    if debts is not None:
        rows, terms, arows, section_for, today = debts
        plan, entries = make_plan(rows, terms, section_for, scale, today)
        run = run_plan(plan, assumptions)
        assets = make_assets(arows, terms, section_for, today)
    else:
        assets = ()
    points = project(seed.balances, assumptions, plan, assets)
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
    debt_note = _debt_note(seed.balances.debt if plan is None else plan.pool, assumptions.debt_target, surplus, money)
    debt_section = _debt_section(plan, entries, run, assumptions, currency, scale, money, observed) if plan is not None else ""
    return (
        _selector(view, currency, currencies, people)
        + '<p class="notice"><strong>Illustrative projection only:</strong> not financial advice or a forecast. '
          'The model uses saved balances; visiting this page does not refresh them.</p>'
        + _suggestion_card(view, currency, token, suggestion, assumptions, scale)
        + '<section class="history" aria-labelledby="projection-heading"><h2 id="projection-heading">'
          'Net worth projection</h2>'
        + asof + budget + debt_note + gap + f'<p>{escape(unit_label)}.</p>'
        + _cards(points[0].display, points[-1].display, assumptions.years, CARD_METRICS + extra_metrics, money)
        + _chart(points, money, assumptions.years)
        + _annual(points, ANNUAL_METRICS + extra_metrics, money, unit_label)
        + '</section>'
        + debt_section
        + _asset_section(assets, points, assumptions, currency, money)
        + _form(view, currency, scale, token, attempted if attempted is not None else values, errors or {})
        + '<details><summary>How this projection works</summary>'
          '<p>Start is the sum of included saved balances, even when their dates differ; the scenario does not '
          'estimate intervening activity. The horizon is measured from this starting point.</p>'
          '<p>Checking and Other assets are modeled as cash. Savings-section assets use the HYSA rate. '
          'The model treats both as available to spend, even if a real account has restrictions. '
          'Every debt-side balance is debt, regardless of section.</p>'
          '<p>Each month, interest and growth accrue first (annual effective rates converted geometrically; '
          'debt APR divided by 12). Debts with a rate in Accounts follow their own schedule instead (below). At month-end, surplus goes to debt up to its balance and target, '
          'then retirement and investments up to their targets, then HYSA. Debt accrual ends when paid off. '
          'Deficits draw cash, then HYSA; any remainder is unfunded.</p>'
          '<p>Cash flows and targets are constant in nominal terms. Today’s purchasing power discounts each '
          'point by elapsed-month inflation without changing cash flows or start balances. No taxes, fees, '
          'new borrowing or automatic sales of other assets are modeled. Values round only for display.</p></details>'
    )

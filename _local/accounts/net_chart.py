"""Projection tab chart: dated saved history (left, compressed) and the projected curve (right) on one shared y-scale.

Honest by construction: the left zone is calendar-spaced steps at saved dates only (no invented points); a break mark
at the history's last date says the time scale changes; the projection starts at ITS OWN start value; if that differs
from the last saved total (a backdated edit, a currency change) a labelled connector shows the difference.
"""
from datetime import date
from decimal import Decimal, localcontext
from html import escape

PRECISION = 64
LEFT, RIGHT, TOP, BOTTOM = 6, 594, 10, 118       # viewBox 600 x 128, same frame as the History plots
SAVED_ZONE = 160                                  # viewBox units for history; style.css keeps the same ratio (160/588)
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def plain(value, currency, scale):
    """$1,234.56 / €3,000.00 / ¥150,000; other currencies keep their code. Text added by L29 only."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        rounded = value.quantize(Decimal(1).scaleb(-scale))
        body = f"{abs(rounded):,.{scale}f}"
    sign = "-" if rounded < 0 else ""
    symbol = SYMBOLS.get(currency)
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{currency} {body}"


def short_date(iso):
    day = date.fromisoformat(iso)
    return f"{day.day} {day:%b}"


def _steps(known, x_of, y_of):
    """Step path over the series; a gap (None) ends the run, never connected across absence. Dots at saved dates."""
    steps, dots, in_run, last_y = [], [], False, None
    for asof, amount in known:
        x = x_of(asof)
        if amount is None:
            if in_run:
                steps.append(f"H{x:.2f}")
            in_run = False
            continue
        y = y_of(amount)
        steps.append(f"H{x:.2f} V{y:.2f}" if in_run else f"M{x:.2f} {y:.2f}")
        dots.append(f"M{x:.2f} {y:.2f}h0")
        in_run, last_y = True, y
    return " ".join(steps), " ".join(dots)


def render(points, years, series, currency, scale, money, *, backdated_note=True):
    """points: projection Points; series: [(asof, Decimal|None)] saved history for this currency, or None if unknown."""
    projected = [p.display.net_worth for p in points]
    start = projected[0]
    known = [(d, v) for d, v in (series or [])]
    values_known = [v for _, v in known if v is not None]
    draw_past = len(known) >= 2 and len(values_known) >= 1
    with localcontext() as ctx:
        ctx.prec = PRECISION
        all_values = projected + values_known * draw_past
        low, high = min(all_values), max(all_values)
        bottom, top = min(low, 0), max(high, 0)

        def y_of(value):
            return Decimal(64) if top == bottom else TOP + Decimal(BOTTOM - TOP) * (top - value) / (top - bottom)

        join = LEFT + (SAVED_ZONE if draw_past else 0)           # x of the latest saved date = projected month 0
        width = RIGHT - join
        n = len(points) - 1
        coords = " ".join(f"{join + Decimal(width) * i / n:.2f},{y_of(v):.2f}" for i, v in enumerate(projected))
        zero_y = y_of(Decimal(0))
        past = connector = ""
        step_note = ""
        if draw_past:
            d0 = date.fromisoformat(known[0][0]).toordinal()
            span = date.fromisoformat(known[-1][0]).toordinal() - d0
            x_of = lambda iso: LEFT + Decimal(SAVED_ZONE) * (date.fromisoformat(iso).toordinal() - d0) / span
            steps, dots = _steps(known, x_of, y_of)
            last_date, last_value = known[-1]
            past = (f'<path class="history-trace nw-saved" vector-effect="non-scaling-stroke" d="{steps}"/>'
                    f'<path class="history-dots nw-saved" vector-effect="non-scaling-stroke" d="{dots}"/>')
            if last_value is not None and last_value != start:
                connector = (f'<path class="nw-step" vector-effect="non-scaling-stroke" d="M{join:.2f} {y_of(last_value):.2f}'
                             f'V{y_of(start):.2f}"><title>Saved total and projection start differ</title></path>'
                             f'<path class="nw-stepdot" vector-effect="non-scaling-stroke" d="M{join:.2f} {y_of(start):.2f}h0"/>')
                gap = start - last_value
                step_note = (f'<p class="history-backdated nw-stepnote">⚠️ Saved history ends at '
                             f'<strong>{escape(plain(last_value, currency, scale))}</strong> on {last_date}; current balances '
                             f'start the projection at <strong>{escape(plain(start, currency, scale))}</strong> '
                             f'({escape(plain(gap, currency, scale)) if gap < 0 else "+" + escape(plain(gap, currency, scale))}). '
                             'Drawn as a step, nothing shifted'
                             + ('. After a backdated edit, history can show later saved balances than the account table.'
                                if backdated_note else '.') + '</p>')
            elif last_value is None:
                step_note = (f'<p class="history-range">No included saved total on {last_date}; '
                             'the projection starts from current balances.</p>')
            split = (f'<path class="nw-split" vector-effect="non-scaling-stroke" d="M{join} 4V{BOTTOM + 6}"/>'
                     f'<path class="nw-break" vector-effect="non-scaling-stroke" d="M{join - 5} {BOTTOM + 8}l4 -8 M{join + 1} {BOTTOM + 8}l4 -8"/>')
        else:
            split = f'<path class="history-dots nw-start" vector-effect="non-scaling-stroke" d="M{join} {y_of(start):.2f}h0"/>'
        summary = (f"Saved net worth ends at {escape(plain(values_known[-1], currency, scale))}. "
                   if draw_past and values_known and known[-1][1] is not None else "")
    desc = (f"{summary}Projection starts at {escape(plain(start, currency, scale))} and ends at "
            f"{escape(plain(projected[-1], currency, scale))} after {years} years. Dotted line is zero. "
            "See the annual breakdown for values.")
    svg = ('<svg class="history-plot nw-plot" viewBox="0 0 600 128" preserveAspectRatio="none" role="img" '
           'aria-labelledby="projection-chart-title" aria-describedby="projection-chart-desc">'
           '<title id="projection-chart-title">Net worth: saved history and projection</title>'
           f'<desc id="projection-chart-desc">{desc}</desc>'
           f'<path class="history-grid nw-zero" vector-effect="non-scaling-stroke" d="M{LEFT} {zero_y:.2f}H{RIGHT}"/>'
           f'{past}{connector}{split}'
           f'<polyline class="history-trace nw-projected" vector-effect="non-scaling-stroke" points="{coords}"/></svg>')
    if draw_past:
        axis = ('<div class="nw-axis" aria-hidden="true">'
                f'<span class="nw-cell"><time datetime="{known[0][0]}" title="{known[0][0]}">{short_date(known[0][0])}</time>'
                f'<time datetime="{known[-1][0]}" title="{known[-1][0]}">{short_date(known[-1][0])}</time></span>'
                f'<span class="nw-cell"><span></span><span>Year {years}</span></span>'
                '<span class="nw-zone nw-zone-saved">Saved</span><span class="nw-zone nw-zone-proj">Projected</span></div>')
        too_short = ""
    else:
        axis = ('<div class="nw-axis nw-axis-solo" aria-hidden="true"><span class="nw-cell"><span>Start</span>'
                f'<span>Year {years}</span></span><span class="nw-zone nw-zone-proj">Projected</span></div>')
        too_short = ('<p class="history-range">📅 Saved history is too short to draw (it needs two dated totals); '
                     'the projection starts from the saved balances.</p>' if series is not None else "")
    low_d, high_d = min(projected), max(projected)
    caption = (f'<p class="history-range">Projected monthly range: {money(low_d)} to {money(high_d)}. '
               'Dotted line: zero. Left of the break mark the time scale is compressed.</p>' if draw_past else
               f'<p class="history-range">Projected monthly range: {money(low_d)} to {money(high_d)}. Dotted line: zero.</p>')
    return svg + axis + caption + too_short + step_note

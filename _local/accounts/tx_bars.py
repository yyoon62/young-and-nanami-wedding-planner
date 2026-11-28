"""Spending by category as ONE stacked bar plus a legend of filter chips (L26 part 8). Pure functions.

Each category has a stable colour class (c0..c15): built-in categories keep fixed slots, any other name is hashed
into the same palette, "Uncategorized" is its own grey. The colours live in style.css. Amounts are exact Decimal;
shares are computed from the exact amounts and rounded only for display, so they can differ from 100 by a point.
"""
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from zlib import crc32

from tx_categories import BUILTIN, default_emoji
from tx_money import fmt, fmt_whole

SLOTS = {name.casefold(): i for i, name in enumerate(BUILTIN)}
PALETTE_SIZE = 16
UNCATEGORIZED = "Uncategorized"
CURRENT = ' aria-current="true"'
WIDTH = 1000          # bar width in SVG user units (one unit = 0.1 %)


def color_class(name):
    if name.casefold() == UNCATEGORIZED.casefold():
        return "cu"
    slot = SLOTS.get(name.casefold())
    if slot is None:
        slot = crc32(name.casefold().encode()) % PALETTE_SIZE
    return f"c{slot % PALETTE_SIZE}"


def ordered(current):
    return sorted(current.items(), key=lambda kv: (-kv[1], kv[0].casefold()))


def share_text(amount, total):
    """'12%', or '<1%' for a nonzero share that rounds to zero."""
    if not total:
        return "0%"
    pct = (amount * 100 / total).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"{pct}%" if pct or not amount else "<1%"


def delta_text(now, before, currency):
    if before is None:
        return "no data"
    diff = now - before
    if diff == 0:
        return f"{fmt(before, currency)} (same)"
    return f"{fmt(before, currency)} ({'▲' if diff > 0 else '▼'} {fmt(abs(diff), currency)})"


def emoji_of(name):
    return default_emoji("" if name.casefold() == UNCATEGORIZED.casefold() else name)


def tooltip(name, amount, total, currency, prev, avg, prev_label, avg_label):
    parts = [f"{name}: {fmt(amount, currency)}", f"{share_text(amount, total)} of spending"]
    if prev_label:
        parts.append(f"{prev_label}: {delta_text(amount, None if prev is None else prev.get(name, Decimal(0)), currency)}")
    if avg_label:
        parts.append(f"{avg_label}: {delta_text(amount, None if avg is None else avg.get(name, Decimal(0)), currency)}")
    return " · ".join(parts)


def stacked_bar(current, currency):
    """The one bar: an SVG of adjacent rects (presentation attributes only: the CSP has no inline styles)."""
    total = sum(current.values(), Decimal(0))
    rects, edge = [], Decimal(0)
    for name, amount in ordered(current):
        start = edge
        edge += amount
        x0 = (start * WIDTH / total).quantize(Decimal("0.01"))
        x1 = WIDTH if edge == total else (edge * WIDTH / total).quantize(Decimal("0.01"))
        rects.append(f'<rect class="seg {color_class(name)}" x="{x0}" width="{x1 - x0}" height="14">'
                     f'<title>{escape(name)}: {fmt(amount, currency)} · {share_text(amount, total)}</title></rect>')
    return (f'<svg class="stack" viewBox="0 0 {WIDTH} 14" preserveAspectRatio="none" role="img" '
            f'aria-label="Spending by category, {escape(currency)}">{"".join(rects)}</svg>')


def legend_chip(name, amount, total, currency, href, active, tip):
    return (f'<a class="tx-leg {color_class(name)}{" is-active" if active else ""}" href="{escape(href)}"'
            f'{CURRENT if active else ""} title="{escape(tip)}">'
            f'<i class="dot" aria-hidden="true"></i>{emoji_of(name)} {escape(name)} '
            f'<span class="amt">{fmt_whole(amount, currency)}</span> · {share_text(amount, total)}</a>')

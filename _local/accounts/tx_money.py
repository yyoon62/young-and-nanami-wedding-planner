"""Display money for /transactions (L26): symbol + thousands separators, exact Decimal, half-up to the
currency's places. New helpers only: tx_periods.money (used by Projections) is untouched.

Only unambiguous symbols are used ($ USD, € EUR, £ GBP, ¥ JPY); every other currency keeps its ISO code as a
prefix. Currencies are never summed here: each call formats one amount in one currency.
"""
from decimal import ROUND_HALF_UP, Decimal

from tx_periods import places, quantize

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def fmt(value, currency, *, absolute=False):
    """$1,234.56 / -$1,234.56 / ¥1,235 / CHF 1,234.56; display rounding only."""
    q = quantize(Decimal(value), currency)
    digits = f"{abs(q):,.{places(currency)}f}"
    symbol = SYMBOLS.get(currency)
    body = f"{symbol}{digits}" if symbol else f"{currency}\u00a0{digits}"
    return "-" + body if q < 0 and not absolute else body


def sign_of(value, currency):
    """-1, 0 or 1 of the amount AS DISPLAYED (so a label never disagrees with its number)."""
    q = quantize(Decimal(value), currency)
    return (q > 0) - (q < 0)


def fmt_whole(value, currency):
    """$1,235: whole units for compact chips (display rounding only; tooltips keep the exact amount)."""
    q = Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    symbol = SYMBOLS.get(currency)
    digits = f"{abs(q):,}"
    body = f"{symbol}{digits}" if symbol else f"{currency}\u00a0{digits}"
    return "-" + body if q < 0 else body

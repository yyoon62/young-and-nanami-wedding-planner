"""Dated account snapshots and server-rendered, per-currency net worth."""
from datetime import date
from decimal import Decimal, localcontext
from html import escape
from itertools import groupby


def install(db):
    """Run inside the accounts migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS balance_history (
        account_id INTEGER NOT NULL, asof TEXT NOT NULL, balance TEXT NOT NULL,
        currency TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE(account_id, asof))""")
    db.execute("""INSERT INTO balance_history (account_id, asof, balance, currency, side)
        SELECT id, asof, balance, currency, side FROM accounts WHERE true
        ON CONFLICT(account_id, asof) DO NOTHING""")

    # Capture in the same transaction as every account writer (including sync).
    # Metadata-only changes and unchanged syncs do not touch recorded_at.
    for name, event in (
        ("insert", "AFTER INSERT ON accounts"),
        ("update", """AFTER UPDATE OF asof, balance, currency, side ON accounts
            WHEN OLD.asof <> NEW.asof OR OLD.balance <> NEW.balance
              OR OLD.currency <> NEW.currency OR OLD.side <> NEW.side"""),
    ):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS balance_history_{name} {event}
            BEGIN
                INSERT INTO balance_history (account_id, asof, balance, currency, side)
                VALUES (NEW.id, NEW.asof, NEW.balance, NEW.currency, NEW.side)
                ON CONFLICT(account_id, asof) DO UPDATE SET balance=excluded.balance,
                    currency=excluded.currency, side=excluded.side, recorded_at=excluded.recorded_at;
            END""")
    # Use a trigger rather than connection-local PRAGMA foreign_keys so cleanup
    # also holds for other connections to this local database.
    db.execute("""CREATE TRIGGER IF NOT EXISTS balance_history_delete AFTER DELETE ON accounts
        BEGIN DELETE FROM balance_history WHERE account_id=OLD.id; END""")


def net_worth(points):
    """Build totals at observed dates from points sorted by asof (ISO calendar date).

    The caller scopes accounts using current owner/exclusion settings. No account
    contributes before its first point; only its latest point as of each date counts.
    """
    previous, totals, counts, timeline = {}, {}, {}, []
    with localcontext() as ctx:
        ctx.prec = 48  # 15 whole + 4 fractional digits per account, up to 19-digit SQLite IDs.
        for asof, rows in groupby(points, key=lambda r: r["asof"]):
            for row in rows:
                account_id, currency = row["account_id"], row["currency"]
                amount = Decimal(row["balance"])
                if row["side"] == "debt":
                    amount = -amount
                if account_id in previous:
                    old_currency, old_amount = previous[account_id]
                    counts[old_currency] -= 1
                    if counts[old_currency]:
                        totals[old_currency] -= old_amount
                    else:
                        del totals[old_currency]
                previous[account_id] = currency, amount
                counts[currency] = counts.get(currency, 0) + 1
                totals[currency] = totals.get(currency, Decimal(0)) + amount
            timeline.append((asof, totals.copy()))
    return timeline


def sparkline(series, label):
    """Calendar-spaced steps and isolated points; never connect across absence."""
    start = date.fromisoformat(series[0][0]).toordinal()
    span = date.fromisoformat(series[-1][0]).toordinal() - start
    values = [amount for _, amount in series if amount is not None]
    low, high = min(values), max(values)
    steps, dots = [], []
    in_run = False
    with localcontext() as ctx:
        ctx.prec = 48
        for asof, amount in series:
            x = 6 + Decimal(588) * (date.fromisoformat(asof).toordinal() - start) / span
            if amount is None:
                if in_run:
                    steps.append(f"H{x:.2f}")  # Carry until this date; no value or dot at the boundary.
                in_run = False
                continue
            y = 64 if low == high else 10 + 108 * (high - amount) / (high - low)
            steps.append(f"H{x:.2f} V{y:.2f}" if in_run else f"M{x:.2f} {y:.2f}")
            dots.append(f"M{x:.2f} {y:.2f}h0")
            in_run = True
    return (f'<svg class="history-plot" viewBox="0 0 600 128" preserveAspectRatio="none" '
            f'role="img" aria-label="{label} net worth trend. Exact values in dated totals.">'
            f'<path class="history-grid" vector-effect="non-scaling-stroke" d="M6 10H594 M6 64H594 M6 118H594"/>'
            f'<path class="history-trace" vector-effect="non-scaling-stroke" d="{" ".join(steps)}"/>'
            f'<path class="history-dots" vector-effect="non-scaling-stroke" d="{" ".join(dots)}"/>'
            f'</svg><div class="history-dates"><time>{series[0][0]}</time><time>{series[-1][0]}</time></div>')


def history_panel(timeline, money, *, has_backdated=False):
    intro = (
        '<section class="history" aria-labelledby="history-heading"><h2 id="history-heading">Net worth history</h2>'
        '<p>Past totals use today’s owners and exclusions. Each account starts at its first saved date; '
        'its last saved balance, currency and asset/debt side carry forward until its next snapshot. '
        'Currencies are shown separately, without conversion. Gaps mean no included balance in that currency.</p>'
    )
    if not timeline:
        return intro + '<p>No dated balances in this view yet.</p></section>'
    if has_backdated:
        intro += ('<p class="history-backdated">After a backdated edit, history can show later saved balances '
                  'than the account table.</p>')

    first_seen = {}
    for i, (_, totals) in enumerate(timeline):
        for currency in totals:
            first_seen.setdefault(currency, i)

    panels = []
    for currency, first in sorted(first_seen.items()):
        label = escape(currency)
        series = [(asof, totals.get(currency)) for asof, totals in timeline[first:]]
        values = [amount for _, amount in series if amount is not None]
        last_date, last_amount = series[-1]
        latest = (f'<strong>{escape(money(last_amount, currency))}</strong> as of <time>{last_date}</time>'
                  if last_amount is not None else f'No included balance on <time>{last_date}</time>')
        if len(values) >= 2:
            plot = sparkline(series, label)
            hi, lo = escape(money(max(values), currency)), escape(money(min(values), currency))
            if lo != hi:
                range_label = f'Range: {lo} to {hi}'
            else:
                range_label = f'Flat at {lo}' if len(values) == len(series) else f'Recorded value: {lo}'
            plot = f'<p class="history-range">{range_label}</p>{plot}'
        else:
            plot = '<p class="history-range">Only one dated total; no trend yet.</p>'
        rows = "".join(
            f'<tr><th scope="row"><time>{asof}</time></th>'
            f'<td>{escape(money(amount, currency)) if amount is not None else "No included balance"}</td></tr>'
            for asof, amount in series
        )
        panels.append(
            f'<section class="history-series"><h3>{label} net worth</h3><p class="history-latest">{latest}</p>'
            f'{plot}<details><summary>Show {label} dated totals</summary>'
            f'<table class="history-table"><caption>{label} net worth by saved date</caption>'
            f'<thead><tr><th scope="col">As of</th><th scope="col">Net worth</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></details></section>'
        )
    return intro + '<div class="history-series-group">' + "".join(panels) + "</div></section>"

"""Batch balances presentation. Scannable dated rows; explicit, nothing silent.

Markup-only. Backend, CSRF token and form field names are unchanged.
Cheapest accessible disclosure: native <details>/<summary> (no scripts, keyboard
and screen-reader support, progressive enhancement, no JS state to lose, visible
expand indicator). Review is first; after applying, linked balances are first.
"""
from contextlib import closing
from html import escape
import sqlite3

from bulk import inventory, mappings


def signed_amount(currency, raw):
    """Show an explicit +/- sign and full precision; never round or hide sign."""
    s = (raw or "").strip()
    neg = s.startswith("-")
    mag = s[1:] if neg else s
    return f"{'-' if neg else '+'}{escape(currency)} {escape(mag)}"


def display(server, owners, notices=(), previous=None, select_all=False, error=None):
    batch = server.batch
    if not batch:
        return ("<p>No provider enabled. You are in <strong>local manual mode</strong>: add and update dated "
                "balances yourself. For an isolated, fictional walkthrough start "
                "<code>python3 server.py --demo</code>. Optional Bridge setup is CLI-only; "
                "see BULK.md. Never paste credentials into this dashboard.</p>")

    def form(action, cls="batch-form"):
        return (f'<form method="post" action="/batch/{action}" class="{cls}">'
                f'<input type="hidden" name="token" value="{escape(server.token)}">')

    # One clear state cue; role=alert error + role=status notices; nothing silent.
    out = ""
    if error:
        out += f'<p class="notice" role="alert">{escape(error)}</p>'
    out += "".join(f'<p class="notice" role="status">{escape(n)}</p>' for n in notices)

    review = batch.review
    if select_all and (not review or (previous or {}).get("review") != review[0]):
        out += '<p class="notice" role="status">Review expired. Discover again.</p>'
        select_all = False

    with closing(sqlite3.connect(server.db)) as db:
        db.row_factory = sqlite3.Row
        links_now, rows_now = mappings(db), inventory(db)
    has_links = bool(links_now)

    def discover(secondary):
        cls = "secondary" if secondary else "primary"
        return (form("discover", "batch-form discover-form") + '<div class="actions">'
                f'<button class="{cls}">Discover accounts for review</button></div></form>'
                '<p class="choice-help">One balances-only request across connections. Discovery changes '
                'nothing and selects nothing.</p>')

    def linked_section():
        h = form("refresh", "batch-form linked-form") + '<h3>Linked balances</h3>'
        h += ('<p class="choice-help">Nothing is checked. Check only rows to refresh. '
              '<strong>Balance as of</strong>, <strong>last operation</strong> and <strong>checked (UTC)</strong> '
              'are separate; a local edit keeps its snapshot; missing/failed items are retained.</p>')
        # Prominent primary action, reachable without scrolling the list.
        h += '<div class="actions actionbar"><button>Refresh selected balances</button></div>'
        h += '<ul class="linked-list">'
        for key, l in links_now.items():
            r = rows_now.get(str(l["local"]))
            h += '<li class="linked-row"><div class="linked-top">'
            h += (f'<label class="check"><input type="checkbox" name="r{escape(str(l["local"]))}"> '
                  f'<span>Refresh <strong>{escape(r["nickname"]) if r else "removed account"}</strong></span></label>')
            h += '</div>'
            if r:
                h += (f'<small>{escape(r["institution"])}</small><p class="tile-balance">{escape(r["currency"])} '
                      f'{escape(r["balance"])}</p><div class="tile-tags"><span>{escape(r["side"].title())}</span>'
                      f'<span>{escape(r["owner"])}</span></div>')
            h += '<div class="linked-meta">'
            if r:
                h += f'<span><span class="lbl">Balance as of</span> {escape(r["asof"])}</span>'
            h += (f'<span class="linked-status"><span class="lbl">Last operation</span> '
                  f'{escape(l["status"])}</span>')
            h += f'<span><span class="lbl">Checked (UTC)</span> {escape(l["checked"])}</span>'
            h += f'<span class="linked-ref">Source ref {escape(key[:8])}</span></div>'
            if r:
                h += (f'<a href="/edit/{escape(str(l["local"]))}">Edit {escape(r["nickname"])}'
                      f'<span class="sr"> account #{escape(str(l["local"]))}</span></a>')
            h += '</li>'
        h += '</ul></form>'
        return h

    # ---- Ordering: review first; after apply, linked-first; initial discover primary ----
    if review:
        nonce, entries, rows, links, warnings = review
        previous = previous if previous and previous.get("review") == nonce else {}

        def check(name):
            return (f'<input type="checkbox" name="{name}"'
                    f'{" checked" if select_all or previous.get(name) else ""}>')

        def select(name, label, choices, hint=""):
            options = "".join(f'<option value="{escape(k)}"{" selected" if previous.get(name) == k else ""}>'
                              f'{escape(v)}</option>' for k, v in choices)
            help_html = f'<p class="choice-help">{escape(hint)}</p>' if hint else ""
            return (f'<label>{escape(label)}<select name="{name}">{options}</select></label>' + help_html)

        out += "".join(f'<p class="notice" role="status">{escape(w)}</p>' for w in warnings)
        out += form("apply", "batch-form review-form") + f'<input type="hidden" name="review" value="{escape(nonce)}">'
        out += f'<h3>Review {len(entries)} balances</h3>'
        out += '<details class="help"><summary>How review works</summary><p>' \
               'Nothing is selected or signed automatically. For each row, check <strong>Include</strong>, ' \
               'choose a destination (match an existing account or create new), and confirm whether the ' \
               'signed amount counts as <strong>asset</strong> or <strong>debt</strong>. Applying saves all ' \
               'checked rows together or none; <strong>Unchecked rows are left out</strong>. A positive number is not proof ' \
               'of an asset. Applying replaces the matched balance and date, not owner or other details.</p></details>'
        # Active primary action reachable without scrolling 12 rows; sticky.
        out += '<div class="actions actionbar">'
        out += '<button type="submit" class="secondary" formaction="/batch/select-all">Select all review rows</button>'
        out += '<button type="submit" class="primary">Apply reviewed selections</button>'
        out += '</div>'
        out += '<p class="choice-help">Uncheck to exclude. Select-all only changes checkboxes.</p>'

        owner_options = [(o, o) for o in owners]
        out += select("owner", "Default owner for new accounts",
                      [("", "Choose owner… (or set per row)"), *owner_options],
                      "Required unless every created row sets an owner; per-row override wins; "
                      "Unspecified is allowed intentionally; matching rows keep their owner.")

        linked = {str(l["local"]) for l in links.values()}
        out += '<ul class="review-list">'
        for i, e in enumerate(entries):
            v = e.values
            match = links.get(e.key)
            signed = signed_amount(v["currency"], e.signed)
            neg = e.signed.strip().startswith("-")
            amount_class = "row-amount neg" if neg else "row-amount"
            out += '<li class="review-row">'
            out += '<div class="row-head">'
            out += (f'<label class="check row-include">{check(f"s{i}")} '
                    f'<span>Include <strong>{escape(v["nickname"])}</strong></span></label>')
            out += (f'<span class="row-amounts"><span class="{amount_class}">{signed}</span> '
                    f'<span class="row-date">as of {escape(v["asof"])}</span></span>')
            out += (f'<span class="row-src"><span class="lbl">Source</span> {escape(v["institution"])} · '
                    f'{escape(v["nickname"])} <span class="row-ref">ref {escape(e.key[:8])}</span></span>')
            out += '</div>'
            out += '<details class="row-details"><summary>Match / create, sign &amp; owner</summary>' \
                   '<div class="row-controls">'
            # Short visible labels + readable match-candidate detail (not clipped <option>).
            options = [] if match else [("", "Choose match or create…"), ("new", "Create new account")]
            candidates = []
            for k, r in rows.items():
                if (k == str(match["local"]) if match else k not in linked and r["currency"] == v["currency"]):
                    options.append((k, f'Match #{k} · {r["nickname"]}'))
                    candidates.append(
                        '<li class="match-candidate"><span class="lbl">Match #' + escape(k) + '</span> '
                        + escape(r["institution"]) + ' · ' + escape(r["nickname"])
                        + '<span class="mc-meta">owner ' + escape(r["owner"]) + ' · ' + escape(r["side"])
                        + ' · ' + escape(r["currency"]) + ' ' + escape(r["balance"])
                        + ' · balance as of ' + escape(r["asof"]) + '</span></li>')
            out += select(f"m{i}", "Destination", options,
                          "Match a compatible local account (same currency & side) or create new. "
                          "No match is assumed; a linked identity cannot switch.")
            if candidates:
                out += ('<details class="match-help"><summary>Compare matching local accounts</summary>'
                        '<ul class="match-list">' + "".join(candidates) + '</ul></details>')
            out += select(f"d{i}", "Count signed amount as",
                          [("", "Confirm sign…"), ("asset", "Asset"), ("debt", "Debt")],
                          "Required. Asset = owned; Debt = owed (stored as magnitude, subtracted once). "
                          "The source sign is shown; you decide what it counts as.")
            if not match:
                out += select(f"o{i}", "Owner if creating", [("", "Use default owner"), *owner_options],
                              "Overrides the default owner for this new account. Unspecified is allowed "
                              "intentionally. Nothing is assumed.")
            else:
                out += ('<p class="choice-help">Linked to <strong>#' + escape(str(match["local"]))
                        + '</strong>; fixed identity. Matching keeps owner, side, currency and other details; '
                        + 'only balance and as-of date are replaced.</p>')
            out += '</div></details></li>'
        out += '</ul></form>'
        out += discover(secondary=True)
        if has_links:
            out += linked_section()
        return out

    if has_links:
        # Linked-first after apply / when returning; rediscovery secondary.
        out += linked_section()
        out += discover(secondary=True)
        return out

    out += discover(secondary=False)
    return out

from contextlib import closing
from html import escape
import sqlite3

from bulk import inventory, mappings


def display(server, owners, notices=(), previous=None):
    batch = server.batch
    head = '<a href="/">← Inventory</a><h2>Batch balances</h2>'
    if not batch:
        return head + ("<p>No provider enabled. For an isolated, fictional walkthrough start "
                       "<code>python3 server.py --demo</code>. Optional Bridge setup is CLI-only; "
                       "see BULK.md. Never paste credentials into this dashboard.</p>")

    def form(action):
        return (f'<form method="post" action="/batch/{action}">'
                f'<input type="hidden" name="token" value="{server.token}">')

    head += f"<h3>{escape(batch.provider.label)}</h3>"
    head += "".join(f'<p role="status">{escape(n)}</p>' for n in notices)
    head += (form("discover") + "<button>Discover accounts for review</button></form>"
             "<p>Requests balances across connections. Discovery does not change the inventory.</p>")
    review = batch.review
    if review:
        nonce, entries, rows, links, warnings = review
        previous = previous if previous and previous.get("review") == nonce else {}
        def check(name):
            return f'<input type="checkbox" name="{name}"{" checked" if previous.get(name) else ""}>'
        def select(name, label, choices):
            options = "".join(f'<option value="{escape(k)}"{" selected" if previous.get(name)==k else ""}>'
                              f'{escape(v)}</option>' for k, v in choices)
            return f'<label>{label}<select name="{name}">{options}</select></label>'
        head += "".join(f"<p>{escape(w)}</p>" for w in warnings)
        head += form("apply") + f'<input type="hidden" name="review" value="{nonce}"><h3>Review {len(entries)} balances</h3>'
        head += ('<p>Select rows; for each, explicitly match or create and confirm how the signed amount '
                 'counts. Applying replaces the matched balance and date, not its owner or other details.</p>'
                 f'<label>{check("all")}Include all review rows</label>')
        owner_options = [(o, o) for o in owners]
        head += select("owner", "Default owner for new accounts", owner_options)
        linked = {str(l["local"]) for l in links.values()}
        for i, e in enumerate(entries):
            v = e.values
            head += (f'<fieldset><legend>{escape(v["institution"])} · {escape(v["nickname"])} '
                     f'· ref {e.key[:8]}</legend><label>{check(f"s{i}")}Include</label>'
                     f'<p>Source: {escape(v["currency"])} {escape(e.signed)} as of {v["asof"]}</p>')
            match = links.get(e.key)
            options = [] if match else [("", "Choose match or create"), ("new", "Create new account")]
            for k, r in rows.items():
                if (k == str(match["local"]) if match else k not in linked and r["currency"] == v["currency"]):
                    options.append((k, f'Match #{k}: {r["institution"]} · {r["nickname"]} · {r["owner"]} · '
                                       f'{r["side"]} · {r["currency"]} {r["balance"]} on {r["asof"]}'))
            head += select(f"m{i}", "Destination", options)
            head += select(f"d{i}", "Count magnitude as", [("", "Confirm sign treatment"), ("asset", "Asset (owned)"),
                                                           ("debt", "Debt (owed)")])
            if not match:
                head += select(f"o{i}", "Owner if creating", [("", "Use default"), *owner_options])
            head += "</fieldset>"
        head += "<button>Apply reviewed selections</button></form>"
    with closing(sqlite3.connect(server.db)) as db:
        db.row_factory = sqlite3.Row
        links, rows = mappings(db), inventory(db)
    if links:
        head += form("refresh") + "<h3>Linked balances</h3><p>Uncheck any to leave unchanged.</p>"
        for key, l in links.items():
            r = rows.get(str(l["local"]))
            detail = (f'{r["institution"]} · {r["nickname"]} · {r["owner"]} · {r["currency"]} '
                      f'{r["balance"]} as of {r["asof"]}' if r else "Removed locally")
            head += (f'<label><input type="checkbox" name="r{l["local"]}" checked>{escape(detail)} '
                     f'· ref {key[:8]} · {escape(l["status"])} ({escape(l["checked"])})</label>')
        head += '<button>Refresh selected balances</button></form>'
    return head

"""Local household financial accounts dashboard. Run with python3 server.py (stdlib only)."""
import argparse
from contextlib import closing, ExitStack
from datetime import date
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
from urllib.parse import parse_qs, urlsplit
from servicers import servicer_get
from bridge import Bridge, Problem
from bulk import Batch, schema


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / ".local/share/planner-accounts/accounts.sqlite3"
FIELDS = ("institution", "nickname", "kind", "side", "currency", "balance", "asof", "owner", "update_method")
PEOPLE = ("Young", "Nanami")
OWNERS = ("Unspecified", *PEOPLE, "Joint")
TAG_OWNERS = (*PEOPLE, "Joint")
DEFAULT_METHOD = "Manual entry"
KINDS = ("Checking", "Savings", "Cash", "Brokerage", "Retirement", "Pension",
         "Health savings", "Digital assets", "Credit card", "Mortgage", "Student loan",
         "Personal loan", "Auto loan", "Line of credit")
ZERO = set("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split())
THREE = set("BHD IQD JOD KWD LYD OMR TND".split())


def places(currency):
    return 0 if currency in ZERO else 3 if currency in THREE else 4 if currency in {"CLF", "UYW"} else 2


def money(value, currency):
    return f"{currency} {value:,.{places(currency)}f}"


def signed_money(value, currency, side):
    prefix = "-" if side == "debt" and value != 0 else ""
    return f"{prefix}{money(value, currency)}"


def validate(raw, today):
    v = {k: raw.get(k, "").strip() for k in FIELDS}
    for k, label in (("institution", "Institution or holder"), ("nickname", "Account name"), ("kind", "Account type")):
        if not 1 <= len(v[k]) <= 100:
            raise ValueError(f"{label}: enter 1–100 characters.")
    if v["side"] not in {"asset", "debt"}:
        raise ValueError("Choose asset or debt.")
    if v["owner"] not in OWNERS:
        raise ValueError("Choose a listed owner or Unspecified.")
    if not 1 <= len(v["update_method"]) <= 100:
        raise ValueError("Update method: enter 1–100 characters.")
    v["currency"] = v["currency"].upper()
    if not re.fullmatch("[A-Z]{3}", v["currency"]):
        raise ValueError("Currency: enter a three-letter currency code, such as USD.")
    if not re.fullmatch(r"\d{1,15}(?:\.\d{1,4})?", v["balance"], flags=re.ASCII):
        raise ValueError("Balance: enter a nonnegative amount with no commas (up to 15 whole digits).")
    scale = places(v["currency"])
    if len(v["balance"].partition(".")[2]) > scale:
        raise ValueError(f"Balance: {v['currency']} accepts up to {scale} decimal places.")
    v["balance"] = f"{Decimal(v['balance']):.{scale}f}"
    try:
        when = date.fromisoformat(v["asof"])
    except ValueError:
        raise ValueError("As-of date: enter a valid calendar date.") from None
    if when > today:
        raise ValueError("As-of date cannot be in the future.")
    v["asof"] = when.isoformat()
    return v


def init_db(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
            kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
            currency TEXT NOT NULL, balance TEXT NOT NULL, asof TEXT NOT NULL)""")
        columns = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
        if "owner" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN owner TEXT NOT NULL DEFAULT 'Unspecified'")
        if "update_method" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN update_method TEXT NOT NULL DEFAULT 'Manual entry'")
        if "excluded" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        if "side_manual" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN side_manual INTEGER NOT NULL DEFAULT 0")
        schema(db)


NAV = (("/", "Accounts"), ("/new", "Add account"),
       ("/servicers", "Statements"), ("/federal-preview", "Federal preview"))


def nav(current=None):
    items = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if current == href else '') + f'>{escape(label)}</a>'
        for href, label in NAV)
    return f'<nav class="sitenav" aria-label="Local accounts sections">{items}</nav>'


def mode_banner(server):
    if getattr(server, "demo", False):
        return ('<p class="mode-banner demo" role="note"><strong>Fictional demo</strong>'
                ' · discarded on exit · synthetic · no live accounts</p>')
    batch = getattr(server, "batch", None)
    if batch is not None and getattr(batch, "provider", None) is not None:
        label = escape(getattr(batch.provider, "label", "Provider") or "Provider")
        return ('<p class="mode-banner configured" role="note"><strong>SimpleFIN connected</strong> · ' + label
                + ' · saved locally · click Sync balances to refresh</p>')
    return ('<p class="mode-banner manual" role="note"><strong>Local manual</strong>'
            ' · no provider · saved on this computer</p>')


def page(content, current=None, heading="Financial accounts"):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(heading)} · Local planner</title><link rel="stylesheet" href="/style.css">
<main>{nav(current)}<header><small>YOUNG &amp; NANAMI · LOCAL PLANNER</small><h1>{escape(heading)}</h1></header>{content}</main></html>"""


def view_url(view):
    return "/" if view == "Household" else f"/?view={view}"


def account_row(r, today, token, view, excluded=False):
    rid = r["id"]
    name = escape(r["nickname"])
    inst = escape(r["institution"])
    kind = escape(r["kind"])
    amt = Decimal(r["balance"])
    c = r["currency"]
    side = r["side"]
    stale = (today - date.fromisoformat(r["asof"])).days > 30
    owner_pills = "".join(
        f'<button type="submit" name="owner" value="{o}" class="pill-btn{" active" if r["owner"] == o else ""}"'
        f' aria-pressed="{"true" if r["owner"] == o else "false"}">{o}</button>'
        for o in TAG_OWNERS)
    owner_cell = (f'<form method="post" action="/owner/{rid}" class="inline-form owner-pills">'
                  f'<input type="hidden" name="token" value="{token}">'
                  f'<input type="hidden" name="view" value="{view}">'
                  f'{owner_pills}</form>')
    next_side = "asset" if side == "debt" else "debt"
    side_toggle = (f'<form method="post" action="/side/{rid}" class="inline-form">'
                   f'<input type="hidden" name="token" value="{token}">'
                   f'<input type="hidden" name="view" value="{view}">'
                   f'<input type="hidden" name="side" value="{next_side}">'
                   f'<button type="submit" class="side-btn {side}" title="Switch between Asset and Debt">'
                   f'{side.title()}</button></form>')
    toggle_label = "Include" if excluded else "Exclude"
    toggle_val = "0" if excluded else "1"
    exclude_btn = (f'<form method="post" action="/exclude/{rid}" class="inline-form">'
                   f'<input type="hidden" name="token" value="{token}">'
                   f'<input type="hidden" name="view" value="{view}">'
                   f'<input type="hidden" name="excluded" value="{toggle_val}">'
                   f'<button type="submit" class="link-btn">{toggle_label}</button></form>')
    stale_tag = ' <span class="stale-pill">Stale</span>' if stale else ""
    return (f'<tr class="{"is-excluded" if excluded else ""}">'
            f'<th scope="row" class="acct-cell"><span class="acct-name">{name}</span>'
            f'<span class="acct-sub">{inst} · {kind}</span></th>'
            f'<td class="num bal-cell"><span class="amt {side}">{signed_money(amt, c, side)}</span> {side_toggle}</td>'
            f'<td class="owner-cell">{owner_cell}</td>'
            f'<td class="date-cell"><time datetime="{r["asof"]}">{r["asof"]}</time>{stale_tag}</td>'
            f'<td class="act-cell">{exclude_btn}<a href="/edit/{rid}">Edit<span class="sr"> {name}</span></a></td>'
            f'</tr>')


def dashboard(rows, today, view, token, has_sync=False, notices=(), error=None):
    active = [r for r in rows if not r["excluded"]]
    excluded = [r for r in rows if r["excluded"]]
    totals = {}
    for r in active:
        c, amt = r["currency"], Decimal(r["balance"])
        group = totals.setdefault(c, {"asset": Decimal(0), "debt": Decimal(0)})
        group[r["side"]] += amt
    cards = "".join(
        f'<section class="card"><small>{c} · NET WORTH</small>'
        f'<strong>{money(t["asset"] - t["debt"], c)}</strong>'
        f'<div>Assets: {money(t["asset"], c)} · Debts: {money(t["debt"], c)}</div></section>'
        for c, t in sorted(totals.items()))
    links = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if label == view else '') + f'>{label}</a>'
        for label, url in [("Household", "/"), *((p, f"/?view={p}") for p in PEOPLE)])
    sync_html = ""
    if has_sync:
        sync_html = (f'<form method="post" action="/sync" class="inline-form">'
                     f'<input type="hidden" name="token" value="{token}">'
                     f'<input type="hidden" name="view" value="{view}">'
                     f'<button type="submit" class="button">↻ Sync balances</button></form>')
    add_cls = "button secondary" if has_sync else "button"
    banner = ""
    if error:
        banner += f'<p class="notice" role="alert">{escape(error)}</p>'
    banner += "".join(f'<p class="notice" role="status">{escape(n)}</p>' for n in notices)

    if active:
        body = "".join(account_row(r, today, token, view, excluded=False) for r in active)
        table = (f'<div class="table-wrap"><table class="acct-table">'
                 f'<thead><tr><th>Account</th><th class="num">Balance</th><th>Owner</th>'
                 f'<th>As of</th><th class="act-col"></th></tr></thead>'
                 f'<tbody>{body}</tbody></table></div>')
    else:
        empty_msg = ("No active accounts in this view. Click <strong>↻ Sync balances</strong> to pull linked accounts, "
                     "or <strong>+ Add account</strong> to add one manually."
                     if has_sync else "No active accounts in this view. Click <strong>+ Add account</strong> to start.")
        table = f'<p class="empty-note">{empty_msg}</p>'

    excluded_html = ""
    if excluded:
        ex_body = "".join(account_row(r, today, token, view, excluded=True) for r in excluded)
        excluded_html = (f'<details class="excluded-box"><summary>Excluded accounts ({len(excluded)})'
                         f' — not counted in totals</summary>'
                         f'<div class="table-wrap"><table class="acct-table">'
                         f'<thead><tr><th>Account</th><th class="num">Balance</th><th>Owner</th>'
                         f'<th>As of</th><th class="act-col"></th></tr></thead>'
                         f'<tbody>{ex_body}</tbody></table></div></details>')

    return page(
        f'{banner}'
        f'<div class="top-bar"><nav class="owner-tabs" aria-label="Inventory views">{links}</nav>'
        f'<div class="top-actions">{sync_html}<a class="{add_cls}" href="/new">+ Add account</a></div></div>'
        f'<div class="cards">{cards}</div>'
        f'{table}{excluded_html}'
        f'<details class="help"><summary>How totals work</summary><p>Household counts every active account once. '
        f'Each person’s view includes their own accounts plus joint accounts (views overlap; do not add them together). '
        f'Totals are separate by currency. Click Young, Nanami, or Joint on any row to tag it; click Asset/Debt to '
        f'override sign detection; click Exclude to leave an account out of totals without deleting it. '
        f'Balances older than 30 days are marked stale and still included.</p></details>',
        current="/")


def form(v, target, token, error="", current=None):
    current = current or ("/new" if target == "/new" else "/")
    def field(key, label, attrs=""):
        return (f'<label>{label}<input required name="{key}" value="{escape(v.get(key, ""))}" '
                f'{attrs}></label>')
    choices = "".join(f'<option value="{k}">' for k in KINDS)
    owners = "".join(f'<option{" selected" if v.get("owner")==k else ""}>{k}</option>' for k in OWNERS)
    remove = f'<p><a href="{target.replace("/edit/", "/remove/")}">Remove account…</a></p>' if target.startswith("/edit/") else ""
    sides = "".join(f'<option value="{k}" {"selected" if v.get("side")==k else ""}>{text}</option>'
                    for k, text in (("asset", "Asset — money or value you own"), ("debt", "Debt — money you owe")))
    return page(f"""<h2>{'Add' if target=='/new' else 'Edit'} account</h2>
{f'<p role="alert">{escape(error)}</p>' if error else ''}
<form method="post" action="{target}" class="edit-form">
<input type="hidden" name="token" value="{token}">
{field('institution','Institution or holder','maxlength="100" placeholder="e.g. Example Bank or Self"')}
{field('nickname','Account name','maxlength="100" placeholder="e.g. Joint checking"')}
{field('kind','Account type — choose or type your own','list="types" maxlength="100"')}
<datalist id="types">{choices}</datalist>
<label>Owner<select required name="owner">{owners}</select></label>
{field('update_method','Update method','maxlength="100"')}
<p>Describe how to get the next balance, e.g. “Copy closing balance from monthly statement”.
Do not enter credentials or account numbers.</p>
<label>Count this balance as<select required name="side"><option value="">Choose asset or debt</option>{sides}</select></label>
{field('currency','Valuation currency','maxlength="3" placeholder="USD"')}
{field('balance','Balance in that currency','inputmode="decimal" placeholder="0.00"')}
<p>Enter the amount without a minus sign. Use Debt for an amount owed, including an overdraft.</p>
{field('asof','Balance as of',f'type="date" max="{date.today()}"')}
<div class="bar"><button>Save account</button><a href="/" class="button secondary">Cancel</a></div></form>{remove}""",
                current=current)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, text, kind="text/html; charset=utf-8", location=None, preview=False):
        if kind.startswith("text/html"):
            text = text.replace("<main>", "<main>" + mode_banner(self.server), 1)
        body = text.encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        policy = "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        self.send_header("Content-Security-Policy", policy + (
            "script-src 'self'; connect-src 'none'; form-action 'none'" if preview else "form-action 'self'"))
        # A no-referrer form sends Origin: null even on a local POST.
        self.send_header("Referrer-Policy", "no-referrer" if preview else "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def render_home(self, view, notices=(), error=None, status=200):
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT * FROM accounts WHERE ? = 'Household' OR owner IN (?, 'Joint')
                                 ORDER BY excluded, side, institution COLLATE NOCASE, nickname COLLATE NOCASE, id""",
                              (view, view)).fetchall()
        return self.send(status, dashboard(rows, date.today(), view, self.server.token,
                                           has_sync=bool(self.server.batch), notices=notices, error=error))

    def do_GET(self):
        if not self.local_host():
            return self.send(403, "Local requests only.")
        if urlsplit(self.path).path == "/batch":
            return self.send(303, "", location="/")
        result = servicer_get(self.path, page=page, form=form, token=self.server.token)
        if result is not None:
            return self.send(*result)
        path = urlsplit(self.path).path
        if path == "/federal-preview":
            return self.send(200, (ROOT / "preview.html").read_text(), preview=True)
        if path == "/preview.js":
            return self.send(200, (ROOT / "preview.js").read_text(), "application/javascript")
        if path == "/style.css":
            return self.send(200, (ROOT / "style.css").read_text(), "text/css")
        if path == "/new":
            return self.send(200, form({"currency": "USD", "asof": str(date.today()), "owner": "Unspecified",
                                        "update_method": DEFAULT_METHOD}, path, self.server.token))
        if path == "/":
            view = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("view", ["Household"])
            if len(view) != 1 or view[0] not in ("Household", *PEOPLE):
                return self.send(400, "Unknown inventory view.")
            notices, self.server.notices = self.server.notices, []
            return self.render_home(view[0], notices=notices)
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            match = re.fullmatch(r"/(edit|remove)/([1-9][0-9]{0,17})", path)
            if match:
                row = db.execute("SELECT * FROM accounts WHERE id=?", (match[2],)).fetchone()
                if row and match[1] == "remove":
                    return self.send(200, page(f"""<h2>Remove {escape(row['nickname'])}?</h2>
<p>This deletes this account's local snapshot.</p><form method="post" action="{path}" class="edit-form">
<input type="hidden" name="token" value="{self.server.token}">
<div class="bar"><button>Remove account</button><a href="/" class="button secondary">Cancel</a></div></form>""", current="/"))
                if row:
                    return self.send(200, form(dict(row), path, self.server.token))
        self.send(404, "Account or page not found.")

    def do_POST(self):
        if not self.local_host() or self.headers.get("Origin") not in {None, f"http://{self.headers.get('Host')}"}:
            return self.send(403, "Local requests only.")
        target = self.path
        edit = re.fullmatch(r"/edit/([1-9][0-9]{0,17})", target)
        remove = re.fullmatch(r"/remove/([1-9][0-9]{0,17})", target)
        owner_act = re.fullmatch(r"/owner/([1-9][0-9]{0,17})", target)
        exclude_act = re.fullmatch(r"/exclude/([1-9][0-9]{0,17})", target)
        side_act = re.fullmatch(r"/side/([1-9][0-9]{0,17})", target)
        sync_act = target == "/sync" and bool(self.server.batch)
        if not any((edit, remove, owner_act, exclude_act, side_act, sync_act, target == "/new")):
            return self.send(404, "Page not found.")
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > 8192:
            return self.send(413, "Form too large.")
        data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(length)).decode("utf8", "replace")).items()}
        if not secrets.compare_digest(data.get("token", "").encode(), self.server.token.encode()):
            return self.send(403, "Reload the form and try again.")
        back = data.get("view", "Household")
        back = back if back in ("Household", *PEOPLE) else "Household"
        if sync_act:
            try:
                self.server.notices = self.server.batch.sync()
            except Problem as exc:
                return self.render_home(back, error=str(exc), status=422)
            return self.send(303, "", location=view_url(back))
        if owner_act:
            chosen = data.get("owner", "")
            if chosen not in OWNERS:
                return self.send(422, "Invalid owner.")
            with closing(sqlite3.connect(self.server.db)) as db, db:
                row = db.execute("SELECT owner FROM accounts WHERE id=?", (owner_act[1],)).fetchone()
                if not row:
                    return self.send(404, "Account not found.")
                new_owner = "Unspecified" if row[0] == chosen else chosen
                db.execute("UPDATE accounts SET owner=? WHERE id=?", (new_owner, owner_act[1]))
            return self.send(303, "", location=view_url(back))
        if exclude_act:
            val = data.get("excluded", "")
            if val not in ("0", "1"):
                return self.send(422, "Invalid exclude flag.")
            with closing(sqlite3.connect(self.server.db)) as db, db:
                changed = db.execute("UPDATE accounts SET excluded=? WHERE id=?", (int(val), exclude_act[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
            return self.send(303, "", location=view_url(back))
        if side_act:
            side = data.get("side", "")
            if side not in ("asset", "debt"):
                return self.send(422, "Invalid side.")
            with closing(sqlite3.connect(self.server.db)) as db, db:
                changed = db.execute("UPDATE accounts SET side=?, side_manual=1 WHERE id=?",
                                     (side, side_act[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
            return self.send(303, "", location=view_url(back))
        if remove:
            with closing(sqlite3.connect(self.server.db)) as db, db:
                db.execute("DELETE FROM accounts WHERE id=?", (remove[1],))
            return self.send(303, "", location="/")
        try:
            values = validate(data, date.today())
        except ValueError as exc:
            return self.send(422, form(data, target, self.server.token, str(exc)))
        params = tuple(values[k] for k in FIELDS)
        with closing(sqlite3.connect(self.server.db)) as db, db:
            if edit:
                prev = db.execute("SELECT side, side_manual FROM accounts WHERE id=?", (edit[1],)).fetchone()
                if not prev:
                    return self.send(404, "Account not found.")
                side_manual = 1 if (prev[1] or prev[0] != values["side"]) else 0
                db.execute("""UPDATE accounts SET institution=?,nickname=?,kind=?,side=?,currency=?,
                              balance=?,asof=?,owner=?,update_method=?,side_manual=? WHERE id=?""",
                           (*params, side_manual, edit[1]))
            else:
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,
                              owner,update_method,excluded,side_manual) VALUES (?,?,?,?,?,?,?,?,?,0,1)""", params)
        self.send(303, "", location="/")


def serve(path, port, provider=None):
    init_db(path)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = secrets.token_urlsafe(32)
    server.db = path
    server.batch = Batch(path, provider, validate) if provider else None
    server.notices = []
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--db", type=Path, default=DEFAULT_DB)
    modes.add_argument("--demo", action="store_true", help="Fictional walkthrough, disposable database only")
    modes.add_argument("--bridge", action="store_true", help="Enable configured SimpleFIN Bridge")
    args = parser.parse_args()
    os.umask(0o077)
    with ExitStack() as stack:
        path, provider = args.db, None
        if args.demo:
            from demo_batch import Demo, seed
            path = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "fictional.sqlite3"
            provider = Demo()
        if args.bridge:
            try:
                provider = Bridge()
            except (Problem, OSError, UnicodeError):
                parser.exit(1, "Bridge disabled: missing or unsafe credential storage. See BULK.md.\n")
        server = stack.enter_context(serve(path, args.port, provider))
        server.demo = args.demo
        if args.demo:
            with closing(sqlite3.connect(path)) as db, db:
                seed(db)
        print(f"Open http://127.0.0.1:{server.server_port}/ · Ctrl-C to stop", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

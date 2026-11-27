"""Manual accounts dashboard. Run with python3 server.py (standard library only)."""
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
from bulk_ui import display


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / ".local/share/planner-accounts/accounts.sqlite3"
FIELDS = ("institution", "nickname", "kind", "side", "currency", "balance", "asof", "owner", "update_method")
PEOPLE = ("Young", "Nanami")
OWNERS = ("Unspecified", *PEOPLE, "Joint")
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
        schema(db)


def page(content):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Financial accounts · Local planner</title><link rel="stylesheet" href="/style.css">
<main><header><small>YOUNG &amp; NANAMI · LOCAL PLANNER</small><h1>Financial accounts</h1>
<p>Dated balance snapshots · saved on this computer</p></header>{content}</main></html>"""


def dashboard(rows, today, view):
    totals, lines = {}, []
    for r in rows:
        c, amt = r["currency"], Decimal(r["balance"])
        group = totals.setdefault(c, {"asset": Decimal(0), "debt": Decimal(0)})
        group[r["side"]] += amt
        stale = (today - date.fromisoformat(r["asof"])).days > 30
        lines.append(f"""<tr><th scope="row">{escape(r['nickname'])}<small>{escape(r['institution'])}</small></th>
<td>{escape(r['kind'])}<small>{r['side'].title()}</small></td><td>{money(amt,c)}</td>
<td>{r['asof']}{'<small class="stale">Stale</small>' if stale else ''}</td>
<td>{escape(r['owner'])}</td><td>{escape(r['update_method'])}</td>
<td><a href="/edit/{r['id']}">Edit<span class="sr"> {escape(r['nickname'])}</span></a></td></tr>""")
    cards = "".join(f"""<section class="card"><small>{c} · NET WORTH</small>
<strong>{money(t['asset']-t['debt'], c)}</strong>
<div>Assets: {money(t['asset'], c)} · Debts: {money(t['debt'], c)}</div></section>"""
                    for c, t in sorted(totals.items()))
    detail = (f"""<div class="table"><table><thead><tr><th>Account</th><th>Type</th>
<th>Balance</th><th>As of</th><th>Owner</th><th>Update method</th><th></th></tr></thead><tbody>{''.join(lines)}</tbody></table></div>
<p>Balances older than 30 days are marked stale and still included. Currencies are totaled separately.</p>"""
              if rows else "<p>No accounts in this view.</p>")
    links = " · ".join(label if label == view else f'<a href="{url}">{label}</a>'
                       for label, url in [("Household", "/"), *((p, f"/?view={p}") for p in PEOPLE)])
    return page(f'<nav aria-label="Inventory views">View: {links}</nav><h2>{view} inventory</h2>'
                '<p>Household counts every account once, including Unspecified. Each person sees their own '
                'accounts plus the full joint balances. These views overlap; do not add them together.</p>'
                '<p>Ownership is a label, not access control. To review or refresh linked accounts, '
                'open <a href="/batch">Batch balances</a>.</p>'
                f'<div class="cards">{cards}</div><div class="bar"><h2>Accounts</h2>'
                f'<a class="button" href="/new">Add account</a></div>{detail}'
                '<p><a href="/servicers">Record a student loan from a statement</a></p>'
                '<p><a href="/federal-preview">Preview federal My Aid Data</a> · No accounts will change.</p>')


def form(v, target, token, error=""):
    def field(key, label, attrs=""):
        return (f'<label>{label}<input required name="{key}" value="{escape(v.get(key, ""))}" '
                f'{attrs}></label>')
    choices = "".join(f'<option value="{k}">' for k in KINDS)
    owners = "".join(f'<option{" selected" if v.get("owner")==k else ""}>{k}</option>' for k in OWNERS)
    remove = f'<p><a href="{target.replace("/edit/", "/remove/")}">Remove account…</a></p>' if target.startswith("/edit/") else ""
    sides = "".join(f'<option value="{k}" {"selected" if v.get("side")==k else ""}>{text}</option>'
                    for k, text in (("asset", "Asset — money or value you own"), ("debt", "Debt — money you owe")))
    return page(f"""<a href="/">← Accounts</a><h2>{'Add' if target=='/new' else 'Edit'} account</h2>
{f'<p role="alert">{escape(error)}</p>' if error else ''}
<form method="post" action="{target}">
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
<div class="bar"><button>Save account</button><a href="/">Cancel</a></div></form>{remove}""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, text, kind="text/html; charset=utf-8", location=None, preview=False):
        if getattr(self.server, "demo", False) and kind.startswith("text/html"):
            text = text.replace("<main>", "<main><p><strong>Fictional demo · discarded on exit</strong></p>", 1)
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

    def do_GET(self):
        if not self.local_host():
            return self.send(403, "Local requests only.")
        if self.path == "/batch":
            return self.send(200, page(display(self.server, OWNERS)))
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
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            if path == "/":
                view = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("view", ["Household"])
                if len(view) != 1 or view[0] not in ("Household", *PEOPLE):
                    return self.send(400, "Unknown inventory view.")
                rows = db.execute("""SELECT * FROM accounts WHERE ? = 'Household' OR owner IN (?, 'Joint')
                                     ORDER BY nickname COLLATE NOCASE, id""", (view[0], view[0])).fetchall()
                return self.send(200, dashboard(rows, date.today(), view[0]))
            match = re.fullmatch(r"/(edit|remove)/([1-9][0-9]{0,17})", path)
            if match:
                row = db.execute("SELECT * FROM accounts WHERE id=?", (match[2],)).fetchone()
                if row and match[1] == "remove":
                    return self.send(200, page(f"""<h2>Remove {escape(row['nickname'])}?</h2>
<p>This deletes this account's local snapshot.</p><form method="post" action="{path}">
<input type="hidden" name="token" value="{self.server.token}">
<div class="bar"><button>Remove account</button><a href="/">Cancel</a></div></form>"""))
                if row:
                    return self.send(200, form(dict(row), path, self.server.token))
        self.send(404, "Account or page not found.")

    def do_POST(self):
        if not self.local_host() or self.headers.get("Origin") not in {None, f"http://{self.headers.get('Host')}"}:
            return self.send(403, "Local requests only.")
        target = self.path
        edit = re.fullmatch(r"/edit/([1-9][0-9]{0,17})", target)
        remove = re.fullmatch(r"/remove/([1-9][0-9]{0,17})", target)
        batch = target in {"/batch/discover", "/batch/apply", "/batch/refresh"} and self.server.batch
        if not batch and not edit and not remove and target != "/new":
            return self.send(404, "Page not found.")
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > (60000 if batch else 8192):
            return self.send(413, "Form too large.")
        data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(length)).decode("utf8", "replace")).items()}
        if not secrets.compare_digest(data.get("token", "").encode(), self.server.token.encode()):
            return self.send(403, "Reload the form and try again.")
        if batch:
            try:
                notices = batch.run(target.rsplit("/", 1)[1], data)
            except Problem as exc:
                return self.send(422, page(display(self.server, OWNERS, [str(exc)], data)))
            if notices:
                return self.send(200, page(display(self.server, OWNERS, notices)))
            return self.send(303, "", location="/batch")
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
                changed = db.execute("UPDATE accounts SET institution=?,nickname=?,kind=?,side=?,currency=?,balance=?,asof=?,owner=?,update_method=? WHERE id=?",
                                     (*params, edit[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
            else:
                db.execute("INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,update_method) VALUES (?,?,?,?,?,?,?,?,?)", params)
        self.send(303, "", location="/")


def serve(path, port, provider=None):
    init_db(path)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = secrets.token_urlsafe(32)
    server.db = path
    server.batch = Batch(path, provider, validate) if provider else None
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--db", type=Path, default=DEFAULT_DB)
    modes.add_argument("--demo", action="store_true", help="Fictional batch walkthrough, disposable database only")
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

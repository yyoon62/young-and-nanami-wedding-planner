"""Manual accounts dashboard. Run with python3 server.py (standard library only)."""
import argparse
from contextlib import closing
from datetime import date
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / ".local/share/planner-accounts/accounts.sqlite3"
FIELDS = ("institution", "nickname", "kind", "side", "currency", "balance", "asof")
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
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
            kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
            currency TEXT NOT NULL, balance TEXT NOT NULL, asof TEXT NOT NULL)""")


def page(content):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Financial accounts · Local planner</title><link rel="stylesheet" href="/style.css">
<main><header><small>YOUNG &amp; NANAMI · LOCAL PLANNER</small><h1>Financial accounts</h1>
<p>Manual balance snapshots · saved on this computer</p></header>{content}</main></html>"""


def dashboard(rows, today):
    totals, lines = {}, []
    for r in rows:
        c, amt = r["currency"], Decimal(r["balance"])
        group = totals.setdefault(c, {"asset": Decimal(0), "debt": Decimal(0)})
        group[r["side"]] += amt
        stale = (today - date.fromisoformat(r["asof"])).days > 30
        lines.append(f"""<tr><th scope="row">{escape(r['nickname'])}<small>{escape(r['institution'])}</small></th>
<td>{escape(r['kind'])}<small>{r['side'].title()}</small></td><td>{money(amt,c)}</td>
<td>{r['asof']}{'<small class="stale">Stale</small>' if stale else ''}</td>
<td><a href="/edit/{r['id']}">Edit<span class="sr"> {escape(r['nickname'])}</span></a></td></tr>""")
    cards = "".join(f"""<section class="card"><small>{c} · NET WORTH</small>
<strong>{money(t['asset']-t['debt'], c)}</strong>
<div>Assets: {money(t['asset'], c)} · Debts: {money(t['debt'], c)}</div></section>"""
                    for c, t in sorted(totals.items()))
    detail = (f"""<div class="table"><table><thead><tr><th>Account</th><th>Type</th>
<th>Balance</th><th>As of</th><th></th></tr></thead><tbody>{''.join(lines)}</tbody></table></div>
<p>Balances older than 30 days are marked stale and still included. Currencies are totaled separately.</p>"""
              if rows else "<p>No accounts yet. Start with a checking account, investment, loan, or any other account.</p>")
    return page(f'<div class="cards">{cards}</div><div class="bar"><h2>Your accounts</h2>'
                f'<a class="button" href="/new">Add account</a></div>{detail}')


def form(v, target, token, error=""):
    def field(key, label, attrs=""):
        return (f'<label>{label}<input required name="{key}" value="{escape(v.get(key, ""))}" '
                f'{attrs}></label>')
    choices = "".join(f'<option value="{k}">' for k in KINDS)
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
<label>Count this balance as<select required name="side"><option value="">Choose asset or debt</option>{sides}</select></label>
{field('currency','Valuation currency','maxlength="3" placeholder="USD"')}
{field('balance','Balance in that currency','inputmode="decimal" placeholder="0.00"')}
<p>Enter the amount without a minus sign. Use Debt for an amount owed, including an overdraft.</p>
{field('asof','Balance as of',f'type="date" max="{date.today()}"')}
<div class="bar"><button>Save account</button><a href="/">Cancel</a></div></form>{remove}""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, text, kind="text/html; charset=utf-8", location=None):
        body = text.encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if not self.local_host():
            return self.send(403, "Local requests only.")
        path = urlsplit(self.path).path
        if path == "/style.css":
            return self.send(200, (ROOT / "style.css").read_text(), "text/css")
        if path == "/new":
            return self.send(200, form({"currency": "USD", "asof": str(date.today())}, path, self.server.token))
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            if path == "/":
                rows = db.execute("SELECT * FROM accounts ORDER BY nickname COLLATE NOCASE, id").fetchall()
                return self.send(200, dashboard(rows, date.today()))
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
        if not edit and not remove and target != "/new":
            return self.send(404, "Page not found.")
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > 8192:
            return self.send(413, "Form too large.")
        data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(length)).decode("utf8", "replace")).items()}
        if not secrets.compare_digest(data.get("token", "").encode(), self.server.token.encode()):
            return self.send(403, "Reload the form and try again.")
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
                changed = db.execute("UPDATE accounts SET institution=?,nickname=?,kind=?,side=?,currency=?,balance=?,asof=? WHERE id=?",
                                     (*params, edit[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
            else:
                db.execute("INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof) VALUES (?,?,?,?,?,?,?)", params)
        self.send(303, "", location="/")


def serve(path, port):
    init_db(path)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = secrets.token_urlsafe(32)
    server.db = path
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    os.umask(0o077)
    with serve(args.db, args.port) as server:
        print(f"Open http://127.0.0.1:{server.server_port}/ · Ctrl-C to stop", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

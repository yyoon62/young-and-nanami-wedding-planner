"""Local household financial accounts dashboard. Run with python3 server.py (stdlib only)."""
import argparse
from contextlib import closing, ExitStack
from datetime import date
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
import sync_status
from history import install as install_history, net_worth
import net_worth_ui
from hosted_guard import Refused, reclaim, require_clean
from hosted_auth import (Auth, Config, Rejected, CHALLENGE_TTL, SESSION_TTL, db_path, external,
                         load_env_file, offline_lock, private, require_crypto)
from logos import KNOWN_DOMAIN_SET, cached_logo, fallback_svg, institution_logo, sanitize_domain
from account_terms import install_terms, load_terms, parse_terms, save_terms
from projections import install_projections
import terms_ui
from projections_ui import projection_post, projection_redirect
from transactions import install_transactions, sync_transactions
from tx_classifier import after_sync as classify_after_sync
from transactions_ui import (EDIT as TX_EDIT, SYNC as TX_SYNC, TRANSACTIONS as TX_PATH,
                             RouteProblem, post_transactions, problem_body, render_transactions)


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / ".local/share/planner-accounts/accounts.sqlite3"
FIELDS = ("institution", "nickname", "kind", "side", "currency", "balance", "asof", "owner", "update_method")
PEOPLE = ("Young", "Nanami")
OWNERS = ("Unspecified", *PEOPLE, "Joint")
SECTIONS = ("Checking", "Savings", "Retirement", "Investments", "Credit cards", "Loans", "Other")
SECTION_KIND = {
    "Checking": "Checking",
    "Savings": "Savings",
    "Retirement": "Retirement",
    "Investments": "Brokerage",
    "Credit cards": "Credit card",
    "Loans": "Student loan",
    "Other": "Other",
}
DEFAULT_METHOD = "Manual entry"
REMOVE_WARNING = "This account and all its saved balance history will be permanently deleted."
KINDS = ("Checking", "Savings", "Cash", "Brokerage", "Retirement", "Pension",
         "Health savings", "Digital assets", "Credit card", "Mortgage", "Student loan",
         "Personal loan", "Auto loan", "Line of credit", "Other")
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
        if "logo_domain" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN logo_domain TEXT NOT NULL DEFAULT ''")
        schema(db)
        install_history(db)
        install_projections(db)
        install_transactions(db)
        install_terms(db)


NAV = (("/", "Accounts"), ("/transactions", "Transactions"), ("/new", "Add account"))


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


def page(content, current=None, heading="Financial accounts", script=None):
    tag = f'<script defer src="{script}"></script>' if script else ""
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(heading)} · Local planner</title><link rel="stylesheet" href="/style.css">{tag}
<main>{nav(current)}<header><small>YOUNG &amp; NANAMI · LOCAL PLANNER</small><h1>{escape(heading)}</h1></header>{content}</main></html>"""


def view_url(view):
    if isinstance(view, net_worth_ui.Scope):
        return view.resolved_url()          # keeps the open tab and currency (L29)
    return "/" if view == "Household" else f"/?view={view}"


def section_for(kind):
    k = (kind or "").strip().lower()
    if k in ("checking", "cash"):
        return "Checking"
    if k in ("savings", "saving", "health savings", "hsa"):
        return "Savings"
    if k in ("retirement", "pension", "401k", "401(k)", "ira", "roth", "roth ira"):
        return "Retirement"
    if k in ("investments", "investment", "brokerage", "digital assets"):
        return "Investments"
    if k in ("credit card", "credit cards"):
        return "Credit cards"
    if k in ("loan", "loans", "student loan", "mortgage", "personal loan", "auto loan", "line of credit"):
        return "Loans"
    return "Other"


def toggle_person(current, person):
    has = set()
    if current in ("Young", "Joint"):
        has.add("Young")
    if current in ("Nanami", "Joint"):
        has.add("Nanami")
    if person in has:
        has.remove(person)
    else:
        has.add(person)
    if has == set(PEOPLE):
        return "Joint"
    return next(iter(has), "Unspecified")


def section_totals(rows):
    totals = {}
    for r in rows:
        c, amt = r["currency"], Decimal(r["balance"])
        totals[c] = totals.get(c, Decimal(0)) + (-amt if r["side"] == "debt" else amt)
    return " · ".join(money(v, c) for c, v in sorted(totals.items()))


def r_terms(row):
    """Terms attached by render_home (None for rows read elsewhere)."""
    try:
        return row["_terms"]
    except (KeyError, IndexError):
        return None


def inline_edit_drawer(r, today, token, view, opened=False, values=None, error=None):
    rid = r["id"]
    v = values if (opened and values) else dict(r)
    owner_opts = "".join(f'<option value="{o}"{" selected" if v.get("owner") == o else ""}>{o}</option>' for o in OWNERS)
    side_opts = "".join(
        f'<option value="{k}"{" selected" if v.get("side") == k else ""}>{label}</option>'
        for k, label in (("asset", "Asset (owned)"), ("debt", "Debt (owed)")))
    err_html = f'<p class="notice" role="alert">{escape(error)}</p>' if (opened and error) else ""
    return (f'<tr class="edit-drawer" id="edit-row-{rid}" data-id="{rid}"{"" if opened else " hidden"}>'
            f'<td colspan="5">{err_html}'
            f'<form method="post" action="/edit/{rid}" class="drawer-form">'
            f'<input type="hidden" name="token" value="{token}">'
            f'{net_worth_ui.hidden(view)}'
            f'<input type="hidden" name="inline" value="1">'
            f'<div class="drawer-grid">'
            f'<label>Account name<input required name="nickname" maxlength="100" value="{escape(str(v.get("nickname", "")))}"></label>'
            f'<label>Institution<input required name="institution" maxlength="100" value="{escape(str(v.get("institution", "")))}"></label>'
            f'<label>Type / section<input required name="kind" list="types" maxlength="100" value="{escape(str(v.get("kind", "")))}"></label>'
            f'<label>Owner<select required name="owner">{owner_opts}</select></label>'
            f'<label>Asset or debt<select required name="side">{side_opts}</select></label>'
            f'<label>Currency<input required name="currency" maxlength="3" value="{escape(str(v.get("currency", "")))}"></label>'
            f'<label>Balance<input required name="balance" inputmode="decimal" value="{escape(str(v.get("balance", "")))}"></label>'
            f'<label>Balance as of<input required type="date" name="asof" max="{today}" value="{escape(str(v.get("asof", "")))}"></label>'
            f'<label>Update method<input required name="update_method" maxlength="100" value="{escape(str(v.get("update_method", "")))}"></label>'
            f'</div>'
            + terms_ui.drawer_fields(r, r_terms(r), section_for, values if opened else None,
                                     side=v.get("side"), currency=v.get("currency")) +
            f'<div class="drawer-actions">'
            f'<div class="drawer-left"><button type="submit">Save changes</button>'
            f'<button type="button" class="secondary drawer-cancel">Cancel</button></div>'
            f'<details class="inline-pop remove-pop"><summary class="danger-link">Remove account…</summary>'
            f'<div class="remove-confirm"><span>{REMOVE_WARNING}</span>'
            f'<button type="submit" formaction="/remove/{rid}" formnovalidate class="danger-btn">Confirm remove</button>'
            f'</div></details></div></form></td></tr>')


def account_row(r, today, token, view, excluded=False, open_edit=None, edit_values=None, edit_error=None, use_logos=False):
    rid = r["id"]
    name = escape(r["nickname"])
    inst = escape(r["institution"])
    logo = institution_logo(r["institution"], r["logo_domain"], use_cached=use_logos)
    sec = section_for(r["kind"])
    amt = Decimal(r["balance"])
    c = r["currency"]
    side = r["side"]
    stale = (today - date.fromisoformat(r["asof"])).days > 30
    owner = r["owner"]
    owner_pills = "".join(
        f'<button type="submit" name="owner" value="{p}"'
        f' class="pill-btn{" active" if owner in (p, "Joint") else ""}"'
        f' aria-pressed="{"true" if owner in (p, "Joint") else "false"}">{p}</button>'
        for p in PEOPLE)
    owner_cell = (f'<form method="post" action="/owner/{rid}" class="inline-form owner-pills">'
                  f'<input type="hidden" name="token" value="{token}">'
                  f'{net_worth_ui.hidden(view)}'
                  f'{owner_pills}</form>')
    rename_pop = (f'<details class="inline-pop">'
                  f'<summary class="icon-btn" title="Rename account" aria-label="Rename {name}">✎</summary>'
                  f'<form method="post" action="/rename/{rid}" class="pop-form">'
                  f'<input type="hidden" name="token" value="{token}">'
                  f'{net_worth_ui.hidden(view)}'
                  f'<input type="text" name="nickname" value="{name}" maxlength="100" required aria-label="New account name">'
                  f'<button type="submit" class="pop-save">Save</button></form></details>')
    sec_buttons = "".join(
        f'<button type="submit" name="section" value="{s}"'
        f' class="sec-opt{" active" if s == sec else ""}">{s}</button>'
        for s in SECTIONS)
    sec_pop = (f'<details class="inline-pop sec-pop">'
               f'<summary class="sec-chip" title="Move to another section">{escape(r["kind"])} ▾</summary>'
               f'<form method="post" action="/section/{rid}" class="sec-menu">'
               f'<input type="hidden" name="token" value="{token}">'
               f'{net_worth_ui.hidden(view)}'
               f'{sec_buttons}</form></details>')
    next_side = "asset" if side == "debt" else "debt"
    side_toggle = (f'<form method="post" action="/side/{rid}" class="inline-form">'
                   f'<input type="hidden" name="token" value="{token}">'
                   f'{net_worth_ui.hidden(view)}'
                   f'<input type="hidden" name="side" value="{next_side}">'
                   f'<button type="submit" class="side-btn {side}" title="Switch between Asset and Debt">'
                   f'{side.title()}</button></form>')
    toggle_label = "Include" if excluded else "Exclude"
    toggle_val = "0" if excluded else "1"
    exclude_btn = (f'<form method="post" action="/exclude/{rid}" class="inline-form">'
                   f'<input type="hidden" name="token" value="{token}">'
                   f'{net_worth_ui.hidden(view)}'
                   f'<input type="hidden" name="excluded" value="{toggle_val}">'
                   f'<button type="submit" class="link-btn">{toggle_label}</button></form>')
    stale_tag = ' <span class="stale-pill">Stale</span>' if stale else ""
    opened = str(rid) == str(open_edit)
    drawer = inline_edit_drawer(r, today, token, view, opened=opened, values=edit_values, error=edit_error)
    return (f'<tr class="acct-row{" is-excluded" if excluded else ""}">'
            f'<th scope="row" class="acct-cell"><div class="acct-with-logo">{logo}<div class="acct-info">'
            f'<div class="acct-head"><span class="acct-name">{name}</span>{rename_pop}</div>'
            f'<div class="acct-sub-row"><span class="acct-sub">{inst}</span>{sec_pop}'
            f'{terms_ui.flag_html(r, r_terms(r), section_for)}</div></div></div></th>'
            f'<td class="num bal-cell"><span class="amt {side}">{signed_money(amt, c, side)}</span> {side_toggle}</td>'
            f'<td class="owner-cell">{owner_cell}</td>'
            f'<td class="date-cell"><time datetime="{r["asof"]}">{r["asof"]}</time>{stale_tag}</td>'
            f'<td class="act-cell">{exclude_btn}'
            f'<a href="/edit/{rid}" class="edit-trigger" data-edit="{rid}" aria-controls="edit-row-{rid}"'
            f' aria-expanded="{"true" if opened else "false"}">Edit<span class="sr"> {name}</span></a></td>'
            f'</tr>{drawer}')


def dashboard(rows, today, view, token, net, has_sync=False, notices=(), error=None,
              open_edit=None, edit_values=None, use_logos=False, sync_note=""):
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
    sync_html = ""
    if has_sync:
        sync_html = (f'<form method="post" action="/sync" class="inline-form">'
                     f'<input type="hidden" name="token" value="{token}">'
                     f'{net_worth_ui.hidden(view)}'
                     f'<button type="submit" class="button">↻ Sync balances</button></form>' + sync_note)
    add_cls = "button secondary" if has_sync else "button"
    banner = ""
    if error and not open_edit:
        banner += f'<p class="notice" role="alert">{escape(error)}</p>'
    banner += "".join(f'<p class="notice" role="status">{escape(n)}</p>' for n in notices)
    choices = "".join(f'<option value="{k}">' for k in KINDS)

    if active:
        grouped = {s: [] for s in SECTIONS}
        for r in active:
            grouped[section_for(r["kind"])].append(r)
        bodies = []
        for sec in SECTIONS:
            sec_rows = grouped[sec]
            if not sec_rows:
                continue
            sub = section_totals(sec_rows)
            hdr = (f'<tr class="sec-row"><th colspan="5"><div class="sec-bar">'
                   f'<span>{sec} <span class="sec-count">{len(sec_rows)}</span></span>'
                   f'<span class="sec-total">{sub}</span></div></th></tr>')
            items = "".join(account_row(r, today, token, view, excluded=False, open_edit=open_edit,
                                        edit_values=edit_values, edit_error=error, use_logos=use_logos) for r in sec_rows)
            bodies.append(f'<tbody class="sec-group" data-section="{sec}">{hdr}{items}</tbody>')
        table = (f'<div class="table-wrap"><table class="acct-table">'
                 f'<thead><tr><th>Account</th><th class="num">Balance</th><th>Owner</th>'
                 f'<th>As of</th><th class="act-col"></th></tr></thead>'
                 f'{"".join(bodies)}</table></div>')
    else:
        empty_msg = ("No active accounts in this view. Click <strong>↻ Sync balances</strong> to pull linked accounts, "
                     "or <strong>+ Add account</strong> to add one manually."
                     if has_sync else "No active accounts in this view. Click <strong>+ Add account</strong> to start.")
        table = f'<p class="empty-note">{empty_msg}</p>'

    excluded_html = ""
    if excluded:
        ex_open = " open" if any(str(r["id"]) == str(open_edit) for r in excluded) else ""
        ex_body = "".join(account_row(r, today, token, view, excluded=True, open_edit=open_edit,
                                      edit_values=edit_values, edit_error=error, use_logos=use_logos) for r in excluded)
        excluded_html = (f'<details class="excluded-box"{ex_open}><summary>Excluded accounts ({len(excluded)})'
                         f' — not counted in totals</summary>'
                         f'<div class="table-wrap"><table class="acct-table">'
                         f'<thead><tr><th>Account</th><th class="num">Balance</th><th>Owner</th>'
                         f'<th>As of</th><th class="act-col"></th></tr></thead>'
                         f'<tbody>{ex_body}</tbody></table></div></details>')

    return page(
        f'{banner}<datalist id="types">{choices}</datalist>'
        f'<div class="top-bar"><nav class="owner-tabs" aria-label="Inventory views">{net.links}</nav>'
        f'<div class="top-actions">{sync_html}<a class="{add_cls}" href="/new">+ Add account</a></div></div>'
        f'<div class="cards">{cards}</div>'
        f'{net.html}'
        f'{table}{excluded_html}'
        f'<details class="help"><summary>How totals work</summary><p>Household counts every active account once. '
        f'Click Young, Nanami, or both on any row to assign ownership (selecting both marks it joint; individual views '
        f'overlap on joint accounts, so do not add them together). Click ✎ to rename, click the section tag to move, '
        f'or click Edit to update all fields in place—re-syncing updates balances only and keeps your names, sections, '
        f'and owners. Balances older than 30 days are marked stale and still included.</p></details>',
        current="/", script="/dashboard.js")


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

    def setup(self):
        if self.server.auth:
            self.request.settimeout(15)
        super().setup()

    def send_bytes(self, code, body, kind, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if self.server.auth else cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send(self, code, text, kind="text/html; charset=utf-8", location=None, preview=False, scripts=False,
             signin=False, cookies=()):
        if kind.startswith("text/html") and not signin:
            if self.server.auth:
                text = text.replace('<a href="/transactions">Transactions</a>', "", 1)
            text = text.replace("<main>", "<main>" + mode_banner(self.server), 1)
            if self.server.auth and "<main>" in text:
                text = text.replace("</main>", self.sign_out_form() + "</main>", 1)
        body = text.encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        policy = "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        if signin:
            gis = "https://accounts.google.com/gsi/"
            policy = f"default-src 'none'; style-src {gis}style; frame-ancestors 'none'; base-uri 'none'; "
            extra = (f"script-src 'self' {gis}client; "
                     f"connect-src 'self' {gis}; frame-src {gis}; form-action 'none'")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        elif preview:
            extra = "script-src 'self'; connect-src 'none'; form-action " + ("'self'" if self.server.auth else "'none'")
        elif scripts:
            extra = "img-src 'self'; script-src 'self'; connect-src 'none'; form-action 'self'"
        else:
            extra = "form-action 'self'"
        self.send_header("Content-Security-Policy", policy + extra)
        if self.server.auth:
            self.send_header("X-Content-Type-Options", "nosniff")
        # A no-referrer form sends Origin: null even on a local POST.
        referrer = "no-referrer" if preview and not self.server.auth else "same-origin"
        if signin:
            referrer = "strict-origin-when-cross-origin" if self.server.auth.config.secure else "no-referrer-when-downgrade"
        self.send_header("Referrer-Policy", referrer)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        if not self.server.auth:
            return super().send_error(code, message, explain)
        # Covers methods BaseHTTPRequestHandler rejects before our GET/POST dispatch.
        if code == 501:
            try:
                code = 405 if self.hosted_session() else 401
            except sqlite3.Error:
                code = 503
        # Some parser errors precede header parsing; never reflect raw input.
        self.close_connection = True
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cookie_value(self, kind):
        name = self.server.auth.config.name(kind) + "="
        matches = [part.strip()[len(name):]
                   for header in self.headers.get_all("Cookie", [])
                   for part in header.split(";") if part.strip().startswith(name)]
        return matches[0] if len(matches) == 1 else ""

    def hosted_session(self):
        return self.server.auth.session(self.cookie_value("session"))

    @property
    def token(self):
        return self.session_row[1] if self.server.auth else self.server.token

    def sign_out_form(self):
        if not getattr(self, "session_row", None):
            return ""
        return ('<form method="post" action="/logout">'
                f'<input type="hidden" name="token" value="{self.token}">'
                '<button type="submit" class="secondary">Sign out</button></form>')

    def sign_in(self):
        return self.send(200, f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Private dashboard · Sign in</title>
<script defer src="https://accounts.google.com/gsi/client"></script>
<script defer src="/auth/sign-in.js"></script>
<h1>Sign in to the private dashboard</h1>
<div id="google-button" data-client="{escape(self.server.auth.config.client)}"></div>
<p id="status" role="status">Loading sign-in…</p>
<noscript>JavaScript is required for Google sign-in.</noscript></html>""", signin=True)

    def exact_origin(self):
        return self.headers.get_all("Origin", []) == [self.server.auth.config.origin]

    def read_form(self, maximum):
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                or int(lengths[0]) > maximum or self.headers.get_all("Transfer-Encoding")):
            raise Rejected(413)
        kinds = self.headers.get_all("Content-Type", [])
        if len(kinds) != 1 or not re.fullmatch(r"application/x-www-form-urlencoded(?:; *charset=utf-8)?",
                                               kinds[0], re.IGNORECASE):
            raise Rejected(415)
        try:
            values = parse_qs(self.rfile.read(int(lengths[0])).decode("utf8", "replace"),
                              keep_blank_values=bool(self.server.auth), max_num_fields=64)
        except ValueError:
            raise Rejected(400) from None
        if self.server.auth and any(len(v) != 1 for v in values.values()):
            raise Rejected(400)
        return {k: v[0] for k, v in values.items()}

    def auth_post(self, raw_target):
        auth = self.server.auth
        config = auth.config
        if not self.exact_origin():
            raise Rejected(403)
        if raw_target == "/auth/challenge":
            if (self.headers.get_all("Content-Length", []) not in ([], ["0"]) or
                    self.headers.get_all("Transfer-Encoding")):
                raise Rejected(400)
            browser, nonce = auth.challenge(self.cookie_value("challenge"))
            return self.send(200, json.dumps({"nonce": nonce}), "application/json",
                             cookies=(config.cookie("challenge", browser, CHALLENGE_TTL),))
        if raw_target == "/auth/exchange":
            data = self.read_form(12000)
            cookie = auth.exchange(self.cookie_value("challenge"), data.get("credential", ""),
                                   self.cookie_value("session"))
            return self.send(204, "", "text/plain",
                             cookies=(config.cookie("session", cookie, SESSION_TTL), config.cookie("challenge")))
        data = self.read_form(8192)
        self.require_csrf(data)
        auth.revoke(self.session_row[0], self.cookie_value("challenge"))
        return self.send(303, "", "text/plain", location="/",
                         cookies=(config.cookie("session"), config.cookie("challenge")))

    def require_csrf(self, data):
        if not secrets.compare_digest(data.get("token", "").encode(), self.token.encode()):
            raise Rejected(403)

    def reject(self, exc):
        self.close_connection = True
        messages = {400: "Invalid request.", 401: "Sign in to continue.", 403: "Request not allowed.",
                    413: "Form too large.", 415: "Unsupported form type.", 503: "Dashboard temporarily unavailable."}
        return self.send(exc.status, messages[exc.status], "text/plain; charset=utf-8")

    def local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def render_home(self, *args, **kwargs):
        status, text, scripts = self.home_page(*args, **kwargs)
        return self.send(status, text, scripts=scripts)

    def home_page(self, view, notices=(), error=None, status=200, open_edit=None, edit_values=None,
                  form_state=None):
        """(status, html, scripts) for the Accounts page."""
        scope = view if isinstance(view, net_worth_ui.Scope) else net_worth_ui.Scope(view)
        view = str(scope)
        owner_scope = "(? = 'Household' OR owner IN (?, 'Joint'))"
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")  # Read current cards and dated totals from one database snapshot.
            rows = db.execute(f"""SELECT * FROM accounts WHERE {owner_scope}
                                 ORDER BY excluded, side, institution COLLATE NOCASE, nickname COLLATE NOCASE, id""",
                              (view, view)).fetchall()
            terms = load_terms(db)
            rows = [dict(r, _terms=terms.get(r["id"])) for r in rows]
            points = db.execute(f"""SELECT h.*, a.asof AS current_asof
                                   FROM balance_history AS h JOIN accounts AS a ON a.id = h.account_id
                                   WHERE a.excluded = 0 AND {owner_scope} ORDER BY h.asof, h.account_id""",
                                (view, view)).fetchall()
            try:
                net = net_worth_ui.build(
                    db, scope, PEOPLE, timeline=net_worth(points), money=money, form_state=form_state,
                    has_backdated=any(p["asof"] > p["current_asof"] for p in points),
                    projection=dict(token=self.token, section_for=section_for, places=places, today=date.today(),
                                    suggestions=not self.server.auth))
            except ValueError as exc:
                return 400, str(exc), False
        return status, dashboard(rows, date.today(), scope, self.token, net,
                                 has_sync=bool(self.server.batch), notices=notices, error=error,
                                 open_edit=open_edit, edit_values=edit_values,
                                 use_logos=bool(self.server.logo_dir),
                                 sync_note=sync_status.note(self.server.db) if self.server.batch else ""), True

    def projection_rerender(self, view, currency, attempted, errors):
        """A refused assumptions save, shown inside the Projection tab with the attempted values kept."""
        status, text, _ = self.home_page(net_worth_ui.Scope(view, "projection", currency), status=422,
                                         form_state=(attempted, errors))
        return status, text, None

    def do_GET(self):
        try:
            return self.route_get()
        except sqlite3.Error:
            if not self.server.auth:
                raise
            return self.reject(Rejected(503))

    def route_get(self):
        if self.server.auth:
            # Check the raw target, before http.server's leading // normalization.
            raw_target = self.requestline.split()[1]
            if raw_target == "/auth/sign-in.js":
                return self.send(200, (ROOT / "sign-in.js").read_text(), "application/javascript")
            self.session_row = self.hosted_session()
            if not self.session_row:
                return self.sign_in() if raw_target == "/" else self.reject(Rejected())
            if not raw_target.startswith("/") or raw_target.startswith("//"):
                return self.reject(Rejected(400))
        elif not self.local_host():
            return self.send(403, "Local requests only.")
        if urlsplit(self.path).path == "/batch":
            return self.send(303, "", location="/")
        result = servicer_get(self.path, page=page, form=form, token=self.token)
        if result is not None:
            return self.send(*result)
        path = urlsplit(self.path).path
        if path == "/projections":      # L29: the page moved into Accounts; old links and bookmarks keep working
            status, body, location = projection_redirect(self.server.db, self.path, page=page, people=PEOPLE)
            return self.send(status, body, location=location)
        if path == "/federal-preview":
            return self.send(200, (ROOT / "preview.html").read_text(), preview=True)
        if path == "/preview.js":
            return self.send(200, (ROOT / "preview.js").read_text(), "application/javascript")
        if path == "/dashboard.js":
            return self.send(200, (ROOT / "dashboard.js").read_text(), "application/javascript")
        if path == "/favicon.ico":
            return self.send_bytes(204, b"", "image/x-icon", cache="private, max-age=86400")
        if path == "/style.css":
            return self.send(200, (ROOT / "style.css").read_text(), "text/css")
        if path.startswith("/logo/") and self.server.logo_dir:
            raw_domain = path[len("/logo/"):]
            domain = sanitize_domain(raw_domain)
            if not domain or domain != raw_domain:
                return self.send(404, "Not found.")
            allowed = domain in KNOWN_DOMAIN_SET
            if not allowed:
                with closing(sqlite3.connect(self.server.db)) as db:
                    allowed = bool(db.execute("SELECT 1 FROM accounts WHERE logo_domain=? LIMIT 1", (domain,)).fetchone())
            if not allowed:
                return self.send(404, "Not found.")
            found = cached_logo(self.server.logo_dir, domain, fetcher=self.server.logo_fetcher,
                                hosted=bool(self.server.auth))
            if found:
                return self.send_bytes(200, found[0], found[1], cache="private, max-age=86400")
            return self.send_bytes(200, fallback_svg(domain.split(".")[0]).encode(), "image/svg+xml")
        if path == TX_PATH and not self.server.auth:
            notices, self.server.notices = self.server.notices, []
            try:
                with closing(sqlite3.connect(self.server.db)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN")
                    body = render_transactions(db, urlsplit(self.path).query, token=self.token,
                                               allowed_views={"Household", *PEOPLE},
                                               can_sync=bool(self.server.batch), logo_dir=self.server.logo_dir)
            except RouteProblem as exc:
                return self.send(exc.status, page(f'<p class="notice" role="alert">{escape(str(exc))}</p>',
                                                  current=TX_PATH, heading="Transactions"))
            banner = "".join(f'<p class="notice" role="status">{escape(n)}</p>' for n in notices)
            return self.send(200, page(banner + body, current=TX_PATH, heading="Transactions"), scripts=True)
        if path == "/new":
            return self.send(200, form({"currency": "USD", "asof": str(date.today()), "owner": "Unspecified",
                                        "update_method": DEFAULT_METHOD}, path, self.token))
        if path == "/":
            try:
                scope = net_worth_ui.parse(urlsplit(self.path).query, PEOPLE)
            except ValueError as exc:
                return self.send(400, str(exc))
            notices, self.server.notices = self.server.notices, []
            return self.render_home(scope, notices=notices)
        with closing(sqlite3.connect(self.server.db)) as db:
            db.row_factory = sqlite3.Row
            match = re.fullmatch(r"/(edit|remove)/([1-9][0-9]{0,17})", path)
            if match:
                row = db.execute("SELECT * FROM accounts WHERE id=?", (match[2],)).fetchone()
                if row and match[1] == "remove":
                    return self.send(200, page(f"""<h2>Remove {escape(row['nickname'])}?</h2>
<p>{REMOVE_WARNING}</p><form method="post" action="{path}" class="edit-form">
<input type="hidden" name="token" value="{self.token}">
<div class="bar"><button>Confirm remove</button><a href="/" class="button secondary">Cancel</a></div></form>""", current="/"))
                if row:
                    return self.send(200, form(dict(row), path, self.token))
        self.send(404, "Account or page not found.")

    def do_POST(self):
        try:
            return self.route_post()
        except sqlite3.Error:
            if not self.server.auth:
                raise
            return self.reject(Rejected(503))

    def route_post(self):
        if self.server.auth:
            raw_target = self.requestline.split()[1]
            self.session_row = self.hosted_session()
            try:
                if raw_target in {"/auth/challenge", "/auth/exchange"}:
                    return self.auth_post(raw_target)
                if not self.session_row:
                    raise Rejected()
                if not self.exact_origin():
                    raise Rejected(403)
                if raw_target == "/logout":
                    return self.auth_post(raw_target)
                # Read/validate before dispatch, even for unknown routes.
                data = self.read_form(8192)
                self.require_csrf(data)
            except Rejected as exc:
                return self.reject(exc)
        elif not self.local_host() or self.headers.get("Origin") not in {None, f"http://{self.headers.get('Host')}"}:
            return self.send(403, "Local requests only.")
        if self.path == "/projections":
            if not self.server.auth:
                try:
                    data = self.read_form(8192)
                    self.require_csrf(data)
                except Rejected as exc:
                    return self.reject(exc)
            status, body, location = projection_post(
                self.server.db, data, page=page, token=self.token, people=PEOPLE,
                section_for=section_for, places=places, today=date.today(), suggestions=not self.server.auth,
                where=lambda v, c: net_worth_ui.Scope(v, "projection", c).url(anchor=True),
                rerender=self.projection_rerender)
            return self.send(status, body, location=location, scripts=True)
        target = self.path
        if not self.server.auth and target in (TX_EDIT, TX_SYNC):
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                    or int(lengths[0]) > 8192 or self.headers.get_all("Transfer-Encoding")):
                return self.send(413, "Form too large.")
            kinds = self.headers.get_all("Content-Type", [])
            if len(kinds) != 1 or not re.fullmatch(r"application/x-www-form-urlencoded(?:; *charset=utf-8)?",
                                                   kinds[0], re.IGNORECASE):
                return self.send(415, "Unsupported form type.")
            raw_body = self.rfile.read(int(lengths[0]))
            try:
                with closing(sqlite3.connect(self.server.db, isolation_level=None)) as db:
                    db.row_factory = sqlite3.Row
                    result = post_transactions(db, target, raw_body, token=self.token,
                                               allowed_views={"Household", *PEOPLE},
                                               can_sync=bool(self.server.batch))
            except RouteProblem as exc:
                return self.send(exc.status, page(problem_body(exc), current=TX_PATH, heading="Transactions"),
                                 scripts=True)
            if result.sync:
                try:
                    sync_res = sync_transactions(self.server.batch, include_pending=result.include_pending)
                except Problem as exc:
                    return self.send(422, page(f'<p class="notice" role="alert">{escape(str(exc))}</p>',
                                               current=TX_PATH, heading="Transactions"))
                tail = "Caught up." if sync_res.caught_up else "More history available — click Sync again later."
                self.server.notices = [f"Synced transactions ({sync_res.windows} window(s)). {tail}",
                                       *sync_res.warnings,
                                       *classify_after_sync(self.server.db, hosted=bool(self.server.auth))]
            return self.send(303, "", location=result.location)
        edit = re.fullmatch(r"/edit/([1-9][0-9]{0,17})", target)
        remove = re.fullmatch(r"/remove/([1-9][0-9]{0,17})", target)
        owner_act = re.fullmatch(r"/owner/([1-9][0-9]{0,17})", target)
        rename_act = re.fullmatch(r"/rename/([1-9][0-9]{0,17})", target)
        section_act = re.fullmatch(r"/section/([1-9][0-9]{0,17})", target)
        exclude_act = re.fullmatch(r"/exclude/([1-9][0-9]{0,17})", target)
        side_act = re.fullmatch(r"/side/([1-9][0-9]{0,17})", target)
        sync_act = target == "/sync" and bool(self.server.batch)
        if not any((edit, remove, owner_act, rename_act, section_act, exclude_act, side_act, sync_act, target == "/new")):
            return self.send(404, "Page not found.")
        if not self.server.auth:
            length = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > 8192:
                return self.send(413, "Form too large.")
            data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(length)).decode("utf8", "replace")).items()}
            if not secrets.compare_digest(data.get("token", "").encode(), self.token.encode()):
                return self.send(403, "Reload the form and try again.")
        back = net_worth_ui.from_form(data, PEOPLE, self.server.db)
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
                new_owner = toggle_person(row[0], chosen) if chosen in PEOPLE else (
                    "Unspecified" if row[0] == chosen else chosen)
                db.execute("UPDATE accounts SET owner=? WHERE id=?", (new_owner, owner_act[1]))
            return self.send(303, "", location=view_url(back))
        if rename_act:
            nickname = data.get("nickname", "").strip()
            if not 1 <= len(nickname) <= 100:
                return self.render_home(back, error="Account name: enter 1–100 characters.", status=422)
            with closing(sqlite3.connect(self.server.db)) as db, db:
                changed = db.execute("UPDATE accounts SET nickname=? WHERE id=?", (nickname, rename_act[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
            return self.send(303, "", location=view_url(back))
        if section_act:
            sec = data.get("section", "")
            if sec not in SECTION_KIND:
                return self.send(422, "Invalid section.")
            with closing(sqlite3.connect(self.server.db)) as db, db:
                changed = db.execute("UPDATE accounts SET kind=? WHERE id=?",
                                     (SECTION_KIND[sec], section_act[1])).rowcount
                if not changed:
                    return self.send(404, "Account not found.")
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
            return self.send(303, "", location=view_url(back))
        try:
            values = validate(data, date.today())
            terms = (parse_terms(data, values["currency"], values["side"], places(values["currency"]),
                                 section_for, values["kind"]) if edit and data.get("terms") == "1" else None)
        except ValueError as exc:
            if edit and data.get("inline") == "1":
                return self.render_home(back, error=str(exc), status=422, open_edit=edit[1], edit_values=data)
            return self.send(422, form(data, target, self.token, str(exc)))
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
                if terms is not None:
                    save_terms(db, edit[1], terms)
            else:
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,
                              owner,update_method,excluded,side_manual) VALUES (?,?,?,?,?,?,?,?,?,0,1)""", params)
        self.send(303, "", location=view_url(back) if (edit and data.get("inline") == "1") else "/")


class HostedServer(ThreadingHTTPServer):
    daemon_threads = False  # Drain request threads before allowing an offline restore.

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.state_lock.close()


def serve(path, port, provider=None, logo_dir=None, *, hosted=None, keys=None, clock=None):
    with ExitStack() as locks:
        auth = None
        if hosted:
            require_crypto()
            path = db_path(path)
            locks.enter_context(offline_lock(path))
            os.close(os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600))
            require_clean(path)  # read-only, before init_db or any other write: no transaction data on the host
            if logo_dir:
                logo_dir = external(logo_dir)
                logo_dir.mkdir(mode=0o700, exist_ok=True)
                private(logo_dir, directory=True)
        init_db(path)
        reclaimed = reclaim(path) if hosted else 0  # purge deleted rows from free pages; the guard counts rows, not bytes
        if hosted:
            auth = Auth(path, hosted, keys=keys, **({"clock": clock} if clock is not None else {}))
            # Construct unbound so state_lock is available even if binding fails.
            server = HostedServer(("127.0.0.1", port), Handler, bind_and_activate=False)
            server.state_lock = locks.pop_all()
            try:
                server.server_bind()
                server.server_activate()
            except BaseException:
                server.server_close()
                raise
        else:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.auth = auth
    server.reclaimed = reclaimed
    server.token = None if hosted else secrets.token_urlsafe(32)
    server.db = path
    server.batch = Batch(path, provider, validate) if provider else None
    server.notices = []
    server.logo_dir = logo_dir
    from logos import _default_fetch, hosted_fetch
    server.logo_fetcher = hosted_fetch if hosted else _default_fetch
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--hosted", action="store_true", help="Require approved household Google sign-in")
    parser.add_argument("--hosted-env", type=Path, help="Load hosted settings from a private env file")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Fictional walkthrough, disposable database only")
    modes.add_argument("--bridge", action="store_true", help="Enable configured SimpleFIN Bridge")
    args = parser.parse_args()
    if args.hosted_env and not args.hosted:
        parser.error("--hosted-env requires --hosted.")
    if (args.demo and (args.hosted or args.db)) or (args.bridge and args.db and not args.hosted):
        parser.error("--demo cannot be combined with --hosted or --db; --bridge with --db requires --hosted.")
    os.umask(0o077)
    with ExitStack() as stack:
        path, provider, hosted = args.db or DEFAULT_DB, None, None
        if args.hosted:
            try:
                hosted = Config.load(load_env_file(args.hosted_env) if args.hosted_env else os.environ)
                path = external(path)
                require_crypto()
                path = db_path(path)
            except (OSError, ValueError) as exc:
                parser.exit(1, f"Hosted startup refused: {exc}\n")
        if args.demo:
            from demo_batch import Demo, seed
            path = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "fictional.sqlite3"
            provider = Demo()
        if args.bridge:
            try:
                provider = Bridge()
            except (Problem, OSError, UnicodeError):
                parser.exit(1, "Bridge disabled: missing or unsafe credential storage. See BULK.md.\n")
        logo_dir = None if args.demo else path.parent / "logos"
        try:
            server = stack.enter_context(serve(path, args.port, provider, logo_dir=logo_dir, hosted=hosted))
        except Refused as exc:
            parser.exit(1, f"Hosted startup refused: {exc}\n")
        except (OSError, ValueError, sqlite3.Error) as exc:
            if not hosted:
                raise
            parser.exit(1, f"Hosted startup refused ({type(exc).__name__}); check private state and listener.\n")
        server.demo = args.demo
        if args.demo:
            with closing(sqlite3.connect(path)) as db, db:
                seed(db)
        if server.reclaimed:
            print(f"Hosted startup: compacted the database file ({server.reclaimed} free pages removed).", flush=True)
        address = hosted.origin if hosted else f"http://127.0.0.1:{server.server_port}"
        print(f"Open {address}/ · Ctrl-C to stop", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

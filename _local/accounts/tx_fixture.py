"""Shared fictional fixtures for transaction tests: temp SQLite DB and an injected Bridge wire."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

from bridge import Bridge
from bulk import Batch, fingerprint
from server import init_db, validate
from transactions import PAGE, install_transactions, monthly_snapshot, sync_transactions
from transactions_ui import EDIT, post_transactions
from tx_categories import add_category


def epoch(iso):
    dt = datetime.fromisoformat(iso)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


def tx(key, when, amount, description, **extra):
    return dict(id=key, posted=epoch(when) if when else 0, amount=amount,
                description=description, **extra)


TOKEN = "fictional-page-token"
NOW = epoch("2026-12-15T12:00:00")
VIEWS = {"Household", "Young", "Nanami"}


class Fixture:
    """Shared fictional on-disk DB + injected Bridge wire (mix into a TestCase)."""
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="fictional-transactions-",
                                                             dir=Path(tempfile.gettempdir()).resolve()))
        self.path = Path(temp) / "accounts.sqlite3"
        init_db(self.path)
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            install_transactions(self.db)
            install_transactions(self.db)
        secret = Path(temp) / "fixture-access"
        secret.touch(mode=0o600)
        secret.write_text("https://fixture:fictional-secret-NOT-live@bridge.simplefin.org/simplefin/fixture")
        self.accounts, self.calls = {}, []
        self.alter = lambda data: data
        self.bridge = Bridge(secret, self.wire)
        self.batch = Batch(self.path, self.bridge, validate)

    def account(self, key, kind="Checking", owner="Young", currency="USD", excluded=False, linked=True):
        with self.db:
            account = self.db.execute("""INSERT INTO accounts
                (institution,nickname,kind,side,currency,balance,asof,owner,excluded)
                VALUES ('Fictional institution',?,?,?,?, '0', '2026-11-01', ?,?)""",
                                      (key, kind, "asset", currency, owner, excluded)).lastrowid
            if linked:
                source = fingerprint(self.bridge.namespace, "fictional-connection", key)
                self.db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status)"
                                " VALUES (?,?,0,0,'','Fictional')", (source, account))
                self.accounts[key] = dict(conn_id="fictional-connection", id=key, currency=currency,
                                          name=key, **{"balance": "0", "balance-date": NOW},
                                          transactions=[])
        return account

    def wire(self, method, host, path, headers):
        self.assertEqual((method, host), ("GET", "bridge.simplefin.org"))
        self.assertIn("Authorization", headers)
        parts = urlsplit(path)
        self.assertEqual(parts.path, "/simplefin/fixture/accounts")
        args = parse_qs(parts.query)
        self.assertEqual(args.pop("version"), ["2"])
        lo, hi = (int(args.pop(k)[0]) for k in ("start-date", "end-date"))
        self.assertTrue(0 < hi - lo <= PAGE)
        pending = args.pop("pending", None)
        self.assertIn(pending, (None, ["1"]))
        self.assertEqual(args, {})
        self.calls.append((lo, hi, pending is not None))
        data = dict(connections=[dict(conn_id="fictional-connection")], errlist=[],
                    accounts=[dict(a, transactions=[
                        t for t in a["transactions"] if
                        (pending is not None if t.get("pending") else lo <= t["posted"] < hi)])
                        for a in self.accounts.values()])
        reply = self.alter(data)
        return reply if isinstance(reply, tuple) else (200, json.dumps(reply).encode())

    def cat(self, *names):
        with self.db:
            for name in names:
                try:
                    add_category(self.db, name)
                except ValueError:
                    pass

    def add(self, account, *rows):
        self.accounts[account]["transactions"].extend(rows)

    def sync(self, **kwargs):
        return sync_transactions(self.batch, **({"now": NOW} | kwargs))

    def snap(self, month="2026-11", view="Household", **opts):
        return monthly_snapshot(self.db, month, view, **opts)

    def row(self, description, month="2026-11", **opts):
        return next(r for r in self.snap(month, **opts).rows if r["description"] == description)

    def post(self, row=None, path=EDIT, allowed_views=VIEWS, can_sync=True, **fields):
        data = dict(token=TOKEN, month="2026-11", view="Household") | fields
        if row is not None:
            data["row"] = str(row)
        return post_transactions(self.db, path, urlencode(data).encode(), token=TOKEN,
                                 allowed_views=allowed_views, can_sync=can_sync)



from contextlib import contextmanager
import socket
from unittest import mock


@contextmanager
def block_network(allow_loopback=False):
    """Fail any attempt to resolve or connect (optionally allowing 127.0.0.1/::1 for local servers)."""
    real_connect, real_create, real_resolve = socket.socket.connect, socket.create_connection, socket.getaddrinfo

    def local(address):
        return isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost")

    def connect(self, address):
        if allow_loopback and local(address):
            return real_connect(self, address)
        raise AssertionError(f"network blocked: {address!r}")

    def create(address, *a, **k):
        if allow_loopback and local(address):
            return real_create(address, *a, **k)
        raise AssertionError(f"network blocked: {address!r}")

    def resolve(host, *a, **k):
        if allow_loopback and host in ("127.0.0.1", "::1", "localhost"):
            return real_resolve(host, *a, **k)
        raise AssertionError(f"network blocked: {host!r}")

    with mock.patch.object(socket.socket, "connect", connect), \
            mock.patch.object(socket, "create_connection", create), \
            mock.patch.object(socket, "getaddrinfo", resolve):
        yield


def put_row(db, account, source, currency, day, amount, description, *, payee="", transfer=None):
    """Insert a posted fictional transaction directly (months of history without a sync)."""
    from transactions import _merchant
    with db:
        db.execute("""INSERT INTO account_transactions (account_id, tx_id, source, posted_date, pending_date,
            amount, currency, description, payee, merchant, pending, pending_active, is_transfer, transfer_manual)
            VALUES (?,?,?,?,NULL,?,?,?,?,?,0,0,?,?)""",
                   (account, f"{description}-{day}-{amount}", source, day, amount, currency, description, payee,
                    _merchant(payee or description), bool(transfer), transfer is not None))


def cover(db, start_iso, end_iso):
    """Mark every linked account as fetched over [start, end) so months count as covered."""
    from transactions import _midnight
    from datetime import date
    lo, hi = _midnight(date.fromisoformat(start_iso)), _midnight(date.fromisoformat(end_iso))
    with db:
        db.execute("DELETE FROM transaction_coverage")
        db.execute("""INSERT INTO transaction_coverage SELECT l.source, a.id, a.currency, ?, ?, 1
                      FROM source_links l JOIN accounts a ON a.id=l.local""", (lo, hi))


def source_of(db, account):
    return db.execute("SELECT source FROM source_links WHERE local=?", (account,)).fetchone()[0]

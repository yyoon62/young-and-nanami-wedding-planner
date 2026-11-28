"""Fictional household for L29 tests and screenshots only. Never touches a real database.

    seed(path)                  # Young/Nanami/Joint accounts, USD + EUR, debts with terms, 3+ dated history points
    seed(path, backdated=True)  # plus a backdated edit: saved history is later than the account's current as-of
    frozen_today()              # pins the server's clock (2026-11-28) so stale labels and month averages are stable
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3
from unittest import mock

import server
from account_terms import save_terms
from projections import Assumptions, save_assumptions
from server import init_db
from tx_fixture import cover, put_row

TODAY = date(2026, 11, 28)


class _Date(date):
    @classmethod
    def today(cls):
        return TODAY


@contextmanager
def frozen_today():
    with mock.patch.object(server, "date", _Date):
        yield TODAY


# key -> (institution, nickname, kind, side, currency, balance, as-of, owner, terms or None)
ACCOUNTS = {
    "young-chk": ("Birch Bank", "Young Checking", "Checking", "asset", "USD", "3900.00", "2026-11-27", "Young", None),
    "joint-sav": ("Birch Bank", "Joint Savings", "Savings", "asset", "USD", "15000.00", "2026-11-27", "Joint", None),
    "cedar-sav": ("Cedar Bank", "Cedar High-Yield Savings", "Savings", "asset", "USD", "7600.00", "2026-10-12", "Joint",
                  dict(rate="4.250", rate_kind="variable")),
    "nanami-card": ("Cedar Bank", "Nanami Rewards Card", "Credit card", "debt", "USD", "1500.00", "2026-11-25", "Nanami",
                    dict(rate="24.990", promo_rate="0", promo_end="2027-04", planned_payment="100.00", paid_in_full=0)),
    "nanami-eur": ("Maple Trust", "Nanami EUR Checking", "Checking", "asset", "EUR", "3000.00", "2026-11-25", "Nanami", None),
    "nanami-eur-sav": ("Maple Trust", "Nanami EUR Savings", "Savings", "asset", "EUR", "6000.00", "2026-11-20", "Nanami", None),
    "maple-a": ("Maple Servicing", "Maple Student Loan A", "Student loan", "debt", "USD", "12000.00", "2026-09-30", "Young",
                dict(rate="5.500", min_payment="140.00")),
    "maple-b": ("Maple Servicing", "Maple Student Loan B", "Student loan", "debt", "USD", "3000.00", "2026-11-20", "Young", None),
    "birch-auto": ("Birch Credit Union", "Birch Auto Loan", "Auto loan", "debt", "USD", "8400.00", "2026-11-20", "Joint",
                   dict(rate="6.900", min_payment="210.00", planned_payment="250.00")),
    "oak-card": ("Oak Bank", "Oak Everyday Card", "Credit card", "debt", "USD", "620.00", "2026-11-26", "Joint", None),
}
# Earlier dated snapshots, written the way a past sync would (account key, as-of, balance): at least 3 dates per currency.
EARLIER = (("young-chk", "2026-10-01", "3600.00"), ("young-chk", "2026-11-03", "3750.00"),
           ("joint-sav", "2026-10-05", "14000.00"), ("cedar-sav", "2026-09-12", "7300.00"),
           ("nanami-card", "2026-10-15", "1800.00"), ("maple-a", "2026-09-01", "12400.00"),
           ("nanami-eur", "2026-10-10", "2800.00"), ("nanami-eur-sav", "2026-10-20", "5500.00"),
           ("birch-auto", "2026-10-20", "8650.00"))


def seed(path, *, backdated=False):
    init_db(path)
    ids = {}
    with closing(sqlite3.connect(path)) as db:
        with db:
            for key, (institution, nickname, kind, side, currency, balance, asof, owner, terms) in ACCOUNTS.items():
                ids[key] = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,update_method)
                    VALUES (?,?,?,?,?,?,?,?,'Fictional')""",
                                      (institution, nickname, kind, side, currency, balance, asof, owner)).lastrowid
                if terms:
                    save_terms(db, ids[key], terms)
            for key, asof, balance in EARLIER:
                db.execute("""INSERT OR REPLACE INTO balance_history (account_id, asof, balance, currency, side)
                    SELECT id, ?, ?, currency, side FROM accounts WHERE id=?""", (asof, balance, ids[key]))
            save_assumptions(db, "Household", "USD", replace(
                Assumptions(), years=10, income=Decimal("5200"), spending=Decimal("3400"), debt_target=Decimal("700")))
            for key in ("young-chk", "oak-card"):            # two feeds: payroll + spending (Aug-Oct = three full months)
                db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES (?,?,0,1,'','ok')",
                           (f"fictional-{key}", ids[key]))
        income = {"2026-08": "3000.00", "2026-09": "2900.00", "2026-10": "3100.00"}
        for month, pay in income.items():
            put_row(db, ids["young-chk"], "fictional-young-chk", "USD", f"{month}-01", pay, "PAYROLL FICTIONAL ROBOTICS")
            put_row(db, ids["young-chk"], "fictional-young-chk", "USD", f"{month}-03", "-1500.00", "Riverside Rent")
            put_row(db, ids["oak-card"], "fictional-oak-card", "USD", f"{month}-14", "-480.00", "GREEN MARKET #17")
            put_row(db, ids["oak-card"], "fictional-oak-card", "USD", f"{month}-20", "-250.00", "SQ *MAPLE CAFE")
        cover(db, "2026-08-01", "2026-11-29")
        if backdated:     # edit Young Checking with an earlier as-of than the saved 2026-11-27 snapshot
            with db:
                db.execute("UPDATE accounts SET balance='3500.00', asof='2026-11-10' WHERE id=?", (ids["young-chk"],))
    return ids


@contextmanager
def running(path, provider=None):
    """Local server on a free port with the frozen clock; yields the server (use `fetch`)."""
    from threading import Thread
    with frozen_today(), server.serve(path, 0, provider) as srv:
        thread = Thread(target=srv.serve_forever)
        thread.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            thread.join()


def fetch(srv, path, data=None, *, headers=None):
    """(status, headers dict, body str) without following redirects. `data` makes it a form POST with the page token."""
    from http.client import HTTPConnection
    from urllib.parse import urlencode
    with closing(HTTPConnection("127.0.0.1", srv.server_port, timeout=5)) as conn:
        if data is None:
            conn.request("GET", path, headers=headers or {})
        else:
            body = urlencode({"token": srv.token, **data})
            conn.request("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()

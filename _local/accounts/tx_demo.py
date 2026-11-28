"""Fictional transactions for browser tests and screenshots only. Never touches a real database."""
from contextlib import contextmanager
from datetime import date, datetime
import sqlite3
from unittest import mock

import server
import transactions_ui
from server import init_db
from tx_fixture import cover, put_row

MONTHS = ("2026-08", "2026-09", "2026-10", "2026-11")
TODAY = date(2026, 11, 28)     # the fictional "now": Nov is the partial month, Aug-Oct the three full ones


class _Date(date):
    @classmethod
    def today(cls):
        return TODAY


class _Datetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=tz)


@contextmanager
def frozen_today():
    """Pin the server's clock so month navigation and 3-full-month averages do not drift with the real date."""
    with mock.patch.object(server, "date", _Date), mock.patch.object(transactions_ui, "datetime", _Datetime):
        yield TODAY


def seed_fictional(path, *, full_history=True, hostile="", categorize=False):
    """Four months for Household: payroll, rent, groceries, dining, a transfer pair, one EUR account."""
    init_db(path)
    db = sqlite3.connect(path)
    accounts = {}
    with db:
        for key, institution, kind, side, currency, owner in (
                ("chk", "Birch Bank", "Checking", "asset", "USD", "Young"),
                ("sav", "Birch Bank", "Savings", "asset", "USD", "Joint"),
                ("card", "Cedar Credit", "Credit card", "debt", "USD", "Nanami"),
                ("eur", "Maple Trust", "Checking", "asset", "EUR", "Nanami")):
            account = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,
                update_method) VALUES (?,?,?,?,?,'1000.00','2026-11-27',?,'Fictional')""",
                                 (institution, f"{institution} {kind}", kind, side, currency, owner)).lastrowid
            source = f"fictional-{key}"
            db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES (?,?,0,1,'','ok')",
                       (source, account))
            accounts[key] = (account, source, currency)

    def put(key, day, amount, description, **kw):
        account, source, currency = accounts[key]
        put_row(db, account, source, currency, day, amount, description, **kw)

    income = {"2026-08": "3000.00", "2026-09": "2900.00", "2026-10": "3100.00", "2026-11": "3000.00"}
    dining = {"2026-08": 300, "2026-09": 250, "2026-10": 200, "2026-11": 300}
    for month in MONTHS:
        put("chk", f"{month}-01", income[month], "PAYROLL FICTIONAL ROBOTICS")
        put("chk", f"{month}-03", "-1500.00" if month != "2026-09" else "-1600.00", "Riverside Rent")
        put("card", f"{month}-09", f"-{dining[month]}.00", "SQ *MAPLE CAFE 04/12 #1234")
        put("card", f"{month}-14", "-480.00" if month != "2026-11" else "-500.00", "GREEN MARKET #17")
        put("chk", f"{month}-16", "-80.00", "Metro Transit Pass")
        put("chk", f"{month}-20", "-600.00", "Transfer to savings", transfer=True)
        put("sav", f"{month}-20", "600.00", "Transfer from checking", transfer=True)
    put("eur", "2026-11-11", "-42.50", "Boulangerie Fictive")
    if hostile:
        put("card", "2026-11-18", "-9.99", hostile, payee=hostile)
    if categorize:   # rent, groceries and dining are set by hand; transit, the bakery and payroll stay open
        with db:
            for like, category in (("Riverside Rent", "Housing"), ("GREEN MARKET%", "Groceries"),
                                   ("SQ *MAPLE CAFE%", "Dining")):
                db.execute("UPDATE account_transactions SET category=?, category_manual=1 WHERE description LIKE ?",
                           (category, like))
    start = "2026-08-01" if full_history else "2026-10-20"
    cover(db, start, "2026-11-29")
    db.close()
    return accounts

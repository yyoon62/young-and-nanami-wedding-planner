"""Fictional debts with terms for browser tests and screenshots only (L23). Never touches a real database."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import sqlite3

from account_terms import save_terms
from projections import Assumptions, save_assumptions
from server import init_db
from tx_fixture import cover, put_row

# nickname -> (institution, kind, side, balance, as-of, owner, terms or None)
ACCOUNTS = {
    "birch-auto": ("Birch Credit Union", "Birch Auto Loan", "Auto loan", "debt", "8400.00", "2026-11-20", "Joint",
                   dict(rate="6.900", min_payment="210.00", planned_payment="250.00")),
    "cedar-card": ("Cedar Bank", "Cedar Rewards Card", "Credit card", "debt", "1500.00", "2026-11-25", "Nanami",
                   dict(rate="24.990", promo_rate="0", promo_end="2027-04", planned_payment="100.00", paid_in_full=0)),
    "maple-a": ("Maple Servicing", "Maple Student Loan A", "Student loan", "debt", "12000.00", "2026-09-30", "Young",
                dict(rate="5.500", min_payment="140.00")),
    "maple-b": ("Maple Servicing", "Maple Student Loan B", "Student loan", "debt", "3000.00", "2026-11-20", "Young", None),
    "oak-card": ("Oak Bank", "Oak Everyday Card", "Credit card", "debt", "620.00", "2026-11-26", "Joint", None),
    "birch-chk": ("Birch Bank", "Birch Checking", "Checking", "asset", "4000.00", "2026-11-27", "Joint", None),
    "cedar-sav": ("Cedar Bank", "Cedar High-Yield Savings", "Savings", "asset", "8000.00", "2026-11-26", "Joint",
                  dict(rate="4.250", rate_kind="variable")),
    "birch-sav": ("Birch Bank", "Birch Savings", "Savings", "asset", "15000.00", "2026-11-27", "Joint", None),
}


def seed_terms_demo(path, *, complete=False):
    """`complete=True` gives Loan B a rate so every non-card debt has one (the avalanche comparison appears)."""
    init_db(path)
    ids = {}
    with closing(sqlite3.connect(path)) as db, db:
        for key, (institution, nickname, kind, side, balance, asof, owner, terms) in ACCOUNTS.items():
            ids[key] = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,update_method)
                VALUES (?,?,?,?,'USD',?,?,?,'Fictional')""", (institution, nickname, kind, side, balance, asof, owner)).lastrowid
            if complete and key == "maple-b":
                terms = dict(rate="7.200", min_payment="60.00")
            if terms:
                save_terms(db, ids[key], terms)
        source = "fictional-birch-auto"      # a transaction feed with three months of confirmed loan payments
        db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES (?,?,0,0,'','Fictional')",
                   (source, ids["birch-auto"]))
        for day, amount in (("2026-08-10", "240.00"), ("2026-09-10", "250.00"), ("2026-10-10", "260.00")):
            put_row(db, ids["birch-auto"], source, "USD", day, amount, "Auto loan payment", transfer=True)
        cover(db, "2026-08-01", "2026-11-29")
        save_assumptions(db, "Household", "USD", replace(
            Assumptions(), years=10, income=Decimal("5200"), spending=Decimal("3400"), debt_target=Decimal("700")))
    return ids

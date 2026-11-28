"""Create a FICTIONAL accounts database for rehearsals and tests. Never use real records.

    python3 seed_fictional.py --db /private/dir/fictional.sqlite3
    python3 seed_fictional.py --db /private/dir/fictional.sqlite3 --add-session member@example.test

There is deliberately no default path, so this can never touch the normal dashboard database.
Seeding is refused when the database already holds any account. Every record is invented;
the transaction rows carry sentinel text/amounts so a test can prove they never leave the
database (hosted mode gates the Transactions tab, but the rows still exist at rest).
"""
import argparse
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from projections import Assumptions, save_assumptions
from server import init_db

CHECKING_BALANCE = "1234.56"
SENTINEL_MERCHANT = "ZZ-SENTINEL-MERCHANT-8841"
SENTINEL_AMOUNTS = ("-7777.77", "-4242.42")
SENTINEL_DESCRIPTION = "ZZ-SENTINEL-DESCRIPTION 8841 FICTIONAL"
PROJECTION_INCOME = "5432.10"
TX_SOURCE = "fictional-v1:tx-source"

ACCOUNTS = (  # institution, nickname, kind, side, currency, balance, owner
    ("Fictional Bank", "Fictional Checking", "Checking", "asset", "USD", CHECKING_BALANCE, "Joint"),
    ("Fictional Bank", "Fictional Savings", "Savings", "asset", "USD", "20000.00", "Young"),
    ("Fictional Broker", "Fictional Brokerage", "Brokerage", "asset", "USD", "15500.25", "Nanami"),
    ("Fictional Broker", "Fictional IRA", "Retirement", "asset", "USD", "30100.75", "Young"),
    ("Fictional Card Co", "Fictional Card", "Credit card", "debt", "USD", "812.40", "Joint"),
    ("Fictional Yen Bank", "Fictional Yen Savings", "Savings", "asset", "JPY", "150000", "Nanami"),
)


def seed(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600))  # private from the start
    init_db(path)
    today = date.today()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            raise SystemExit("Refusing to seed: the database already contains accounts.")
        for institution, nickname, kind, side, currency, balance, owner in ACCOUNTS:
            db.execute("""INSERT INTO accounts (institution, nickname, kind, side, currency, balance,
                          asof, owner, update_method, excluded, side_manual)
                          VALUES (?,?,?,?,?,?,?,?,'Manual entry',0,1)""",
                       (institution, nickname, kind, side, currency, balance, today.isoformat(), owner))
        rows = db.execute("SELECT id, side, currency, balance FROM accounts").fetchall()
        for account_id, side, currency, balance in rows:  # two earlier months of history
            step = Decimal(1) if currency == "JPY" else Decimal("0.01")
            for months, factor in ((1, "0.98"), (2, "0.95")):
                db.execute("""INSERT INTO balance_history (account_id, asof, balance, currency, side)
                              VALUES (?,?,?,?,?)""",
                           (account_id, (today - timedelta(days=31 * months)).isoformat(),
                            str((Decimal(balance) * Decimal(factor)).quantize(step)), currency, side))
        checking = db.execute("SELECT id FROM accounts WHERE nickname='Fictional Checking'").fetchone()[0]
        # A source link plus coverage makes these rows visible to the *local* Transactions page, so a
        # leak in hosted mode would actually show them (unlinked/uncovered rows are filtered out).
        db.execute("""INSERT INTO source_links (source, local, stamp, sign, snapshot, status)
                      VALUES (?,?,0,0,'','Fictional')""", (TX_SOURCE, checking))
        epoch = date(1970, 1, 1)
        db.execute("""INSERT INTO transaction_coverage (source, account_id, currency, start_at, end_at, revision)
                      VALUES (?,?,'USD',?,?,1)""",
                   (TX_SOURCE, checking, (today - timedelta(days=120) - epoch).days * 86400,
                    (today + timedelta(days=70) - epoch).days * 86400))
        posted = today.isoformat()
        for tx_id, amount, merchant, description in (
            ("fictional-tx-1", SENTINEL_AMOUNTS[0], SENTINEL_MERCHANT, SENTINEL_DESCRIPTION),
            ("fictional-tx-2", SENTINEL_AMOUNTS[1], SENTINEL_MERCHANT, SENTINEL_DESCRIPTION + " B"),
            ("fictional-tx-3", "2500.00", "FICTIONAL PAYROLL CO", "FICTIONAL PAYROLL"),
        ):
            db.execute("""INSERT INTO account_transactions (account_id, tx_id, source, posted_date,
                          pending_date, amount, currency, description, payee, merchant, pending,
                          pending_active) VALUES (?,?,?,?,NULL,?,'USD',?,?,?,0,0)""",
                       (checking, tx_id, TX_SOURCE, posted, amount, description, merchant, merchant))
        db.execute("""INSERT INTO transaction_merchant_rules (account_id, source, currency, merchant, category)
                      VALUES (?,?,'USD',?,'Fictional category')""", (checking, TX_SOURCE, SENTINEL_MERCHANT))
        save_assumptions(db, "Household", "USD", Assumptions(income=Decimal(PROJECTION_INCOME)))


def add_session(path, email):
    """Insert an unexpired fictional sign-in session so a backup can prove it scrubs sessions.

    Needs the hosted tables, i.e. the hosted dashboard must have started on this database once.
    """
    sid = hashlib.sha256(b"fictional-session-" + email.encode()).hexdigest()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='hosted_sessions'").fetchone():
            raise SystemExit("Start the hosted dashboard once so the session tables exist.")
        db.execute("INSERT OR IGNORE INTO hosted_identities VALUES (?,?)", (email, "fictional-sub-" + email))
        db.execute("INSERT OR REPLACE INTO hosted_sessions VALUES (?,?,?,?)",
                   (sid, email, "fictional-csrf", 4102444800))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--add-session", metavar="EMAIL")
    args = parser.parse_args()
    os.umask(0o077)
    if args.add_session:
        add_session(args.db, args.add_session)
        print("Fictional session added.")
    else:
        seed(args.db)
        print("Fictional database seeded.")


if __name__ == "__main__":
    main()

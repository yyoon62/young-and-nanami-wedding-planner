"""Scrubbed export: a fresh accounts database holding balances only, safe to install on the host.

    python3 deploy/export_scrubbed.py --out /private/dir/accounts-export.sqlite3 [--db SOURCE] [--no-assumptions]
                                      [--with-transactions]

Copies only the tables in hosted_guard.EXPORT_TABLES (accounts, balance history, Bridge links, account terms,
projection assumptions) into a NEW file made by the app's own schema. Transactions, merchant rules,
classifier data, sign-in state and anything added later are never read into the output. The source is opened
read-only and never changed. The output is created 0600, refuses to replace a file, and holds real balances:
delete it after installing it and keep it out of Git, notes and synced folders.

--with-transactions (L30b) is for a host that was opted in (`server.py --hosted --transactions`): it also copies
hosted_guard.TRANSACTION_TABLES (transactions, coverage, merchant rules, categories, classifier cache/settings),
so the export then holds real transactions. Never install such a file on a host that was not opted in; its
start-up check refuses it.
"""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hosted_auth import external
from hosted_guard import EXPORT_TABLES, TRANSACTION_TABLES
from server import DEFAULT_DB, init_db

REQUIRED = {"accounts": {"id", "institution", "nickname", "kind", "side", "currency", "balance", "asof"},
            "balance_history": {"account_id", "asof", "balance", "currency", "side"},
            "source_links": {"source", "local"}}
NOTE = ("projection_assumptions may hold monthly income/spending figures you accepted from transaction-based "
        "suggestions (totals, not rows); use --no-assumptions to leave them out.")


class Failure(Exception):
    pass


def columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def snapshot(source):
    """Consistent in-memory copy of the source, including committed WAL data; the source is not written."""
    if not source.is_file():
        raise Failure(f"Source database not found: {source}")
    copy = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.backup(copy)
        names = {r[0] for r in copy.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        for table, needed in REQUIRED.items():
            if table not in names or not needed <= set(columns(copy, table)):
                raise Failure("Source is not a dashboard accounts database.")
        return copy, names
    except sqlite3.Error as exc:
        copy.close()
        raise Failure(f"Cannot read the source database ({type(exc).__name__}: {exc}); "
                      "close the dashboard server and retry.") from None
    except Failure:
        copy.close()
        raise


# init_db seeds these on every database; a transaction export replaces the seeded rows with the real ones.
SEEDED = ("transaction_categories", "classifier_settings")


def build(stage, source, names, assumptions, transactions=False):
    """Fill the freshly migrated `stage` database; returns ({table: rows copied}, {table: [columns not copied]})."""
    copied, dropped = {}, {}
    init_db(stage)
    tables = EXPORT_TABLES + (TRANSACTION_TABLES if transactions else ())
    with closing(sqlite3.connect(stage)) as db, db:
        for table in tables:
            wanted = table != "projection_assumptions" or assumptions
            if table not in names or not wanted:
                copied[table] = 0
                continue
            have, want = columns(source, table), columns(db, table)
            shared = [c for c in have if c in want]
            if set(have) - set(shared):
                dropped[table] = sorted(set(have) - set(shared))
            if table == "balance_history" or table in SEEDED:
                # Inserting accounts fired the history triggers, and init_db seeded the defaults: replace both
                # with the recorded rows.
                db.execute(f"DELETE FROM {table}")
            rows = source.execute(f"SELECT {','.join(shared)} FROM {table}").fetchall()
            db.executemany(f"INSERT INTO {table} ({','.join(shared)}) VALUES ({','.join('?' * len(shared))})", rows)
            copied[table] = len(rows)
        for table in ("accounts", *(TRANSACTION_TABLES if transactions else ())):
            top = source.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone() \
                if "sqlite_sequence" in names else None
            if top:  # a deleted highest id must not be handed out again
                db.execute("INSERT INTO sqlite_sequence (name, seq) SELECT ?, ? WHERE NOT EXISTS "
                           "(SELECT 1 FROM sqlite_sequence WHERE name=?)", (table, top[0], table))
                db.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (top[0], table))
    with closing(sqlite3.connect(stage)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA freelist_count").fetchone()[0]:
            raise Failure("The new database failed its own integrity check.")
    return copied, dropped


def excluded(source, names, transactions=False):
    skipped = []
    for table in sorted(names - set(EXPORT_TABLES) - set(TRANSACTION_TABLES if transactions else ())):
        if not table.startswith("sqlite_"):
            skipped.append((table, source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]))
    return skipped


def export(source, out, assumptions=True, transactions=False):
    out = external(out)
    source = Path(os.path.abspath(source))
    if os.path.lexists(out):
        raise Failure(f"Output already exists, not replacing it: {out}")
    if not out.parent.is_dir():
        raise Failure(f"Output directory does not exist: {out.parent}")
    snap, names = snapshot(source)
    with closing(snap):
        fd, name = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=out.parent)  # 0600, same filesystem
        os.close(fd)
        stage = Path(name)
        try:
            copied, dropped = build(stage, snap, names, assumptions, transactions)
            try:
                os.link(stage, out)  # publish atomically; never replaces an existing file
            except FileExistsError:
                raise Failure(f"Output already exists, not replacing it: {out}") from None
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                if os.path.lexists(str(stage) + suffix):
                    os.unlink(str(stage) + suffix)
        return copied, dropped, excluded(snap, names, transactions)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"source database (default {DEFAULT_DB})")
    parser.add_argument("--out", type=Path, required=True, help="new file to create; must not exist")
    parser.add_argument("--no-assumptions", action="store_true", help="leave projection_assumptions empty")
    parser.add_argument("--with-transactions", action="store_true",
                        help="also copy the transaction tables, for a host started with --transactions (L30b)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        copied, dropped, skipped = export(args.db, args.out, not args.no_assumptions, args.with_transactions)
    except Failure as exc:
        sys.exit(f"Export failed: {exc}")
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.exit(f"Export failed: {' '.join(str(exc).split()) or type(exc).__name__}")
    print(f"Scrubbed export written: {external(args.out)} (mode 600)")
    print("Copied rows:")
    for table in EXPORT_TABLES + (TRANSACTION_TABLES if args.with_transactions else ()):
        print(f"  {table:<{28 if args.with_transactions else 24}}{copied[table]:>8}")
    print("Excluded tables (not copied): " + (", ".join(f"{t} ({n})" for t, n in skipped) or "none"))
    for table, cols in dropped.items():
        print(f"Columns not copied (unknown to this app version): {table}.{', '.join(cols)}")
    print("Note: " + (NOTE if not args.no_assumptions else "projection_assumptions was left empty (--no-assumptions)."))
    if args.with_transactions:
        print("This file also holds REAL TRANSACTIONS: install it only on a host started with --transactions.")
    print("This file holds real balances: delete it after installing it; never put it in Git, notes or a synced folder.")


if __name__ == "__main__":
    main()

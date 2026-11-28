"""What may live in the hosted (Pi) database: one policy for the export, the startup guard and backups.

The export (deploy/export_scrubbed.py) copies EXPORT_TABLES and nothing else, so a table added later is
excluded until someone lists it here on purpose. The guard reads a database read-only and reports every
row that is not in HOSTED_TABLES, which is how transaction, classifier and merchant-rule data is kept off
the Pi and out of its encrypted backups.

Honest limit: the guard counts ROWS. Deleted rows can linger in a SQLite file's free pages, so a copied
file is not proven clean by an empty table. The export builds a fresh file and is the only supported way
to move data to the host.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

# Balance-level data that the export copies, in copy order (accounts first: its triggers write history).
EXPORT_TABLES = ("accounts", "balance_history", "source_links", "account_terms", "projection_assumptions")

# Tables the host may legitimately hold rows in: the export, plus what the host itself writes
# (Bridge sync quota timestamps, Google identity pins, sign-in sessions and challenges).
HOSTED_TABLES = EXPORT_TABLES + ("fetch_attempts", "hosted_identities", "hosted_sessions", "hosted_challenges")

# init_db seeds built-in category names on every database, so those rows are expected on the host.
# Custom categories (typed by the user or inferred from transactions) are not.
ROW_RULES = {"transaction_categories": "builtin=1"}

EXPORT_COMMAND = "python3 deploy/export_scrubbed.py --out FILE"


class Refused(ValueError):
    """The database holds data that must not be on the host."""


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def violations(db):
    """[(table, rows)] for every table holding rows the host must not have; internal sqlite_* tables ignored."""
    found = []
    for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' "
                               "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name").fetchall():
        if table in HOSTED_TABLES:
            continue
        quoted = _quote(table)
        count = None
        if table in ROW_RULES:
            try:
                count = db.execute(f"SELECT COUNT(*) FROM {quoted} WHERE NOT ({ROW_RULES[table]})").fetchone()[0]
            except sqlite3.OperationalError:
                count = None  # unknown shape: fail closed and count every row
        if count is None:
            count = db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        if count:
            found.append((table, count))
    return found


def describe(found):
    return "; ".join(f"table {table} holds {count} row{'s' * (count != 1)}" for table, count in found)


def check(path):
    """Read-only check of the file at `path`; [] when it is missing, empty or clean. Raises Refused on any doubt."""
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            return violations(db)
    except sqlite3.Error as exc:
        raise Refused(f"the database could not be read safely ({type(exc).__name__}: {exc})") from None


def require_clean(path):
    found = check(path)
    if found:
        raise Refused(f"{describe(found)}. Transaction data must not be on the host. "
                      f"On the machine that holds your data run {EXPORT_COMMAND} and install that file instead.")


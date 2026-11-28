"""Headless daily balance sync (L30a): the page's "Sync balances" button without a browser, for a timer on the Pi.

    python3 daily_sync.py [--db PATH] [--hosted]

Uses the saved SimpleFIN credential and the same Batch.sync() as the button, so the safety rules are identical:
the 24-requests-per-24-hours guard (kept in the database, shared with the web server), the link / side / owner
rules, and balance history captured by the existing triggers. Balances only: it never fetches transactions.

Exit status 0 means at least one account was really refreshed. Anything else prints ONE line to stderr and exits 1
(systemd then marks the unit failed and may retry). It never prints a traceback or the text of an unexpected
error, because those can contain URLs; the credential is never printed at all.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import time

from bridge import Bridge, Problem
from bulk import Batch
from hosted_auth import db_path, external
from hosted_guard import Refused, require_clean
from server import DEFAULT_DB, validate
from sync_status import last_ok


def run(path, provider, *, hosted=False, out=print, err=print, clock=time.time):
    """Sync once. Returns 0 on success (summary to `out`), 1 on any failure (one line to `err`)."""
    started = clock()
    try:
        if hosted:
            path = db_path(external(path))
            require_clean(path)          # read-only, before any write: the Pi must not hold transaction data (until L30b)
        else:
            path = Path(path)
        if not path.is_file():
            err(f"daily sync failed: no database at {path}.")
            return 1
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_links'").fetchone():
                err("daily sync failed: this database has no sync tables yet; start the dashboard once.")
                return 1
        notices = Batch(path, provider, validate).sync()
    except (Problem, Refused) as exc:
        err(f"daily sync failed: {exc}")
        return 1
    except Exception as exc:             # deliberately only the type: messages may carry URLs
        err(f"daily sync failed ({type(exc).__name__}); see the dashboard and the SimpleFIN connection.")
        return 1
    last = last_ok(path)
    # `checked` has minute precision, so allow the minute the run started in.
    if last is None or last < started - 60:
        err("daily sync failed: no account was refreshed. " + " ".join(notices))
        return 1
    out(" ".join(notices))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--hosted", action="store_true",
                        help="apply the hosted rules: private paths, refuse a database holding transactions")
    args = parser.parse_args(argv)
    try:
        provider = Bridge()
    except (Problem, OSError, UnicodeError):
        print("daily sync failed: missing or unsafe SimpleFIN credential storage. See BULK.md.", file=sys.stderr)
        return 1
    return run(args.db, provider, hosted=args.hosted, err=lambda line: print(line, file=sys.stderr))


if __name__ == "__main__":
    sys.exit(main())

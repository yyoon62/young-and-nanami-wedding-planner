"""Headless daily balance sync (L30a): the page's "Sync balances" button without a browser, for a timer on the Pi.

    python3 daily_sync.py [--db PATH] [--hosted] [--transactions]

Uses the saved SimpleFIN credential and the same Batch.sync() as the button, so the safety rules are identical:
the 24-requests-per-24-hours guard (kept in the database, shared with the web server), the link / side / owner
rules, and balance history captured by the existing triggers. Without --transactions it is balances only.

--transactions (L30b, for a host started with `server.py --hosted --transactions`) then also runs the same
sync_transactions() as the page's "Sync transactions" button (it resumes from the saved coverage, normally one
30-day window with a few days of overlap, so about 2 requests a day in total) and, if the classifier is on and
a key is saved, categorises new merchants. Balances are saved before transactions are attempted: a transaction
failure exits 1 but never loses the balance snapshot.

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
from transactions import sync_transactions
from tx_classifier import after_sync as classify_after_sync


def run(path, provider, *, hosted=False, transactions=False, out=print, err=print, clock=time.time, now=None):
    """Sync once. Returns 0 on success (summary to `out`), 1 on any failure (one line to `err`)."""
    started = clock()
    try:
        if hosted:
            path = db_path(external(path))
            require_clean(path, transactions)   # read-only, before any write: only an opted-in host may hold transactions
        else:
            path = Path(path)
        if not path.is_file():
            err(f"daily sync failed: no database at {path}.")
            return 1
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_links'").fetchone():
                err("daily sync failed: this database has no sync tables yet; start the dashboard once.")
                return 1
        batch = Batch(path, provider, validate)
        notices = batch.sync()
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
    if transactions:
        try:
            result = sync_transactions(batch, **({"now": now} if now is not None else {}))
        except Problem as exc:
            err(f"daily sync failed: balances were saved, transactions were not: {exc}")
            return 1
        except Exception as exc:
            err(f"daily sync failed: balances were saved, transactions were not ({type(exc).__name__}).")
            return 1
        tail = "caught up" if result.caught_up else "more history is available; the next run continues"
        notices += [f"Transactions: {result.windows} window(s), {tail}.", *result.warnings,
                    *classify_after_sync(path, hosted=False)]
    out(" ".join(notices))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--hosted", action="store_true",
                        help="apply the hosted rules: private paths, refuse a database holding transactions")
    parser.add_argument("--transactions", action="store_true",
                        help="also pull transactions and categorise new merchants (a host started with --transactions)")
    args = parser.parse_args(argv)
    if args.transactions and not args.hosted:
        parser.error("--transactions is for the hosted Pi; use the page's Sync transactions button locally.")
    try:
        provider = Bridge()
    except (Problem, OSError, UnicodeError):
        print("daily sync failed: missing or unsafe SimpleFIN credential storage. See BULK.md.", file=sys.stderr)
        return 1
    return run(args.db, provider, hosted=args.hosted, transactions=args.transactions,
               err=lambda line: print(line, file=sys.stderr))


if __name__ == "__main__":
    sys.exit(main())

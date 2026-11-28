"""L30a: headless daily balance sync + the "last synced" note. Fictional data, no network."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import daily_sync
import sync_status
from bridge import Problem
from demo_batch import seed
from server import init_db, serve
from test_bulk import Fixture

DEPLOY = Path(__file__).resolve().parent / "deploy"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()          # macOS temp is behind a symlink; the hosted checks refuse those
        self.path = self.dir / "fictional.sqlite3"
        init_db(self.path)
        os.chmod(self.path, 0o600)
        self.provider = Fixture()
        with closing(sqlite3.connect(self.path)) as db, db:
            seed(db)
        self.lines, self.errors = [], []

    def run_sync(self, provider=None, **kw):
        return daily_sync.run(self.path, provider or self.provider, out=self.lines.append, err=self.errors.append, **kw)

    def sql(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql, args).fetchall()


class Run(Base):
    def test_success_syncs_balances_and_records_history_once_per_day(self):
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.errors, [])
        self.assertIn("12 added", self.lines[0])
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.sql("SELECT count(*) FROM fetch_attempts"), [(1,)])
        self.assertEqual(self.sql("SELECT count(*) FROM accounts"), [(13,)])           # 1 seeded + 12 synced
        history = self.sql("SELECT count(*) FROM balance_history")[0][0]
        self.assertGreaterEqual(history, 13)
        # A second run (a retry, or a manual click) adds no accounts and no second history row for the same day.
        self.assertEqual(self.run_sync(), 0)
        self.assertIn("12 unchanged", self.lines[1])
        self.assertEqual(self.sql("SELECT count(*) FROM accounts"), [(13,)])
        self.assertEqual(self.sql("SELECT count(*) FROM balance_history"), [(history,)])
        self.assertEqual(self.sql("SELECT count(*) FROM fetch_attempts"), [(2,)])      # each run counts against the 24/day guard

    def test_never_fetches_transactions(self):
        self.run_sync()
        self.assertEqual(self.sql("SELECT count(*) FROM account_transactions"), [(0,)])
        self.assertEqual(self.provider.calls, 1)

    def test_provider_problem_is_one_line_exit_1_and_changes_nothing(self):
        class Broken(Fixture):
            def fetch(self):
                raise Problem("SimpleFIN Bridge rejected the credential.")
        before = self.sql("SELECT count(*) FROM accounts")
        self.assertEqual(self.run_sync(Broken()), 1)
        self.assertEqual(self.errors, ["daily sync failed: SimpleFIN Bridge rejected the credential."])
        self.assertEqual(self.sql("SELECT count(*) FROM accounts"), before)

    def test_unexpected_error_prints_only_its_type_never_its_text(self):
        class Leaky(Fixture):
            def fetch(self):
                raise RuntimeError("GET https://user:SECRETTOKEN@bridge.example/simplefin failed")
        self.assertEqual(self.run_sync(Leaky()), 1)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("RuntimeError", self.errors[0])
        self.assertNotIn("SECRETTOKEN", self.errors[0])
        self.assertNotIn("https://", self.errors[0])

    def test_daily_guard_blocks_before_any_request(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT INTO fetch_attempts VALUES (?)", [(time.time(),)] * 24)
        self.assertEqual(self.run_sync(), 1)
        self.assertIn("Daily guard", self.errors[0])
        self.assertEqual(self.provider.calls, 0)

    def test_a_run_that_refreshes_nothing_is_a_failure(self):
        self.assertEqual(self.run_sync(), 0)
        class Silent(Fixture):          # the bank stopped answering: linked accounts are "Missing or provider error"
            def fetch(self):
                return dict(connections=[], accounts=[], errlist=[])
        self.errors.clear()
        self.assertEqual(self.run_sync(Silent(), clock=lambda: time.time() + 3 * 3600), 1)
        self.assertIn("no account was refreshed", self.errors[0])

    def test_missing_database_or_tables_fail_clearly(self):
        self.assertEqual(daily_sync.run(self.dir / "absent.sqlite3", self.provider, err=self.errors.append), 1)
        self.assertIn("no database", self.errors[0])
        bare = self.dir / "bare.sqlite3"
        sqlite3.connect(bare).close()
        self.errors.clear()
        self.assertEqual(daily_sync.run(bare, self.provider, err=self.errors.append), 1)
        self.assertIn("no sync tables", self.errors[0])
        self.assertEqual(self.provider.calls, 0)

    def test_main_without_a_credential_fails_without_a_traceback(self):
        with patch.object(daily_sync, "Bridge", side_effect=Problem("x")), patch("sys.stderr") as err:
            self.assertEqual(daily_sync.main(["--db", str(self.path)]), 1)
        self.assertIn("credential", "".join(c.args[0] for c in err.write.call_args_list))


class Hosted(Base):
    def test_hosted_mode_runs_on_a_clean_private_database(self):
        self.assertEqual(self.run_sync(hosted=True), 0, self.errors)

    def test_hosted_mode_refuses_a_database_holding_transactions_before_any_request(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,
                description,payee,merchant,pending,pending_active) VALUES (1,'t1','bulk','2026-01-01','-1.00','USD',
                'FICTIONAL COFFEE','','FICTIONAL COFFEE',0,0)""")
        self.assertEqual(self.run_sync(hosted=True), 1)
        self.assertIn("account_transactions", self.errors[0])
        self.assertEqual(self.provider.calls, 0)

    def test_hosted_mode_refuses_a_world_readable_database(self):
        os.chmod(self.path, 0o644)
        self.assertEqual(self.run_sync(hosted=True), 1)
        self.assertEqual(self.provider.calls, 0)


class Status(Base):
    def epoch(self, text):
        return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()

    def test_never_synced(self):
        self.assertIsNone(sync_status.last_ok(self.path))
        self.assertIn("Not synced yet", sync_status.note(self.path))

    def test_fresh_stale_and_held_statuses(self):
        self.run_sync()
        self.sql("UPDATE source_links SET checked='2026-01-10 06:30 UTC'")
        last = self.epoch("2026-01-10 06:30")
        self.assertEqual(sync_status.last_ok(self.path), last)
        self.assertIn("Synced 1 h ago", sync_status.note(self.path, now=last + 3600))
        stale = sync_status.note(self.path, now=last + 9 * 86400)
        self.assertIn("sync-stale", stale)
        self.assertIn("⚠️ Last synced 9 days ago", stale)
        self.assertIn("2026-01-10 06:30 UTC", stale)
        # A bank that stopped answering is retained but not refreshed: it must not look fresh.
        self.sql("UPDATE source_links SET status='Missing or provider error; retained'")
        self.assertIsNone(sync_status.last_ok(self.path))
        self.assertIn("Not synced yet", sync_status.note(self.path))

    def test_age_wording(self):
        self.assertEqual(sync_status.ago(30), "just now")
        self.assertEqual(sync_status.ago(10 * 60), "10 min ago")
        self.assertEqual(sync_status.ago(5 * 3600), "5 h ago")
        self.assertEqual(sync_status.ago(3 * 86400), "3 days ago")
        self.assertEqual(sync_status.ago(-5), "just now")

    def test_accounts_page_shows_the_note_next_to_the_button(self):
        server = serve(self.path, 0, self.provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        url = f"http://127.0.0.1:{server.server_port}/"
        with urlopen(url, timeout=5) as page:
            html = page.read().decode()
        self.assertIn("↻ Sync balances", html)
        self.assertIn("Not synced yet", html)
        self.assertEqual(self.run_sync(), 0)
        with urlopen(url, timeout=5) as page:
            html = page.read().decode()
        self.assertIn("Synced just now", html)
        self.assertNotIn("sync-stale", html)


class Units(unittest.TestCase):
    def test_units_are_hosted_catch_up_and_keep_secrets_out(self):
        service = (DEPLOY / "accounts-sync.service").read_text()
        timer = (DEPLOY / "accounts-sync.timer").read_text()
        self.assertIn("daily_sync.py --hosted", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("UMask=0077", service)
        for text in (service, timer):
            self.assertNotIn("EnvironmentFile", text)
            self.assertNotIn("bridge.access", text)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=*-*-* 06:30:00", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()

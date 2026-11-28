"""L24: the scrubbed export and the hosted policy it shares with the startup guard. Synthetic data only."""
from contextlib import closing
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "deploy"))
import seed_fictional
from account_terms import save_terms
from hosted_auth import Auth
import hosted_guard
from server import init_db
from test_hosted import CONFIG

SENTINEL = b"ZZ-SENTINEL-COFFEE"
SCRIPT = str(HERE / "deploy" / "export_scrubbed.py")
TX_TABLES = ("account_transactions", "transaction_coverage", "transaction_merchant_rules",
             "classifier_cache", "classifier_settings")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path, sql):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql).fetchall()


def make_source(folder, name="source.sqlite3"):
    """Fictional database with every current table populated, sentinels in all transaction-derived tables."""
    path = Path(folder) / name
    seed_fictional.seed(path)
    Auth(path, CONFIG)  # hosted tables, as if this file had once been served hosted
    with closing(sqlite3.connect(path)) as db, db:
        checking = db.execute("SELECT id FROM accounts WHERE nickname='Fictional Checking'").fetchone()[0]
        loan = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
            VALUES ('Fictional Bank','Fictional Loan','Personal loan','debt','USD','1000.00','2026-11-01','Joint')""").lastrowid
        save_terms(db, loan, dict(rate="4.500", planned_payment="120.00"))
        db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,description,
            payee,merchant,pending,pending_active) VALUES (?,?,?,'2026-11-02','-3.50','USD','ZZ-SENTINEL-COFFEE #1','',
            'ZZ-SENTINEL-COFFEE',0,0)""", (checking, "coffee-1", seed_fictional.TX_SOURCE))
        db.execute("""INSERT OR REPLACE INTO transaction_merchant_rules (account_id,source,currency,merchant,category)
            VALUES (?,?,'USD','ZZ-SENTINEL-COFFEE','Coffee')""", (checking, seed_fictional.TX_SOURCE))
        db.execute("INSERT INTO classifier_cache (clean_key,category,status) VALUES ('ZZ-SENTINEL-COFFEE','Dining','suggested')")
        db.execute("INSERT INTO classifier_settings VALUES ('status','ZZ-SENTINEL-COFFEE note')")
        db.execute("INSERT INTO fetch_attempts VALUES (1.0)")
        db.execute("INSERT INTO hosted_identities VALUES ('member.one@gmail.com','sub-1')")
        db.execute("INSERT INTO hosted_sessions VALUES ('sid','member.one@gmail.com','csrf',4102444800)")
        db.execute("INSERT INTO hosted_challenges VALUES ('b','n',4102444800)")
    return path


def run(*args, umask=None):
    return subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, cwd=HERE,
                          preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.out = self.dir / "out" / "export.sqlite3"
        self.out.parent.mkdir(mode=0o700)

    def export(self, source, *extra, okay=True, out=None, umask=None):
        result = run("--db", source, "--out", out or self.out, *extra, umask=umask)
        self.assertEqual(result.returncode == 0, okay, result.stdout + result.stderr)
        return result


class Export(Base):
    def test_every_table_populated_sentinel_absent_everywhere_and_counts_printed(self):
        source = make_source(self.dir)
        with closing(sqlite3.connect(source)) as db:
            for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                self.assertGreater(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, f"{table} not populated")
        self.assertIn(SENTINEL, source.read_bytes())
        before = sha(source)
        result = self.export(source)
        self.assertEqual(sha(source), before, "the source must not change")
        data = self.out.read_bytes()
        for marker in (SENTINEL, seed_fictional.SENTINEL_MERCHANT.encode(), seed_fictional.SENTINEL_DESCRIPTION.encode(),
                       b"member.one@gmail.com", b"7777.77"):
            self.assertNotIn(marker, data)
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["export.sqlite3"])  # no sidecar or stage file
        for table in hosted_guard.EXPORT_TABLES:
            if table != "fetch_attempts":
                self.assertEqual(rows(self.out, f"SELECT * FROM {table} ORDER BY 1,2"),
                                 rows(source, f"SELECT * FROM {table} ORDER BY 1,2"), table)
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM accounts"), [(7,)])
        for table in TX_TABLES + ("fetch_attempts",):
            self.assertEqual(rows(self.out, f"SELECT COUNT(*) FROM {table}"), [(0,)], table)
        # Sign-in state is not exported; the host creates its own tables at first hosted start.
        self.assertEqual(rows(self.out, "SELECT name FROM sqlite_schema WHERE name LIKE 'hosted\\_%' ESCAPE '\\'"), [])
        for line in ("accounts                       7", "balance_history", "account_terms                  1",
                     "projection_assumptions         1", "source_links                   1", "account_transactions (4)",
                     "classifier_cache (1)", "Note: projection_assumptions may hold", "delete it after installing"):
            self.assertIn(line, result.stdout)

    def test_history_rows_are_the_recorded_ones_and_init_db_adds_nothing(self):
        source = make_source(self.dir)
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("UPDATE balance_history SET recorded_at='2020-01-01T00:00:00.000Z'")
        self.export(source)
        query = "SELECT account_id,asof,balance,currency,side,recorded_at FROM balance_history ORDER BY 1,2"
        self.assertEqual(rows(self.out, query), rows(source, query))
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM balance_history"), [(19,)])  # 6 accounts x 3 dates + the new loan
        init_db(self.out)  # what every start does; the triggers and backfill must find nothing to add or rewrite
        init_db(self.out)
        self.assertEqual(rows(self.out, query), rows(source, query))
        self.assertEqual(rows(self.out, "SELECT seq FROM sqlite_sequence WHERE name='accounts'"),
                         rows(source, "SELECT seq FROM sqlite_sequence WHERE name='accounts'"))

    def test_deleted_rows_leave_no_free_pages_in_the_export(self):
        source = make_source(self.dir)
        with closing(sqlite3.connect(source)) as db, db:
            checking = db.execute("SELECT id FROM accounts WHERE nickname='Fictional Checking'").fetchone()[0]
            db.executemany("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,description,
                payee,merchant,pending,pending_active) VALUES (?,?,'bulk','2026-01-01','-1.00','USD','ZZ-SENTINEL-COFFEE','',
                'ZZ-SENTINEL-COFFEE',0,0)""", ((checking, f"bulk-{i}") for i in range(5000)))
        grown = source.stat().st_size
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("DELETE FROM account_transactions")
            db.execute("DELETE FROM transaction_merchant_rules")
            db.execute("DELETE FROM classifier_cache")
            db.execute("DELETE FROM classifier_settings")
        self.assertGreater(rows(source, "PRAGMA freelist_count")[0][0], 100)
        self.assertIn(SENTINEL, source.read_bytes(), "precondition: a copied file would carry the remnants")
        self.export(source)
        self.assertEqual(rows(self.out, "PRAGMA freelist_count"), [(0,)])
        self.assertNotIn(SENTINEL, self.out.read_bytes())
        self.assertLess(self.out.stat().st_size, 200_000)
        self.assertGreater(grown, 5 * self.out.stat().st_size)

    def test_extra_table_and_columns_from_newer_branches(self):
        source = make_source(self.dir)
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("CREATE TABLE future_notes (note TEXT)")
            db.execute("INSERT INTO future_notes VALUES ('ZZ-SENTINEL-COFFEE')")
            db.execute("ALTER TABLE accounts ADD COLUMN l29_extra TEXT DEFAULT 'kept?'")
            if "owner_override" not in [r[1] for r in db.execute("PRAGMA table_info(account_transactions)")]:
                db.execute("ALTER TABLE account_transactions ADD COLUMN owner_override TEXT")  # L26 (now built in)
            db.execute("UPDATE account_transactions SET owner_override='Young'")
        result = self.export(source)
        self.assertEqual(rows(self.out, "SELECT name FROM sqlite_schema WHERE name='future_notes'"), [])
        self.assertNotIn(SENTINEL, self.out.read_bytes())
        self.assertIn("future_notes (1)", result.stdout)
        self.assertIn("accounts.l29_extra", result.stdout)  # reported, not silently dropped
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM accounts"), [(7,)])
        init_db(self.out)  # opens cleanly under the app's migrations
        self.assertNotIn("l29_extra", [r[1] for r in sqlite3.connect(self.out).execute("PRAGMA table_info(accounts)")])

    def test_source_from_an_older_version_missing_a_column(self):
        source = make_source(self.dir)
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("ALTER TABLE accounts DROP COLUMN logo_domain")
        self.export(source)
        self.assertEqual(rows(self.out, "SELECT DISTINCT logo_domain FROM accounts"), [("",)])

    def test_no_assumptions_flag_exports_an_empty_table(self):
        source = make_source(self.dir)
        result = self.export(source, "--no-assumptions")
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM projection_assumptions"), [(0,)])
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM accounts"), [(7,)])
        self.assertNotIn(b"5432.10", self.out.read_bytes())
        self.assertIn("left empty", result.stdout)
        self.assertEqual(rows(source, "SELECT COUNT(*) FROM projection_assumptions"), [(1,)])

    def test_live_wal_source_is_read_without_changing_its_main_file(self):
        source = make_source(self.dir)
        live = sqlite3.connect(source)
        self.addCleanup(live.close)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof)
                        VALUES ('F','Only In WAL','Checking','asset','USD','1.00','2026-11-28')""")
        live.commit()
        before = sha(source)
        self.export(source)
        self.assertEqual(sha(source), before)
        self.assertEqual(rows(self.out, "SELECT COUNT(*) FROM accounts WHERE nickname='Only In WAL'"), [(1,)])

    def test_output_mode_is_0600_whatever_the_umask(self):
        source = make_source(self.dir)
        self.export(source, umask=0)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_failures_are_one_line_leave_nothing_behind_and_never_touch_the_source(self):
        source = make_source(self.dir)
        before = sha(source)
        self.export(source)
        kept = self.out.read_bytes()
        junk = self.dir / "junk.sqlite3"
        junk.write_text("not a database " * 50)
        other = self.dir / "other.sqlite3"
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE accounts (x)")
        git = self.dir / "repo"
        git.mkdir()
        (git / ".git").write_text("gitdir: fictional")
        link = self.dir / "link"
        link.symlink_to(self.out.parent, target_is_directory=True)
        cases = (
            (source, self.out, "already exists"),
            (self.dir / "missing.sqlite3", self.dir / "o1.sqlite3", "not found"),
            (junk, self.dir / "o2.sqlite3", "Cannot read"),
            (other, self.dir / "o3.sqlite3", "not a dashboard accounts database"),
            (source, self.dir / "nodir" / "o4.sqlite3", "directory does not exist"),
            (source, git / "o5.sqlite3", "outside Git"),
            (source, link / "o6.sqlite3", "without symlinks"),
        )
        for src, out, message in cases:
            with self.subTest(message=message):
                listing = sorted(p.name for p in out.parent.iterdir()) if out.parent.exists() else None
                result = run("--db", src, "--out", out)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(sorted(p.name for p in out.parent.iterdir()) if out.parent.exists() else None, listing)
        self.assertEqual(self.out.read_bytes(), kept, "an existing output must not be replaced")
        self.assertEqual(sha(source), before)
        self.assertEqual(sorted(p.name for p in self.dir.glob(".export-*")), [])

    def test_default_source_is_the_apps_default_path_and_help_says_so(self):
        from server import DEFAULT_DB
        result = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True, text=True)
        self.assertIn(str(DEFAULT_DB), "".join(result.stdout.split()))


class Policy(Base):
    def test_tripwire_every_table_the_app_creates_is_classified(self):
        """A table added later must be an explicit decision: export it, allow it on the host, or acknowledge exclusion."""
        path = self.dir / "fresh.sqlite3"
        init_db(path)
        Auth(path, CONFIG)
        acknowledged_excluded = {"account_transactions", "transaction_coverage", "transaction_merchant_rules",
                                 "transaction_categories", "classifier_cache", "classifier_settings"}
        names = {r[0] for r in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        unclassified = names - set(hosted_guard.HOSTED_TABLES) - acknowledged_excluded
        self.assertEqual(unclassified, set(), "new table: add it to hosted_guard.EXPORT_TABLES/HOSTED_TABLES or acknowledge it here")
        self.assertEqual(set(hosted_guard.EXPORT_TABLES) - names, set())
        self.assertLessEqual(set(hosted_guard.HOSTED_TABLES) - names, set())

    def test_guard_passes_a_fresh_migrated_file_a_missing_file_and_an_empty_file(self):
        fresh = self.dir / "fresh.sqlite3"
        init_db(fresh)
        Auth(fresh, CONFIG)
        self.assertEqual(rows(fresh, "SELECT COUNT(*) FROM transaction_categories")[0][0] > 0, True)  # seeded built-ins
        empty = self.dir / "empty.sqlite3"
        empty.touch()
        for path in (fresh, empty, self.dir / "missing.sqlite3"):
            self.assertEqual(hosted_guard.check(path), [], path.name)
        self.assertFalse((self.dir / "missing.sqlite3").exists())

    def test_guard_reports_each_non_allowed_table_with_counts_and_does_not_write(self):
        source = make_source(self.dir)
        before = sha(source)
        found = dict(hosted_guard.check(source))
        self.assertEqual(found, {"account_transactions": 4, "transaction_coverage": 1, "transaction_merchant_rules": 2,
                                 "classifier_cache": 1, "classifier_settings": 1})
        self.assertEqual(sha(source), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["out", "source.sqlite3"])
        with self.assertRaisesRegex(hosted_guard.Refused, r"account_transactions holds 4 rows.*export_scrubbed\.py"):
            hosted_guard.require_clean(source)

    def test_custom_categories_and_unknown_tables_trip_the_guard_but_built_ins_do_not(self):
        path = self.dir / "c.sqlite3"
        init_db(path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO transaction_categories(name, builtin, sort) VALUES ('ZZ-SENTINEL-COFFEE', 0, 1000)")
        self.assertEqual(hosted_guard.check(path), [("transaction_categories", 1)])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM transaction_categories WHERE builtin=0")
            db.execute("CREATE TABLE future_notes (x)")
            db.execute("INSERT INTO future_notes VALUES (1)")
        self.assertEqual(hosted_guard.check(path), [("future_notes", 1)])

    def test_unreadable_file_is_refused_not_ignored(self):
        junk = self.dir / "junk.sqlite3"
        junk.write_text("not a database " * 50)
        with self.assertRaises(hosted_guard.Refused):
            hosted_guard.check(junk)


if __name__ == "__main__":
    unittest.main()

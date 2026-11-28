import base64
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from bridge import Bridge, Problem, claim, credentials, enroll
from bulk import Batch
from demo_batch import Demo, seed
from server import init_db, validate


class Fixture(Demo):
    def __init__(self):
        self.payload = super().fetch()
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return deepcopy(self.payload)


class SyncFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.db"
        init_db(self.path)
        self.provider = Fixture()
        self.restart()
        with closing(sqlite3.connect(self.path)) as db, db:
            seed(db)

    def restart(self):
        self.batch = Batch(self.path, self.provider, validate)

    def sql(self, sql):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql).fetchall()

    def test_one_click_sync_infers_sign_and_preserves_local_overrides(self):
        notices = self.batch.sync()
        self.assertIn("12 added", notices[0])
        # Seeded manual card (id=1) + 12 synced accounts = 13 rows total
        rows = self.sql("select id, institution, nickname, side, balance, owner from accounts order by id")
        self.assertEqual(len(rows), 13)
        # First synced row (id=2) had -20.01 -> automatically debt; id=3 had 10.10 -> asset
        self.assertEqual(rows[1][3:5], ("debt", "20.01"))
        self.assertEqual(rows[2][3:5], ("asset", "10.10"))
        # User tags owner, flips side manually on id=4, and excludes id=5
        self.sql("update accounts set owner='Nanami' where id=2")
        self.sql("update accounts set side='debt', side_manual=1 where id=4")
        self.sql("update accounts set excluded=1 where id=5")
        self.restart()
        ac = self.provider.payload["accounts"]
        for a in ac:
            a["balance-date"] += 86400
        ac[0]["balance"] = "-21.88"            # updated debt
        ac[1]["balance"] = "-5.00"             # sign flip on non-overridden row -> flips to debt
        ac[2]["balance"] = "15.00"             # positive on manually-overridden debt -> stays debt
        ac[3]["balance-date"] -= 2 * 86400     # older than accepted source timestamp -> held
        ac[4]["balance"] = "0"                 # legitimate zero
        ac[5]["balance"] = "0.001"             # unsupported precision -> held
        ac[8]["currency"] = "USD"              # currency change -> held
        ac.append(ac[9].copy())                # repeated full identity -> held
        del ac[11]                             # missing, never remove
        self.provider.payload["errlist"] = [dict(code="act.failed", conn_id="2", account_id="2",
                                                 msg="PRIVATE-FICTIONAL-ERROR")]
        notice = self.batch.sync()
        self.assertIn("Account lookup failed", str(notice))
        self.assertNotIn("PRIVATE", str(notice))
        after = self.sql("select id, owner, side, balance, excluded from accounts where id>=2 order by id")
        self.assertEqual(after[0], (2, "Nanami", "debt", "21.88", 0))
        self.assertEqual(after[1], (3, "Unspecified", "debt", "5.00", 0))
        self.assertEqual(after[2], (4, "Unspecified", "debt", "15.00", 0))  # side_manual preserved
        self.assertEqual(after[3], (5, "Unspecified", "asset", "10.10", 1)) # older held, excluded preserved
        self.assertEqual(after[4], (6, "Unspecified", "asset", "0.00", 0))
        self.assertEqual(after[5], (7, "Unspecified", "asset", "10.10", 0)) # invalid precision held
        self.assertEqual(len(after), 12)  # missing row never deleted
        self.provider.payload = {}
        with self.assertRaisesRegex(Problem, "v2 account set"):
            self.batch.sync()

    def test_daily_quota_counts_failed_requests_across_restarts(self):
        self.provider.payload = None
        for _ in range(24):
            with self.assertRaisesRegex(Problem, "v2 account set"):
                self.batch.sync()
        self.restart()
        with self.assertRaisesRegex(Problem, "Daily guard"):
            self.batch.sync()
        self.assertEqual(self.provider.calls, 24)

    def test_deleted_synced_row_is_not_recreated(self):
        self.batch.sync()
        self.sql("delete from accounts where id=2")
        self.batch.sync()
        self.assertEqual(self.sql("select count(*) from accounts"), [(12,)])
        self.assertIn("not recreated", self.sql("select status from source_links where local=2")[0][0])

    def test_precision_is_not_float_or_round_to_fit(self):
        a = self.provider.payload["accounts"]
        a[1]["balance"] = "999999999999999.9100"
        a[2].update(currency="KWD", balance="0.001")
        a[8]["balance"] = "501.000"
        self.batch.sync()
        self.assertEqual(self.sql("select balance from accounts where id in (3,4,10) order by id"),
                         [("999999999999999.91",), ("0.001",), ("501",)])

    def test_malformed_and_future_snapshots_held(self):
        for change in [{"balance-date": int(time.time())+200}, {"balance-date": 0}, {"balance": None},
                       {"balance": "1e3"}, {"currency": "https://example.invalid/points"}, {"conn_id": ""}]:
            with tempfile.TemporaryDirectory() as t:
                db = Path(t) / "x.db"
                init_db(db)
                p = Fixture()
                p.payload["accounts"][0].update(change)
                notices = Batch(db, p, validate).sync()
                self.assertIn("11 added", notices[0])
                self.assertGreater(len(notices), 1)

    def test_malformed_signed_balance_does_not_cancel_valid_peer(self):
        self.batch.sync()
        bad, good = self.provider.payload["accounts"][:2]
        for a in (bad, good):
            a["balance-date"] += 86400
        bad["balance"], good["balance"] = "- 1", "21.42"
        notices = self.batch.sync()
        self.assertIn("Provider row 1: invalid, unsupported or duplicate; held.", notices)
        self.assertEqual(self.sql("select balance,status from accounts join source_links on id=local "
                                  "where id in (2,3) order by id"),
                         [("20.01", "Missing or provider error; retained"), ("21.42", "Updated")])


class BridgePrivacy(unittest.TestCase):
    access = "https://fiction:FICTIONAL-PRIVATE@beta-bridge.simplefin.org/simplefin"
    token = base64.b64encode(b"https://bridge.simplefin.org/simplefin/claim/fiction").decode()

    def test_private_cli_exchange_and_offline_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "vault" / "access"
            logs, calls = StringIO(), []
            def fake(m, h, p, headers):
                calls.append((m, h, p, headers))
                return 200, self.access.encode() if m == "POST" else json.dumps(Demo().fetch()).encode()
            with redirect_stdout(logs), redirect_stderr(logs), patch("getpass.getpass", return_value=self.token):
                enroll(path, fake)
                b = Bridge(path, fake)
                self.assertEqual(len(b.fetch()["accounts"]), 12)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(calls[0][:3], ("POST", "bridge.simplefin.org", "/simplefin/claim/fiction"))
            self.assertEqual(calls[1][:3], ("GET", "beta-bridge.simplefin.org", "/simplefin/accounts?version=2&balances-only=1"))
            self.assertEqual(calls[1][3]["Authorization"], "Basic " + base64.b64encode(b"fiction:FICTIONAL-PRIVATE").decode())
            self.assertEqual(logs.getvalue(), "")
            path.chmod(0o644)
            with self.assertRaises(Problem):
                Bridge(path, fake)
            path.chmod(0o600)
            path.with_suffix(".link").symlink_to(path)
            with self.assertRaises(OSError):
                Bridge(path.with_suffix(".link"), fake)
            (path.parent / ".git").mkdir()
            with self.assertRaisesRegex(Problem, "outside Git"):
                Bridge(path, fake)

    def test_allowlist_and_redacted_errors(self):
        for url in ("http://bridge.simplefin.org/simplefin/claim/fiction",
                    "https://bridge.simplefin.org.other.invalid/simplefin/claim/fiction",
                    "https://bridge.simplefin.org:8443/simplefin/claim/fiction"):
            with self.assertRaisesRegex(Problem, "no request"):
                claim(base64.b64encode(url.encode()), lambda *_: self.fail("unexpected request"))
        for bad in (self.access.replace("https:", "http:"), self.access+"?key=secret",
                    self.access.replace("beta-bridge.", "other."), self.access+"#x"):
            with self.assertRaises(Problem):
                credentials(bad)
        for status in (302, 403, 500):
            calls = []
            def fake(*args):
                calls.append(args)
                return status, self.access.encode()
            with self.assertRaisesRegex(Problem, "compromised"):
                claim(self.token, fake)
            self.assertEqual(len(calls), 1)
        # Origins, error bodies, redirects and details of transport exceptions are never rendered.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "secret"
            p.write_text(self.access)
            p.chmod(0o600)
            for body, reason in [(b'{"accounts":[],"accounts":[]}', "duplicate"),
                                 (b'{"connections":[{"name":"FICTIONAL-PRIVATE"}]}', "sensitive")]:
                with self.assertRaisesRegex(Problem, reason):
                    Bridge(p, lambda *_: (200, body)).fetch()
            for status, msg in [(302, "redirects"), (403, "revoked"), (402, "payment")]:
                b = Bridge(p, lambda *_: (status, self.access.encode()))
                with self.assertRaisesRegex(Problem, msg) as caught:
                    b.fetch()
                self.assertNotIn("FICTIONAL-PRIVATE", str(caught.exception))

    def test_claimed_nested_root_is_preserved_but_never_displayed(self):
        suffix = "/v2/test-FICTIONAL-ROOT"
        for slash in ("", "/"):
            root = self.access + suffix + slash
            received = claim(self.token, lambda *_: (200, root.encode()))
            self.assertEqual(received, root)
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp).resolve() / "access"
                p.write_text(received)
                p.chmod(0o600)
                targets = []
                def echo(method, host, target, headers):
                    targets.append(target)
                    return 200, b'{"accounts":[{"name":"test-FICTIONAL-ROOT"}]}'
                b = Bridge(p, echo)
                with self.assertRaisesRegex(Problem, "sensitive") as exc:
                    b.fetch()
                self.assertNotIn("FICTIONAL-ROOT", str(exc.exception))
                self.assertEqual(targets, ["/simplefin" + suffix + "/accounts?version=2&balances-only=1"])
        for bad in ("/.", "/v2/../x", "//x", "/x//", "/%2e%2e", "/x%2fy", "/x\\y",
                    "/v2?user=x", "/v2#x", "/x y", "/x\nq", "/x@y"):
            with self.subTest(bad=bad), self.assertRaises(Problem):
                credentials(self.access + bad)

    def test_demo_cannot_target_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "do-not-open.db"
            path.write_bytes(b"untouched")
            exe = Path(__file__).with_name("server.py")
            result = subprocess.run([sys.executable, exe, "--demo", "--db", str(path)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), b"untouched")

    def test_demo_cli_does_not_touch_normal_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)/".local/share/planner-accounts"
            vault.mkdir(parents=True)
            # Both sentinels are deliberately invalid: accidentally opening either must fail.
            for name in ("bridge.access", "accounts.sqlite3"):
                (vault/name).write_bytes(b"synthetic sentinel")
            cmd = [sys.executable, Path(__file__).with_name("server.py"), "--demo", "--port", "0"]
            with subprocess.Popen(cmd, env=dict(os.environ, HOME=tmp, TMPDIR=tmp), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as p:
                try:
                    url = re.search(r"http://127\.0\.0\.1:\d+/", p.stdout.readline())[0]
                    with urlopen(url, timeout=4) as response:
                        html = response.read().decode()
                        self.assertIn("Fictional demo", html)
                        self.assertIn("Household card", html)
                finally:
                    p.send_signal(signal.SIGINT)
                    _, err = p.communicate(timeout=5)
                self.assertEqual((p.returncode, err), (0, ""))
            self.assertEqual(sorted(x.name for x in vault.iterdir()), ["accounts.sqlite3", "bridge.access"])
            self.assertTrue(all(x.read_bytes() == b"synthetic sentinel" for x in vault.iterdir()))

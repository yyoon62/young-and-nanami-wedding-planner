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


class ReviewFlow(unittest.TestCase):
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

    def review(self):
        self.batch.run("discover", {})
        fields = {"review": self.batch.review[0], "all": "on", "owner": "Young", "o1": "Nanami"}
        for i in range(12):
            fields[f"m{i}"] = "1" if i == 0 else "new"
            fields[f"d{i}"] = "debt" if i == 0 else "asset"
        return fields

    def test_atomic_review_validation_and_racing_manual_edit(self):
        f = self.review()
        with self.assertRaises(Problem):
            self.batch.run("apply", dict(f, d11=""))
        self.assertEqual(self.sql("select balance from accounts"), [("30.00",)])
        self.sql("update accounts set balance='25.00'")
        with self.assertRaisesRegex(Problem, "changed since review"):
            self.batch.run("apply", f)
        self.assertEqual(self.sql("select * from source_links"), [])
        self.batch.run("apply", self.review())
        with self.assertRaisesRegex(Problem, "expired"):
            self.batch.run("apply", f)

    def test_restart_refresh_conflicts_errors_and_no_deletion(self):
        self.batch.run("apply", self.review())
        self.restart()
        f = self.review()
        with self.assertRaisesRegex(Problem, "cannot create"):
            self.batch.run("apply", f)
        self.assertEqual(len(self.sql("select * from accounts")), 12)
        self.sql("update accounts set balance='0.27', asof='2024-01-01' where id=2")
        ac = self.provider.payload["accounts"]
        for a in ac:
            a["balance-date"] += 86400
        ac[0].update(name="Renamed", balance="-21.88")
        ac[2]["balance"] = "-2.00"             # sign reversal
        ac[3]["balance-date"] -= 2*86400       # older than accepted source timestamp
        ac[4]["balance"] = "0"                 # legitimate zero
        ac[5]["balance"] = "0.001"             # unsupported precision
        ac[6]["balance"] = "3.33"              # unselected
        ac[7]["balance-date"] -= 86400
        ac[7]["balance"] = "99.99"             # conflicting timestamp
        ac[8]["currency"] = "USD"
        ac.append(ac[9].copy())                # repeated full identity
        del ac[11]                             # missing, never remove
        self.provider.payload["errlist"] = [dict(code="act.failed", conn_id="2", account_id="2",
                                                msg="PRIVATE-FICTIONAL-ERROR")]
        selected = {"r"+str(i): "on" for i in range(1, 13) if i != 7}
        notice = self.batch.run("refresh", selected)
        self.assertIn("Account lookup failed", str(notice))
        self.assertNotIn("PRIVATE", str(notice))
        rows = self.sql("select owner,nickname,balance from accounts order by id")
        self.assertEqual(rows[0], ("Joint", "Household card", "21.88"))
        self.assertEqual(rows[1], ("Nanami", "Savings", "0.27"))
        self.assertEqual([r[2] for r in rows[2:]], ["10.10","10.10","0.00","10.10","10.10","10.10",*(["501"]*4)])
        states = [r[0] for r in self.sql("select status from source_links order by local")]
        for i, text in [(1,"Local edit"), (2,"sign"), (3,"Older"), (5,"invalid"), (7,"Conflicting"),
                        (8,"Currency"), (9,"invalid"), (10,"provider error"), (11,"Missing")]:
            self.assertIn(text, states[i])
        self.provider.payload = {}
        with self.assertRaisesRegex(Problem, "v2 account set"):
            self.batch.run("refresh", selected)
        self.assertEqual(rows, self.sql("select owner,nickname,balance from accounts order by id"))

    def test_daily_quota_counts_failed_requests_across_restarts(self):
        self.provider.payload = None
        for _ in range(24):
            with self.assertRaisesRegex(Problem, "v2 account set"):
                self.batch.run("discover", {})
        self.restart()
        with self.assertRaisesRegex(Problem, "Daily guard"):
            self.batch.run("discover", {})
        self.assertEqual(self.provider.calls, 24)

    def test_older_match_and_deleted_link(self):
        self.sql("update accounts set asof='2025-01-01'")
        with self.assertRaisesRegex(Problem, "older"):
            self.batch.run("apply", self.review())
        self.sql("update accounts set asof='2024-01-01'")
        self.batch.run("apply", self.review())
        self.sql("delete from accounts where id=1")
        self.batch.run("refresh", {"r1": "on"})
        self.assertEqual(self.sql("select count(*) from accounts"), [(11,)])
        self.assertIn("not recreated", self.sql("select status from source_links where local=1")[0][0])
        f = self.review()
        with self.assertRaises(Problem):
            self.batch.run("apply", dict(f, m0="new"))

    def test_precision_is_not_float_or_round_to_fit(self):
        a = self.provider.payload["accounts"]
        a[1]["balance"] = "999999999999999.9100"
        a[2].update(currency="KWD", balance="0.001")
        a[8]["balance"] = "501.000"
        self.batch.run("apply", self.review())
        self.assertEqual(self.sql("select balance from accounts where id in (2,3,9) order by id"),
                         [("999999999999999.91",), ("0.001",), ("501",)])

    def test_malformed_and_future_snapshots_held(self):
        for change in [{"balance-date": int(time.time())+200}, {"balance-date": 0}, {"balance": None},
                       {"balance": "1e3"}, {"currency": "https://example.invalid/points"}, {"conn_id": ""}]:
            p = Fixture()
            p.payload["accounts"][0].update(change)
            b = Batch(self.path, p, validate)
            b.run("discover", {})
            self.assertEqual(len(b.review[1]), 11)
            self.assertTrue(b.review[-1])


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

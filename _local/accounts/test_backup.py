"""Real GPG and SQLite round trips using temporary keyrings, databases and fictional balances."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from demo_batch import Demo
from test_history import BASE, DAY
from test_hosted import NOW, ORIGIN, RSA, challenge, csrf, login, request, running


@unittest.skipUnless(shutil.which("gpg"), "GnuPG is needed for real backup tests")
class BackupFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name).resolve()
        cls.rsa = RSA(cls.root)
        cls.recovery = cls.root / "r"
        cls.encrypt_only = cls.root / "p"
        cls.destination = cls.root / "backup-disk"
        cls.state = cls.root / "state"
        for path in (cls.recovery, cls.encrypt_only, cls.destination, cls.state):
            path.mkdir(mode=0o700)
        # Use a short socket path on platforms with a UNIX sockaddr path limit.
        cls.addClassCleanup(cls.kill_agents)
        cls.gpg(cls.recovery, "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", "Fictional L17 recovery", "rsa2048", "encr", "1d")
        ls = cls.gpg(cls.recovery, "--with-colons", "--list-keys").decode().splitlines()
        cls.recipient = next(line.split(":")[9] for line in ls if line.startswith("fpr:"))
        cls.gpg(cls.encrypt_only, "--import", raw=cls.gpg(cls.recovery, "--export", cls.recipient))

    @classmethod
    def kill_agents(cls):
        if shutil.which("gpgconf"):
            for path in (cls.recovery, cls.encrypt_only):
                subprocess.run(["gpgconf", "--homedir", path, "--kill", "gpg-agent"], capture_output=True)

    @staticmethod
    def gpg(home, *args, raw=None):
        proc = subprocess.run(["gpg", "--no-options", "--homedir", home, "--batch", *args],
                              input=raw, capture_output=True)
        if proc.returncode:
            raise AssertionError(proc.stderr.decode())
        return proc.stdout

    def invoke(self, action, path, *args, home=None, recipient=None, okay=True):
        env = dict(os.environ, GNUPGHOME=str(home or self.encrypt_only), ACCOUNTS_BACKUP_DIR=str(self.destination),
                   ACCOUNTS_BACKUP_RECIPIENT=recipient or self.recipient)
        result = subprocess.run(["/bin/sh", "deploy/backup-sqlite.sh", action, "--db", path, *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, okay, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_online_wal_backup_and_atomic_offline_restore(self):
        db_path = self.state / "fictional.db"
        account = dict(BASE, nickname="PRIVATE-LAB-MARKER", balance="100.10")
        a, b, pending = {}, {}, {}
        with running(db_path, self.rsa, lambda: NOW, provider=Demo()) as server:
            self.assertEqual(login(server, self.rsa, a)[0], 204)
            token = csrf(server, a)
            with closing(sqlite3.connect(db_path)) as wal:
                self.assertEqual(wal.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                self.assertEqual(request(server, "/sync", dict(token=token), jar=a, origin=ORIGIN)[0], 303)
                self.assertEqual(request(server, "/new", dict(account, token=token), jar=a, origin=ORIGIN)[0], 303)
                with wal:
                    wal.execute("BEGIN IMMEDIATE")
                    wal.execute("UPDATE accounts SET nickname = 'NOT-COMMITTED' WHERE nickname = ?", (account["nickname"],))
                    self.assertIn("Dry-run", self.invoke("backup", db_path, "--dry-run"))
                    self.assertEqual(list(self.destination.iterdir()), [])
                    self.assertIn("Encrypted backup written", self.invoke("backup", db_path))
                    archive, = self.destination.iterdir()
                    self.assertEqual(archive.suffix, ".gpg")
                    self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn(account["nickname"].encode(), archive.read_bytes())
                self.assertIn("Stop the hosted dashboard", self.invoke("restore", db_path, "--archive", archive,
                                                                        "--dry-run", home=self.recovery, okay=False))
            self.assertEqual(login(server, self.rsa, b, email="member.beta@workspace.example", hd="workspace.example",
                                   sub="200002")[0], 204)
            pending_credential = self.rsa.make(challenge(server, pending))
        decrypted = self.gpg(self.recovery, "--decrypt", str(archive))
        # Auth data removed before encryption, not simply discarded at restore.
        self.assertNotIn(token.encode(), decrypted)
        self.assertNotIn(b"NOT-COMMITTED", decrypted)
        snapshot = sqlite3.connect(":memory:")
        with closing(snapshot):
            snapshot.deserialize(decrypted)
            for table in ("hosted_sessions", "hosted_challenges"):
                self.assertEqual(snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (0,))
        before = db_path.read_bytes()
        self.assertIn("Dry-run", self.invoke("restore", db_path, "--archive", archive, "--dry-run", home=self.recovery))
        self.assertEqual(db_path.read_bytes(), before)
        self.invoke("restore", db_path, "--archive", archive, okay=False)  # Public key alone cannot restore.
        corrupted = self.destination / "truncated.sqlite3.gpg"
        corrupted.write_bytes(archive.read_bytes()[:-30])
        corrupted.chmod(0o600)
        self.invoke("restore", db_path, "--archive", corrupted, home=self.recovery, okay=False)
        self.assertEqual(db_path.read_bytes(), before)
        self.invoke("backup", db_path, recipient="0" * 40, okay=False)
        self.assertEqual(set(self.destination.iterdir()), {archive, corrupted})
        self.assertIn("sessions and sign-in challenges revoked",
                      self.invoke("restore", db_path, "--archive", archive, home=self.recovery))
        self.assertEqual({p.name for p in self.state.iterdir()},
                         {"fictional.db", "fictional.db.hosted-lock", "fictional.db.backup-lock", "logos"})
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("SELECT nickname,balance,currency,asof FROM accounts WHERE nickname=?",
                                        (account["nickname"],)).fetchall(),
                             [(account["nickname"], "100.10", "USD", DAY + "01")])
            self.assertEqual(db.execute("SELECT h.balance FROM balance_history h JOIN accounts a "
                                        "ON h.account_id=a.id WHERE a.nickname=?",
                                        (account["nickname"],)).fetchone(), ("100.10",))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_links").fetchone(), (12,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hosted_identities").fetchone(), (2,))
        with running(db_path, self.rsa, lambda: NOW) as server:
            self.assertEqual(request(server, "/new", jar=a)[0], 401)
            self.assertEqual(request(server, "/new", jar=b)[0], 401)
            self.assertEqual(request(server, "/auth/exchange", dict(credential=pending_credential), jar=pending,
                                     origin=ORIGIN)[0], 401)
            self.assertEqual(login(server, self.rsa, {}, email="member.beta@workspace.example", hd="workspace.example",
                                   sub="replacement")[0], 403)
            self.assertEqual(login(server, self.rsa, a)[0], 204)
            self.assertIn(account["nickname"].encode(), request(server, "/", jar=a)[2])

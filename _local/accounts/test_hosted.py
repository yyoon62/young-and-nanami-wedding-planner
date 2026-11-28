"""Hosted account acceptance without network, provider credentials, or real Google accounts."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from importlib.util import find_spec
import os
from pathlib import Path
import re
import sqlite3
import select
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy"))
from demo_batch import Demo
from hosted_auth import Auth, CHALLENGE_TTL, SESSION_TTL, Config, GoogleKeys, Rejected, db_path, offline_lock
from server import serve
from test_history import BASE, DAY
import seed_fictional

ENV = dict(GOOGLE_CLIENT_ID="12345-fictional.apps.googleusercontent.com",
           LOANS_ORIGIN="https://accounts.example.test", LOANS_SESSION_KEY="56" * 32,
           LOANS_MEMBERS="memberalpha@gmail.com,member.beta@workspace.example")
CONFIG = Config.load(ENV)
ORIGIN = CONFIG.origin
FORM = "application/x-www-form-urlencoded"
NOW = 1800000000
HAS_CRYPTO = find_spec("cryptography") is not None


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class RSA:
    def __init__(self, folder):
        self.path = Path(folder) / "fictional-rsa.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", self.path], check=True, capture_output=True)
        out = subprocess.check_output(["openssl", "rsa", "-in", self.path, "-noout", "-modulus"])
        self.jwk = {"n": b64(bytes.fromhex(out.decode().strip().split("=")[1])), "e": "AQAB"}

    def make(self, nonce, now=NOW, header=None, **claims):
        fields = dict(aud=CONFIG.client, iss="https://accounts.google.com", nonce=nonce,
                      iat=now, exp=now + 3600, sub="100001", email=CONFIG.members[0], email_verified=True)
        fields.update(claims)
        header = dict(alg="RS256", kid="fixture") if header is None else header
        message = ".".join(b64(json.dumps(item).encode()) for item in (header, fields))
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.path], input=message.encode(),
                             capture_output=True, check=True).stdout
        return message + "." + b64(sig)

    def keys(self, kid):
        return self.jwk if kid == "fixture" else None


@contextmanager
def running(path, rsa, time_read=None, *, http=False, provider=None):
    with serve(path, 0, provider, logo_dir=path.parent / "logos", hosted=CONFIG, keys=rsa.keys,
               clock=time_read) as server:
        server.logo_fetcher = None
        if http:
            server.auth.config = replace(CONFIG, origin=f"http://127.0.0.1:{server.server_port}")
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, target, data=None, jar=None, origin=None, method=None, headers=None):
    headers = dict(headers or {})
    if jar:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    if origin is not None:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = FORM
    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=10)) as conn:
        conn.request(method or ("POST" if data is not None else "GET"), target,
                     urlencode(data) if data is not None else None, headers)
        with conn.getresponse() as response:
            cookies, result = response.headers.get_all("Set-Cookie", []), response.read()
            status, returned_headers = response.status, response.headers
    if jar is not None:
        for header in cookies:
            for k, c in SimpleCookie(header).items():
                if c["max-age"] == "0":
                    jar.pop(k, None)
                else:
                    jar[k] = c.value
    return status, returned_headers, result


def challenge(server, jar):
    status, _, contents = request(server, "/auth/challenge", jar=jar, origin=server.auth.config.origin, method="POST")
    assert status == 200, status
    return json.loads(contents)["nonce"]


def login(server, rsa, jar, **claims):
    nonce = challenge(server, jar)
    return request(server, "/auth/exchange", {"credential": rsa.make(nonce, now=int(server.auth.clock()), **claims)},
                   jar=jar, origin=server.auth.config.origin)


def csrf(server, jar):
    body = request(server, "/new", jar=jar)[2]
    return re.search(rb'name="token" value="([^"]+)"', body)[1].decode()


class Startup(unittest.TestCase):
    def test_strict_configuration(self):
        for field, values in {
            "LOANS_MEMBERS": ("a@gmail.com,A@gmail.com", "a@gmail.com,b@gmail.com,c@gmail.com",
                              "a@gmail.com,b+tag@gmail.com", "a@gmail.com,b.b@gmail.com",
                              "a@gmail.com,b@googlemail.com", "a@gmail.com,b+tag@example.com", ""),
            "LOANS_ORIGIN": ("", "https://pi.example.test/", "https://pi.example.test:443",
                            "https://pi.example.test:65536", "https://pi.example.test/?x",
                            "https://pi..test", "https://pi.example.test@localhost",
                            "http://localhost.example.test:8765", "https://pi.example.test\r\n",
                            "https://127.1", "https://pi.example.1", "http://[::1]:8765"),
            "GOOGLE_CLIENT_ID": ("", "fictional", "123-xxx.apps.googleusercontent.com\n"),
            "LOANS_SESSION_KEY": ("", "ff" * 31, "z0" * 32, "ff" * 33),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Config.load(dict(ENV, **{field: value}))
        for origin in ("http://localhost:8765", "http://127.0.0.1:8765"):
            config = Config.load(dict(ENV, LOANS_ORIGIN=origin))
            self.assertNotIn("Secure", config.cookie("session", "sample", 10))
            self.assertNotIn("__Host-", config.cookie("session"))

    def test_rejects_git_worktrees_symlinks_and_shared_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()
            repo = path / "repository"
            repo.mkdir(0o700)
            (repo / ".git").write_text("gitdir: fictional")
            (path / "linked").symlink_to(repo, target_is_directory=True)
            weak = path / "weak.db"
            weak.touch()
            weak.chmod(0o644)  # not touch(mode): the process umask (0077 under systemd) would narrow it
            for candidate in (repo / "subdir" / "a", path / "linked" / "a", weak):
                with self.assertRaises(ValueError):
                    db_path(candidate)
            self.assertFalse((repo / "subdir").exists())

    def test_cli_guards_before_bridge_or_db_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "never-created.sqlite"
            repo = Path(tmp).resolve() / "repository"  # do not rely on the code itself being a Git checkout
            repo.mkdir(0o700)
            (repo / ".git").write_text("gitdir: fictional")
            cmd = [sys.executable, "server.py", "--hosted"]
            for extra, env, message in [
                (["--demo"], ENV, "--demo cannot be combined"),
                (["--bridge", "--db", str(db)], dict(ENV, GOOGLE_CLIENT_ID=""), "Hosted startup refused"),
                (["--db", str(repo / "never-created.sqlite")], ENV, "outside Git"),
            ]:
                p = subprocess.run(cmd + extra, env=dict(os.environ, **env), capture_output=True, text=True)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(message, p.stderr)
            self.assertFalse(db.exists())

    def test_hosted_env_refuses_insecure_file_before_reading_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            envfile, link, db = (folder / name for name in ("hosted.env", "linked.env", "never.sqlite"))
            envfile.write_text("".join(f"{k}={v}\n" for k, v in ENV.items()))
            envfile.chmod(0o644)
            link.symlink_to(envfile)
            command = [sys.executable, "server.py", "--hosted", "--bridge", "--db", str(db), "--hosted-env"]
            for path, reason in ((envfile, "mode 600"), (link, "without symlinks")):
                r = subprocess.run(command + [str(path)], env=dict(os.environ, HOME=str(folder)), capture_output=True,
                                   text=True, timeout=5)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(reason, r.stderr)
                self.assertFalse(db.exists())
            envfile.chmod(0o600)
            with envfile.open("a") as stream:
                stream.write("LOANS_ORIGIN=https://new.example.test\n")
            r = subprocess.run(command + [str(envfile)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("one literal KEY=value", r.stderr)
            self.assertFalse(db.exists())

    def test_missing_hosted_dependency_fails_before_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp).resolve() / "missing-library.sqlite"
            env = dict(os.environ, **ENV)
            env.pop("PYTHONPATH", None)
            r = subprocess.run([sys.executable, "-S", "server.py", "--hosted", "--db", db], env=env,
                               capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("requirements-hosted.txt", r.stderr)
            self.assertFalse(db.exists())

    @unittest.skipUnless(HAS_CRYPTO, "install requirements-hosted.txt for hosted acceptance")
    def test_hosted_and_bridge_start_with_private_env_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            envfile = home / "hosted.env"
            envfile.write_text("".join(f"{k}={v}\n" for k, v in ENV.items()))
            envfile.chmod(0o600)
            state = home / ".local/share/planner-accounts"
            state.mkdir(mode=0o700, parents=True)
            access = state / "bridge.access"
            access.write_text("https://fictional:neverfetch@beta-bridge.simplefin.org/simplefin/synthetic")
            access.chmod(0o600)
            with subprocess.Popen([sys.executable, "server.py", "--port", "0", "--hosted", "--bridge",
                                   "--hosted-env", envfile], env=dict(os.environ, HOME=str(home), GOOGLE_CLIENT_ID=""),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0], "hosted server did not start")
                    self.assertIn(f"Open {ORIGIN}/", process.stdout.readline())
                    with closing(sqlite3.connect(state / "accounts.sqlite3")) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone(), (0,))
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, process.stderr.read())

    @patch("hosted_auth.HTTPSConnection", autospec=True)
    def test_google_key_endpoint_is_fixed_and_does_not_follow_redirects(self, connect):
        connection = connect.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "public, max-age=300"
        jwk = dict(kty="RSA", alg="RS256", use="sig", kid="legitimate", n="fictional", e="AQAB")
        response.read.return_value = json.dumps({"keys": [jwk, dict(jwk, alg="HS256", kid="no")]}).encode()
        keys = GoogleKeys()
        self.assertEqual(keys("legitimate"), jwk)
        self.assertIsNone(keys("no"))
        connect.assert_called_once_with("www.googleapis.com", timeout=5)
        connection.request.assert_called_once_with("GET", "/oauth2/v3/certs")
        response.status = 302
        with self.assertRaises(Rejected) as exc:
            GoogleKeys()("legitimate")
        self.assertEqual(exc.exception.status, 503)
        self.assertEqual(connect.call_count, 2)


@unittest.skipUnless(HAS_CRYPTO, "install requirements-hosted.txt for hosted acceptance")
class HostedFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.rsa = RSA(tmp.name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name).resolve() / "fictional.db"
        self.now = NOW
        ctx = running(self.db, self.rsa, lambda: self.now, provider=Demo())
        self.server = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def assert_private(self, response, status):
        self.assertEqual(response[0], status, response[2])
        self.assertEqual(response[1]["Cache-Control"], "no-store")
        return response[2]

    def test_clean_root_only_and_no_cache_before_any_dispatch(self):
        member = {}
        self.assert_private(login(self.server, self.rsa, member), 204)
        token = csrf(self.server, member)
        self.assert_private(request(self.server, "/new", dict(BASE, token=token, institution="Fictional Chase",
                                                             nickname="LAB-SECRET-ACCOUNT"),
                                    jar=member, origin=ORIGIN), 303)
        response = request(self.server, "/", headers={"Host": "untrusted", "X-Forwarded-Host": "malicious"})
        html = self.assert_private(response, 200)
        self.assertIn(b"Sign in to the private dashboard", html)
        self.assertEqual(response[1]["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")
        self.assertIn("https://accounts.google.com/gsi/client", response[1]["Content-Security-Policy"])
        for marker in (b"LAB-SECRET", b"SimpleFIN", b"Net worth history", b'dashboard.js', b'style.css', b'name="token"'):
            self.assertNotIn(marker, html)
        for target in ("/?view=Household", "//", "/%2f", "/batch", "/sync", "/owner/1", "/rename/1",
                       "/section/1", "/exclude/1", "/side/1", "/new", "/new?servicer=uas", "/edit/1",
                       "/remove/1", "/logo/chase.com", "/servicers", "/federal-preview", "/preview.js",
                       "/dashboard.js", "/favicon.ico", "/style.css", "/api/future", "/logout"):
            with self.subTest(target=target):
                self.assert_private(request(self.server, target), 401)
                self.assert_private(request(self.server, target, {"token": token}, origin=ORIGIN), 401)
        for method in ("OPTIONS", "PUT", "HEAD", "TRACE"):
            self.assert_private(request(self.server, "/", method=method), 401)
        self.assert_private(request(self.server, "/auth/sign-in.js"), 200)
        self.assert_private(request(self.server, "/logo/chase.com", jar=member), 200)
        self.assert_private(request(self.server, "/api/future", jar=member), 404)
        self.assert_private(request(self.server, "/?view=unknown", jar=member), 400)

    def test_both_members_and_csrf_origin_signout(self):
        a, b = {}, {}
        response = login(self.server, self.rsa, a, email="member.alpha@gmail.com")
        self.assert_private(response, 204)
        self.assertIn("__Host-accounts-session=", response[1]["Set-Cookie"])
        for attribute in ("Secure", "HttpOnly", "Path=/", "SameSite=Lax", "Max-Age=3600"):
            self.assertIn(attribute, response[1]["Set-Cookie"])
        self.assertNotIn("Domain=", response[1]["Set-Cookie"])
        self.assert_private(login(self.server, self.rsa, b, sub="200002", email=CONFIG.members[1],
                                  hd="workspace.example"), 204)
        one, two = csrf(self.server, a), csrf(self.server, b)
        self.assertNotEqual(one, two)
        xss = '\"><img src=x onerror="alert(1)">'
        for owner, token, balance, currency, name in [
            (a, one, "100.10", "USD", xss), (b, two, "20.20", "USD", "Fictional two"),
            (a, one, "500", "JPY", "Fictional JPY"),
        ]:
            self.assert_private(request(self.server, "/new", dict(BASE, token=token, balance=balance,
                                                                 currency=currency, nickname=name),
                                        jar=owner, origin=ORIGIN), 303)
        for owner in (a, b):
            html = self.assert_private(request(self.server, "/", jar=owner), 200)
            for marker in (b"USD 120.30", b"JPY 500", b"Net worth history", b"2024-11-", b"&lt;img src=x",
                           b'<form method="post" action="/logout">'):
                self.assertIn(marker, html)
            self.assertNotIn(xss.encode(), html)
        self.assert_private(request(self.server, "/logout", jar=a), 404)
        self.assert_private(request(self.server, "/logout?token=" + one, jar=a), 404)
        self.assert_private(request(self.server, "/new", jar=a), 200)
        for bad_token, bad_origin in ((one, "https://unapproved.test"), (one, None), (two, ORIGIN), ("", ORIGIN)):
            self.assert_private(request(self.server, "/rename/1", {"token": bad_token, "nickname": "not applied"},
                                        jar=a, origin=bad_origin,
                                        headers={"Host": "unapproved.test", "X-Forwarded-Origin": ORIGIN}), 403)
            self.assert_private(request(self.server, "/logout", {"token": bad_token}, jar=a, origin=bad_origin), 403)
        replay = a.copy()
        self.assert_private(request(self.server, "/logout?ignore=1", {"token": one}, jar=a, origin=ORIGIN), 404)
        self.assert_private(request(self.server, "/new", jar=a), 200)
        out = request(self.server, "/logout", {"token": one}, jar=a, origin=ORIGIN)
        self.assert_private(out, 303)
        self.assertEqual(a, {})
        self.assertIn("Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure", out[1]["Set-Cookie"])
        self.assert_private(request(self.server, "/new", jar=replay), 401)
        self.assert_private(request(self.server, "/new", jar=b), 200)
        self.assertNotIn(b"not applied", request(self.server, "/", jar=b)[2])

    def test_google_claim_signature_workspace_and_sub_fail_closed(self):
        for fields, status in [
            (dict(aud="another-client"), 401), (dict(azp="another-client"), 401),
            (dict(iss="https://google-attacker.test"), 401), (dict(exp=NOW), 401),
            (dict(iat=NOW + 60), 401), (dict(nbf=NOW + 60), 401), (dict(exp=True), 401),
            (dict(exp=NOW + 7200), 401),
            (dict(email="stranger@gmail.com"), 403), (dict(email_verified="true"), 403),
            (dict(email=CONFIG.members[1]), 403), (dict(sub=""), 401),
            (dict(header={"alg": "none", "kid": "fixture"}), 401),
            (dict(header={"alg": "RS256", "kid": "different", "jku": "https://example.test/jwks"}), 401),
        ]:
            with self.subTest(fields=fields):
                jar = {}
                self.assert_private(login(self.server, self.rsa, jar, **fields), status)
                self.assert_private(request(self.server, "/edit/1", jar=jar), 401)
        jar = {}
        nonce = challenge(self.server, jar)
        credential = self.rsa.make(nonce)
        head, body, sig = credential.split(".")
        bad = ".".join((head, body, ("a" if sig[0] != "a" else "b") + sig[1:]))
        for value in (bad, credential + "=", self.rsa.make("another-challenge")):
            self.assert_private(request(self.server, "/auth/exchange", {"credential": value}, jar=jar, origin=ORIGIN), 401)
        self.assert_private(login(self.server, self.rsa, {}), 204)
        self.assert_private(login(self.server, self.rsa, {}, sub="replacement"), 403)
        self.assert_private(login(self.server, self.rsa, {}, email=CONFIG.members[1], hd="workspace.example"), 403)

    def test_exchange_atomic_replay_origin_and_expiry(self):
        jar = {}
        self.assert_private(request(self.server, "/auth/challenge", method="POST",
                                    headers={"X-Forwarded-Proto": "https", "Host": "accounts.example.test"}), 403)
        nonce = challenge(self.server, jar)
        credential = self.rsa.make(nonce)
        data = {"credential": credential}
        for origin in (None, "null", "https://elsewhere.example", ORIGIN + "/"):
            self.assert_private(request(self.server, "/auth/exchange", data, jar=jar, origin=origin), 403)
        self.assert_private(request(self.server, "/auth/exchange", data, jar={}, origin=ORIGIN), 401)
        fresh = {}
        challenge(self.server, fresh)
        self.assert_private(request(self.server, "/auth/exchange", data, jar=fresh, origin=ORIGIN), 401)
        replay = jar.copy()
        copies = [jar, replay.copy()]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda candidate: request(self.server, "/auth/exchange", data,
                                                              jar=candidate, origin=ORIGIN)[0], copies))
        self.assertEqual(sorted(results), [204, 401])
        saved = next(candidate for candidate, code in zip(copies, results) if code == 204)
        self.assert_private(request(self.server, "/auth/exchange", data, jar=replay, origin=ORIGIN), 401)
        tampered = {k: v[:-1] + ("a" if v[-1] != "a" else "b") for k, v in saved.items()}
        self.assert_private(request(self.server, "/style.css", jar=tampered), 401)
        self.assert_private(request(self.server, "/style.css", jar=saved), 200)
        self.now += SESSION_TTL
        self.assert_private(request(self.server, "/style.css", jar=saved), 401)
        old = {}
        old_token = self.rsa.make(challenge(self.server, old), now=self.now)
        self.now += CHALLENGE_TTL
        self.assert_private(request(self.server, "/auth/exchange", dict(credential=old_token), jar=old, origin=ORIGIN), 401)

    def test_session_and_identity_pins_are_stored_in_sqlite(self):
        jar = {}
        self.assert_private(login(self.server, self.rsa, jar), 204)
        # The hosted listener registers the same lock used by the restore tool.
        with self.assertRaisesRegex(ValueError, "Stop the hosted dashboard"), offline_lock(self.db):
            self.fail("A running hosted listener must hold the offline lock.")
        # An independent auth instance sees persistent sessions, not process-local state.
        auth = Auth(self.db, CONFIG, keys=self.rsa.keys, clock=lambda: self.now)
        self.assertEqual(auth.session(jar[CONFIG.name("session")])[1], csrf(self.server, jar))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT * FROM hosted_identities").fetchall(), [(CONFIG.members[0], "100001")])


class CountingProvider:
    """A provider that records every fetch. Hosted Transactions must never cause one."""
    namespace = "fictional-counting-v1"
    label = "Counting fixture"

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return dict(connections=[], accounts=[], errlist=[])


TX_TABLES = ("account_transactions", "transaction_coverage", "transaction_merchant_rules")
PROJECTION_FORM = dict(years="10", income="5000", spending="3000", debt_target="", retirement_target="",
                       investment_target="", checking_apy="0", hysa_apy="4", investment_return="7",
                       retirement_return="7", debt_apr="6", inflation="2.5", mode="nominal",
                       view="Household", currency="USD")


@unittest.skipUnless(HAS_CRYPTO, "install requirements-hosted.txt for hosted acceptance")
class HostedTransactionsGated(unittest.TestCase):
    """L21: Transactions is unreachable when hosted, for every route and method.

    The DB deliberately contains fictional transaction rows ("gated off" is not "absent").
    NO_TRANSACTION_DATA_ON_ANY_HOSTED_ROUTE below is a generic sentinel test: when another task
    adds transaction-derived features (for example projection suggestions), re-run
    `python -m unittest test_hosted.HostedTransactionsGated` after merging.
    """
    SENTINELS = (seed_fictional.SENTINEL_MERCHANT, seed_fictional.SENTINEL_DESCRIPTION,
                 "7777.77", "7,777.77", "4242.42", "4,242.42", "Fictional category", "FICTIONAL PAYROLL")

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.rsa = RSA(tmp.name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name).resolve() / "fictional.db"
        seed_fictional.seed(self.db)
        self.now = NOW
        self.provider = CountingProvider()
        ctx = running(self.db, self.rsa, lambda: self.now, provider=self.provider)
        self.server = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.a, self.b = {}, {}
        assert login(self.server, self.rsa, self.a, email="member.alpha@gmail.com")[0] == 204
        assert login(self.server, self.rsa, self.b, sub="200002", email=CONFIG.members[1],
                     hd="workspace.example")[0] == 204

    def tx_state(self):
        with closing(sqlite3.connect(self.db)) as db:
            return [db.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in TX_TABLES]

    def assert_no_sentinel(self, response, label):
        text = response[2].decode("utf8", "replace")
        for marker in self.SENTINELS:
            if marker in text:  # not assertNotIn: its failure message would print the whole page
                self.fail(f"{marker!r} leaked from {label}")

    def test_positive_control_local_mode_does_show_the_fixture_rows(self):
        """Without this, 'sentinel absent' could pass only because the fixture rows never render at all."""
        local = Path(self.db).with_name("local.db")
        seed_fictional.seed(local)
        with serve(local, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                status, _, body = request(server, "/transactions")
                html = body.decode()
                self.assertEqual(status, 200)
                self.assertIn(seed_fictional.SENTINEL_MERCHANT, html)
                self.assertIn("7,777.77".replace(",", ""), html.replace(",", ""))
                self.assertIn(">Transactions</a>", request(server, "/")[2].decode())
            finally:
                server.shutdown()
                thread.join()

    def test_unauthenticated_every_transaction_and_projection_route_is_401_no_store(self):
        token = "x" * 43
        for method_target in ("/new", "/transactions", "/transactions?month=2026-11&view=Young",
                              "/transactions/", "/transactions/edit", "/transactions/sync", "/projections",
                              "/projections?view=Young&currency=USD"):
            with self.subTest(target=method_target):
                response = request(self.server, method_target)
                self.assertEqual((response[0], response[1]["Cache-Control"]), (401, "no-store"))
                post = request(self.server, method_target, {"token": token}, origin=ORIGIN)
                self.assertEqual((post[0], post[1]["Cache-Control"]), (401, "no-store"))
                self.assert_no_sentinel(response, method_target)
        root = request(self.server, "/")
        self.assertEqual(root[0], 200)
        self.assertIn(b"Sign in to the private dashboard", root[2])
        self.assertEqual(self.provider.calls, 0)

    def test_signed_in_transaction_routes_are_absent_and_writes_are_refused(self):
        before = self.tx_state()
        self.assertTrue(any(before[0]), "fixture must contain transaction rows")
        for jar in (self.a, self.b):
            token = csrf(self.server, jar)
            for target in ("/transactions", "/transactions/", "/transactions?month=2024-11&view=Young",
                           "/transactions;x", "/transactions/edit", "/transactions/sync", "/Transactions",
                           "/api/transactions", "//transactions"):
                with self.subTest(target=target):
                    response = request(self.server, target, jar=jar)
                    self.assertIn(response[0], (400, 404), target)
                    self.assertEqual(response[1]["Cache-Control"], "no-store")
                    self.assert_no_sentinel(response, target)
            for target, extra in (("/transactions/sync", {"include_pending": "1"}),
                                  ("/transactions/edit", {"row": "1", "action": "category",
                                                          "category": "Hacked", "month": "2024-11",
                                                          "view": "Household"}),
                                  ("/transactions", {})):
                with self.subTest(post=target):
                    response = request(self.server, target, dict(token=token, **extra), jar=jar, origin=ORIGIN)
                    self.assertEqual(response[0], 404, target)
                    self.assert_no_sentinel(response, "POST " + target)
        self.assertEqual(self.tx_state(), before, "a hosted request changed transaction data")
        self.assertEqual(self.provider.calls, 0, "hosted Transactions must never call the provider")

    def test_l23_hosted_shows_terms_and_schedules_but_never_observed_payments(self):
        """L23: account terms and amortization are balance data (shown); observed payments are transaction-derived."""
        from account_terms import save_terms
        with closing(sqlite3.connect(self.db)) as db, db:
            loan = db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                VALUES ('Fictional Bank','Fictional Loan','Personal loan','debt','USD','1000.00',date('now'),'Joint')""").lastrowid
            save_terms(db, loan, dict(rate="4.500", planned_payment="120.00"))
            db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES ('l23-src',?,0,0,'','F')", (loan,))
            db.execute("INSERT INTO transaction_coverage VALUES ('l23-src',?,'USD',0,9999999999,1)", (loan,))
            for month in range(1, 4):
                db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,description,
                    payee,merchant,pending,pending_active,is_transfer,transfer_manual)
                    VALUES (?,?,'l23-src',date('now','start of month',?),'3141.59','USD','Loan payment','','loan payment',0,0,1,1)""",
                           (loan, f"l23-{month}", f"-{month} months"))
        for jar in (self.a, self.b):
            for target in ("/projections", "/projections?view=Young&currency=USD"):
                response = request(self.server, target, jar=jar)
                self.assertEqual(response[0], 200, target)
                text = response[2].decode()
                self.assertIn("Fictional Loan", text)
                self.assertIn("4.5% APR", text)
                self.assertIn("(planned)", text)
                self.assertNotIn("3141", text)
                self.assertNotIn("observed", text.lower())
        # Terms are saved through the existing hosted edit route (session + exact Origin + CSRF).
        token = csrf(self.server, self.a)
        form = dict(token=token, view="Household", inline="1", nickname="Fictional Loan", institution="Fictional Bank",
                    kind="Personal loan", owner="Joint", side="debt", currency="USD", balance="1000.00",
                    asof="2026-11-01", update_method="Manual entry", terms="1", t_debt="1", t_rate="6.000",
                    t_kind="fixed", t_min="50.00", t_planned="150.00", t_promo_rate="", t_promo_end="")
        self.assertEqual(request(self.server, f"/edit/{loan}", form, jar=self.a, origin=ORIGIN)[0], 303)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT rate, planned_payment FROM account_terms WHERE account_id=?", (loan,)).fetchone(),
                             ("6.000", "150.00"))
        self.assertEqual(request(self.server, f"/edit/{loan}", form, jar=self.a)[0] in (401, 403, 405), True)  # no Origin

    def test_nav_has_no_transactions_link_on_any_page(self):
        for jar in (self.a, self.b):
            for target in ("/", "/?view=Young", "/projections", "/servicers", "/new", "/federal-preview"):
                html = request(self.server, target, jar=jar)[2].decode()
                self.assertIn('href="/projections"' if target != "/federal-preview" else "Federal", html, target)
                self.assertNotIn("/transactions", html, target)
                self.assertNotIn(">Transactions<", html, target)

    def test_NO_TRANSACTION_DATA_ON_ANY_HOSTED_ROUTE(self):
        """Sign in as each member and GET everything linked from the hosted nav, plus all projection views."""
        visited = 0
        for jar in (self.a, self.b):
            home = request(self.server, "/", jar=jar)[2].decode()
            nav = re.search(r'<nav class="sitenav".*?</nav>', home, re.S)[0]
            targets = {"/", "/?view=Household", "/?view=Young", "/?view=Nanami", "/servicers", "/new",
                       "/federal-preview", "/preview.js", "/dashboard.js", "/style.css", "/edit/1", "/remove/1",
                       "/new?servicer=uas", "/transactions", "/transactions?month=2024-11", *re.findall(r'href="(/[^"]*)"', nav)}
            for view in ("Household", "Young", "Nanami"):
                for currency in ("USD", "JPY", "EUR"):  # EUR: unavailable, must still not leak
                    targets.add(f"/projections?view={view}&currency={currency}")
                targets.add(f"/projections?view={view}")
            for target in sorted(targets):
                response = request(self.server, target, jar=jar)
                self.assertIn(response[0], (200, 400, 404), target)
                self.assert_no_sentinel(response, target)
                visited += response[0] == 200
            # Also the projection and logo caches reached by the same session.
            self.assert_no_sentinel(request(self.server, "/logo/chase.com", jar=jar), "/logo")
        self.assertGreaterEqual(visited, 30)
        self.assertEqual(self.provider.calls, 0)


@unittest.skipUnless(HAS_CRYPTO, "install requirements-hosted.txt for hosted acceptance")
class HostedProjections(unittest.TestCase):
    """L21: /projections POST needs session + exact Origin + CSRF; assumptions are household-shared."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.rsa = RSA(tmp.name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name).resolve() / "fictional.db"
        seed_fictional.seed(self.db)
        self.now = NOW
        ctx = running(self.db, self.rsa, lambda: self.now)
        self.server = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.a, self.b = {}, {}
        assert login(self.server, self.rsa, self.a, email="member.alpha@gmail.com")[0] == 204
        assert login(self.server, self.rsa, self.b, sub="200002", email=CONFIG.members[1],
                     hd="workspace.example")[0] == 204

    def saved(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT view, currency, assumptions FROM projection_assumptions ORDER BY 1, 2").fetchall()

    def token(self, jar):
        body = request(self.server, "/projections", jar=jar)[2]
        return re.search(rb'name="token" value="([^"]+)"', body)[1].decode()

    def post(self, jar, token, origin=ORIGIN, **fields):
        return request(self.server, "/projections", dict(PROJECTION_FORM, token=token, **fields),
                       jar=jar, origin=origin)

    def test_rejections_save_nothing_and_valid_post_is_shared(self):
        before = self.saved()
        self.assertEqual([row[:2] for row in before], [("Household", "USD")])
        token = self.token(self.a)
        # Missing CSRF token, wrong token, another session's token.
        for bad in ("", "wrong", self.token(self.b)):
            self.assertEqual(self.post(self.a, bad)[0], 403, bad)
        no_token = dict(PROJECTION_FORM)
        self.assertEqual(request(self.server, "/projections", no_token, jar=self.a, origin=ORIGIN)[0], 403)
        # Wrong, missing and duplicated Origin.
        for origin in ("https://unapproved.test", ORIGIN + "/", ORIGIN.replace("https", "http"), None):
            self.assertEqual(self.post(self.a, token, origin=origin)[0], 403, origin)
        # No session at all.
        self.assertEqual(self.post({}, token)[0], 401)
        self.assertEqual(self.saved(), before)
        # Expired session: the same cookie that worked a moment ago is refused and nothing is saved.
        self.now += SESSION_TTL
        for jar in (self.a, self.b):
            response = self.post(jar, token)
            self.assertEqual((response[0], response[1]["Cache-Control"]), (401, "no-store"))
        self.assertEqual(self.saved(), before)
        self.now = NOW
        # Valid session, token and Origin: saved. The second member sees the same assumptions.
        self.assertEqual(self.post(self.a, token)[0], 303)
        after = request(self.server, "/projections?view=Household&currency=USD", jar=self.b)[2].decode()
        self.assertRegex(after, r'name="income"[^>]*value="5000"')
        self.assertNotIn(seed_fictional.PROJECTION_INCOME, after)
        # Owner views are shared data too: Young's scenario saved by member A is visible to member B only
        # through the same table, never as a permission boundary.
        self.assertEqual(self.post(self.a, token, view="Young", income="777")[0], 303)
        young = request(self.server, "/projections?view=Young&currency=USD", jar=self.b)[2].decode()
        self.assertRegex(young, r'name="income"[^>]*value="777"')
        self.assertEqual([row[:2] for row in self.saved()], [("Household", "USD"), ("Young", "USD")])
        # Member B can overwrite (last writer wins); there is no per-member scenario.
        self.assertEqual(self.post(self.b, self.token(self.b), income="6000")[0], 303)
        again = request(self.server, "/projections?view=Household&currency=USD", jar=self.a)[2].decode()
        self.assertRegex(again, r'name="income"[^>]*value="6000"')

    def test_invalid_values_are_422_and_not_saved(self):
        before = self.saved()
        token = self.token(self.a)
        self.assertEqual(self.post(self.a, token, income="1,000")[0], 422)
        self.assertEqual(self.post(self.a, token, view="Mallory")[0], 422)
        self.assertEqual(self.post(self.a, token, currency="EUR")[0], 422)
        self.assertEqual(self.saved(), before)


class DeployUnits(unittest.TestCase):
    """Static checks on the shipped systemd units (they cannot be run on the build host)."""
    DEPLOY = Path(__file__).resolve().parent / "deploy"

    def exec_start(self, name):
        text = (self.DEPLOY / name).read_text()
        lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(lines), 1, name)
        return lines[0]

    def test_dashboard_unit_is_hosted_loopback_and_never_ships_bridge(self):
        line = self.exec_start("accounts-dashboard.service")
        for required in ("--port 8765", "--hosted", "--hosted-env %h/.config/planner-accounts/hosted.env",
                         "%h/.local/share/planner-accounts-venv/bin/python"):
            self.assertIn(required, line)
        # Bridge is enabled only by a drop-in at the Bridge-enrolment step (see the runbook): without
        # bridge.access the process exits and systemd would restart it every 10 seconds.
        self.assertNotIn("--bridge", line)
        self.assertNotIn("--db", line)
        text = (self.DEPLOY / "accounts-dashboard.service").read_text()
        self.assertIn("UMask=0077", text)
        self.assertIn("WantedBy=default.target", text)

    def test_backup_unit_uses_stdlib_python_and_a_private_umask(self):
        line = self.exec_start("accounts-backup.service")
        self.assertIn("backup-sqlite.sh backup", line)
        self.assertNotIn("--db", line)
        self.assertIn("UMask=0077", (self.DEPLOY / "accounts-backup.service").read_text())

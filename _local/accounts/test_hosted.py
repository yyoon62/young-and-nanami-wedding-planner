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

from demo_batch import Demo
from hosted_auth import Auth, CHALLENGE_TTL, SESSION_TTL, Config, GoogleKeys, Rejected, db_path, offline_lock
from server import serve
from test_history import BASE, DAY

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
            weak.touch(0o644)
            for candidate in (repo / "subdir" / "a", path / "linked" / "a", weak):
                with self.assertRaises(ValueError):
                    db_path(candidate)
            self.assertFalse((repo / "subdir").exists())

    def test_cli_guards_before_bridge_or_db_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "never-created.sqlite"
            cmd = [sys.executable, "server.py", "--hosted"]
            for extra, env, message in [
                (["--demo"], ENV, "--demo cannot be combined"),
                (["--bridge", "--db", str(db)], dict(ENV, GOOGLE_CLIENT_ID=""), "Hosted startup refused"),
                (["--db", str(Path.cwd() / "never-created.sqlite")], ENV, "outside Git"),
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

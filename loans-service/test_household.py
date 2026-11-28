"""Offline HTTP acceptance tests. The Google verifier is real; its cert transport is local."""
import base64
import json
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from auth import TTL
from main import app
from storage import Config, transaction

ORIGIN = "https://accounts.household.test"
A, B = "member-a@workspace.example.test", "member-b@workspace.example.test"
H = {"Origin": ORIGIN, "X-Dashboard-Action": "1"}
COL = "/api/accounts"
BODY = {"name": "<img src=x onerror=alert(1)>", "amount": "999999999999.99",
        "currency": "CAD", "as_of": "2024-02-29"}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class Household(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = cls.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.certs = SimpleNamespace(status=200, data=json.dumps({"fiction": pub.decode()}).encode())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = dict(GOOGLE_CLIENT_ID="offline-client", LOANS_MEMBERS=f"{A},{B}", LOANS_ORIGIN=ORIGIN,
                   LOANS_SESSION_KEY=secrets.token_hex(32), LOANS_DB=str(Path(tmp.name) / "manual.db"))
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch("auth.GoogleRequest", return_value=lambda *a, **kw: self.certs))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN))
        self.cfg = app.state.cfg

    def token(self, who=A, **overrides):
        now = int(time.time())
        payload = dict(iss="https://accounts.google.com", aud=self.cfg.client, iat=now-1, exp=now+300,
                       email=who, hd="workspace.example.test", email_verified=True, sub=f"sub-{who}",
                       nonce=uuid4().hex) | overrides
        msg = b".".join(b64(json.dumps(v).encode()) for v in ({"alg": "RS256", "kid": "fiction"}, payload))
        return (msg + b"." + b64(self.key.sign(msg, padding.PKCS1v15(), hashes.SHA256()))).decode()

    def req(self, method, path, cookie=None, headers=None, **kwargs):
        # Never accidentally inherit A's cookie when making B's request.
        self.client.cookies.clear()
        hdr = dict(H if headers is None else headers)
        if cookie:
            hdr["Cookie"] = f"{self.cfg.cookie}={cookie}"
        r = self.client.request(method, path, headers=hdr, **kwargs)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        return r

    def login(self, who=A):
        r = self.req("POST", "/api/login", json={"credential": self.token(who)})
        self.assertEqual(r.status_code, 204, r.text)
        for flag in ("__Host-loans=", "HttpOnly", "Secure", "SameSite=lax", "Path=/", f"Max-Age={TTL}"):
            self.assertIn(flag, r.headers["set-cookie"])
        self.assertNotIn("Domain=", r.headers["set-cookie"])
        return r.cookies[self.cfg.cookie]

    def create(self, cookie):
        r = self.req("POST", COL, cookie, json=BODY | {"create_key": str(uuid4())})
        self.assertEqual(r.status_code, 201, r.text)
        return COL + "/" + r.json()["id"]

    def test_signed_google_credentials(self):
        signed = self.token()
        msg, sig = signed.rsplit(".", 1)
        bad_sig = msg + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
        for invalid in (bad_sig, "not-a-token", self.token(iss="untrusted"), self.token(aud="wrong"),
                        self.token(exp=1), self.token(iat=int(time.time()) + 300)):
            self.assertEqual(self.req("POST", "/api/login", json={"credential": invalid}).status_code, 401)
        for kwargs in ({"email_verified": "true"}, {"email_verified": False}, {"hd": ""},
                       {"sub": ""}, {"sub": None}, {"email": "stranger@example.test"}):
            self.assertEqual(self.req("POST", "/api/login", json={"credential": self.token(**kwargs)}).status_code, 403)
        self.login()
        for token in (self.token(sub="reassigned"), self.token(B, sub=f"sub-{A}")):
            self.assertEqual(self.req("POST", "/api/login", json={"credential": token}).status_code, 403)

    def test_cookie_revocation_tamper_and_expiration(self):
        token = self.token()
        r = self.req("POST", "/api/login", json={"credential": token})
        cookie = r.cookies[self.cfg.cookie]
        for invalid in (None, cookie[1:], cookie + "x"):
            self.assertEqual(self.req("GET", COL, invalid).status_code, 401)
        with patch("time.time", return_value=time.time() + TTL + 1):
            self.assertEqual(self.req("GET", COL, cookie).status_code, 401)
        logout = self.req("POST", "/api/logout", cookie)
        self.assertEqual(logout.status_code, 204)
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])
        self.assertEqual(self.req("GET", COL, cookie).status_code, 401)
        self.assertEqual(self.req("POST", "/api/login", json={"credential": token}).status_code, 401)
        cookie = self.login()
        with transaction(self.cfg, True) as db:
            db.execute("UPDATE sessions SET expires=0")
        self.assertEqual(self.req("GET", COL, cookie).status_code, 401)

    def test_every_write_requires_exact_origin_and_header(self):
        a = self.login()
        path = self.create(a)
        for method, target in (("POST", "/api/login"), ("POST", "/api/logout"), ("POST", COL),
                               ("PUT", path), ("DELETE", path), ("PUT", path+"/share"), ("DELETE", path+"/share")):
            for headers in ({}, {"Origin": ORIGIN}, {"X-Dashboard-Action": "1"},
                            H | {"Origin": "null"}, H | {"Origin": "https://sibling.household.test",
                                "X-Forwarded-Host": "sibling.household.test", "X-Forwarded-Proto": "http"}):
                with self.subTest(method=method, target=target, headers=headers):
                    self.assertEqual(self.req(method, target, a, headers=headers).status_code, 403)
        self.assertFalse(self.req("GET", path, a).json()["shared"])

    def test_account_lifecycle_and_guessed_id(self):
        a, b = self.login(), self.login(B)
        path = self.create(a)
        share = path+"/share"
        for shared in (False, True, False):
            if shared:
                self.assertEqual(self.req("PUT", share, a).status_code, 204)
            listing = self.req("GET", COL, b).json()["accounts"]
            self.assertEqual(len(listing), int(shared))
            detail = self.req("GET", path, b)
            self.assertEqual(detail.status_code, 200 if shared else 404)
            if shared:
                self.assertFalse(detail.json()["mine"])
                for key in BODY:
                    self.assertEqual(detail.json()[key], BODY[key])
            for method, target in (("PUT", path), ("DELETE", path), ("PUT", share), ("DELETE", share)):
                self.assertEqual(self.req(method, target, b, json=BODY).status_code, 404)
            if shared:
                self.assertEqual(self.req("DELETE", share, a).status_code, 204)
        self.assertEqual(self.req("PUT", path, a, json=BODY | {"amount": "-0.01"}).status_code, 204)
        self.assertEqual(self.req("GET", path, a).json()["amount"], "-0.01")
        self.assertEqual(self.req("DELETE", path, a).status_code, 204)
        self.assertEqual(self.req("GET", path, a).status_code, 404)

    def test_data_validation_and_duplicate_submission(self):
        a = self.login()
        body = BODY | {"create_key": str(uuid4())}
        for change in ({"owner": f"sub-{B}"}, {"shared": True}, {"recipient": B}, {"as_of": "2023-02-29"},
                       {"as_of": "2999-01-01"}, {"amount": "0.009"}, {"amount": 0.01}, {"currency": "JPY"}):
            self.assertEqual(self.req("POST", COL, a, json=body | change).status_code, 422)
        self.assertEqual(self.req("POST", COL, a, json=body).status_code, 201)
        self.assertEqual(self.req("POST", COL, a, json=body).status_code, 409)
        self.assertEqual(self.req("POST", COL, a, json=body | {"currency": "EUR", "create_key": str(uuid4())}).status_code, 201)
        rows = self.req("GET", COL, a).json()["accounts"]
        self.assertEqual(sorted((r["currency"], r["amount"]) for r in rows),
                         [("CAD", BODY["amount"]), ("EUR", BODY["amount"])])

    def test_restart_and_membership_change(self):
        a, b = self.login(), self.login(B)
        path = self.create(a)
        self.req("PUT", path+"/share", a)
        with TestClient(app, base_url=ORIGIN) as restarted:
            self.client = restarted
            self.assertTrue(self.req("GET", path, a).json()["mine"])
            self.assertEqual(self.req("GET", path, b).json()["name"], BODY["name"])
        new = "replacement@workspace.example.test"
        with patch.dict(os.environ, LOANS_MEMBERS=f"{A},{new}"), TestClient(app, base_url=ORIGIN) as restarted:
            self.client = restarted
            self.assertEqual(self.req("GET", COL, b).status_code, 401)
            self.assertEqual(self.req("GET", path, self.login(new)).status_code, 404)
            self.assertFalse(self.req("GET", path, a).json()["shared"])
        with TestClient(app, base_url=ORIGIN) as restarted:
            self.client = restarted
            self.assertEqual(self.req("GET", COL, b).status_code, 401)

    def test_invalid_configuration_refuses_to_start(self):
        for key in ("GOOGLE_CLIENT_ID", "LOANS_MEMBERS", "LOANS_ORIGIN", "LOANS_SESSION_KEY", "LOANS_DB"):
            with patch.dict(os.environ, {key: ""}), self.assertRaises(ValueError):
                Config.load()
        for change in ({"LOANS_MEMBERS": f"{A},{A.upper()}"}, {"LOANS_MEMBERS": f"{A},{B},"},
                       {"LOANS_MEMBERS": "a.b+fixture@gmail.com,ab@gmail.com"},
                       {"LOANS_MEMBERS": "not-mail,"+B}, {"LOANS_ORIGIN": "http://accounts.example.test"},
                       {"LOANS_DB": str(Path(__file__).resolve().with_name("must-not-create.db"))}):
            with patch.dict(os.environ, change), self.assertRaises(ValueError):
                Config.load()

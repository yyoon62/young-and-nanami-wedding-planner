"""Opt-in household authentication. Standard library only; no Google client secret."""
import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
from http.client import HTTPSConnection, HTTPException
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

SESSION_TTL = 3600
CHALLENGE_TTL = 300
GOOGLE_HOST = "www.googleapis.com"
GOOGLE_KEYS = "/oauth2/v3/certs"
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN = rf"{LABEL}(?:\.{LABEL})+"
RANDOM = r"[A-Za-z0-9_-]{43}"
AUTH_TABLES = ("hosted_sessions", "hosted_challenges")


class Rejected(Exception):
    def __init__(self, status=401):
        self.status = status


def external(path):
    path = Path(os.path.abspath(path))
    if path.resolve() != path or any(
        os.path.lexists(p / ".git") or ((p / "HEAD").is_file() and (p / "objects").is_dir())
        for p in path.parents
    ):
        raise ValueError("Use a path outside Git, without symlinks.")
    return path


def private(path, directory=False):
    s = path.lstat()
    check = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not check(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != mode
            or (not directory and s.st_nlink != 1)):
        raise ValueError(f"State must be owned by the service user and mode {mode:o}.")


def db_path(path):
    path = external(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(path.parent, directory=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            private(candidate)
    return path


@contextmanager
def state_lock(path, purpose, busy):
    """Advisory lock inside private state; do not unlink lockfiles."""
    import fcntl

    lock = path.with_name(path.name + f".{purpose}-lock")
    fd = os.open(lock, os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR, 0o600)
    try:
        private(lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(busy) from None
        yield
    finally:
        os.close(fd)


def offline_lock(path):
    return state_lock(path, "hosted", "Stop the hosted dashboard before replacing its database.")


def primary(email):
    if not re.fullmatch(rf"[a-z0-9_%+-]+(?:\.[a-z0-9_%+-]+)*@{DOMAIN}", email):
        return False
    name, domain = email.split("@")
    if domain in {"gmail.com", "googlemail.com"}:
        return domain == "gmail.com" and re.fullmatch(r"[a-z0-9]+", name) is not None
    return "+" not in name


@dataclass(frozen=True)
class Config:
    client: str
    members: tuple
    origin: str
    key: bytes = field(repr=False)

    @classmethod
    def load(cls, env):
        client = env.get("GOOGLE_CLIENT_ID", "")
        members = tuple(v.strip().lower() for v in env.get("LOANS_MEMBERS", "").split(","))
        origin = env.get("LOANS_ORIGIN", "")
        key = env.get("LOANS_SESSION_KEY", "")
        if not re.fullmatch(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", client):
            raise ValueError("Set GOOGLE_CLIENT_ID to a Google Web OAuth client ID.")
        if len(members) != 2 or len(set(members)) != 2 or not all(map(primary, members)):
            raise ValueError("Set LOANS_MEMBERS to two distinct primary addresses (Gmail without dots or aliases).")
        if not re.fullmatch(rf"https?://(?:{DOMAIN}|localhost|\[::1\])(?::[1-9][0-9]{{0,4}})?", origin):
            raise ValueError("Set LOANS_ORIGIN to the exact browser origin, without a trailing slash.")
        url = urlsplit(origin)
        host = url.hostname
        if len(host) > 253:
            raise ValueError("Origin hostname is too long.")
        if host != "::1" and re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.split(".")[-1]):
            # Browsers canonicalize IPv4 (including nondecimal spellings) in Origin.
            if str(IPv4Address(host)) != host:
                raise ValueError("Use a canonical origin hostname.")
        if (url.port == (443 if url.scheme == "https" else 80)
                or (url.scheme == "http" and host not in {"localhost", "127.0.0.1"})):
            raise ValueError("Use HTTPS (HTTP only on loopback); omit the default port.")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError("Set LOANS_SESSION_KEY to 64 hex digits from a secure random source.")
        return cls(client, members, origin, bytes.fromhex(key))

    @property
    def secure(self):
        return self.origin.startswith("https:")

    def name(self, kind):
        return ("__Host-" if self.secure else "") + "accounts-" + kind

    def cookie(self, kind, value="", age=0):
        return (f"{self.name(kind)}={value}; Max-Age={age}; Path=/; HttpOnly; SameSite=Lax"
                + ("; Secure" if self.secure else ""))


def decode(segment):
    """Accept only canonical, unpadded base64url."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", segment):
        raise ValueError()
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != segment:
        raise ValueError()
    return raw


def unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate JSON key.")
    return obj


class GoogleKeys:
    """Use Google's fixed HTTPS JWKS endpoint; token headers never select a URL."""
    def __init__(self):
        self.lock = threading.Lock()
        self.keys = {}
        self.until = self.last = float("-inf")

    def __call__(self, kid):
        with self.lock:
            now = time.monotonic()
            if now < self.until and kid in self.keys:
                return self.keys[kid]
            # Unknown kid may indicate rotation; throttle all refreshes (including failures).
            if now - self.last >= 30:
                self.last = now
                conn = HTTPSConnection(GOOGLE_HOST, timeout=5)
                try:
                    conn.request("GET", GOOGLE_KEYS)
                    res = conn.getresponse()
                    raw = res.read(65537)
                    if res.status != 200 or len(raw) > 65536:
                        raise ValueError()
                    keys = json.loads(raw, object_pairs_hook=unique)["keys"]
                    self.keys = {k["kid"]: k for k in keys
                                 if k.get("kty") == "RSA" and k.get("alg") == "RS256" and k.get("use") == "sig"}
                    age = re.search(r"(?:^|,\s*)max-age=([0-9]{1,8})(?:,|$)",
                                    res.getheader("Cache-Control", ""))
                    self.until = now + (min(int(age[1]), 3600) if age else 300)
                    return self.keys.get(kid)
                except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
                    raise Rejected(503) from None
                finally:
                    conn.close()
            if now >= self.until:
                raise Rejected(503)
            return self.keys.get(kid)


def verify_signature(jwk, signing_input, signature):
    if not jwk:
        return False
    n, e = (int.from_bytes(decode(jwk[k]), "big") for k in ("n", "e"))
    bits = n.bit_length()
    width = (bits + 7) // 8
    s = int.from_bytes(signature, "big")
    if not (2048 <= bits <= 4096 and n % 2 and e == 65537 and len(signature) == width and s < n):
        return False
    # Compare the ENTIRE EMSA-PKCS1-v1_5 block for SHA-256, not a hash prefix or suffix.
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(pow(s, e, n).to_bytes(width, "big"), expected)


def verify(token, config, nonce, keys, now):
    try:
        head, body, sig = token.split(".")
        header = json.loads(decode(head), object_pairs_hook=unique)
        kid = header.get("kid")
        if not (header.get("alg") == "RS256" and "crit" not in header
                and isinstance(kid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", kid)):
            raise ValueError()
        if not verify_signature(keys(kid), f"{head}.{body}".encode("ascii"), decode(sig)):
            raise ValueError()
        c = json.loads(decode(body), object_pairs_hook=unique)
        nbf = c.get("nbf", c["iat"])
        if not (type(c["exp"]) is type(c["iat"]) is type(nbf) is int and max(c["iat"], nbf) <= now + 30
                and now < c["exp"] <= c["iat"] + SESSION_TTL + 30
                and c["iss"] in ("accounts.google.com", "https://accounts.google.com")
                and c["aud"] == config.client and c.get("azp", config.client) == config.client
                and c["nonce"] == nonce):
            raise ValueError()
        sub, email = c["sub"], c["email"]
        if not isinstance(sub, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", sub):
            raise ValueError()
        if not isinstance(email, str):
            raise ValueError()
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        raise Rejected() from None
    email = email.lower()
    if re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)*@gmail\.com", email):
        # Google can return a dotted presentation for the same Gmail mailbox.
        email = email.split("@")[0].replace(".", "") + "@gmail.com"
    hd = c.get("hd")
    if (c.get("email_verified") is not True or email not in config.members
            or not (email.endswith("@gmail.com") or isinstance(hd, str) and re.fullmatch(DOMAIN, hd))):
        raise Rejected(403)
    return email, sub, c["exp"]


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class Auth:
    def __init__(self, path, config, keys=None, clock=time.time):
        self.path, self.config, self.clock = path, config, clock
        self.keys = keys if keys is not None else GoogleKeys()
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hosted_identities (
                email TEXT PRIMARY KEY NOT NULL, sub TEXT UNIQUE NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS hosted_sessions (
                sid TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES hosted_identities(email),
                csrf TEXT NOT NULL, expires INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS hosted_challenges (
                browser TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL)""")
            db.execute("DELETE FROM hosted_sessions WHERE email NOT IN (?,?)", config.members)

    @contextmanager
    def db(self, write=True):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def challenge(self, prior=""):
        now = int(self.clock())
        browser, nonce = (secrets.token_urlsafe(32) for _ in range(2))
        with self.db() as db:
            db.execute("DELETE FROM hosted_challenges WHERE expires <= ?", (now,))
            if re.fullmatch(RANDOM, prior):
                db.execute("DELETE FROM hosted_challenges WHERE browser=?", (digest(prior),))
            db.execute("INSERT INTO hosted_challenges VALUES (?,?,?)",
                       (digest(browser), nonce, now + CHALLENGE_TTL))
        return browser, nonce

    def signature(self, sid):
        return hmac.new(self.config.key, b"accounts-session-v1:" + sid.encode("ascii"), "sha256").hexdigest()

    def exchange(self, browser, credential, old_cookie=""):
        if not re.fullmatch(RANDOM, browser) or not 1 <= len(credential) <= 8192:
            raise Rejected()
        now = int(self.clock())
        key = digest(browser)
        with self.db(write=False) as db:
            row = db.execute("SELECT nonce FROM hosted_challenges WHERE browser=? AND expires>?",
                             (key, now)).fetchone()
        if not row:
            raise Rejected()
        # No network I/O in a SQLite write transaction. Re-check expiry and consume atomically.
        email, sub, exp = verify(credential, self.config, row[0], self.keys, now)
        now = int(self.clock())
        if now >= exp:
            raise Rejected()
        sid, csrf = (secrets.token_urlsafe(32) for _ in range(2))
        old_sid = self.cookie_sid(old_cookie)
        with self.db() as db:
            if not db.execute("DELETE FROM hosted_challenges WHERE browser=? AND expires>?", (key, now)).rowcount:
                raise Rejected()
            db.execute("INSERT OR IGNORE INTO hosted_identities VALUES (?,?)", (email, sub))
            if not db.execute("SELECT 1 FROM hosted_identities WHERE email=? AND sub=?", (email, sub)).fetchone():
                raise Rejected(403)
            db.execute("DELETE FROM hosted_sessions WHERE expires<=? OR sid=?", (now, old_sid))
            db.execute("INSERT INTO hosted_sessions VALUES (?,?,?,?)",
                       (digest(sid), email, csrf, now + SESSION_TTL))
        return sid + "." + self.signature(sid)

    def cookie_sid(self, cookie):
        if re.fullmatch(RANDOM + r"\.[0-9a-f]{64}", cookie):
            sid, mac = cookie.split(".")
            if hmac.compare_digest(mac, self.signature(sid)):
                return digest(sid)
        return None

    def session(self, cookie):
        key = self.cookie_sid(cookie)
        if not key:
            return None
        with self.db(write=False) as db:
            return db.execute("""SELECT sid, csrf FROM hosted_sessions
                WHERE sid=? AND expires>? AND email IN (?,?)""",
                (key, int(self.clock()), *self.config.members)).fetchone()

    def revoke(self, key, challenge=""):
        with self.db() as db:
            db.execute("DELETE FROM hosted_sessions WHERE sid=?", (key,))
            if re.fullmatch(RANDOM, challenge):
                db.execute("DELETE FROM hosted_challenges WHERE browser=?", (digest(challenge),))

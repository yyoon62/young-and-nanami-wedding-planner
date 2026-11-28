"""Single-instance household storage; all callers get short-lived transactions."""
import os
import re
import sqlite3
from contextlib import contextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    client: str
    members: frozenset[str]
    origin: str
    secret: bytes
    db: Path

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def cookie(self):
        # __Host- prevents a sibling domain from planting our HTTPS cookie.
        return "__Host-loans" if self.secure else "loans-local"

    @classmethod
    def load(cls):
        client = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        raw = [v.strip().lower() for v in os.environ.get("LOANS_MEMBERS", "").split(",")]
        if not client or len(raw) != 2 or len(set(raw)) != 2 or not all(
            re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", v) for v in raw
        ):
            raise ValueError("Configure a client and exactly two distinct member addresses.")
        def mailbox(email):
            local, domain = email.split("@")
            if domain in {"gmail.com", "googlemail.com"}:
                return local.partition("+")[0].replace(".", "") + "@gmail.com"
            return email
        if mailbox(raw[0]) == mailbox(raw[1]):
            raise ValueError("Gmail aliases are not distinct members.")
        origin = os.environ.get("LOANS_ORIGIN", "")
        url = urlsplit(origin)
        if not (re.fullmatch(r"https?://[a-z0-9.-]+(?::[0-9]+)?", origin)
                and url.hostname and url.port != 0 and
                (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1"})):
            raise ValueError("Configure the exact external HTTPS origin (HTTP only on loopback).")
        secret = os.environ.get("LOANS_SESSION_KEY", "")
        if not re.fullmatch("[a-fA-F0-9]{64}", secret):
            raise ValueError("Configure a random 32-byte hex session key.")
        path = Path(os.environ.get("LOANS_DB", ""))
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Configure an absolute external SQLite path.")
        path = path.resolve()
        if any((p / ".git").exists() for p in path.parents):
            raise ValueError("The database must be outside Git.")
        return cls(client, frozenset(raw), origin, bytes.fromhex(secret), path)


@contextmanager
def transaction(cfg, write=False):
    with closing(sqlite3.connect(cfg.db, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with db:
            yield db


def initialize(cfg):
    cfg.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cfg.db.parent.stat().st_mode & 0o077:
        raise ValueError("Use a private (0700) database directory.")
    os.close(os.open(cfg.db, os.O_CREAT | os.O_RDWR, 0o600))
    with transaction(cfg, True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS identities(sub TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            digest TEXT PRIMARY KEY, sub TEXT NOT NULL REFERENCES identities, expires INTEGER NOT NULL)""")
        db.execute("CREATE TABLE IF NOT EXISTS exchanges(digest TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS accounts(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES identities, recipient TEXT,
            name TEXT NOT NULL, cents INTEGER NOT NULL, currency TEXT NOT NULL, as_of TEXT NOT NULL,
            create_key TEXT NOT NULL, UNIQUE(owner,create_key))""")
        db.execute("""DELETE FROM sessions WHERE sub NOT IN
            (SELECT sub FROM identities WHERE email IN (?,?))""", tuple(cfg.members))
        db.execute("""UPDATE accounts SET recipient=NULL WHERE recipient NOT IN (?,?) OR owner NOT IN
            (SELECT sub FROM identities WHERE email IN (?,?))""", (*cfg.members, *cfg.members))

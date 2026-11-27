"""Opt-in SimpleFIN Bridge transport. No credentials in the HTTP dashboard."""
import base64
import getpass
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import unquote
import warnings


HOST = r"(?:beta-)?bridge\.simplefin\.org"
CLAIM = re.compile(rf"https://({HOST})(/simplefin/claim/[A-Za-z0-9._~-]+)")
ACCESS = re.compile(rf"https://([A-Za-z0-9._~%+-]+):([A-Za-z0-9._~%+-]+)@({HOST})"
                    r"(/simplefin(?:/[A-Za-z0-9._~-]+)*)/?")
STORE = Path.home() / ".local/share/planner-accounts/bridge.access"


class Problem(Exception):
    """Only locally authored, safe-to-display messages."""


def unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Problem("Ambiguous provider response (duplicate field); balances held.")
    return result


def wire(method, host, path, headers):
    conn = HTTPSConnection(host, timeout=20)  # default certificate checks; no redirects or proxies
    try:
        conn.request(method, path, headers=headers)
        res = conn.getresponse()
        limit = 4096 if method == "POST" else 2_000_000
        body = res.read(limit + 1) if res.status == 200 else b""
        if len(body) > limit:
            raise Problem("Bridge response too large.")
        return res.status, body
    finally:
        conn.close()


def credentials(url):
    m = ACCESS.fullmatch(url)
    if not m or len(url) > 4096 or any(part in {".", ".."} for part in m[4].split("/")):
        raise Problem("Unrecognized Bridge access URL; no account request made.")
    auth = base64.b64encode((unquote(m[1]) + ":" + unquote(m[2])).encode()).decode()
    return m[3], m[4], {"Authorization": "Basic " + auth}, {m[2], unquote(m[2]), auth}


def claim(token, transport):
    try:
        m = CLAIM.fullmatch(base64.b64decode(token, validate=True).decode("ascii"))
        if not m:
            raise ValueError()
    except ValueError:
        raise Problem("Invalid Bridge setup token; no request made.") from None
    try:
        status, body = transport("POST", m[1], m[2], {"Content-Length": "0"})
        if status != 200:
            raise ValueError()
        url = body.decode("ascii").strip()
        credentials(url)
        return url
    except Exception:
        raise Problem("Claim failed. Token may be used or compromised; disable it at Bridge before retrying.") from None


def private_parent(path):
    p = path.parent
    if p.resolve() != p.absolute() or any((a / ".git").exists() for a in (p, *p.parents)):
        raise Problem("Bridge storage must be outside Git, without symlinks.")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.stat().st_uid != os.getuid() or stat.S_IMODE(p.stat().st_mode) != 0o700:
        raise Problem("Bridge storage directory must be owned by you and mode 0700.")


def enroll(path=STORE, transport=wire):
    private_parent(path)
    # Reserve a private file before spending the one-time token. Never replace a credential.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f, warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)  # fail instead of echoing
            f.write(claim(getpass.getpass("Bridge setup token (hidden): "), transport))
    except BaseException:
        path.unlink()
        raise


class Bridge:
    label = "SimpleFIN Bridge"

    def __init__(self, path=STORE, transport=wire):
        private_parent(path)
        with os.fdopen(os.open(path, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK)) as f:
            s = os.fstat(f.fileno())
            if (not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o600
                    or s.st_nlink != 1 or s.st_uid != os.getuid()):
                raise Problem("Bridge credential must be a private regular file (0600).")
            url = f.read(4097)
            self.host, self.root, self.headers, self.private_strings = credentials(url)
            self.private_paths = set(self.root.split("/")[2:])
        self.namespace = "bridge:" + self.host
        if self.private_paths:
            self.namespace += ":" + hashlib.sha256(self.root.encode()).hexdigest()
        self.transport = transport

    def fetch(self):
        try:
            status, body = self.transport("GET", self.host, self.root + "/accounts?version=2&balances-only=1",
                                          self.headers)
            if status != 200:
                raise Problem({402: "Bridge reports payment required.", 403: "Bridge access denied or revoked."}
                              .get(status, "Bridge request failed; redirects are not followed."))
            data = json.loads(body, object_pairs_hook=unique_fields)
            # Only names and currency/signed balance strings may leave the parser for the UI.
            # Fail closed if the service echoes auth material in one of those display fields.
            if isinstance(data, dict):
                for group in ("connections", "accounts"):
                    for row in data.get(group, []):
                        if isinstance(row, dict) and any(
                            secret in value for field in ("name", "currency", "balance")
                            if isinstance(value := row.get(field), str)
                            for secret in (self.private_strings | self.private_paths if field == "name"
                                           else self.private_strings)
                        ):
                            raise Problem("Unexpected sensitive provider field; balances held.")
            return data
        except Problem:
            raise
        except Exception:
            raise Problem("Bridge unavailable or response unreadable.") from None


if __name__ == "__main__":
    if sys.argv[1:] != ["setup"]:
        sys.exit("Usage: python3 bridge.py setup (never pass a token as an argument)")
    try:
        enroll()
    except Problem as exc:
        sys.exit(str(exc))
    except Exception:
        sys.exit("Setup not completed. Check terminal and private storage; an existing file is never replaced.")
    print("Credential saved locally. No accounts fetched.")

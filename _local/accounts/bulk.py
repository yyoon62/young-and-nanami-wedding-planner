"""Reviewed source identity -> local account. No name-based inference or implicit inserts."""
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import re
import secrets
import sqlite3
from threading import Lock
import time

from bridge import Problem


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS source_links (
        source TEXT PRIMARY KEY, local INTEGER NOT NULL UNIQUE, stamp INTEGER NOT NULL,
        sign INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
        checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M UTC','now')))""")
    db.execute("CREATE TABLE IF NOT EXISTS fetch_attempts (at REAL NOT NULL)")


def fingerprint(*v):
    return hashlib.sha256(json.dumps(v).encode()).hexdigest()


def snapshot(row):
    return fingerprint(*(row[k] for k in ("balance", "asof", "currency", "side")))


def text(v, limit):
    if not isinstance(v, str) or not 0 < len(v) <= limit or not v.strip() or not v.isprintable():
        raise ValueError()
    return v


@dataclass
class Entry:
    key: str
    values: dict
    signed: str
    stamp: int

    @property
    def sign(self):
        n = Decimal(self.signed)
        return (n > 0) - (n < 0)


def unpack(payload, namespace, validate):
    if not isinstance(payload, dict) or not all(isinstance(payload.get(k), list)
            for k in ("connections", "accounts", "errlist")):
        raise Problem("Not a v2 account set. Nothing applied.")
    if len(payload["accounts"]) > 200:
        raise Problem("Batch exceeds the local review limit (200).")
    entries, seen, warnings, blocked = {}, set(), [], []
    connections = {}
    try:
        for c in payload["connections"]:
            cid = text(c["conn_id"], 256)
            if cid in connections:
                raise ValueError()
            connections[cid] = text(c["name"], 100)
    except (ValueError, KeyError, TypeError):
        raise Problem("Invalid or duplicate connection identity.") from None
    if payload.get("errors"):
        blocked.append((None, None))
        warnings.append("Bridge reported a general warning or quota issue; batch held.")
    reasons = {"con.auth": "Institution sign-in required", "act.failed": "Account lookup failed",
               "act.missingdata": "Incomplete account data", "gen.auth": "Bridge sign-in required",
               "gen.api": "Provider API issue"}
    for err in payload["errlist"]:
        # Never render provider text, URLs, IDs or credentials from diagnostics.
        err = err if isinstance(err, dict) else {}
        code = err.get("code")
        cid, aid = err.get("conn_id"), err.get("account_id")
        if not isinstance(cid, str) or not cid:
            cid = None
        if not isinstance(aid, str) or not aid:
            aid = None
        blocked.append((cid, aid))
        warnings.append(reasons.get(code if isinstance(code, str) else "", "Provider reported an error")
                        + "; affected balances held.")
    for i, a in enumerate(payload["accounts"], 1):
        try:
            cid, aid = text(a["conn_id"], 256), text(a["id"], 256)
            key = fingerprint(namespace, cid, aid)
            if key in seen:
                entries.pop(key, None)
                raise ValueError()
            seen.add(key)
            if any(c in (None, cid) and k in (None, aid) for c, k in blocked):
                continue
            stamp, signed = a["balance-date"], text(a["balance"], 32)
            if type(stamp) != int or not 0 < stamp <= time.time():
                raise ValueError()
            if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", signed):
                raise ValueError()
            magnitude = signed.removeprefix("-")
            if "." in magnitude and magnitude.partition(".")[2].isdigit():
                magnitude = magnitude.rstrip("0").rstrip(".")  # exact; never round to fit
            v = validate(dict(institution=connections[cid], nickname=text(a["name"], 100),
                              kind="Linked account", side="asset", owner="Unspecified",
                              currency=text(a["currency"], 3), balance=magnitude,
                              asof=str(date.fromtimestamp(stamp)), update_method="Batch refresh"), date.today())
            entries[key] = Entry(key, v, signed, stamp)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            warnings.append(f"Provider row {i}: invalid, unsupported or duplicate; held.")
    return entries, warnings


def inventory(db):
    return {str(r["id"]): r for r in db.execute("SELECT * FROM accounts ORDER BY id")}


def mappings(db):
    return {r["source"]: r for r in db.execute("SELECT * FROM source_links ORDER BY local")}


class Batch:
    def __init__(self, path, provider, validate):
        self.path, self.provider, self.validate = path, provider, validate
        self.lock = Lock()
        self.review = None

    def fetch(self, db):
        # Commit the attempt BEFORE I/O, including failures, across restarts/processes.
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fetch_attempts WHERE at <= ?", (time.time() - 86400,))
            if db.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0] >= 24:
                raise Problem("Daily guard: 24 attempts in the past 24 hours. Try later.")
            db.execute("INSERT INTO fetch_attempts VALUES (?)", (time.time(),))
        return unpack(self.provider.fetch(), self.provider.namespace, self.validate)

    def run(self, action, data):
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            if action == "discover":
                self.review = None
                entries, warnings = self.fetch(db)
                rows, links = inventory(db), mappings(db)
                self.review = (secrets.token_urlsafe(16), list(entries.values()), rows, links, warnings)
            elif action == "apply":
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self.apply(db, data)
                self.review = None
            elif action == "refresh":
                selected = [k for k, l in mappings(db).items() if data.get("r"+str(l["local"]))]
                if not selected:
                    raise Problem("Select at least one linked account.")
                self.review = None
                try:
                    entries, warnings = self.fetch(db)
                except Problem as exc:
                    with db:
                        db.executemany("""UPDATE source_links SET status=?,
                            checked=strftime('%Y-%m-%d %H:%M UTC','now') WHERE source=?""",
                                       [(str(exc), key) for key in selected])
                    raise
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    links, rows = mappings(db), inventory(db)
                    for key in selected:
                        l, e = links[key], entries.get(key)
                        row = rows.get(str(l["local"]))
                        status = refresh_status(row, l, e)
                        if status == "Updated":
                            save(db, row["id"], e, l["sign"] or e.sign)
                        else:
                            db.execute("""UPDATE source_links SET status=?, checked=strftime('%Y-%m-%d %H:%M UTC','now')
                                WHERE source=?""", (status, key))
                return warnings
            return []

    def apply(self, db, data):
        if not self.review or data.get("review") != self.review[0]:
            raise Problem("Review expired. Discover again.")
        _, entries, old_rows, old_links, _ = self.review
        rows, links = inventory(db), mappings(db)
        if rows != old_rows or links != old_links:
            raise Problem("Inventory changed since review. Discover again.")
        selected = [(i, e) for i, e in enumerate(entries) if data.get(f"s{i}")]
        if not selected:
            raise Problem("Select at least one account.")
        for i, e in selected:
            target, side = data.get(f"m{i}"), data.get(f"d{i}")
            link = links.get(e.key)
            if link and target != str(link["local"]):
                raise Problem("A linked identity cannot create or switch accounts.")
            if side not in ("debt", "asset"):
                raise Problem("Confirm asset or debt for each selected signed balance.")
            if target == "new":
                v = dict(e.values, side=side, owner=data.get(f"o{i}") or data.get("owner", ""))
                try:
                    v = self.validate(v, date.today())
                except ValueError:
                    raise Problem("Choose a valid default owner or override.") from None
                local = db.execute(f"INSERT INTO accounts ({','.join(v)}) VALUES ({','.join('?' for _ in v)})",
                                   tuple(v.values())).lastrowid
            else:
                row = rows.get(target)
                if not row or (row["side"], row["currency"]) != (side, e.values["currency"]):
                    raise Problem("Choose create or a compatible existing account (same currency and side).")
                local = row["id"]
                if not link and db.execute("SELECT 1 FROM source_links WHERE local=?", (local,)).fetchone():
                    raise Problem("Two sources cannot match the same account.")
                if row["asof"] > e.values["asof"] or (link and e.stamp < link["stamp"]):
                    raise Problem("Source is older. Deselect that account; keep its local snapshot.")
            save(db, local, e, e.sign)


def refresh_status(row, link, e):
    if not row:
        return "Local account removed; not recreated"
    if not e:
        return "Missing, invalid or provider error; retained"
    if snapshot(row) != link["snapshot"]:
        return "Local edit retained; discover and confirm again"
    if row["currency"] != e.values["currency"] or (e.sign and e.sign != link["sign"]):
        return "Currency or sign changed; review required"
    if e.stamp < link["stamp"] or e.values["asof"] < row["asof"]:
        return "Older source ignored; local snapshot retained"
    if e.stamp == link["stamp"]:
        return "Unchanged" if row["balance"] == e.values["balance"] else "Conflicting timestamp; review required"
    return "Updated"


def save(db, local, e, sign):
    db.execute("UPDATE accounts SET balance=?, asof=? WHERE id=?", (e.values["balance"], e.values["asof"], local))
    row = db.execute("SELECT * FROM accounts WHERE id=?", (local,)).fetchone()
    db.execute("""INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(source) DO UPDATE SET stamp=excluded.stamp, sign=excluded.sign,
                  snapshot=excluded.snapshot,status=excluded.status,checked=excluded.checked""",
               (e.key, local, e.stamp, sign, snapshot(row), "Updated"))

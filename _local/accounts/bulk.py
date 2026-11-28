"""One-click SimpleFIN balance sync. Infers asset/debt from sign; preserves local overrides."""
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import re
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


def infer_kind(institution, nickname, side):
    hay = f" {institution} {nickname} ".lower()
    if any(k in hay for k in ("401k", "401(k)", "403b", "403(b)", "457", " ira ", "roth", "retirement", "pension", " tsp ")):
        return "Retirement"
    if any(k in hay for k in ("student loan", "nelnet", "aidvantage", "ecsi", "uas ", "mohela")):
        return "Student loan"
    if "mortgage" in hay:
        return "Mortgage"
    if any(k in hay for k in (" loan", "heloc", "auto loan")):
        return "Loan"
    if any(k in hay for k in ("card", "visa", "mastercard", "amex", "discover", "sapphire", "freedom", "signature", "rewards")):
        return "Credit card"
    if any(k in hay for k in ("checking", "chk")):
        return "Checking"
    if any(k in hay for k in ("saving", "hsa", "money market")):
        return "Savings"
    if any(k in hay for k in ("brokerage", "invest", "taxable", "vanguard", "fidelity", "schwab", "robinhood", "crypto")):
        return "Brokerage"
    return "Other"


def unpack(payload, namespace, validate):
    if not isinstance(payload, dict) or not all(isinstance(payload.get(k), list)
            for k in ("connections", "accounts", "errlist")):
        raise Problem("Not a v2 account set. Nothing applied.")
    if len(payload["accounts"]) > 200:
        raise Problem("Batch exceeds the local limit (200 accounts).")
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
        warnings.append("Bridge reported a general warning or quota issue; balances held.")
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
            inferred_side = "debt" if Decimal(signed) < 0 else "asset"
            inst_name, acct_name = connections[cid], text(a["name"], 100)
            v = validate(dict(institution=inst_name, nickname=acct_name,
                              kind=infer_kind(inst_name, acct_name, inferred_side), side=inferred_side,
                              owner="Unspecified", currency=text(a["currency"], 3), balance=magnitude,
                              asof=str(date.fromtimestamp(stamp)), update_method="SimpleFIN sync"), date.today())
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

    def fetch(self, db):
        # Commit the attempt BEFORE I/O, including failures, across restarts/processes.
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fetch_attempts WHERE at <= ?", (time.time() - 86400,))
            if db.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0] >= 24:
                raise Problem("Daily guard: 24 attempts in the past 24 hours. Try later.")
            db.execute("INSERT INTO fetch_attempts VALUES (?)", (time.time(),))
        return unpack(self.provider.fetch(), self.provider.namespace, self.validate)

    def sync(self):
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            entries, warnings = self.fetch(db)
            added = updated = unchanged = held = 0
            with db:
                db.execute("BEGIN IMMEDIATE")
                links, rows = mappings(db), inventory(db)
                for key, e in entries.items():
                    inferred_side = e.values["side"]
                    if key in links:
                        link = links[key]
                        row = rows.get(str(link["local"]))
                        if not row:
                            status = "Removed locally; not recreated"
                            held += 1
                        elif row["currency"] != e.values["currency"]:
                            status = "Currency changed; retained"
                            held += 1
                        elif e.stamp < link["stamp"] or e.values["asof"] < row["asof"]:
                            status = "Older source ignored"
                            held += 1
                        else:
                            new_side = row["side"] if row["side_manual"] else inferred_side
                            if (row["balance"] == e.values["balance"] and row["asof"] == e.values["asof"]
                                    and row["side"] == new_side):
                                db.execute("""UPDATE source_links SET stamp=?, sign=?, status=?,
                                    checked=strftime('%Y-%m-%d %H:%M UTC','now') WHERE source=?""",
                                           (max(link["stamp"], e.stamp), e.sign, "Unchanged", key))
                                unchanged += 1
                                continue
                            db.execute("UPDATE accounts SET balance=?, asof=?, side=? WHERE id=?",
                                       (e.values["balance"], e.values["asof"], new_side, row["id"]))
                            fresh = db.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
                            db.execute("""UPDATE source_links SET stamp=?, sign=?, snapshot=?, status=?,
                                checked=strftime('%Y-%m-%d %H:%M UTC','now') WHERE source=?""",
                                       (e.stamp, e.sign, snapshot(fresh), "Updated", key))
                            updated += 1
                            continue
                        db.execute("""UPDATE source_links SET status=?,
                            checked=strftime('%Y-%m-%d %H:%M UTC','now') WHERE source=?""", (status, key))
                    else:
                        v = e.values
                        local = db.execute("""INSERT INTO accounts
                            (institution,nickname,kind,side,currency,balance,asof,owner,update_method,excluded,side_manual)
                            VALUES (?,?,?,?,?,?,?,?,?,0,0)""",
                            (v["institution"], v["nickname"], v["kind"], v["side"], v["currency"],
                             v["balance"], v["asof"], v["owner"], v["update_method"])).lastrowid
                        fresh = db.execute("SELECT * FROM accounts WHERE id=?", (local,)).fetchone()
                        db.execute("""INSERT INTO source_links (source,local,stamp,sign,snapshot,status)
                            VALUES (?,?,?,?,?,?)""", (key, local, e.stamp, e.sign, snapshot(fresh), "Added"))
                        added += 1
                for key, link in links.items():
                    if key not in entries:
                        db.execute("""UPDATE source_links SET status=?,
                            checked=strftime('%Y-%m-%d %H:%M UTC','now') WHERE source=?""",
                                   ("Missing or provider error; retained", key))
                        held += 1
            parts = []
            if added:
                parts.append(f"{added} added")
            if updated:
                parts.append(f"{updated} updated")
            if unchanged:
                parts.append(f"{unchanged} unchanged")
            if held:
                parts.append(f"{held} held")
            summary = "Synced: " + (", ".join(parts) if parts else "no accounts returned") + "."
            return [summary, *warnings]

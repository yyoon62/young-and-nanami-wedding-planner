"""Local SimpleFIN transaction history and signed cash flow (UTC).

The v2 protocol defines [start-date, end-date), positive deposits and an
explicit pending flag. The Bridge guide caps ranges at 90 days, recommends
5 days of overlap, and makes no guarantee of institution history depth.
No provider balance, category, or diagnostic text is written by this sync.
"""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import re
import sqlite3
import time
from urllib.parse import urlencode

from bridge import Bridge, Problem, unique_fields
from bulk import fingerprint, text
from tx_classifier import install_classifier
from tx_categories import (clean_merchant, add_category, canonical, install_categories, list_categories,
                           load_cache, load_rules, resolve)


DAY = 86400
PAGE = 30 * DAY
INITIAL_LOOKBACK = 90 * DAY
OVERLAP = 5 * DAY
VIEWS = ("Household", "Young", "Nanami")
CURRENCY = re.compile(r"[A-Z]{3}")
AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def install_transactions(db):
    """Add tables/triggers inside the parent's accounts migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS account_transactions (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        tx_id TEXT NOT NULL, source TEXT NOT NULL,
        posted_date TEXT, pending_date TEXT, amount TEXT NOT NULL, currency TEXT NOT NULL,
        description TEXT NOT NULL, payee TEXT NOT NULL, merchant TEXT NOT NULL,
        pending INTEGER NOT NULL CHECK(pending IN (0,1)),
        pending_active INTEGER NOT NULL CHECK(pending_active IN (0,1)),
        category TEXT NOT NULL DEFAULT '', category_manual INTEGER NOT NULL DEFAULT 0,
        is_transfer INTEGER NOT NULL DEFAULT 0, transfer_manual INTEGER NOT NULL DEFAULT 0,
        CHECK((pending=1 AND posted_date IS NULL) OR (pending=0 AND posted_date IS NOT NULL)),
        UNIQUE(account_id, tx_id))""")
    db.execute("""CREATE INDEX IF NOT EXISTS account_transactions_posted
                  ON account_transactions(posted_date)""")
    db.execute("""CREATE INDEX IF NOT EXISTS account_transactions_pending
                  ON account_transactions(pending_active, pending_date)""")
    db.execute("""CREATE TABLE IF NOT EXISTS transaction_coverage (
        source TEXT PRIMARY KEY, account_id INTEGER NOT NULL, currency TEXT NOT NULL,
        start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, revision INTEGER NOT NULL,
        CHECK(start_at < end_at))""")
    db.execute("""CREATE TABLE IF NOT EXISTS transaction_merchant_rules (
        account_id INTEGER NOT NULL, source TEXT NOT NULL, currency TEXT NOT NULL,
        merchant TEXT NOT NULL, category TEXT NOT NULL,
        is_transfer INTEGER CHECK(is_transfer IN (0,1)),
        PRIMARY KEY(account_id, source, currency, merchant))""")
    if "is_transfer" not in {r[1] for r in db.execute("PRAGMA table_info(transaction_merchant_rules)")}:
        db.execute("""ALTER TABLE transaction_merchant_rules
                      ADD COLUMN is_transfer INTEGER CHECK(is_transfer IN (0,1))""")
    if "clean_key" not in {r[1] for r in db.execute("PRAGMA table_info(transaction_merchant_rules)")}:
        db.execute("ALTER TABLE transaction_merchant_rules ADD COLUMN clean_key TEXT")
    for account, source, currency, merchant in db.execute(
            "SELECT account_id, source, currency, merchant FROM transaction_merchant_rules "
            "WHERE clean_key IS NULL").fetchall():
        db.execute("""UPDATE transaction_merchant_rules SET clean_key=? WHERE account_id=? AND source=?
                      AND currency=? AND merchant=?""", (clean_merchant(merchant), account, source, currency, merchant))
    install_categories(db)
    install_classifier(db)
    db.execute("""CREATE TRIGGER IF NOT EXISTS account_transactions_delete
        AFTER DELETE ON accounts BEGIN
            DELETE FROM account_transactions WHERE account_id=OLD.id;
            DELETE FROM transaction_coverage WHERE account_id=OLD.id;
            DELETE FROM transaction_merchant_rules WHERE account_id=OLD.id;
        END""")


def utc_date(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).date()


def _midnight(day):
    return (day - date(1970, 1, 1)).days * DAY


def month_dates(month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("Choose a valid UTC month.")
    try:
        start = date.fromisoformat(month + "-01")
        if not 1970 <= start.year <= 9998:
            raise ValueError()
        end = (start + timedelta(days=31)).replace(day=1)
    except ValueError:
        raise ValueError("Choose a valid UTC month.") from None
    return start, end


def _merchant(value):
    return " ".join(value.casefold().split())


def _optional_text(value, limit):
    if value == "":
        return ""
    return text(value, limit)


def _links(db):
    return {source: (account, currency) for source, account, currency in db.execute("""
        SELECT l.source, a.id, a.currency FROM source_links l JOIN accounts a ON a.id=l.local
        ORDER BY l.source""")}


def _coverage(db):
    return {s: (a, c, lo, hi, rev) for s, a, c, lo, hi, rev in db.execute("""
        SELECT source, account_id, currency, start_at, end_at, revision FROM transaction_coverage""")}


def _reserve(db):
    # Batch.fetch couples its reservation to a balances-only request. Use its
    # exact rolling ledger/limit here, also committing failures BEFORE I/O.
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        db.execute("DELETE FROM fetch_attempts WHERE at <= ?", (now - DAY,))
        if db.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0] >= 24:
            raise Problem("Daily guard: 24 attempts in the past 24 hours. Try later.")
        db.execute("INSERT INTO fetch_attempts VALUES (?)", (now,))


def _fetch(provider, start, end, pending):
    args = {"version": 2, "start-date": start, "end-date": end}
    if pending:
        args["pending"] = 1
    try:
        status, body = provider.transport("GET", provider.host,
                                          provider.root + "/accounts?" + urlencode(args),
                                          provider.headers)
    except Exception:
        raise Problem("Bridge transaction request failed.") from None
    if status != 200:
        raise Problem({402: "Bridge reports payment required.",
                       403: "Bridge access denied or revoked."}
                      .get(status, "Bridge transaction request failed; redirects are not followed."))
    try:
        return json.loads(body, object_pairs_hook=unique_fields)
    except (Problem, ValueError, TypeError, RecursionError):
        raise Problem("Bridge transaction response unavailable or unreadable.") from None


_ERRORS = {"con.auth": "Institution sign-in required", "act.failed": "Account lookup failed",
           "act.missingdata": "Incomplete transaction listing", "gen.auth": "Bridge sign-in required",
           "gen.api": "Provider API issue"}


_QUOTA_RE = re.compile(r"\b(?:quota|rate[\s-]*limit|too many requests|requests?\s+per\s+day|daily\s+limit)\b")
_RANGE_RE = re.compile(r"\b(?:date\s+range|start-?date|end-?date|days?|window)\b")


def _api_reason(entry):
    msg = (entry.get("msg") or "").lower() if isinstance(entry, dict) and isinstance(entry.get("msg"), str) else ""
    if _QUOTA_RE.search(msg):
        return "Approaching SimpleFIN daily request limit"
    if _RANGE_RE.search(msg):
        return "Provider rejected requested date range"
    if re.search(r"\bversion\b", msg):
        return "Provider rejected protocol version"
    if re.search(r"\b(?:param|query|argument|unknown)\b", msg):
        return "Provider rejected a query parameter"
    return _ERRORS["gen.api"]


def _unpack(provider, data, links, start, end, include_pending):
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list)
            for k in ("connections", "accounts", "errlist")) or len(data["accounts"]) > 200:
        raise Problem("Invalid v2 transaction AccountSet; window held.")
    soft_warnings = []
    if data["errlist"] or data.get("errors", []) != []:
        hard_reasons = set()
        for e in data["errlist"]:
            code = e.get("code") if isinstance(e, dict) else None
            if code == "gen.api":
                reason = _api_reason(e)
                if reason == "Approaching SimpleFIN daily request limit" and data["accounts"]:
                    soft_warnings.append(reason)
                else:
                    hard_reasons.add(reason)
            else:
                hard_reasons.add(_ERRORS.get(code, "Provider error") if isinstance(code, str) else "Provider error")
        if data.get("errors", []) != []:
            hard_reasons.add("Provider warning or quota issue")
        if hard_reasons:
            raise Problem("; ".join(sorted(hard_reasons)) + "; window held.")
    seen, found, rows = set(), set(), []
    try:
        connections = set()
        for c in data["connections"]:
            cid = text(c["conn_id"], 256)
            if cid in connections:
                raise ValueError()
            connections.add(cid)
        for a in data["accounts"]:
            cid, aid = text(a["conn_id"], 256), text(a["id"], 256)
            source = fingerprint(provider.namespace, cid, aid)
            if cid not in connections or source in seen:
                raise ValueError()
            seen.add(source)
            if source not in links:
                continue
            account, currency = links[source]
            if a.get("currency") != currency or not CURRENCY.fullmatch(currency):
                raise Problem("Linked account currency mismatch or unsupported currency; window held.")
            if not isinstance(a.get("transactions"), list):
                raise Problem("Linked account did not supply a transaction list; window held.")
            found.add(source)
            tx_seen = set()
            for tx in a["transactions"]:
                tx_id = fingerprint(source, text(tx["id"], 256))
                if tx_id in tx_seen:
                    raise ValueError()
                tx_seen.add(tx_id)
                posted, pending = tx["posted"], tx.get("pending", False)
                if type(posted) is not int or type(pending) is not bool or posted < 0:
                    raise ValueError()
                if posted == 0:
                    pending = True
                if pending and not include_pending:
                    continue
                # Real banks often timestamp posted dates at noon UTC or local midnight,
                # so allow slight boundary drift while still rejecting far-future dates.
                if not pending and not (0 < posted <= end + 2 * DAY):
                    raise Problem("Provider returned a posting far outside the requested UTC window; window held.")
                transacted = tx.get("transacted_at", 0) if pending else 0
                if type(transacted) is not int or transacted < 0:
                    raise ValueError()
                raw_amount = text(tx["amount"], 64)
                if not AMOUNT.fullmatch(raw_amount):
                    raise ValueError()
                description = _optional_text(tx["description"], 512)
                payee = _optional_text(tx.get("payee", ""), 256)  # optional provider extension
                if any(secret in field for secret in provider.private_strings | provider.private_paths
                       for field in (description, payee)):
                    raise Problem("Unexpected sensitive transaction field; window held.")
                rows.append((account, tx_id, source, None if pending else utc_date(posted).isoformat(),
                             utc_date(posted or transacted or end - 1).isoformat() if pending else None,
                             raw_amount, currency, description, payee, _merchant(payee or description),
                             int(pending), int(pending)))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise Problem("Malformed or duplicate transaction data; window held.") from None
    if found != links.keys():
        raise Problem("A linked account was missing; window held. Check the configured Bridge connection.")
    return rows, sorted(set(soft_warnings))


def _apply(db, rows, links, coverage, start, end, final):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if _links(db) != links or _coverage(db) != coverage:
            raise Problem("Transaction links or history changed during sync; retry.")
        if final:
            # The final request alone can report pending. Expire previous display
            # rows even if pending was not requested, without losing manual edits.
            db.executemany("""UPDATE account_transactions SET pending_active=0
                WHERE account_id=? AND source=? AND pending_active=1""",
                           ((account, source) for source, (account, _) in links.items()))
        rules = {(account, source, currency, merchant): category
                 for account, source, currency, merchant, category in db.execute(
                     "SELECT account_id, source, currency, merchant, category FROM transaction_merchant_rules")}
        db.executemany("""INSERT INTO account_transactions (
            account_id, tx_id, source, posted_date, pending_date, amount, currency, description, payee,
            merchant, pending, pending_active, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,tx_id) DO UPDATE SET posted_date=excluded.posted_date,
                pending_date=excluded.pending_date, amount=excluded.amount, currency=excluded.currency,
                description=excluded.description, payee=excluded.payee, merchant=excluded.merchant,
                pending=excluded.pending, pending_active=excluded.pending_active,
                category=CASE WHEN category_manual=0 AND account_transactions.category=''
                              THEN excluded.category ELSE account_transactions.category END
            WHERE account_transactions.pending=1 OR excluded.pending=0""",
                       ((*row, rules.get((row[0], row[2], row[6], row[9]), "")) for row in rows))
        for source, (account, currency) in links.items():
            previous = coverage.get(source)
            lo, hi, rev = start, end, 1
            if previous and previous[:2] == (account, currency):
                old_lo, old_hi, old_rev = previous[2:]
                rev += old_rev
                if start <= old_hi and end >= old_lo:
                    lo, hi = min(lo, old_lo), max(hi, old_hi)
                else:
                    # Don't claim coverage over an unqueried gap.
                    lo, hi = old_lo, old_hi
            db.execute("""INSERT INTO transaction_coverage VALUES (?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET account_id=excluded.account_id,
                currency=excluded.currency, start_at=excluded.start_at, end_at=excluded.end_at,
                revision=excluded.revision""", (source, account, currency, lo, hi, rev))


@dataclass(frozen=True)
class SyncResult:
    windows: int
    caught_up: bool
    warnings: tuple = ()


def sync_transactions(batch, *, include_pending=False, now=None):
    """Opt-in, at most five windows per click. Uses Batch.path/provider/lock.

    Earlier successful windows survive a later Problem. Caller must not hold a
    DB write transaction or the batch lock. `now` is an injectable epoch second.
    Only a real Bridge transport interface is supported; tests inject its wire.
    """
    provider = batch.provider
    if not isinstance(provider, Bridge):
        raise Problem("Transaction sync requires the configured SimpleFIN Bridge.")
    with batch.lock, closing(sqlite3.connect(batch.path)) as db:
        with db:
            db.execute("BEGIN")
            links, coverage = _links(db), _coverage(db)
        if not links:
            raise Problem("No linked accounts. Sync balances first.")
        end = int(time.time()) if now is None else now
        today = utc_date(end)
        initial = _midnight(today - timedelta(days=INITIAL_LOOKBACK // DAY))
        start = min(max(1, c[2], c[3] - OVERLAP)
                    if (c := coverage.get(s)) and c[:2] == link else initial
                    for s, link in links.items())
        if start >= end:
            raise Problem("Local clock precedes saved transaction coverage; check the clock.")
        done, warnings = 0, []
        while start < end and done < 5:
            stop = min(start + PAGE, end)
            final = stop == end
            _reserve(db)
            rows, soft = _unpack(provider, _fetch(provider, start, stop, final and include_pending),
                                 links, start, stop, final and include_pending)
            _apply(db, rows, links, coverage, start, stop, final)
            coverage = _coverage(db)
            start, done = (stop if final else stop - OVERLAP), done + 1
            if soft:
                warnings.extend(soft)
                break  # Stop paging immediately when Bridge warns about daily quota.
        return SyncResult(done, start == end, tuple(dict.fromkeys(warnings)))


# Match only corroborated, narrowly defined checking/savings → credit-card
# payment descriptions; a deposit/refund of the right size alone isn't proof.
_BANK_PAYMENT = re.compile(
    r"(?:(?:online|automatic|autopay) )?(?:credit card|card|visa|mastercard|amex|discover|american express)"
    r" (?:payment|autopay)(?: (?:ending in )?\*?[0-9]{4})?")
_CARD_PAYMENT = re.compile(
    r"(?:payment (?:thank you|received)|(?:auto ?pay|online|automatic|credit card|card) payment"
    r"(?: (?:thank you|received))?)")
_BANK_KINDS = ("checking", "savings", "saving")
_INTERNAL_MOVE = re.compile(r"(?:(?:online|internal|account|bank) )?transfer"
                            r"(?: (?:in|out)| (?:to|from) .+)?")


def _signal(row, pattern):
    return any(pattern.fullmatch(" ".join(re.sub(r"[^a-z0-9*]+", " ", value.casefold()).split()))
               for value in (row["description"], row["payee"]))


def _card_credit(row):
    return (row["kind"].lower() in ("credit card", "credit cards") and Decimal(row["amount"]) > 0
            and _signal(row, _CARD_PAYMENT))


def _transfer_override(row):
    return row["is_transfer"] if row["transfer_manual"] else row["merchant_transfer"]


def _match_transfers(rows):
    bank, card = defaultdict(list), []
    for r in rows:
        if r["pending"] or _transfer_override(r) == 0:
            continue
        amount = Decimal(r["amount"])
        if _card_credit(r):
            card.append(r)
        elif (r["kind"].lower() in _BANK_KINDS and amount < 0
              and _signal(r, _BANK_PAYMENT)):
            bank[(r["currency"], amount.copy_abs(), r["posted_date"])].append(r["row_id"])
    sole_partner, bank_degree = {}, defaultdict(int)
    for credit in card:
        day = date.fromisoformat(credit["posted_date"])
        cid = credit["row_id"]
        degree, sole = 0, None
        for i in range(-3, 4):
            key = (credit["currency"], Decimal(credit["amount"]), str(day + timedelta(days=i)))
            if count := len(bank.get(key, ())):
                degree += count
                bank_degree[key] += 1
                sole = key
        if degree == 1:
            sole_partner[cid] = sole
    # Mutual uniqueness, not greedy nearest-date matching. Ambiguous legs stay for review.
    return {r for cid, key in sole_partner.items() if bank_degree[key] == 1 for r in (cid, bank[key][0])}


def _internal_suggestions(rows):
    # Require transfer wording on BOTH legs, then look for mutual uniqueness
    # across distinct accounts. Even this is a hint, not an automatic exclusion.
    buckets = defaultdict(lambda: defaultdict(list))
    candidates = []
    for r in rows:
        if (r["pending"] or _transfer_override(r) is not None or r["kind"].lower() not in _BANK_KINDS
                or not _signal(r, _INTERNAL_MOVE)):
            continue
        amount = Decimal(r["amount"])
        if amount:
            day = date.fromisoformat(r["posted_date"])
            buckets[(r["currency"], amount, day)][r["account_id"]].append(r["row_id"])
            candidates.append((r["row_id"], r["account_id"], r["currency"], amount, day))
    sole_partner = {}
    for row, account, currency, amount, day in candidates:
        degree, sole = 0, None
        for offset in range(-3, 4):
            key = (currency, amount.copy_negate(), day + timedelta(days=offset))
            for other_account, others in buckets.get(key, {}).items():
                if other_account == account:
                    continue
                degree += len(others)
                sole = others[0]
                if degree > 1:
                    break
            if degree > 1:
                break
        if degree == 1:
            sole_partner[row] = sole
    return {row for row, peer in sole_partner.items() if sole_partner.get(peer) == row}


_VISIBLE = """FROM account_transactions t JOIN accounts a ON a.id=t.account_id
    JOIN source_links l ON l.local=a.id AND l.source=t.source"""
_SCOPE = """WHERE a.excluded=0 AND t.currency=a.currency AND (?='Household' OR a.owner IN (?,'Joint'))"""


@dataclass
class Totals:
    credits: Decimal = Decimal(0)
    spending: Decimal = Decimal(0)
    net: Decimal = Decimal(0)
    review: Decimal = Decimal(0)
    category_spending: dict = field(default_factory=dict)
    category_suggested: dict = field(default_factory=dict)  # category -> cleaned merchants suggested by the classifier


@dataclass
class Snapshot:
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    coverage: list = field(default_factory=list)


def _dicts(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def category_context(db):
    """Rules, classifier cache and canonical names for read-time categories (one read)."""
    return load_rules(db), load_cache(db), {n.casefold(): n for n in list_categories(db)}


def monthly_snapshot(db, month, view, *, show_pending=False, household_matching=False, context=None):
    """Caller owns a read snapshot and authorizes view/household_matching."""
    if view not in VIEWS:
        raise ValueError("Unknown account view.")
    start, end = month_dates(month)
    scope = "Household" if household_matching else view
    # A competing edge of the other leg may be six days from the displayed row.
    # Three days of query padding can falsely turn an ambiguous edge into a pair.
    rows = _dicts(db.execute(f"""SELECT t.*, a.kind, a.owner, a.nickname, a.institution, a.logo_domain,
        r.is_transfer AS merchant_transfer {_VISIBLE}
        LEFT JOIN transaction_merchant_rules r ON r.account_id=t.account_id AND r.source=t.source
            AND r.currency=t.currency AND r.merchant=t.merchant
        {_SCOPE} AND ((t.posted_date>=? AND t.posted_date<?)
        OR (? AND t.pending_active=1 AND t.pending_date>=? AND t.pending_date<?))
        ORDER BY coalesce(t.posted_date,t.pending_date) DESC, t.row_id DESC""",
        (scope, scope, str(start - timedelta(days=6)), str(end + timedelta(days=6)),
         show_pending, str(start), str(end))))
    rules, cache, canon = context or category_context(db)
    for r in rows:
        r["category_stored"] = r["category"]
        r["category"], r["category_source"] = resolve(r, rules, cache, canon)
        r["clean_key"] = clean_merchant(r["merchant"])
        hit = cache.get(r["clean_key"]) if r["category_source"] == "suggested" else None
        r["emoji"] = hit[2] if hit else ""
    matched = _match_transfers(rows)
    # Suggestions never depend on the presence of a counterpart outside the
    # selected view, even if the viewer is authorized to switch to Household.
    suggested = _internal_suggestions(r for r in rows if view == "Household" or r["owner"] in (view, "Joint"))
    result = Snapshot()
    result.coverage = _dicts(db.execute("""SELECT a.nickname, a.currency, a.institution,
        l.source IS NOT NULL AS linked, c.start_at, c.end_at
        FROM accounts a LEFT JOIN source_links l ON l.local=a.id
        LEFT JOIN transaction_coverage c ON c.source=l.source AND c.account_id=a.id AND c.currency=a.currency
        WHERE a.excluded=0 AND (?='Household' OR a.owner IN (?,'Joint'))
        ORDER BY a.institution, a.nickname, a.id""", (view, view)))
    for c in result.coverage:
        c["covered"] = (c["start_at"] is not None and c["start_at"] <= _midnight(start)
                        and c["end_at"] >= _midnight(end))
    by_currency = defaultdict(list)
    for c in result.coverage:
        by_currency[c["currency"]].append(c["covered"])
    for currency, covered in by_currency.items():
        if all(covered):
            result.totals[currency] = Totals()
    with localcontext() as ctx:
        # One amount can use its bound for whole digits and another for fractional
        # digits. Allow both, plus up to 19 digits of SQLite row count.
        ctx.prec = 150
        for r in rows:
            if view != "Household" and r["owner"] not in (view, "Joint"):
                continue
            if not str(start) <= (r["posted_date"] or r["pending_date"]) < str(end):
                continue
            override = _transfer_override(r)
            if r["pending"]:
                r["flow"] = "Pending"
            elif r["transfer_manual"]:
                r["flow"] = "Transfer (manual)" if override else "Cash flow (manual)"
            elif override is not None:
                r["flow"] = "Transfer (merchant rule)" if override else "Cash flow (merchant rule)"
            elif r["row_id"] in matched:
                r["flow"] = "Matched transfer"
            else:
                r["flow"] = "Review card credit" if _card_credit(r) else "Cash flow"
            r["effective_transfer"] = not r["pending"] and (
                bool(override) if override is not None else r["row_id"] in matched)
            r["suggested_transfer"] = r["row_id"] in suggested and not r["effective_transfer"]
            result.rows.append(r)
            if r["pending"]:
                continue
            totals = result.totals.setdefault(r["currency"], Totals())
            if r["flow"] == "Review card credit":
                totals.review += Decimal(r["amount"])
            elif not r["effective_transfer"]:
                amount = Decimal(r["amount"])
                if amount > 0:
                    totals.credits += amount
                elif amount < 0:
                    spending = amount.copy_abs()
                    totals.spending += spending
                    category = r["category"] or "Uncategorized"
                    totals.category_spending[category] = totals.category_spending.get(category, Decimal(0)) + spending
                    if r["category_source"] == "suggested":
                        totals.category_suggested.setdefault(category, set()).add(r["clean_key"])
                totals.net += amount
    return result


def _set_transfer(db, row_id, transfer):
    db.execute("UPDATE account_transactions SET is_transfer=?, transfer_manual=? WHERE row_id=?",
               (transfer == "yes", transfer != "auto", row_id))


def _set_merchant_transfer(db, key, clean, merchant_transfer):
    if merchant_transfer == "none":
        db.execute("""UPDATE transaction_merchant_rules SET is_transfer=NULL
            WHERE account_id=? AND source=? AND currency=? AND merchant=?""", key)
        db.execute("""DELETE FROM transaction_merchant_rules WHERE account_id=? AND source=?
            AND currency=? AND merchant=? AND category='' AND is_transfer IS NULL""", key)
    else:
        db.execute("""INSERT INTO transaction_merchant_rules
            (account_id, source, currency, merchant, category, is_transfer, clean_key) VALUES (?,?,?,?,'',?,?)
            ON CONFLICT(account_id, source, currency, merchant) DO UPDATE
            SET is_transfer=excluded.is_transfer""", (*key, merchant_transfer == "yes", clean))


def _set_category_rule(db, key, clean, category):
    db.execute("""INSERT INTO transaction_merchant_rules
        (account_id, source, currency, merchant, category, clean_key) VALUES (?,?,?,?,?,?)
        ON CONFLICT(account_id, source, currency, merchant) DO UPDATE
        SET category=excluded.category, clean_key=excluded.clean_key""", (*key, category, clean))
    if clean:
        # Older rules for the same cleaned merchant must not outvote the one just saved.
        db.execute("""UPDATE transaction_merchant_rules SET category=? WHERE account_id=? AND source=?
            AND currency=? AND clean_key=? AND category<>''""", (category, key[0], key[1], key[2], clean))


def manual_rows_for_merchant(db, row):
    """Other rows of the same account/currency/cleaned merchant that carry a manual category."""
    clean = clean_merchant(row["merchant"])
    return sum(1 for rid, merchant in db.execute(
        """SELECT row_id, merchant FROM account_transactions WHERE account_id=? AND source=? AND currency=?
           AND category_manual=1""", (row["account_id"], row["source"], row["currency"]))
        if rid != row["row_id"] and (merchant == row["merchant"] or (clean and clean_merchant(merchant) == clean)))


def manual_index(db):
    """{(account, source, currency, key): rows with a manual category}, key = cleaned merchant or exact."""
    index = defaultdict(int)
    for account, source, currency, merchant in db.execute(
            "SELECT account_id, source, currency, merchant FROM account_transactions WHERE category_manual=1"):
        index[(account, source, currency, clean_merchant(merchant) or merchant)] += 1
    return index


ACTIONS = ("category", "rule", "transfer", "transfer_rule", "save", "add_category")


def edit_transaction(db, row_id, view, month, *, action, category="", transfer="", merchant_transfer="",
                     new_category="", apply=False):
    """Apply a validated scoped edit inside the caller's BEGIN IMMEDIATE.

    `save` is the drawer's one-shot edit (category, optional new category, apply-to-merchant,
    transfer flags). Rules are scoped to this account and currency and matched on the cleaned
    merchant key, so a store number or date in the description does not defeat them.
    """
    if view not in VIEWS:
        raise ValueError("Unknown account view.")
    start, end = month_dates(month)
    if action not in ACTIONS:
        raise ValueError("Unknown transaction action.")
    if transfer not in ("", "auto", "yes", "no") or merchant_transfer not in ("", "none", "yes", "no"):
        raise ValueError("Choose a valid transfer setting.")
    if action == "transfer" and not transfer:
        raise ValueError("Choose a transfer setting.")
    if action == "transfer_rule" and not merchant_transfer:
        raise ValueError("Choose a merchant transfer rule setting.")
    category = category.strip() if isinstance(category, str) else ""
    if action in ("category", "rule", "save"):
        if category:
            _optional_text(category, 48)
            category = canonical(db, category)
            if category is None:
                raise ValueError("Choose a category from the list, or add it first.")
        if (action == "rule" or (action == "save" and apply)) and not (category or new_category.strip()):
            raise ValueError("Choose a category to apply to this merchant.")
    found = _dicts(db.execute(f"""SELECT t.* {_VISIBLE} {_SCOPE} AND t.row_id=?
        AND coalesce(t.posted_date,t.pending_date)>=? AND coalesce(t.posted_date,t.pending_date)<?
        AND (t.pending=0 OR t.pending_active=1)""", (view, view, row_id, str(start), str(end))))
    if not found:
        return False
    row = found[0]
    key = (row["account_id"], row["source"], row["currency"], row["merchant"])
    clean = clean_merchant(row["merchant"])
    if action == "add_category":
        add_category(db, new_category)
        return True
    if action == "transfer":
        _set_transfer(db, row_id, transfer)
        return True
    if action == "transfer_rule":
        if row["pending"] or not row["merchant"]:
            raise ValueError("A merchant rule needs a posted transaction with a merchant.")
        _set_merchant_transfer(db, key, clean, merchant_transfer)
        return True
    apply = apply or action == "rule"
    if action in ("category", "rule", "save"):
        if new_category.strip():
            category = add_category(db, new_category)
        if apply:
            if row["pending"] or not row["merchant"]:
                raise ValueError("A merchant rule needs a posted transaction with a merchant.")
            _set_category_rule(db, key, clean, category)
            # The row follows the live rule; no stale stamp or manual flag may shadow it.
            db.execute("UPDATE account_transactions SET category='', category_manual=0 WHERE row_id=?", (row_id,))
        else:
            rules, cache, canon = category_context(db)
            current, source = resolve(row, rules, cache, canon)
            if action == "category" or category != current or source in ("suggested", "review"):
                db.execute("UPDATE account_transactions SET category=?, category_manual=1 WHERE row_id=?",
                           (category, row_id))
    if action == "save":
        if transfer:
            _set_transfer(db, row_id, transfer)
        if merchant_transfer and not (row["pending"] or not row["merchant"]):
            _set_merchant_transfer(db, key, clean, merchant_transfer)
    return True

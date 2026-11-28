"""Slice A: one-drawer edit, category list, cleaned-key rules, read-time precedence, migration."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server import init_db
from transactions import monthly_snapshot
from transactions_ui import RouteProblem, render_transactions
from tx_categories import BUILTIN, clean_merchant, list_categories, normalize_name, resolve
from tx_fixture import Fixture, TOKEN, VIEWS, tx

PRE_L22_DDL = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
    kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')), currency TEXT NOT NULL,
    balance TEXT NOT NULL, asof TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'Unspecified',
    update_method TEXT NOT NULL DEFAULT 'Manual entry', excluded INTEGER NOT NULL DEFAULT 0,
    side_manual INTEGER NOT NULL DEFAULT 0, logo_domain TEXT NOT NULL DEFAULT '');
CREATE TABLE source_links (source TEXT PRIMARY KEY, local INTEGER NOT NULL UNIQUE, stamp INTEGER NOT NULL,
    sign INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
    checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M UTC','now')));
CREATE TABLE fetch_attempts (at REAL NOT NULL);
CREATE TABLE balance_history (account_id INTEGER NOT NULL, asof TEXT NOT NULL, balance TEXT NOT NULL,
    currency TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(account_id, asof));
CREATE TABLE projection_assumptions (view TEXT NOT NULL, currency TEXT NOT NULL, assumptions TEXT NOT NULL,
    PRIMARY KEY(view, currency));
CREATE TABLE account_transactions (row_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
    tx_id TEXT NOT NULL, source TEXT NOT NULL, posted_date TEXT, pending_date TEXT, amount TEXT NOT NULL,
    currency TEXT NOT NULL, description TEXT NOT NULL, payee TEXT NOT NULL, merchant TEXT NOT NULL,
    pending INTEGER NOT NULL CHECK(pending IN (0,1)), pending_active INTEGER NOT NULL CHECK(pending_active IN (0,1)),
    category TEXT NOT NULL DEFAULT '', category_manual INTEGER NOT NULL DEFAULT 0,
    is_transfer INTEGER NOT NULL DEFAULT 0, transfer_manual INTEGER NOT NULL DEFAULT 0,
    CHECK((pending=1 AND posted_date IS NULL) OR (pending=0 AND posted_date IS NOT NULL)),
    UNIQUE(account_id, tx_id));
CREATE TABLE transaction_coverage (source TEXT PRIMARY KEY, account_id INTEGER NOT NULL, currency TEXT NOT NULL,
    start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, revision INTEGER NOT NULL, CHECK(start_at < end_at));
CREATE TABLE transaction_merchant_rules (account_id INTEGER NOT NULL, source TEXT NOT NULL, currency TEXT NOT NULL,
    merchant TEXT NOT NULL, category TEXT NOT NULL, is_transfer INTEGER CHECK(is_transfer IN (0,1)),
    PRIMARY KEY(account_id, source, currency, merchant));
"""


def dump(db, tables):
    return {t: db.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall() for t in tables}


class MaplePath(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("m1", "2026-11-03", "-12.40", "Maple Cafe"),
                 tx("m2", "2026-11-10", "-9.00", "Maple Cafe"),
                 tx("m3", "2026-11-12", "-6.00", "SQ *MAPLE CAFE 11/12 #1234"),
                 tx("m4", "2026-11-19", "-6.50", "SQ *MAPLE CAFE 11/19 #1234"))
        self.add("Card", tx("c1", "2026-11-04", "-3.00", "Maple Cafe"))
        self.sync()

    def rows(self, description, month="2026-11"):
        return [r for r in self.snap(month).rows if r["description"] == description]

    def rule_count(self):
        return self.db.execute("SELECT count(*) FROM transaction_merchant_rules WHERE category<>''").fetchone()[0]

    def test_drawer_save_apply_unticked_stale_stamp_and_later_sync(self):
        bank_first, bank_second = (r for r in self.rows("Maple Cafe") if r["nickname"] == "Bank")
        card = next(r for r in self.rows("Maple Cafe") if r["nickname"] == "Card")
        # A row edited by hand (earlier deliberate choice) is exempt from the merchant rule.
        self.post(bank_second["row_id"], action="save", category="Dining", transfer="auto", merchant_transfer="none")
        self.post(bank_first["row_id"], action="save", category="Dining", apply="1", transfer="auto")
        by = {r["row_id"]: r for r in self.snap().rows}
        self.assertEqual(by[bank_first["row_id"]]["category"], "Dining")
        self.assertEqual(by[bank_second["row_id"]]["category_source"], "manual")
        self.assertEqual(self.rows("SQ *MAPLE CAFE 11/12 #1234")[0]["category"], "Dining")  # cleaned key
        self.assertEqual(self.rows("SQ *MAPLE CAFE 11/19 #1234")[0]["category"], "Dining")
        self.assertEqual(by[card["row_id"]]["category"], "")  # other account: not in scope
        # A later sync brings the same exact merchant: stamped Dining at insert (a stale stamp).
        self.add("Bank", tx("m5", "2026-12-11", "-5.00", "Maple Cafe"))
        self.sync()
        late = next(r for r in self.snap("2026-12").rows if r["amount"] == "-5.00")
        self.assertEqual((late["category_stored"], late["category"]), ("Dining", "Dining"))
        # Re-save with a different category: the live rule beats the stale stamp on every unmanaged row.
        self.post(bank_first["row_id"], action="save", category="Groceries", apply="1", transfer="auto")
        by = {r["row_id"]: r for r in (*self.snap().rows, *self.snap("2026-12").rows)}
        self.assertEqual(by[late["row_id"]]["category"], "Groceries")
        self.assertEqual(self.rows("SQ *MAPLE CAFE 11/19 #1234")[0]["category"], "Groceries")
        self.assertEqual(by[bank_second["row_id"]]["category"], "Dining")  # manual still wins
        self.assertEqual(by[card["row_id"]]["category"], "")
        self.assertEqual(self.rule_count(), 1)
        # A brand-new row whose description carries a different date/ref still gets the rule.
        self.add("Bank", tx("m6", "2026-12-12", "-4.00", "SQ *Maple Cafe 12/12 #77"))
        self.sync()
        self.assertEqual(self.rows("SQ *Maple Cafe 12/12 #77", "2026-12")[0]["category"], "Groceries")
        # Unticked: only that row changes.
        target = self.rows("SQ *MAPLE CAFE 11/12 #1234")[0]
        self.post(target["row_id"], action="save", category="Shopping", transfer="auto")
        self.assertEqual(self.rows("SQ *MAPLE CAFE 11/12 #1234")[0]["category"], "Shopping")
        self.assertEqual(self.rows("SQ *MAPLE CAFE 11/19 #1234")[0]["category"], "Groceries")
        self.assertEqual(self.rule_count(), 1)

    def test_drawer_states_scope_and_manual_note(self):
        bank = [r for r in self.rows("Maple Cafe") if r["nickname"] == "Bank"]
        row = bank[0]
        self.post(bank[1]["row_id"], action="save", category="Dining")
        html = render_transactions(self.db, "month=2026-11", token=TOKEN, allowed_views=VIEWS)
        self.assertIn("Apply to this merchant on this account (USD) matching “maple cafe”", html)
        self.assertEqual(html.count("1 other row(s) with a category you set by hand will not change"), 3)
        self.assertEqual(html.count('name="action" value="save"'), len(self.snap().rows))
        self.assertNotIn('name="action" value="category"', html)  # per-row button forms are gone
        self.assertIn('id="tx-%d"' % row["row_id"], html)

    def test_add_category_duplicate_blank_keep_form_open_and_survive_restart(self):
        row = self.rows("Maple Cafe")[0]
        self.post(row["row_id"], action="add_category", new_category="Pets")
        self.assertIn("Pets", list_categories(self.db))
        self.assertIn('<option value="Pets">Pets</option>',
                      render_transactions(self.db, "month=2026-11", token=TOKEN, allowed_views=VIEWS))
        for bad, message in (("pets", "already exists"), ("  ", "Enter a category name"), ("", "Enter a category name"),
                             ("Uncategorized", "reserved"), ("x" * 49, "up to 48"), ("a\x00b", "printable")):
            with self.subTest(bad=bad), self.assertRaises(RouteProblem) as caught:
                self.post(row["row_id"], action="add_category", new_category=bad)
            self.assertEqual(caught.exception.status, 422)
            self.assertIn(message, str(caught.exception))
            body = caught.exception.body  # full page: drawer open, error shown
            self.assertIn('<details class="inline-pop tx-drawer" open>', body)
            self.assertIn('role="alert"', body)
        with self.assertRaises(RouteProblem) as caught:  # unknown category cannot be saved on a row
            self.post(row["row_id"], action="save", category="Not a category")
        self.assertIn("from the list", str(caught.exception))
        self.post(row["row_id"], action="save", category="", new_category="Hobbies")  # add + assign in one Save
        self.assertEqual(self.rows("Maple Cafe")[0]["category"], "Hobbies")
        self.db.close()
        init_db(self.path)  # "restart": migration is idempotent and the list persists
        with closing(sqlite3.connect(self.path)) as fresh:
            names = list_categories(fresh)
        self.assertTrue({"Pets", "Hobbies", *BUILTIN} <= set(names))
        self.assertEqual(len(names), len({n.casefold() for n in names}))
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))

    def test_apply_needs_category_and_valid_flag(self):
        row = self.rows("Maple Cafe")[0]
        with self.assertRaises(RouteProblem) as caught:
            self.post(row["row_id"], action="save", category="", apply="1")
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("Choose a category", str(caught.exception))
        with self.assertRaises(RouteProblem) as caught:
            self.post(row["row_id"], action="save", category="Dining", apply="2")
        self.assertEqual(caught.exception.status, 400)

    def test_saving_only_a_transfer_flag_does_not_freeze_the_category(self):
        row = self.rows("Maple Cafe")[0]
        self.post(row["row_id"], action="save", category="", transfer="no")
        self.assertEqual(self.rows("Maple Cafe")[0]["category_source"], "none")
        self.post(row["row_id"], action="save", category="Dining", apply="1")
        self.post(row["row_id"], action="save", category="Dining", transfer="auto")
        self.assertEqual(self.rows("Maple Cafe")[0]["category_source"], "rule")  # same value: no manual flag


class Precedence(unittest.TestCase):
    ROW = dict(category="", category_manual=0, account_id=1, source="s", currency="USD", merchant="maple cafe")

    def test_manual_beats_rule_beats_stamp_beats_cache_beats_none(self):
        rules = ({(1, "s", "USD", "maple cafe"): "Groceries"}, {})
        cache = {"maple cafe": ("Dining", "suggested")}
        canon = {"groceries": "Groceries", "dining": "Dining", "pets": "Pets"}
        r = dict(self.ROW)
        self.assertEqual(resolve(r, rules, cache, canon), ("Groceries", "rule"))
        self.assertEqual(resolve(dict(r, category="Pets", category_manual=1), rules, cache, canon), ("Pets", "manual"))
        self.assertEqual(resolve(dict(r, category="", category_manual=1), rules, cache, canon), ("", "manual"))
        self.assertEqual(resolve(dict(r, category="pets"), ({}, {}), cache, canon), ("Pets", "stamp"))
        self.assertEqual(resolve(r, ({}, {}), cache, canon), ("Dining", "suggested"))
        self.assertEqual(resolve(r, ({}, {}), {"maple cafe": ("", "review")}, canon), ("", "review"))
        self.assertEqual(resolve(r, ({}, {}), {}, canon), ("", "none"))
        self.assertEqual(resolve(dict(r, merchant="other"), rules, {}, canon), ("", "none"))

    def test_cleaned_key_is_stable_and_privacy_minimal(self):
        for raw in ("Maple Cafe", "SQ *MAPLE CAFE 11/12 #1234", "sq*Maple  Cafe 11/19 #77", "MAPLE CAFE #9"):
            self.assertEqual(clean_merchant(raw), "maple cafe")
        self.assertEqual(clean_merchant("POS DEBIT 4412 AMZN Mktp US*2K4TT0"), "amzn mktp us")
        self.assertEqual(clean_merchant("12 34"), "")
        self.assertEqual(clean_merchant("AB"), "")
        self.assertEqual(clean_merchant("a" * 200), "a" * 48)
        self.assertEqual(clean_merchant(None), "")
        self.assertNotIn("1234", clean_merchant("Zelle to Jane Doe conf 1234567"))
        self.assertEqual(normalize_name("  Pet\u00a0Care  "), "Pet Care")


class Migration(unittest.TestCase):
    TABLES = ("accounts", "source_links", "balance_history", "projection_assumptions", "account_transactions",
              "transaction_coverage")

    def test_pre_l22_database_opens_unchanged_and_new_tables_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "pre-l22.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(PRE_L22_DDL)
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank','Checking','Checking','asset','USD','10.00','2026-11-01','Young')""")
                db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES ('s',1,0,1,'','ok')")
                db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,
                    description,payee,merchant,pending,pending_active,category,category_manual)
                    VALUES (1,'t1','s','2026-11-03','-12.40','USD','Maple Cafe','','maple cafe',0,0,'Legacy Free Text',1)""")
                db.execute("INSERT INTO transaction_coverage VALUES ('s',1,'USD',1,2,1)")
                db.execute("""INSERT INTO transaction_merchant_rules (account_id,source,currency,merchant,category,is_transfer)
                    VALUES (1,'s','USD','sq *maple cafe 11/12 #1234','Dining',NULL)""")
                db.execute("""INSERT INTO projection_assumptions VALUES ('Household','USD',
                    '{"years": "10", "income": "100"}')""")
                db.execute("INSERT INTO balance_history (account_id,asof,balance,currency,side) "
                           "VALUES (1,'2026-11-01','10.00','USD','asset')")
            with closing(sqlite3.connect(path)) as db:
                before = dump(db, self.TABLES)
                rules_before = db.execute("SELECT account_id,source,currency,merchant,category,is_transfer "
                                          "FROM transaction_merchant_rules").fetchall()
            init_db(path)
            init_db(path)  # second open must change nothing
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(dump(db, self.TABLES), before)
                self.assertEqual(db.execute("SELECT account_id,source,currency,merchant,category,is_transfer "
                                            "FROM transaction_merchant_rules").fetchall(), rules_before)
                self.assertEqual(db.execute("SELECT clean_key FROM transaction_merchant_rules").fetchone()[0],
                                 "maple cafe")  # legacy exact rule gains a cleaned key
                names = [r[0] for r in db.execute("SELECT name FROM transaction_categories ORDER BY name")]
                self.assertIn("Legacy Free Text", names)  # existing free text joins the list, not lost
                self.assertEqual(len(names), len(BUILTIN) + 2)  # builtins + built-in Income + the legacy free text
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.assertEqual(len(tables), len(set(tables)))
                self.assertEqual(monthly_snapshot(db, "2026-11", "Household").rows[0]["category"], "Legacy Free Text")


if __name__ == "__main__":
    unittest.main()

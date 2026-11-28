"""L23 slice A: account_terms table, drawer fields, rate-missing flags, additive migration (fictional data only)."""
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from account_terms import Terms, install_terms, is_paid_in_full, load_terms, parse_terms, save_terms
from server import init_db, section_for
from test_dashboard import running

# Literal schema of a database as last written by f30667f (pre-L23): `sqlite3 db .schema`, no account_terms.
PRE_L23_DDL = """
CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
            kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
            currency TEXT NOT NULL, balance TEXT NOT NULL, asof TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'Unspecified', update_method TEXT NOT NULL DEFAULT 'Manual entry', excluded INTEGER NOT NULL DEFAULT 0, side_manual INTEGER NOT NULL DEFAULT 0, logo_domain TEXT NOT NULL DEFAULT '');
CREATE TABLE source_links (
        source TEXT PRIMARY KEY, local INTEGER NOT NULL UNIQUE, stamp INTEGER NOT NULL,
        sign INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
        checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M UTC','now')));
CREATE TABLE fetch_attempts (at REAL NOT NULL);
CREATE TABLE balance_history (
        account_id INTEGER NOT NULL, asof TEXT NOT NULL, balance TEXT NOT NULL,
        currency TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')),
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE(account_id, asof));
CREATE TRIGGER balance_history_insert AFTER INSERT ON accounts
            BEGIN
                INSERT INTO balance_history (account_id, asof, balance, currency, side)
                VALUES (NEW.id, NEW.asof, NEW.balance, NEW.currency, NEW.side)
                ON CONFLICT(account_id, asof) DO UPDATE SET balance=excluded.balance,
                    currency=excluded.currency, side=excluded.side, recorded_at=excluded.recorded_at;
            END;
CREATE TRIGGER balance_history_update AFTER UPDATE OF asof, balance, currency, side ON accounts
            WHEN OLD.asof <> NEW.asof OR OLD.balance <> NEW.balance
              OR OLD.currency <> NEW.currency OR OLD.side <> NEW.side
            BEGIN
                INSERT INTO balance_history (account_id, asof, balance, currency, side)
                VALUES (NEW.id, NEW.asof, NEW.balance, NEW.currency, NEW.side)
                ON CONFLICT(account_id, asof) DO UPDATE SET balance=excluded.balance,
                    currency=excluded.currency, side=excluded.side, recorded_at=excluded.recorded_at;
            END;
CREATE TRIGGER balance_history_delete AFTER DELETE ON accounts
        BEGIN DELETE FROM balance_history WHERE account_id=OLD.id; END;
CREATE TABLE projection_assumptions (
        view TEXT NOT NULL, currency TEXT NOT NULL, assumptions TEXT NOT NULL,
        PRIMARY KEY(view, currency));
CREATE TABLE account_transactions (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        tx_id TEXT NOT NULL, source TEXT NOT NULL,
        posted_date TEXT, pending_date TEXT, amount TEXT NOT NULL, currency TEXT NOT NULL,
        description TEXT NOT NULL, payee TEXT NOT NULL, merchant TEXT NOT NULL,
        pending INTEGER NOT NULL CHECK(pending IN (0,1)),
        pending_active INTEGER NOT NULL CHECK(pending_active IN (0,1)),
        category TEXT NOT NULL DEFAULT '', category_manual INTEGER NOT NULL DEFAULT 0,
        is_transfer INTEGER NOT NULL DEFAULT 0, transfer_manual INTEGER NOT NULL DEFAULT 0,
        CHECK((pending=1 AND posted_date IS NULL) OR (pending=0 AND posted_date IS NOT NULL)),
        UNIQUE(account_id, tx_id));
CREATE INDEX account_transactions_posted
                  ON account_transactions(posted_date);
CREATE INDEX account_transactions_pending
                  ON account_transactions(pending_active, pending_date);
CREATE TABLE transaction_coverage (
        source TEXT PRIMARY KEY, account_id INTEGER NOT NULL, currency TEXT NOT NULL,
        start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, revision INTEGER NOT NULL,
        CHECK(start_at < end_at));
CREATE TABLE transaction_merchant_rules (
        account_id INTEGER NOT NULL, source TEXT NOT NULL, currency TEXT NOT NULL,
        merchant TEXT NOT NULL, category TEXT NOT NULL,
        is_transfer INTEGER CHECK(is_transfer IN (0,1)), clean_key TEXT,
        PRIMARY KEY(account_id, source, currency, merchant));
CREATE TABLE transaction_categories (
        name TEXT PRIMARY KEY COLLATE NOCASE, builtin INTEGER NOT NULL DEFAULT 0,
        sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE classifier_cache (
        clean_key TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT '', emoji TEXT NOT NULL DEFAULT '',
        confidence REAL, status TEXT NOT NULL CHECK(status IN ('suggested','review','pending')),
        model TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0, next_try_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE classifier_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER account_transactions_delete
        AFTER DELETE ON accounts BEGIN
            DELETE FROM account_transactions WHERE account_id=OLD.id;
            DELETE FROM transaction_coverage WHERE account_id=OLD.id;
            DELETE FROM transaction_merchant_rules WHERE account_id=OLD.id;
        END;
"""
TODAY = str(date.today())


def parse(raw, kind="Student loan", side="debt", currency="USD", places=2):
    return parse_terms({"terms": "1", **raw}, currency, side, places, section_for, kind)


class Parsing(unittest.TestCase):
    def test_debt_fields_normalise_to_exact_decimal_text(self):
        got = parse({"t_debt": "1", "t_rate": "4.50", "t_kind": "variable", "t_promo_rate": "0", "t_promo_end": "2027-04",
                     "t_min": "25.00", "t_planned": "100"})
        self.assertEqual(got, dict(rate="4.50", rate_kind="variable", promo_rate="0", promo_end="2027-04",
                                   min_payment="25.00", planned_payment="100", paid_in_full=None))

    def test_blank_rate_is_rate_missing_not_zero_and_zero_payment_is_blank(self):
        got = parse({"t_debt": "1", "t_rate": "", "t_min": "0.00", "t_planned": ""})
        self.assertEqual((got["rate"], got["min_payment"], got["planned_payment"]), ("", "", ""))

    def test_rejections(self):
        for raw, text in (({"t_rate": "100.001"}, "Interest rate"), ({"t_rate": "-1"}, "Interest rate"),
                          ({"t_rate": "4,5"}, "Interest rate"), ({"t_rate": "1e2"}, "Interest rate"),
                          ({"t_kind": "floating"}, "Rate type"),
                          ({"t_promo_rate": "0"}, "both the promotional rate and the month"),
                          ({"t_promo_end": "2027-13", "t_promo_rate": "0", "t_rate": "9"}, "Promotional rate ends"),
                          ({"t_promo_rate": "0", "t_promo_end": "2027-04"}, "regular rate"),
                          ({"t_min": "1.234"}, "Minimum payment"), ({"t_planned": "-5"}, "Planned payment"),
                          ({"t_min": "50", "t_planned": "40"}, "below the minimum")):
            with self.assertRaisesRegex(ValueError, text, msg=raw):
                parse({"t_debt": "1", **raw})

    def test_zero_decimal_currency_rejects_cents(self):
        with self.assertRaises(ValueError):
            parse({"t_debt": "1", "t_planned": "1000.5"}, currency="KRW", places=0)
        self.assertEqual(parse({"t_debt": "1", "t_planned": "1000"}, currency="KRW", places=0)["planned_payment"], "1000")

    def test_paid_in_full_follows_the_account_type_and_is_stored_only_when_it_diverges(self):
        card, loan = ("Credit card", "debt"), ("Student loan", "debt")
        self.assertIsNone(parse({"t_debt": "1", "t_pif": "1"}, kind=card[0])["paid_in_full"])      # default on
        self.assertEqual(parse({"t_debt": "1"}, kind=card[0])["paid_in_full"], 0)                  # user turned it off
        self.assertIsNone(parse({"t_debt": "1"}, kind=loan[0])["paid_in_full"])                    # default off
        self.assertEqual(parse({"t_debt": "1", "t_pif": "1"}, kind=loan[0])["paid_in_full"], 1)
        self.assertTrue(is_paid_in_full(None, "Credit card", "debt", section_for))
        self.assertFalse(is_paid_in_full(None, "Student loan", "debt", section_for))
        self.assertFalse(is_paid_in_full(None, "Credit card", "asset", section_for))               # credit balance is an asset
        self.assertFalse(is_paid_in_full(Terms(paid_in_full=False), "Credit card", "debt", section_for))

    def test_asset_accounts_take_only_an_apy_and_ignore_debt_fields(self):
        got = parse({"t_rate": "4.000", "t_kind": "fixed", "t_planned": "99", "t_pif": "1"}, kind="Savings", side="asset")
        self.assertEqual(got, dict(rate="4.000", rate_kind="fixed"))

    def test_an_omitted_field_is_never_cleared(self):
        self.assertEqual(parse({"t_rate": "5"}, kind="Savings", side="asset"), dict(rate="5"))


class Pages(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp).resolve() / "fictional.sqlite3"
        init_db(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            for nick, kind, side, balance in (("Cedar Card", "Credit card", "debt", "400.00"),
                                              ("Birch Loan", "Student loan", "debt", "1000.00"),
                                              ("Maple Savings", "Savings", "asset", "10000.00"),
                                              ("Oak Checking", "Checking", "asset", "50.00")):
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank',?,?,?,'USD',?,?,'Joint')""", (nick, kind, side, balance, TODAY))
        self.ids = {r[1]: r[0] for r in sqlite3.connect(self.path).execute("SELECT id, nickname FROM accounts")}

    def terms(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT * FROM account_terms ORDER BY account_id").fetchall()

    def form(self, nickname, **extra):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM accounts WHERE nickname=?", (nickname,)).fetchone()
        base = dict(inline="1", view="Household", nickname=r["nickname"], institution=r["institution"], kind=r["kind"],
                    owner=r["owner"], side=r["side"], currency=r["currency"], balance=r["balance"], asof=r["asof"],
                    update_method=r["update_method"])
        return r["id"], dict(base, **extra)

    def row(self, html, nickname):
        """The visible account row (not its drawer)."""
        at = html.index(f'<span class="acct-name">{nickname}</span>')
        return html[html.rindex('<tr class="acct-row', 0, at):html.index('<tr class="edit-drawer"', at)]

    def drawer(self, html, nickname):
        rid = self.ids[nickname]
        return re.search(rf'<tr class="edit-drawer" id="edit-row-{rid}".*?</tr>', html, re.S)[0]

    def test_new_card_is_paid_in_full_and_a_loan_is_not_with_no_row(self):
        with running(self.path) as request:
            html = request("/")[1]
        self.assertRegex(self.drawer(html, "Cedar Card"), r'name="t_pif" value="1" checked')
        self.assertNotRegex(self.drawer(html, "Birch Loan"), r'name="t_pif" value="1" checked')
        self.assertEqual(self.terms(), [])
        card_row = self.row(html, "Cedar Card")
        self.assertIn("paid in full", card_row)
        self.assertNotIn("rate missing", card_row)

    def test_toggling_paid_in_full_persists_and_toggling_back_restores_the_default(self):
        with running(self.path) as request:
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            rid, data = self.form("Cedar Card", token=token, terms="1", t_debt="1", t_rate="22.990", t_kind="fixed",
                                  t_promo_rate="", t_promo_end="", t_min="25.00", t_planned="")
            self.assertEqual(request(f"/edit/{rid}", data)[0], 303)            # box unchecked: a carried balance
            self.assertEqual(self.terms()[0][7], 0)
            html = request("/")[1]
            self.assertNotRegex(self.drawer(html, "Cedar Card"), r'name="t_pif" value="1" checked')
            self.assertNotIn("paid in full", self.row(html, "Cedar Card"))
            self.assertEqual(request(f"/edit/{rid}", dict(data, t_pif="1"))[0], 303)
            self.assertIsNone(self.terms()[0][7])
            self.assertIn("22.990", self.drawer(request("/")[1], "Cedar Card"))

    def test_rate_missing_flags_then_a_rate_removes_them_and_checking_is_never_flagged(self):
        with running(self.path) as request:
            html = request("/")[1]
            self.assertIn("rate missing", self.row(html, "Birch Loan"))
            self.assertIn("payment missing", self.row(html, "Birch Loan"))
            self.assertIn("rate missing", self.row(html, "Maple Savings"))
            self.assertNotIn("rate missing", self.row(html, "Oak Checking"))
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            rid, data = self.form("Birch Loan", token=token, terms="1", t_debt="1", t_rate="4.500", t_kind="variable",
                                  t_min="25.00", t_planned="100.00", t_promo_rate="", t_promo_end="")
            self.assertEqual(request(f"/edit/{rid}", data)[0], 303)
            sid, sdata = self.form("Maple Savings", token=token, terms="1", t_rate="4.000", t_kind="fixed")
            self.assertEqual(request(f"/edit/{sid}", sdata)[0], 303)
            html = request("/")[1]
            for nick in ("Birch Loan", "Maple Savings"):
                self.assertNotIn("rate missing", self.row(html, nick))
            self.assertNotIn("payment missing", self.row(html, "Birch Loan"))
            self.assertIn("assumes today", self.drawer(html, "Birch Loan"))
        got = {r[0]: r[1:] for r in self.terms()}
        self.assertEqual(got[rid], ("4.500", "variable", "", "", "25.00", "100.00", None))
        self.assertEqual(got[sid][0], "4.000")

    def test_validation_error_keeps_the_drawer_open_with_values_and_saves_nothing(self):
        with running(self.path) as request:
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            rid, data = self.form("Birch Loan", token=token, balance="999.00", terms="1", t_debt="1", t_rate="abc",
                                  t_kind="fixed", t_min="", t_planned="")
            status, html = request(f"/edit/{rid}", data)
            self.assertEqual(status, 422)
            drawer = self.drawer(html, "Birch Loan")
            self.assertNotIn(" hidden", drawer.split(">")[0])
            self.assertIn("Interest rate (APR)", html)
            self.assertIn('value="abc"', drawer)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT balance FROM accounts WHERE id=?", (rid,)).fetchone(), ("1000.00",))
            self.assertEqual(self.terms(), [])

    def test_other_edits_and_the_plain_form_never_touch_terms(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            save_terms(db, self.ids["Birch Loan"], dict(rate="4.500", min_payment="25.00"))
        before = self.terms()
        with running(self.path) as request:
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            rid = self.ids["Birch Loan"]
            for path, data in ((f"/rename/{rid}", dict(nickname="Renamed")), (f"/section/{rid}", dict(section="Loans")),
                               (f"/side/{rid}", dict(side="asset")), (f"/owner/{rid}", dict(owner="Young")),
                               (f"/exclude/{rid}", dict(excluded="1"))):
                self.assertEqual(request(path, dict(data, token=token, view="Household"))[0], 303, path)
            _, plain = self.form("Renamed", token=token, inline="")
            plain.pop("inline")
            self.assertEqual(request(f"/edit/{rid}", plain)[0], 303)        # no terms=1 marker: terms untouched
            self.assertEqual(self.terms(), before)

    def test_removing_an_account_removes_its_terms(self):
        rid = self.ids["Birch Loan"]
        with closing(sqlite3.connect(self.path)) as db, db:
            save_terms(db, rid, dict(rate="4.500"))
            save_terms(db, self.ids["Cedar Card"], dict(rate="9"))
        with running(self.path) as request:
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            self.assertEqual(request(f"/remove/{rid}", dict(token=token, view="Household"))[0], 303)
        self.assertEqual([r[0] for r in self.terms()], [self.ids["Cedar Card"]])

    def test_load_returns_exact_decimals(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            save_terms(db, 1, dict(rate="4.500", promo_rate="0", promo_end="2027-04", planned_payment="100.00"))
        with closing(sqlite3.connect(self.path)) as db:
            t = load_terms(db)[1]
        self.assertEqual((t.rate, t.promo_rate, t.payment, t.paid_in_full), (Decimal("4.5"), Decimal(0), Decimal("100.00"), None))


class Migration(unittest.TestCase):
    TABLES = ("accounts", "source_links", "balance_history", "projection_assumptions", "account_transactions",
              "transaction_coverage", "transaction_merchant_rules", "classifier_cache", "classifier_settings")

    def test_literal_pre_l23_database_opens_twice_unchanged_and_creates_account_terms_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "pre-l23.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(PRE_L23_DDL)
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank','Loan','Student loan','debt','USD','1000.00','2026-11-01','Young')""")
                db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES ('s',1,0,1,'','ok')")
                db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,
                    description,payee,merchant,pending,pending_active,category,category_manual)
                    VALUES (1,'t1','s','2026-11-03','50.00','USD','Payment','','payment',0,0,'',0)""")
                db.execute("INSERT INTO transaction_coverage VALUES ('s',1,'USD',1,2,1)")
                db.execute("""INSERT INTO transaction_merchant_rules (account_id,source,currency,merchant,category,is_transfer,clean_key)
                    VALUES (1,'s','USD','payment','Other',NULL,'payment')""")
                db.execute("INSERT INTO projection_assumptions VALUES ('Household','USD','{\"years\": \"10\"}')")
                db.execute("INSERT INTO classifier_cache(clean_key,category,emoji,confidence,status) VALUES ('payment','Other','📦',0.9,'suggested')")
                db.execute("INSERT INTO classifier_settings VALUES ('enabled','0')")
            with closing(sqlite3.connect(path)) as db:
                self.assertNotIn("account_terms", [r[0] for r in db.execute("SELECT name FROM sqlite_master")])
                cols = {t: ",".join(r[1] for r in db.execute(f"PRAGMA table_info({t})")) for t in self.TABLES}
                before = {t: db.execute(f"SELECT {cols[t]} FROM {t} ORDER BY 1,2").fetchall() for t in self.TABLES}
            init_db(path)
            init_db(path)
            with closing(sqlite3.connect(path)) as db:
                # Compare the pre-existing columns only: later parts add columns (e.g. L26 owner_override) additively.
                after = {t: db.execute(f"SELECT {cols[t]} FROM {t} ORDER BY 1,2").fetchall() for t in self.TABLES}
                self.assertEqual(after, before)
                names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'account_terms%'")]
                self.assertEqual(sorted(names), ["account_terms", "account_terms_delete"])
                self.assertEqual(db.execute("SELECT count(*) FROM account_terms").fetchone(), (0,))
                # Existing delete triggers are untouched and the new one removes terms with the account.
                save_terms(db, 1, dict(rate="4.5"))
                db.execute("DELETE FROM accounts WHERE id=1")
                self.assertEqual(db.execute("SELECT count(*) FROM account_terms").fetchone(), (0,))
                self.assertEqual(db.execute("SELECT count(*) FROM account_transactions").fetchone(), (0,))

    def test_install_terms_is_idempotent_on_a_current_database(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
        install_terms(db)
        install_terms(db)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'account_terms%'").fetchone(), (2,))


if __name__ == "__main__":
    unittest.main()

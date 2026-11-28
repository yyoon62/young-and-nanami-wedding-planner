"""L26 part 1a: per-transaction owner override (column, effective-owner scope, migration, sync survival)."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

import server
import tx_owners
from server import init_db
from transactions import edit_transaction, monthly_snapshot
from tx_fixture import Fixture, tx
from tx_owners import apply_owner, effective, stored_override, toggle_person

# The literal pre-L26 transactions DDL (no owner_override); everything else is created by init_db.
PRE_L26_TX_DDL = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, nickname TEXT NOT NULL,
    kind TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('asset','debt')), currency TEXT NOT NULL,
    balance TEXT NOT NULL, asof TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'Unspecified',
    update_method TEXT NOT NULL DEFAULT 'Manual entry', excluded INTEGER NOT NULL DEFAULT 0,
    side_manual INTEGER NOT NULL DEFAULT 0, logo_domain TEXT NOT NULL DEFAULT '');
CREATE TABLE source_links (source TEXT PRIMARY KEY, local INTEGER NOT NULL UNIQUE, stamp INTEGER NOT NULL,
    sign INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
    checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M UTC','now')));
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
"""


class Pure(unittest.TestCase):
    def test_toggle_person_equals_the_accounts_page_for_every_owner_and_person(self):
        for current in (*server.OWNERS, "Nobody", ""):
            for person in server.PEOPLE:
                with self.subTest(current=current, person=person):
                    self.assertEqual(toggle_person(current, person), server.toggle_person(current, person))
        self.assertEqual(tx_owners.PEOPLE, server.PEOPLE)

    def test_stored_override_clears_equal_to_default_and_unspecified(self):
        self.assertIsNone(stored_override("Young", "Young"))
        self.assertIsNone(stored_override("Joint", "Joint"))
        self.assertIsNone(stored_override("Young", "Unspecified"))
        self.assertIsNone(stored_override("Unspecified", "Unspecified"))
        self.assertEqual(stored_override("Young", "Nanami"), "Nanami")
        self.assertEqual(stored_override("Unspecified", "Young"), "Young")
        self.assertEqual(stored_override("Young", "Joint"), "Joint")
        self.assertEqual(effective(None, "Young"), "Young")
        self.assertEqual(effective("Nanami", "Young"), "Nanami")


class Owners(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.joint = self.account("Joint card", "Credit card", owner="Joint")
        self.young = self.account("Young bank", owner="Young")
        self.nan = self.account("Nanami bank", owner="Nanami")
        self.unspec = self.account("Unspecified bank", owner="Unspecified")
        self.add("Joint card", tx("fuel", "2026-12-12", "-41.30", "Harbor Fuel"),
                 tx("noodle", "2026-12-13", "-10.00", "Noodle House"))
        self.add("Young bank", tx("pay", "2026-12-12", "3000.00", "Payroll Fictional"),
                 tx("gym", "2026-12-13", "-20.00", "Fictional Gym"))
        self.add("Nanami bank", tx("book", "2026-12-13", "-5.00", "Page Turner"))
        self.add("Unspecified bank", tx("misc", "2026-12-13", "-7.00", "Mystery Shop"))
        self.sync()

    def click(self, description, person, view="Household", month="2026-12"):
        row = self.row(description, month)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.assertTrue(edit_transaction(self.db, row["row_id"], view, month, action="owner", owner=person))
        return row["row_id"]

    def reset(self, description, view="Household", month="2026-12"):
        row = self.row(description, month)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.assertTrue(edit_transaction(self.db, row["row_id"], view, month, action="owner_reset"))

    def spending(self, view, month="2026-12"):
        return self.snap(month, view, household_matching=True).totals["USD"].spending

    def stored(self, description):
        return self.db.execute("SELECT owner_override FROM account_transactions WHERE description=?",
                               (description,)).fetchone()[0]

    def test_default_follows_the_account_and_views_are_unchanged_without_overrides(self):
        for description, owner in (("Harbor Fuel", "Joint"), ("Fictional Gym", "Young"), ("Page Turner", "Nanami"),
                                   ("Mystery Shop", "Unspecified")):
            r = self.row(description, "2026-12")
            self.assertEqual((r["owner"], r["eff_owner"], r["owner_override"]), (owner, owner, None))
        self.assertEqual(self.spending("Household"), Decimal("83.30"))
        self.assertEqual(self.spending("Young"), Decimal("71.30"))      # joint card (51.30) + own gym (20)
        self.assertEqual(self.spending("Nanami"), Decimal("56.30"))     # joint card + her own 5

    def test_override_on_a_joint_card_moves_one_purchase_between_views_only(self):
        self.click("Harbor Fuel", "Young")           # Joint -> Nanami only
        self.assertEqual(self.stored("Harbor Fuel"), "Nanami")
        self.assertEqual(self.row("Harbor Fuel", "2026-12")["eff_owner"], "Nanami")
        self.assertEqual(self.spending("Young"), Decimal("30.00"))      # 71.30 - 41.30
        self.assertEqual(self.spending("Nanami"), Decimal("56.30"))     # unchanged
        self.assertEqual(self.spending("Household"), Decimal("83.30"))  # unchanged
        self.assertEqual(self.stored("Noodle House"), None)             # only that transaction
        names = {r["description"] for r in self.snap("2026-12", "Young").rows}
        self.assertNotIn("Harbor Fuel", names)
        self.assertIn("Noodle House", names)

    def test_pill_sequences_follow_the_accounts_page_and_clear_when_equal_to_default(self):
        self.click("Fictional Gym", "Nanami")        # Young -> Joint
        self.assertEqual(self.stored("Fictional Gym"), "Joint")
        self.click("Fictional Gym", "Young")         # Joint -> Nanami
        self.assertEqual(self.stored("Fictional Gym"), "Nanami")
        self.assertIn("Fictional Gym", {r["description"] for r in self.snap("2026-12", "Nanami").rows})
        self.assertNotIn("Fictional Gym", {r["description"] for r in self.snap("2026-12", "Young").rows})
        self.click("Fictional Gym", "Nanami", view="Nanami")   # only-active pill -> Unspecified -> cleared, never stored
        self.assertIsNone(self.stored("Fictional Gym"))
        self.assertEqual(self.row("Fictional Gym", "2026-12")["eff_owner"], "Young")
        # Choosing the account's own owner again via pills clears it too.
        self.click("Harbor Fuel", "Nanami")          # Joint -> Young only
        self.assertEqual(self.stored("Harbor Fuel"), "Young")
        self.click("Harbor Fuel", "Nanami")          # Young only -> Joint == account owner -> cleared
        self.assertIsNone(self.stored("Harbor Fuel"))
        # Clicking the only active pill of a non-overridden row stores nothing.
        self.click("Page Turner", "Nanami")
        self.assertIsNone(self.stored("Page Turner"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM account_transactions "
                                         "WHERE owner_override='Unspecified'").fetchone()[0], 0)

    def test_reset_and_unspecified_account(self):
        self.click("Mystery Shop", "Young")          # Unspecified -> Young
        self.assertEqual(self.stored("Mystery Shop"), "Young")
        self.assertIn("Mystery Shop", {r["description"] for r in self.snap("2026-12", "Young").rows})
        self.assertNotIn("Mystery Shop", {r["description"] for r in self.snap("2026-12", "Nanami").rows})
        self.reset("Mystery Shop")
        self.assertIsNone(self.stored("Mystery Shop"))
        self.assertNotIn("Mystery Shop", {r["description"] for r in self.snap("2026-12", "Young").rows})
        self.click("Mystery Shop", "Young")
        self.click("Mystery Shop", "Young")          # Young -> Unspecified == account owner -> cleared
        self.assertIsNone(self.stored("Mystery Shop"))

    def test_override_survives_a_resync_of_the_same_transaction_but_a_new_one_takes_the_account_owner(self):
        row_id = self.click("Harbor Fuel", "Young")
        self.assertEqual(self.stored("Harbor Fuel"), "Nanami")
        self.accounts["Joint card"]["transactions"][0].update(description="Harbor Fuel Station", amount="-41.35")
        self.add("Joint card", tx("fresh", "2026-12-14", "-3.00", "Brand New"))
        self.sync()
        r = self.row("Harbor Fuel Station", "2026-12")
        self.assertEqual((r["row_id"], r["owner_override"], r["eff_owner"], r["amount"]), (row_id, "Nanami", "Nanami", "-41.35"))
        fresh = self.row("Brand New", "2026-12")
        self.assertEqual((fresh["owner_override"], fresh["eff_owner"]), (None, "Joint"))

    def test_override_survives_pending_to_posted_with_the_same_id(self):
        self.add("Young bank", tx("same", None, "-9.01", "Pending purchase", pending=True))
        self.sync(include_pending=True)
        p = self.row("Pending purchase", "2026-12", show_pending=True)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            edit_transaction(self.db, p["row_id"], "Household", "2026-12", action="owner", owner="Nanami")
        self.assertEqual(self.stored("Pending purchase"), "Joint")
        self.accounts["Young bank"]["transactions"] = [t for t in self.accounts["Young bank"]["transactions"]
                                                       if t["id"] != "same"] + [tx("same", "2026-12-14", "-8.75", "Posted purchase")]
        self.sync()
        r = self.row("Posted purchase", "2026-12")
        self.assertEqual((r["row_id"], r["owner_override"], r["pending"]), (p["row_id"], "Joint", 0))

    def test_account_owner_change_leaves_an_overridden_row_alone_and_moves_the_rest(self):
        self.click("Fictional Gym", "Nanami")        # Young account row -> Joint (override)
        self.click("Fictional Gym", "Young")         # -> Nanami only
        self.assertEqual(self.stored("Fictional Gym"), "Nanami")
        with self.db:
            self.db.execute("UPDATE accounts SET owner='Nanami' WHERE id=?", (self.young,))
        gym, pay = self.row("Fictional Gym", "2026-12"), self.row("Payroll Fictional", "2026-12")
        self.assertEqual((gym["eff_owner"], pay["eff_owner"]), ("Nanami", "Nanami"))   # follow the account / keep override
        with self.db:
            self.db.execute("UPDATE accounts SET owner='Young' WHERE id=?", (self.young,))
        gym, pay = self.row("Fictional Gym", "2026-12"), self.row("Payroll Fictional", "2026-12")
        self.assertEqual((gym["eff_owner"], pay["eff_owner"]), ("Nanami", "Young"))    # override kept, others follow

    def test_excluded_accounts_stay_excluded_whatever_the_override(self):
        self.click("Fictional Gym", "Nanami")
        with self.db:
            self.db.execute("UPDATE accounts SET excluded=1 WHERE id=?", (self.young,))
        for view in ("Household", "Young", "Nanami"):
            self.assertNotIn("Fictional Gym", {r["description"] for r in self.snap("2026-12", view).rows})

    def test_totals_in_every_view_use_the_effective_owner_and_category_totals_agree(self):
        self.click("Harbor Fuel", "Young")           # Nanami only
        self.click("Payroll Fictional", "Nanami")    # Young -> Joint
        for view in ("Household", "Young", "Nanami"):
            t = self.snap("2026-12", view, household_matching=True).totals["USD"]
            self.assertEqual(sum(t.category_spending.values()), t.spending)
        young = self.snap("2026-12", "Young", household_matching=True).totals["USD"]
        nanami = self.snap("2026-12", "Nanami", household_matching=True).totals["USD"]
        self.assertEqual((young.credits, nanami.credits), (Decimal("3000.00"), Decimal("3000.00")))   # Joint -> both
        self.assertEqual((young.spending, nanami.spending), (Decimal("30.00"), Decimal("56.30")))

    def test_coverage_follows_overrides_so_an_overridden_row_never_shows_without_its_account(self):
        def coverage(view):
            return {c["nickname"] for c in self.snap("2026-12", view).coverage}
        self.assertNotIn("Young bank", coverage("Nanami"))
        self.click("Fictional Gym", "Nanami")        # Young -> Joint: now belongs to Nanami's view too
        self.assertIn("Young bank", coverage("Nanami"))
        row = self.row("Fictional Gym", "2026-12", view="Nanami")
        self.assertEqual(row["nickname"], "Young bank")
        self.reset("Fictional Gym")
        self.assertNotIn("Young bank", coverage("Nanami"))

    def test_row_outside_the_view_cannot_be_edited(self):
        row = self.row("Fictional Gym", "2026-12")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.assertFalse(edit_transaction(self.db, row["row_id"], "Nanami", "2026-12", action="owner", owner="Nanami"))
        self.assertIsNone(self.stored("Fictional Gym"))

    def test_invalid_owner_is_rejected(self):
        row = self.row("Fictional Gym", "2026-12")
        for bad in ("", "Joint", "Unspecified", "Someone"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    edit_transaction(self.db, row["row_id"], "Household", "2026-12", action="owner", owner=bad)
        self.assertIsNone(self.stored("Fictional Gym"))

    def test_check_constraint_refuses_unspecified_in_the_column(self):
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE account_transactions SET owner_override='Unspecified' WHERE row_id=1")

    def test_apply_owner_returns_the_new_effective_owner(self):
        row = self.row("Harbor Fuel", "2026-12")
        row["account_owner"] = row["owner"]
        with self.db:
            self.assertEqual(apply_owner(self.db, row, "Young"), "Nanami")
            row["owner_override"] = "Nanami"
            self.assertEqual(apply_owner(self.db, row, None), "Joint")


class Migration(unittest.TestCase):
    def test_literal_pre_l26_database_opens_twice_unchanged_with_one_new_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "pre-l26.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(PRE_L26_TX_DDL)
                db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner)
                    VALUES ('Fictional Bank','Checking','Checking','asset','USD','10.00','2026-11-01','Young')""")
                db.execute("INSERT INTO source_links (source,local,stamp,sign,snapshot,status) VALUES ('s',1,0,1,'','ok')")
                db.execute("""INSERT INTO account_transactions (account_id,tx_id,source,posted_date,amount,currency,
                    description,payee,merchant,pending,pending_active,category,category_manual)
                    VALUES (1,'t1','s','2026-11-03','-12.40','USD','Maple Cafe','','maple cafe',0,0,'Dining',1)""")
                db.execute("INSERT INTO transaction_coverage VALUES ('s',1,'USD',1,2,1)")
            cols = "row_id,account_id,tx_id,source,posted_date,pending_date,amount,currency,description,payee,merchant,pending,pending_active,category,category_manual,is_transfer,transfer_manual"

            def old_view():
                with closing(sqlite3.connect(path)) as db:
                    return db.execute(f"SELECT {cols} FROM account_transactions").fetchall()
            before = old_view()
            init_db(path)
            init_db(path)
            self.assertEqual(old_view(), before)
            with closing(sqlite3.connect(path)) as db:
                info = [r for r in db.execute("PRAGMA table_info(account_transactions)") if r[1] == "owner_override"]
                self.assertEqual(len(info), 1)
                self.assertEqual(db.execute("SELECT owner_override FROM account_transactions").fetchone(), (None,))
                with self.assertRaises(sqlite3.IntegrityError), db:
                    db.execute("UPDATE account_transactions SET owner_override='Unspecified'")
                with db:
                    db.execute("UPDATE account_transactions SET owner_override='Nanami'")
            init_db(path)                      # reopening keeps a stored override
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT owner_override FROM account_transactions").fetchone(), ("Nanami",))
                snap = monthly_snapshot(db, "2026-11", "Nanami")
                self.assertEqual([r["eff_owner"] for r in snap.rows], ["Nanami"])
                self.assertEqual(monthly_snapshot(db, "2026-11", "Young").rows, [])


if __name__ == "__main__":
    unittest.main()

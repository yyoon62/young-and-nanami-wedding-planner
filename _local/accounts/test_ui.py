"""Presentation-level regressions: scannable dated rows, nothing silent, no JS added.

Synthetic fictional data only; temporary database. Backend is unchanged; these
assert markup contracts that keep keyboard/screen-reader/no-overflow behaviour.
"""
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bulk import Batch
from bulk_ui import display, signed_amount
from demo_batch import Demo, seed
from server import OWNERS, init_db, validate


class _Server:
    def __init__(self, db, batch):
        self.db, self.batch, self.token = db, batch, "test-token"


class ReviewPresentation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.db"
        init_db(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            seed(db)
        self.batch = Batch(self.path, Demo(), validate)
        self.batch.run("discover", {})
        self.server = _Server(self.path, self.batch)

    def render(self, **kw):
        return display(self.server, OWNERS, **kw)

    def test_scannable_list_signed_amounts_dates_native_disclosure_nothing_silent(self):
        html = self.render()
        self.assertIn("Review 12 balances", html)
        self.assertIn('class="review-list"', html)
        self.assertNotIn("<fieldset", html)  # not a wall of fieldsets
        self.assertEqual(len(re.findall(r'<li class="review-row"', html)), 12)
        # Cheapest accessible disclosure: native <details>/<summary>, no scripts.
        self.assertIn("<details", html)
        self.assertIn("<summary>", html)
        self.assertNotIn("<script", html)
        # signed amounts + dates; explicit include/match/create/side/owner readable choices
        self.assertIn("+USD 10.10", html)
        self.assertIn("-USD 20.01", html)  # negative card shown signed, not hidden
        self.assertIn("+JPY 501", html)
        self.assertRegex(html, r"as of 20\d\d-\d\d-\d\d")
        for label in ("Include", "Destination", "Count signed amount as",
                      "Create new account", "Confirm sign…", "Asset", "Debt",
                      "Owner if creating", "Default owner for new accounts"):
            self.assertIn(label, html)
        # nothing pre-selected: no silent selection, sign, link or refresh
        self.assertEqual(len(re.findall(r'name="s\d+" checked', html)), 0)
        self.assertEqual(len(re.findall(r'name="[mdso]\d+"[^>]*selected', html)), 0)
        self.assertNotRegex(html, r'<input[^>]*\schecked')
        # explicit select-all exists and does not apply
        self.assertIn('formaction="/batch/select-all"', html)
        self.assertIn("Select all review rows", html)
        self.assertIn("Unchecked rows are left out", html)
        self.assertIn('role="alert"', self.render(error="Confirm asset or debt"))  # cheapest counterexample state

    def test_select_all_then_exclusion_preserves_other_choices(self):
        prev = {"review": self.batch.review[0], "owner": "Young", "d1": "asset", "m1": "new", "s1": "on"}
        html = self.render(previous=prev, select_all=True)
        self.assertIn('name="owner"', html)
        self.assertEqual(len(re.findall(r'name="s\d+" checked', html)), 12)
        # select-all keeps previously made explicit choices; user can still exclude rows
        self.assertIn('value="asset" selected', html)
        self.assertIn('value="new" selected', html)

    def test_linked_rows_selective_refresh_distinct_status_and_dates(self):
        fields = {"review": self.batch.review[0], "owner": "Young"}
        for i in range(12):
            fields[f"s{i}"] = "on"
            fields[f"m{i}"] = "1" if i == 0 else "new"
            fields[f"d{i}"] = "debt" if i == 0 else "asset"
        self.batch.run("apply", fields)
        html = self.render()
        self.assertIn('class="linked-list"', html)
        self.assertEqual(len(re.findall(r'<li class="linked-row"', html)), 12)
        self.assertEqual(len(re.findall(r'name="r\d+" checked', html)), 0)  # no silent refresh
        # distinct labeled facts: balance date vs operation status vs UTC check time
        for label in ("Balance as of", "Last operation", "Checked (UTC)"):
            self.assertIn(label, html)
        self.assertIn("Refresh selected balances", html)
        self.assertIn("/edit/", html)  # each linked row usable (selective refresh + edit)
        self.assertIn('name="token" value="test-token"', html)  # CSRF preserved

    def test_cheapest_counterexample_sign_and_selection(self):
        # Minimal: signed source shown, but sign treatment must still be confirmed.
        self.assertEqual(signed_amount("USD", "10.10"), "+USD 10.10")
        self.assertEqual(signed_amount("USD", "-20.01"), "-USD 20.01")
        self.assertEqual(signed_amount("JPY", "501.000"), "+JPY 501.000")  # no rounding
        # Cheapest counterexample: a single row, everything valid except no explicit side
        # -> nothing is applied, inventory unchanged, choices preserved.
        with self.assertRaisesRegex(Exception, "asset or debt"):
            self.batch.run("apply", {"review": self.batch.review[0], "s0": "on",
                                     "m0": "1", "d0": "", "owner": "Young"})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT balance FROM accounts").fetchone()[0], "30.00")


if __name__ == "__main__":
    unittest.main()

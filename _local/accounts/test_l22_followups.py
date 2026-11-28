"""L23 slice 0: Income category, payroll default, emoji on every row, suggestions above the chart, privacy details."""
from contextlib import closing
from datetime import date
import json
import re
import sqlite3
import tempfile
from pathlib import Path
import unittest

import server
import tx_classifier as C
from projections_ui import projection_get
from transactions_ui import render_transactions
from tx_categories import (BUILTIN, CUSTOM_EMOJI, DEFAULT_EMOJI, TRANSFER_EMOJI, UNCATEGORIZED_EMOJI,
                           builtin_categories, default_emoji, list_categories, resolve)
from tx_fixture import Fixture, TOKEN, VIEWS, cover, put_row, source_of

TODAY = date(2026, 11, 28)


def page(content, current=None, heading=""):
    return server.page(content, current=current, heading=heading)


class Income(unittest.TestCase):
    ROW = dict(category="", category_manual=0, account_id=1, source="s", currency="USD", merchant="payroll fictional",
               description="PAYROLL FICTIONAL ROBOTICS", payee="", amount="3000.00")
    CANON = {"income": "Income", "groceries": "Groceries"}

    def test_positive_payroll_is_income_by_default_but_manual_rule_stamp_win_and_cache_review_loses(self):
        review = {"payroll fictional": ("", "review", "")}
        self.assertEqual(resolve(self.ROW, ({}, {}), review, self.CANON), ("Income", "default"))
        self.assertEqual(resolve(self.ROW, ({}, {}), {}, self.CANON), ("Income", "default"))
        rules = ({(1, "s", "USD", "payroll fictional"): "Groceries"}, {})
        self.assertEqual(resolve(self.ROW, rules, review, self.CANON), ("Groceries", "rule"))
        self.assertEqual(resolve(dict(self.ROW, category="Groceries", category_manual=1), ({}, {}), review, self.CANON),
                         ("Groceries", "manual"))
        self.assertEqual(resolve(dict(self.ROW, category="Groceries"), ({}, {}), review, self.CANON),
                         ("Groceries", "stamp"))

    def test_only_positive_payroll_wording_counts(self):
        for row in (dict(self.ROW, amount="-3000.00"), dict(self.ROW, amount="0"), dict(self.ROW, amount="x"),
                    dict(self.ROW, description="Maple Cafe", merchant="maple cafe")):
            self.assertNotEqual(resolve(row, ({}, {}), {}, self.CANON)[1], "default", row)
        self.assertEqual(resolve(dict(self.ROW, description="ACH Direct Deposit ACME"), ({}, {}), {}, self.CANON)[0],
                         "Income")

    def test_income_is_builtin_listed_but_never_offered_to_or_accepted_from_the_classifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "a.sqlite3"
            server.init_db(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertIn("Income", list_categories(db))
                self.assertEqual(db.execute("SELECT builtin FROM transaction_categories WHERE name='Income'").fetchone(), (1,))
                self.assertEqual(builtin_categories(db), list(BUILTIN))
                allowed = builtin_categories(db)
        reply = json.dumps({"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": C.TOOL, "input": {
            "results": [dict(id="m1", category="Income", emoji="💵", confidence=0.99)]}}]})
        verdict = C.parse_reply(reply, {"m1": "acme"}, allowed)
        good = reply.replace("Income", "Groceries")
        self.assertEqual(C.parse_reply(good, {"m1": "acme"}, allowed).accepted, {"m1": ("Groceries", "💵", 0.99)})
        self.assertEqual((verdict.accepted, verdict.review), ({}, {"m1": None}))

    def test_a_custom_income_from_before_is_promoted_in_place_and_reopen_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "a.sqlite3"
            server.init_db(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DELETE FROM transaction_categories WHERE name='Income'")
                db.execute("INSERT INTO transaction_categories(name, builtin, sort) VALUES ('income', 0, 1000)")
            server.init_db(path)
            server.init_db(path)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT name, builtin FROM transaction_categories WHERE name='Income'").fetchall()
                self.assertEqual(rows, [("Income", 1)])
                self.assertNotIn("Income", [n for n in builtin_categories(db)])


class EmojiAndPage(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.chk = self.account("Checking")
        self.sav = self.account("Savings", "Savings")
        self.src = source_of(self.db, self.chk)
        for day, amount, desc in (("2026-11-01", "3000.00", "PAYROLL FICTIONAL ROBOTICS"), ("2026-11-03", "-12.40", "Maple Cafe"),
                                  ("2026-11-04", "-5.00", "Totally Unknown Vendor")):
            put_row(self.db, self.chk, self.src, "USD", day, amount, desc)
        put_row(self.db, self.chk, self.src, "USD", "2026-11-05", "-50.00", "Move", transfer=True)
        put_row(self.db, self.sav, source_of(self.db, self.sav), "USD", "2026-11-05", "50.00", "Move in", transfer=True)
        cover(self.db, "2026-08-01", "2026-11-29")
        with self.db:
            self.db.execute("UPDATE account_transactions SET category='Dining', category_manual=1 WHERE description='Maple Cafe'")

    def html(self, query="month=2026-11"):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=TODAY)

    def test_every_row_shows_an_emoji_and_defaults_are_single_graphemes(self):
        for value in (*DEFAULT_EMOJI.values(), TRANSFER_EMOJI, UNCATEGORIZED_EMOJI, CUSTOM_EMOJI):
            self.assertTrue(C.one_emoji(value), value)
        self.assertEqual({*DEFAULT_EMOJI} - {*BUILTIN}, {"Income"})
        self.assertEqual(default_emoji("dining"), DEFAULT_EMOJI["Dining"])
        self.assertEqual(default_emoji("Pets"), CUSTOM_EMOJI)
        html = self.html()
        rows = html.count('class="acct-row"')
        self.assertEqual(rows, 5)
        self.assertEqual(html.count('class="tx-emoji"'), rows)
        self.assertIn(f'tx-emoji" aria-hidden="true">{DEFAULT_EMOJI["Income"]}</span> PAYROLL', html)   # payroll -> Income
        self.assertIn("(built-in default)", html)
        self.assertNotIn("Needs review", html)
        self.assertIn(f'>{TRANSFER_EMOJI}</span> Move', html)
        self.assertIn(f'>{UNCATEGORIZED_EMOJI}</span> Totally Unknown', html)

    def test_classifier_emoji_only_shows_for_a_suggested_row_never_beside_a_user_category(self):
        with self.db:
            self.db.execute("""INSERT INTO classifier_cache(clean_key, category, emoji, confidence, status)
                VALUES ('maple cafe','Shopping','🛍️',0.9,'suggested'), ('totally unknown vendor','Shopping','🧶',0.9,'suggested')""")
        html = self.html()
        self.assertNotIn("🧶 Maple", html)                      # user category (manual) wins; default Dining emoji
        self.assertIn(f'>{DEFAULT_EMOJI["Dining"]}</span> Maple', html)
        self.assertIn(">🧶</span> Totally Unknown", html)        # effective source is "suggested"

    def test_privacy_is_one_line_with_the_full_statement_in_details(self):
        html = self.html()
        panel = html[html.index('<details class="classifier"'):]
        panel = panel[:panel.index('<h2')] if '<h2' in panel else panel
        self.assertIn("Only cleaned merchant names (no amounts, dates or accounts) are sent, once each.", panel)
        self.assertRegex(panel, r'<details class="classifier-details"><summary>Privacy details</summary>.*A bare person')
        self.assertIn("Not available when the dashboard is hosted.", panel)

    def test_projection_suggestions_directly_follow_the_chart_in_the_page(self):
        """L29 reverses the L23-0 default (suggestions above the chart): the chart now sits right under the tab bar."""
        for month in ("2026-08", "2026-09", "2026-10"):
            put_row(self.db, self.chk, self.src, "USD", f"{month}-01", "3000.00", "Payroll Fictional Co")
            put_row(self.db, self.chk, self.src, "USD", f"{month}-05", "-1000.00", "Landlord Rent")
        with self.db:
            self.db.execute("UPDATE accounts SET balance='100.00', asof='2026-11-01'")
        body = projection_get(self.path, "/projections", page=page, token="t", people=server.PEOPLE,
                              section_for=server.section_for, places=server.places, today=TODAY, suggestions=True)[1]
        self.assertLess(body.index('class="net-chart"'), body.index('id="projection-suggestion"'))
        self.assertLess(body.index('id="projection-heading"'), body.index('id="projection-suggestion"'))
        self.assertLess(body.index('id="projection-suggestion"'), body.index('class="cards projection-cards"'))
        hosted = projection_get(self.path, "/projections", page=page, token="t", people=server.PEOPLE,
                                section_for=server.section_for, places=server.places, today=TODAY, suggestions=False)[1]
        self.assertNotIn("projection-suggestion", hosted)


if __name__ == "__main__":
    unittest.main()

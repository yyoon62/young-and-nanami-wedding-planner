"""L26 part 9: transfer-like built-in categories, the card-issuer rule, classifier allowance, no double counting."""
from contextlib import closing
from decimal import Decimal
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

import tx_classifier as C
from server import init_db
from tx_categories import TRANSFER_EMOJIS, TRANSFER_LIKE, builtin_categories, default_emoji, list_categories
from tx_fixture import Fixture, TOKEN
from tx_fixture import tx
from tx_transfers import card_issuer_terms, names_card_issuer


class Model(unittest.TestCase):
    def test_four_builtin_transfer_categories_with_their_emoji_and_a_unique_emoji_each(self):
        self.assertEqual(TRANSFER_LIKE, ("Card payment", "Loan payment", "Savings & investing", "Transfer"))
        self.assertEqual([TRANSFER_EMOJIS[n] for n in TRANSFER_LIKE], ["↔️", "🏦", "📈", "🔁"])
        from tx_categories import DEFAULT_EMOJI
        values = list(DEFAULT_EMOJI.values())
        self.assertEqual(len(values), len(set(values)))               # Subscriptions no longer shares 🔁
        self.assertEqual(default_emoji("card payment"), "↔️")

    def test_migration_adds_them_once_and_promotes_an_old_custom_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "a.sqlite3"
            init_db(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DELETE FROM transaction_categories WHERE name IN (?,?,?,?)", TRANSFER_LIKE)
                db.execute("INSERT INTO transaction_categories(name, builtin, sort) VALUES ('transfer', 0, 1000)")
            init_db(path)
            init_db(path)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT name, builtin FROM transaction_categories WHERE name IN (?,?,?,?)",
                                  TRANSFER_LIKE).fetchall()
                self.assertEqual(sorted(rows), sorted((n, 1) for n in TRANSFER_LIKE))
                self.assertEqual(db.execute("SELECT count(*) FROM transaction_categories WHERE name='Transfer'").fetchone()[0], 1)
                self.assertEqual(builtin_categories(db)[-4:], list(TRANSFER_LIKE))

    def test_classifier_may_choose_them_and_its_prompt_explains_them(self):
        body = C.build_request("claude-x", list(["Dining", *TRANSFER_LIKE]), [("m1", "fidelity money line")])
        self.assertEqual(body["tools"][0]["input_schema"]["properties"]["results"]["items"]["properties"]["category"]["enum"],
                         ["Dining", *TRANSFER_LIKE])
        for words in ("Card payment", "Loan payment", "Savings & investing", "clearly", "never one of them"):
            self.assertIn(words, C.SYSTEM)

    def test_a_transfer_like_pick_needs_more_confidence_than_an_ordinary_one(self):
        def reply(category, confidence):
            return json.dumps({"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": C.TOOL, "input": {
                "results": [dict(id="m1", category=category, emoji="📈", confidence=confidence)]}}]}).encode()
        allowed = ["Shopping", *TRANSFER_LIKE]
        self.assertIn("m1", C.parse_reply(reply("Shopping", 0.75), {"m1": "x"}, allowed).accepted)
        self.assertIn("m1", C.parse_reply(reply("Savings & investing", 0.9), {"m1": "x"}, allowed).accepted)
        low = C.parse_reply(reply("Savings & investing", 0.75), {"m1": "x"}, allowed)
        self.assertNotIn("m1", low.accepted)
        self.assertIn("m1", low.review)                                # shows as Uncategorized for the user to pick

    def test_issuer_rule_needs_the_issuer_and_a_payment_hint_and_never_fires_on_a_bare_own_bank(self):
        terms = (("american express", "american express"), ("amex", "american express"),
                 ("chase", "chase"), ("cedar", "cedar credit"), ("cedar credit", "cedar credit"))
        def row(text, institution="Birch Bank", kind="Checking", amount="-100.00", **kw):
            return dict(description=text, payee="", institution=institution, kind=kind, amount=amount, pending=0, **kw)
        for text in ("AMERICAN EXPRESS ACH PMT", "AMEX EPAYMENT", "CEDAR CREDIT AUTOPAY 1234", "Cedar Credit online scheduled payment"):
            self.assertTrue(names_card_issuer(row(text), terms), text)
        for text in ("Cedar Market groceries", "Birch Bank fee", "American Express", "Cedarwood Hardware payment"):
            self.assertFalse(names_card_issuer(row(text), terms), text)
        self.assertFalse(names_card_issuer(row("CHASE FEE", institution="Chase"), terms))             # own bank, no card word
        self.assertFalse(names_card_issuer(row("CHASE AUTOPAY", institution="Chase"), terms))
        self.assertTrue(names_card_issuer(row("CHASE CREDIT CRD AUTOPAY", institution="Chase"), terms))
        self.assertFalse(names_card_issuer(row("AMEX EPAYMENT", amount="50.00"), terms))               # a credit is not a payment out
        self.assertFalse(names_card_issuer(row("AMEX EPAYMENT", kind="Credit card"), terms))
        self.assertFalse(names_card_issuer(row("AMEX EPAYMENT") | {"pending": 1}, terms))


class Totals(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.account("Card", "Credit card")
        with self.db:
            self.db.execute("UPDATE accounts SET institution='Cedar Credit' WHERE nickname='Card'")
        self.add("Bank", tx("a", "2026-11-03", "3000", "PAYROLL Fictional"), tx("b", "2026-11-04", "-1000.00", "CEDAR CREDIT AUTOPAY 4411"),
                 tx("c", "2026-11-05", "-650.00", "Zelle payment to Jane Doe"), tx("d", "2026-11-06", "-200.00", "Corner Shop"))
        self.add("Card", tx("p", "2026-11-04", "1000.00", "PAYMENT THANK YOU"), tx("x", "2026-11-02", "-400.00", "Gadget Barn"),
                 tx("y", "2026-11-03", "-600.00", "Harbor Fuel"))
        self.sync()

    def test_card_purchases_and_the_card_payment_are_counted_once(self):
        totals = self.snap().totals["USD"]
        self.assertEqual(totals.spending, Decimal("1850.00"))      # 400 + 600 on the card + 650 Zelle + 200 shop; NOT +1,000 for the payment
        self.assertEqual(totals.credits, Decimal("3000"))          # the 1,000 credit on the card is not income
        self.assertEqual(sum(totals.category_spending.values()), totals.spending)
        self.assertNotIn("Card payment", totals.category_spending)

    def test_the_issuer_debit_is_a_card_payment_by_default_and_the_credit_is_matched_not_to_confirm(self):
        debit, credit = self.row("CEDAR CREDIT AUTOPAY 4411"), self.row("PAYMENT THANK YOU")
        self.assertEqual((debit["category"], debit["category_source"], debit["effective_transfer"]),
                         ("Card payment", "default", True))
        self.assertEqual(credit["flow"], "Matched transfer")        # pair found: nothing left "to confirm"
        self.assertEqual(self.snap().totals["USD"].review_count, 0)

    def test_without_a_matching_bank_leg_the_credit_stays_to_confirm_and_still_counts_once(self):
        with self.db:
            self.db.execute("DELETE FROM account_transactions WHERE description LIKE 'CEDAR CREDIT AUTOPAY%'")
        totals = self.snap().totals["USD"]
        self.assertEqual((totals.review_count, totals.review), (1, Decimal("1000.00")))
        self.assertEqual(totals.spending, Decimal("1850.00"))
        self.assertEqual(totals.credits, Decimal("3000"))

    def test_the_user_can_override_and_zelle_stays_a_plain_person_to_person_row(self):
        row = self.row("CEDAR CREDIT AUTOPAY 4411")["row_id"]
        self.post(row, pick="Dining")                               # a manual category beats the default
        again = self.row("CEDAR CREDIT AUTOPAY 4411")
        self.assertEqual((again["category"], again["effective_transfer"]), ("Dining", False))
        self.assertEqual(self.snap().totals["USD"].spending, Decimal("2850.00"))
        zelle = self.row("Zelle payment to Jane Doe")
        self.assertEqual((zelle["category"], zelle["effective_transfer"]), ("", False))
        wanted, withheld = C.candidates(self.db, 0)
        self.assertNotIn("zelle payment to jane doe", wanted)       # never sent to the AI
        self.assertIn("zelle payment to jane doe", withheld)

    def test_manual_not_a_transfer_beats_the_default_and_a_transfer_pick_reverses_it(self):
        row = self.row("CEDAR CREDIT AUTOPAY 4411")["row_id"]
        self.post(row, action="transfer", transfer="no")
        self.assertFalse(self.row("CEDAR CREDIT AUTOPAY 4411")["effective_transfer"])
        self.post(row, pick="Loan payment")                         # a transfer-like pick clears the earlier "no"
        self.assertTrue(self.row("CEDAR CREDIT AUTOPAY 4411")["effective_transfer"])
        self.assertEqual(self.db.execute("SELECT transfer_manual FROM account_transactions WHERE row_id=?", (row,)).fetchone(), (0,))

    def test_picking_a_spending_category_on_a_matched_or_auto_transfer_says_not_a_transfer(self):
        credit = self.row("PAYMENT THANK YOU")["row_id"]
        self.assertTrue(self.row("PAYMENT THANK YOU")["effective_transfer"])      # matched pair
        self.post(credit, pick="Shopping")
        self.assertFalse(self.row("PAYMENT THANK YOU")["effective_transfer"])
        self.assertEqual(self.db.execute("SELECT transfer_manual, is_transfer FROM account_transactions WHERE row_id=?",
                                         (credit,)).fetchone(), (1, 0))

    def test_a_rule_with_a_transfer_like_category_applies_to_the_merchant(self):
        fid = self.row("Corner Shop")["row_id"]
        self.post(fid, pick="Savings & investing", apply="1")
        self.assertTrue(self.row("Corner Shop")["effective_transfer"])
        self.assertEqual(self.snap().totals["USD"].spending, Decimal("1650.00"))
        self.assertEqual(self.row("Corner Shop")["flow"], "Transfer (category)")

    def test_issuer_terms_come_from_linked_included_card_accounts_only(self):
        terms = card_issuer_terms(self.db)
        self.assertIn(("cedar", "cedar credit"), terms)
        self.assertNotIn("fictional", [t for t, _ in terms])
        with self.db:
            self.db.execute("UPDATE accounts SET excluded=1 WHERE nickname='Card'")
        self.assertEqual(card_issuer_terms(self.db), ())


if __name__ == "__main__":
    unittest.main()

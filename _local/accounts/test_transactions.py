"""Fictional on-disk SQLite + injected Bridge wire; no server or network needed."""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from bridge import Bridge, Problem
from bulk import Batch, fingerprint
from server import init_db, validate
from transactions import DAY, OVERLAP, PAGE, install_transactions, monthly_snapshot, sync_transactions
from transactions_ui import EDIT, SYNC, RouteProblem, post_transactions, render_transactions


from tx_fixture import Fixture, NOW, TOKEN, VIEWS, epoch, tx
from tx_categories import add_category


class TransactionsTest(Fixture, unittest.TestCase):
    def test_corroborated_payment_exact_currency_and_joint_views(self):
        self.account("Joint checking", owner="Joint")
        self.account("Joint card", "Credit card", owner="Joint")
        self.account("Excluded", excluded=True)
        self.account("Yen checking", currency="JPY", owner="Nanami")
        self.add("Joint checking", tx("shared-id", "2026-11-09", "-40.00", "Credit card payment"))
        self.add("Joint card", tx("shared-id", "2026-11-12", "40", "PAYMENT THANK YOU"),
                 tx("food", "2026-11-10", "-40.00", "Grocery"))
        self.add("Excluded", tx("ignored", "2026-11-12", "-100", "Excluded"))
        self.add("Yen checking", tx("local", "2026-11-09", "-40", "Credit card payment"))
        self.accounts["Provider-only account"] = self.accounts["Joint checking"] | {"id": "unlinked"}
        self.assertEqual(self.sync().windows, 4)
        self.assertTrue(all(left[1] - OVERLAP == right[0] < left[1] < right[1]
                            for left, right in zip(self.calls, self.calls[1:])))
        self.assertEqual(self.calls[-1][1], NOW)
        self.assertEqual(self.db.execute("SELECT count(*) FROM accounts").fetchone()[0], 4)
        for view in VIEWS:
            snapshot = self.snap(view=view)
            usd = snapshot.totals["USD"]
            self.assertEqual((usd.spending, usd.credits, usd.net), (40, 0, -40))
            self.assertEqual(sum(r["flow"] == "Matched transfer" for r in snapshot.rows), 2)
            self.assertNotIn("Excluded", {r["nickname"] for r in snapshot.rows})
            self.assertEqual("JPY" in snapshot.totals, view != "Young")
            if view != "Young":
                self.assertEqual(snapshot.totals["JPY"].spending, 40)

    def test_unrelated_refund_deposit_and_unmatched_card_credit(self):
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("d", "2026-11-10", "40", "Deposit"),
                 tx("p", "2026-11-08", "-40", "Credit card payment"))
        self.add("Card", tx("r", "2026-11-09", "40", "Grocery refund"),
                 tx("u", "2026-11-12", "40", "Payment received"))
        self.sync()
        t = self.snap().totals["USD"]
        self.assertEqual((t.spending, t.credits, t.net, t.review), (40, 80, 40, 40))
        self.assertEqual(self.row("Grocery refund")["flow"], "Cash flow")
        self.assertEqual(self.row("Payment received")["flow"], "Review card credit")
        self.post(self.row("Payment received")["row_id"], action="transfer", transfer="no")
        self.assertEqual(self.snap().totals["USD"].credits, 120)

    def test_utc_boundary_and_six_day_counterexample_to_month_padding(self):
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("bank", "2026-11-28", "-20", "Credit card payment"),
                 tx("before", "2026-12-01T00:59:59+01:00", "-0.0001", "Before month"),
                 tx("after", "2026-11-30T19:00:00-05:00", "-0.0002", "Start of month"))
        self.add("Card", tx("first", "2026-12-01", "20", "Payment thank you"))
        self.sync()
        self.assertEqual(self.row("Payment thank you", "2026-12")["flow"], "Matched transfer")
        self.assertEqual(self.row("Before month")["posted_date"], "2026-11-30")
        self.assertEqual(self.row("Start of month", "2026-12")["posted_date"], "2026-12-01")
        # A competing card credit lies six days before the December credit.
        # A three-day read pad returns the bank leg but misses its other edge.
        self.add("Card", tx("other", "2026-11-25", "20", "Online payment"))
        self.sync()
        self.assertEqual(self.row("Payment thank you", "2026-12")["flow"], "Matched transfer",
                         "old postings are outside the normal five-day refresh")
        # Use a fresh instrument's backfill to replay the older correction.
        self.account("New account")
        self.sync()
        self.assertEqual(self.row("Payment thank you", "2026-12")["flow"], "Review card credit")
        self.assertEqual(self.snap().totals["USD"].spending, Decimal("20.0001"))
        self.assertEqual(self.snap("2026-12").totals["USD"].spending, Decimal("0.0002"))

    def test_exact_money_larger_than_default_decimal_context(self):
        self.account("Bank")
        whole, fraction = "9" * 63, "0" * 61 + "1"
        self.add("Bank", tx("large", "2026-11-03", whole, "Large"),
                 tx("tiny", "2026-11-02", "0." + fraction, "Tiny"),
                 tx("offset", "2026-11-01", "-" + whole, "Offset"))
        self.sync()
        total = self.snap().totals["USD"]
        self.assertEqual(total.credits, Decimal(whole + "." + fraction))
        self.assertEqual(total.net, Decimal("0." + fraction))

    def test_history_partial_missing_restart_and_incremental_overlap(self):
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("old", "2026-09-16", "-1", "First included second"),
                 tx("cut", "2026-10-16", "-2", "Second page boundary"),
                 tx("end", "2026-12-15T12:00:00", "-3", "Endpoint exclusive"))
        fail = lambda data: data | {"errlist": [{"code": "act.missingdata", "msg": "<private>"}]}
        self.alter = lambda data: fail(data) if len(self.calls) == 2 else data
        with self.assertRaisesRegex(Problem, "^Incomplete transaction listing; window held\\.$"):
            self.sync()
        first, second = self.calls
        self.assertEqual(first[0], epoch("2026-09-16"))
        self.assertEqual(first[1] - OVERLAP, second[0])
        self.assertEqual({r[0] for r in self.db.execute("SELECT end_at FROM transaction_coverage")}, {first[1]})
        self.assertEqual(self.db.execute("SELECT count(*) FROM account_transactions").fetchone()[0], 1)
        self.alter = lambda data: data | {"accounts": data["accounts"][:1]}
        with self.assertRaisesRegex(Problem, "linked account was missing"):
            self.sync()
        self.assertEqual({r[0] for r in self.db.execute("SELECT end_at FROM transaction_coverage")}, {first[1]})
        self.assertEqual(self.db.execute("SELECT count(*) FROM account_transactions").fetchone()[0], 1)
        self.alter = lambda data: data
        # New object/connection, same local database.
        self.batch = Batch(self.path, self.bridge, validate)
        result = self.sync()
        self.assertTrue(result.caught_up)
        self.assertEqual(self.calls[3][0], first[1] - OVERLAP)
        self.assertEqual(self.row("Second page boundary", "2026-10")["amount"], "-2")
        self.assertEqual(len(self.snap("2026-12").rows), 0)
        before_repeat = len(self.calls)
        self.assertEqual(self.sync(now=NOW + DAY).windows, 1)
        self.assertEqual(self.calls[before_repeat], (NOW - OVERLAP, NOW + DAY, False))
        self.assertEqual(self.db.execute("SELECT count(*) FROM account_transactions").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0], len(self.calls))
        self.assertEqual(self.row("First included second", "2026-09")["amount"], "-1")

    def test_link_only_and_shared_quota_including_failed_calls(self):
        self.account("Unlinked", linked=False)
        with self.assertRaisesRegex(Problem, "No linked accounts"):
            self.sync()
        self.assertEqual(self.calls, [])
        self.account("Linked")
        self.accounts["Foreign"] = self.accounts["Linked"] | {"id": "unknown"}
        with self.db:
            self.db.executemany("INSERT INTO fetch_attempts VALUES (?)", [(time.time(),)] * 23)
        self.alter = lambda _: (403, b"not displayed")
        with self.assertRaisesRegex(Problem, "denied or revoked"):
            self.sync()
        with self.assertRaisesRegex(Problem, r"24 SimpleFIN requests.*Next request available at \d{1,2}:\d\d [AP]M\.$"):
            self.batch.sync()
        with self.assertRaisesRegex(Problem, r"24 SimpleFIN requests.*Next request available at \d{1,2}:\d\d [AP]M\.$"):
            self.sync()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM accounts").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM transaction_coverage").fetchone()[0], 0)

    def test_overlap_catches_correction_near_first_pagination_boundary(self):
        self.account("Bank")
        corrected = tx("boundary", "2026-10-15", "-1", "Boundary purchase")
        self.add("Bank", corrected)

        def respond(data):
            if len(self.calls) == 1:
                self.accounts["Bank"]["transactions"] = [corrected | {"amount": "-1.25"}]
            return data

        self.alter = respond
        self.assertTrue(self.sync().caught_up)
        self.assertEqual(self.snap("2026-10").totals["USD"].spending, Decimal("1.25"))
        self.assertEqual(len(self.snap("2026-10").rows), 1)

    def test_fail_closed_on_bad_response_and_no_provider_text_in_errors(self):
        self.account("Bank")
        self.add("Bank", tx("x", "2026-09-17", "-5", "fictional-secret-NOT-live"))
        with self.assertRaisesRegex(Problem, "^Unexpected sensitive transaction field"):
            self.sync()
        self.accounts["Bank"]["transactions"][0]["description"] = "Valid"
        for alter, message in [
            (lambda _: (200, b'{"errlist":[],"errlist":[]}'), "unreadable"),
            (lambda d: d | {"errors": ["<provider message>"]}, "warning or quota"),
            (lambda d: d | {"errlist": [{"code": "gen.api", "msg": "Requested date range exceeds limit of 60 days <secret>"}]}, "requested date range"),
            (lambda d: d | {"accounts": [], "errlist": [{"code": "gen.api", "msg": "Daily request quota <secret>"}]}, "daily request limit"),
            (lambda d: d | {"accounts": [d["accounts"][0] | {"currency": "JPY"}]}, "currency mismatch"),
            (lambda d: d | {"accounts": [d["accounts"][0] | {"transactions": None}]}, "transaction list"),
            (lambda d: d | {"accounts": [d["accounts"][0]] * 2}, "duplicate"),
            (lambda d: d | {"accounts": [d["accounts"][0] | {
                "transactions": d["accounts"][0]["transactions"] * 2}]}, "duplicate"),
        ]:
            with self.subTest(expected=message):
                self.alter = alter
                with self.assertRaisesRegex(Problem, message) as caught:
                    self.sync()
                self.assertNotIn("<provider message>", str(caught.exception))
        self.assertEqual(self.db.execute("SELECT count(*) FROM transaction_coverage").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM account_transactions").fetchone()[0], 0)

    def test_future_dated_posting_is_skipped_not_fatal(self):
        self.account("Bank")
        self.add("Bank", tx("now", "2026-11-03", "-4", "In window"),
                 tx("future", "2026-12-20", "-1", "Scheduled payment"))
        self.assertTrue(self.sync().caught_up)
        self.assertEqual([r[0] for r in self.db.execute("SELECT description FROM account_transactions")],
                         ["In window"])

    def test_pending_hidden_transition_replay_and_expiration(self):
        self.account("Bank")
        pending = tx("same", None, "-9.01", "Pending purchase", pending=True)
        self.add("Bank", pending)
        self.sync()
        self.assertFalse(any(p for _, _, p in self.calls))
        self.assertEqual(len(self.snap("2026-12", show_pending=True).rows), 0)
        self.sync(include_pending=True)
        self.assertTrue(self.calls[-1][2])
        self.assertEqual(len(self.snap("2026-12").rows), 0)
        p = self.row("Pending purchase", "2026-12", show_pending=True)
        self.assertIsNone(p["posted_date"])
        self.assertEqual(self.snap("2026-12", show_pending=True).totals, {})
        self.cat("Fictional")
        self.post(p["row_id"], action="category", category="Fictional", month="2026-12")
        posted = tx("same", "2026-12-14", "-8.75", "Posted purchase")
        self.accounts["Bank"]["transactions"] = [posted]
        self.sync()
        self.assertEqual(self.snap("2026-12").totals["USD"].spending, Decimal("8.75"))
        self.assertEqual(self.row("Posted purchase", "2026-12")["category"], "Fictional")
        self.accounts["Bank"]["transactions"] = [pending, tx("temp", None, "-1", "Short-lived", pending=True)]
        self.sync(include_pending=True)
        self.assertEqual(self.snap("2026-12", show_pending=True).totals["USD"].spending, Decimal("8.75"))
        self.assertEqual(len(self.snap("2026-12", show_pending=True).rows), 2)
        self.sync()
        self.assertEqual(len(self.snap("2026-12", show_pending=True).rows), 1)

    def test_manual_flags_and_account_scoped_rules_on_provider_updates(self):
        bank = self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("keep", "2026-12-11", "-30", "Credit card payment"),
                 tx("move", "2026-12-12", "-4", "Other transfer"),
                 tx("rule", "2026-12-12", "-1", "Vendor #1", payee="Farm Stand"),
                 tx("blank", "2026-12-12", "-2", "Vendor #2", payee="FARM  STAND"))
        self.add("Card", tx("credit", "2026-12-12", "30", "Payment thank you"),
                 tx("scoped", "2026-12-12", "-5", "Vendor #3", payee="Farm Stand"))
        self.sync()
        for desc, action, value in [("Credit card payment", "transfer", "no"),
                                    ("Other transfer", "transfer", "yes"),
                                    ("Vendor #2", "category", ""),
                                    ("Vendor #1", "rule", "Groceries")]:
            rid = self.row(desc, "2026-12")["row_id"]
            self.post(rid, action=action, month="2026-12",
                      **{"transfer" if action == "transfer" else "category": value})
        for row in self.accounts["Bank"]["transactions"][:2]:
            row["amount"] = "-30.0" if row["id"] == "keep" else "-4.1"
        self.add("Bank", tx("later", "2026-12-14", "-7", "Vendor #4", payee="farm stand"))
        self.sync()
        keep = self.row("Credit card payment", "2026-12")
        move = self.row("Other transfer", "2026-12")
        self.assertEqual((keep["transfer_manual"], keep["is_transfer"], keep["amount"]), (1, 0, "-30.0"))
        self.assertEqual((move["transfer_manual"], move["is_transfer"], move["amount"]), (1, 1, "-4.1"))
        self.assertEqual(self.row("Vendor #1", "2026-12")["category"], "Groceries")
        self.assertEqual(self.row("Vendor #2", "2026-12")["category"], "")
        self.assertEqual(self.row("Vendor #3", "2026-12")["category"], "")
        self.assertEqual(self.row("Vendor #4", "2026-12")["category"], "Groceries")
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE id=?", (bank,))
        for table in ("account_transactions", "transaction_coverage", "transaction_merchant_rules"):
            self.assertEqual(self.db.execute(f"SELECT count(*) FROM {table} WHERE account_id=?", (bank,))
                             .fetchone()[0], 0)

    def test_render_filters_zero_coverage_escaping_and_authorized_post(self):
        self.account('Bank"><svg onload=alert(1)>')
        self.account("Unlinked", currency="GBP", linked=False)
        desc = "<script>alert(1)</script>"
        self.add('Bank"><svg onload=alert(1)>', tx("x", "2026-11-11", "-10.123", desc,
                                                 payee='"><img src=x onerror=alert(2)>'))
        html = lambda query: render_transactions(self.db, query, allowed_views=VIEWS,
                                                 token=TOKEN, can_sync=True, today=date(2026, 11, 20))
        self.assertIn("No posted data with sufficient coverage", html(""))
        self.sync()
        row = self.row(desc)["row_id"]
        self.cat('"><b>Groceries</b>')
        self.post(row, action="category", category='"><b>Groceries</b>')
        rendered = html("")
        for raw in ("<script>", "<svg onload", "<img", "<b>Groceries", "style=", "https://"):
            self.assertNotIn(raw, rendered)
        for safe in ("&lt;script&gt;", "&lt;svg", "&lt;img", "&lt;b&gt;Groceries", "-10.123",
                     "Not linked; transaction data unavailable", TOKEN, 'action="/transactions/edit"'):
            self.assertIn(safe, rendered)
        self.assertNotIn("&lt;script&gt;", html("q=no-such-merchant"))
        self.assertIn("Net overspent <strong>$10.12</strong>", html("q=no-such-merchant"))  # list filter, not totals
        self.assertIn("No posted transactions reported", html("month=2026-10"))
        self.assertIn("No posted data with sufficient coverage", html("month=2024-10"))
        for expected, fields in [(403, {"token": "incorrect"}), (404, {"view": "Nanami"}),
                                 (404, {"month": "2026-12"}), (400, {"view": "Joint"}),
                                 (400, {"month": "2026-00"}), (422, {"category": "\x00"}), (422, {"category": "Not in the list"})]:
            with self.subTest(fields=fields), self.assertRaises(RouteProblem) as caught:
                self.post(row, **(dict(action="category", category="Dining") | fields))
            self.assertEqual(caught.exception.status, expected)
        with self.assertRaises(RouteProblem) as caught:
            self.post(row, action="category", category="Dining", allowed_views={"Young"})
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(RouteProblem) as caught:
            render_transactions(self.db, "view=Nanami", token=TOKEN, allowed_views={"Young"})
        self.assertEqual(caught.exception.status, 403)
        for body in (b"token=a&token=b", b"q=%FF", b"q=%XY", b"token=\xff", b"unknown=1"):
            with self.assertRaises(RouteProblem) as caught:
                post_transactions(self.db, EDIT, body, token=TOKEN, allowed_views=VIEWS)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.row(desc)["category"], '"><b>Groceries</b>')
        with self.assertRaises(RouteProblem) as caught:
            self.post(path=SYNC, allowed_views={"Young"}, view="Young")
        self.assertEqual(caught.exception.status, 403)
        self.assertTrue(self.post(path=SYNC, include_pending="1").include_pending)
        location = self.post(row, action="transfer", transfer="yes", q="x&view=Nanami").location
        self.assertEqual(parse_qs(urlsplit(location).query)["view"], ["Household"])

    def test_category_breakdown_agrees_with_totals_and_ignores_list_filters(self):
        self.account("Joint bank", owner="Joint")
        self.account("Joint card", "Credit card", owner="Joint")
        self.account("Personal", owner="Nanami")
        self.account("Yen", owner="Young", currency="JPY")
        self.account("Excluded", excluded=True)
        self.add("Joint bank", tx("groceries", "2026-12-12", "-40.0001", "Grocery"),
                 tx("uncategorized", "2026-12-13", "-2.003", "Other"),
                 tx("payment", "2026-12-10", "-100", "Credit card payment"),
                 tx("pending", None, "-200", "Pending debit", pending=True))
        self.add("Joint card", tx("p", "2026-12-11", "100.00", "Payment thank you"))
        self.add("Personal", tx("private", "2026-12-12", "-9", "Personal debit"))
        self.add("Yen", tx("fx", "2026-12-12", "-300", "Yen debit"))
        self.add("Excluded", tx("excluded", "2026-12-12", "-90", "Excluded debit"))
        self.sync(include_pending=True)
        self.cat("Food")
        self.post(self.row("Grocery", "2026-12")["row_id"], month="2026-12",
                  action="category", category="Food")
        for view in VIEWS:
            totals = self.snap("2026-12", view, show_pending=True).totals
            expected = {"Food": Decimal("40.0001"), "Uncategorized": Decimal(
                "2.003" if view == "Young" else "11.003")}
            self.assertEqual(totals["USD"].category_spending, expected)
            if view != "Nanami":
                self.assertEqual(totals["JPY"].category_spending, {"Uncategorized": Decimal("300")})
            for total in totals.values():
                self.assertEqual(sum(total.category_spending.values()), total.spending)
        rendered = render_transactions(self.db, "month=2026-12&currency=JPY&q=no-such-list-row",
                                       token=TOKEN, allowed_views={"Young"})
        for shown in ("Spending by category", "Uncategorized", "Food", "$42.00", "¥300", "Income",
                      "includes refunds", "overspent", "not measured savings",
                      "No transactions match"):
            self.assertIn(shown, rendered)
        self.assertIn('<span class="cur" aria-current="page">Young</span>', rendered)
        self.assertIn('<span class="off" aria-disabled="true">Nanami</span>', rendered)      # Household is not allowed here
        self.assertNotIn("view=Household", rendered)
        with self.assertRaises(RouteProblem) as caught:
            render_transactions(self.db, "view=Household", token=TOKEN, allowed_views={"Young"})
        self.assertEqual(caught.exception.status, 403)

    def test_merchant_transfer_rule_three_states_precedence_and_replay(self):
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("p", "2026-12-12", "-30", "Credit card payment", payee="Local merchant"),
                 tx("m", "2026-12-12", "-9", "Manual non-transfer", payee="LOCAL  MERCHANT"),
                 tx("t", "2026-12-12", "-2", "Manual transfer", payee="local merchant"))
        self.add("Card", tx("credit", "2026-12-12", "30", "Payment thank you"),
                 tx("unrelated", "2026-12-12", "-4", "Different account", payee="Local merchant"))
        self.sync()

        def edit(desc, **fields):
            return self.post(self.row(desc, "2026-12")["row_id"], month="2026-12", **fields)

        self.assertTrue(self.row("Credit card payment", "2026-12")["effective_transfer"])
        edit("Manual non-transfer", action="transfer", transfer="no")
        edit("Manual non-transfer", action="category", category="")
        edit("Manual transfer", action="transfer", transfer="yes")
        self.assertEqual(self.db.execute("SELECT count(*) FROM transaction_merchant_rules").fetchone()[0], 0)
        self.cat("Test category")
        edit("Credit card payment", action="rule", category="Test category")
        self.assertIsNone(self.row("Credit card payment", "2026-12")["merchant_transfer"])
        edit("Credit card payment", action="transfer_rule", merchant_transfer="yes")
        self.assertEqual(self.row("Credit card payment", "2026-12")["flow"], "Transfer (merchant rule)")
        self.assertFalse(self.row("Different account", "2026-12")["effective_transfer"])
        self.add("Bank", tx("future", "2026-12-14", "-5", "New eligible", payee="local merchant"))
        self.accounts["Bank"]["transactions"][1]["amount"] = "-9.1"
        self.sync()
        self.assertTrue(self.row("New eligible", "2026-12")["effective_transfer"])
        self.assertEqual(self.row("New eligible", "2026-12")["category"], "Test category")
        self.assertEqual(self.row("Manual non-transfer", "2026-12")["category"], "")
        self.assertEqual(self.row("Manual non-transfer", "2026-12")["flow"], "Cash flow (manual)")
        self.assertEqual(self.row("Manual non-transfer", "2026-12")["amount"], "-9.1")
        edit("Credit card payment", action="transfer_rule", merchant_transfer="no")
        self.assertEqual(self.row("Credit card payment", "2026-12")["flow"], "Cash flow (merchant rule)")
        self.assertTrue(self.row("Manual transfer", "2026-12")["effective_transfer"])
        rendered = render_transactions(self.db, "month=2026-12&edit=%d" % self.row("Credit card payment", "2026-12")["row_id"],
                                       token=TOKEN, allowed_views=VIEWS)          # the drawer is rendered for the row being edited
        self.assertIn('name="merchant_transfer"', rendered)
        self.assertIn('<option value="no" selected>No, never a transfer</option>', rendered)
        self.assertIn("Always treat “local merchant” on this account as a transfer", rendered.replace("&quot;", '"'))
        self.assertIn("Transfer = money between your own accounts; not counted as spending.", rendered)
        for old_wording in ("Merchant transfer rule", "transfer category", "Merchant transfer"):
            self.assertNotIn(old_wording, rendered)
        self.assertNotIn("Cash flow (merchant rule)", rendered)       # internal value only, never shown (L26-10)
        edit("Credit card payment", action="transfer_rule", merchant_transfer="none")
        self.assertEqual(self.row("Credit card payment", "2026-12")["flow"], "Matched transfer")
        self.assertEqual(self.row("New eligible", "2026-12")["category"], "Test category")

    def test_internal_suggestions_mutual_unique_and_do_not_leak_other_view(self):
        self.account("Joint bank", owner="Joint")
        self.account("Private savings", "Savings", owner="Nanami")
        self.account("Young savings", "Savings")
        self.account("Yen", "Savings", currency="JPY")
        self.add("Joint bank", tx("x", "2026-11-12", "-40.00", "Online transfer to savings"),
                 tx("independent", "2026-11-12", "40", "Payroll deposit"),
                 tx("same-account", "2026-11-12", "40", "Transfer from checking"),
                 tx("ambiguous", "2026-11-12", "-17", "Bank transfer out"))
        self.add("Private savings", tx("x", "2026-11-15", "40", "Transfer from checking"),
                 tx("refund", "2026-11-12", "40", "Store refund"),
                 tx("amb1", "2026-11-13", "17", "Transfer in"))
        self.add("Young savings", tx("amb2", "2026-11-13", "17", "Transfer in"))
        self.add("Yen", tx("fx", "2026-11-13", "40", "Transfer in"))
        self.sync()
        household = self.snap()
        self.assertEqual({r["nickname"] for r in household.rows if r["suggested_transfer"]},
                         {"Joint bank", "Private savings"})
        self.assertEqual(sum(r["suggested_transfer"] for r in household.rows), 2)
        self.assertFalse(any(r["effective_transfer"] for r in household.rows))
        self.assertEqual((household.totals["USD"].spending, household.totals["USD"].credits), (57, 194))
        self.assertFalse(self.row("Online transfer to savings", view="Young")["suggested_transfer"])
        self.assertTrue(self.row("Online transfer to savings", view="Nanami")["suggested_transfer"])
        for view, allowed, should_show in [("Young", {"Young"}, False), ("Household", VIEWS, True)]:
            query = urlencode(dict(month="2026-11", view=view, q="Online transfer to savings"))
            rendered = render_transactions(self.db, query, token=TOKEN,
                                           allowed_views=allowed)
            self.assertEqual("Possible internal transfer" in rendered, should_show)
            self.assertEqual("Private savings" in rendered, should_show)
        for r in household.rows:
            if r["suggested_transfer"]:
                self.post(r["row_id"], action="transfer", transfer="yes")
        self.assertEqual((self.snap().totals["USD"].spending, self.snap().totals["USD"].credits), (17, 154))


if __name__ == "__main__":
    unittest.main()

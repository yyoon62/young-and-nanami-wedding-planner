"""L26 parts 3-4: plain-language labels, short visible text, and the single 'How these numbers work' box."""
from datetime import date
import re
import unittest

from transactions_ui import render_transactions
from tx_fixture import Fixture, TOKEN, VIEWS, tx


class Language(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.add("Bank", tx("a", "2026-12-10", "-12.00", "SQ *MAPLE CAFE 04/12 #1234"), tx("b", "2026-12-11", "-3.00", "***"))
        self.sync()

    def html(self, query="month=2026-12"):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=date(2026, 12, 15))

    def test_drawer_says_what_the_transfer_rule_does_and_old_wording_is_gone(self):
        html = self.html()
        self.assertEqual(html.count("Always treat “maple cafe” on this account as a transfer"), 1)
        self.assertEqual(html.count("Transfer = money between your own accounts; not counted as spending."), 2)   # once per row drawer
        self.assertIn("Always treat “***” on this account as a transfer", html)       # no cleaned key: the description
        self.assertIn('name="merchant_transfer"', html)
        for old in ("Merchant transfer rule", "transfer category", "Merchant transfer"):
            self.assertNotIn(old, html)
        values = re.findall(r'<option value="(none|yes|no)"[^>]*>([^<]*)</option>', html)
        self.assertIn(("none", "No rule"), values)
        self.assertIn(("yes", "Yes, always a transfer"), values)
        self.assertIn(("no", "No, never a transfer"), values)


OLD_SUBSTANCE = (
    "including refunds; it is not verified income", "signed cash flow", "not measured savings",
    "Pending, transfers and explicit unmatched card-payment credits are outside totals; unmatched bank debits remain spending",
    "Automatic card matching requires corroboration on both available legs", "Checking and savings matches are suggestions only",
    "A manual transfer choice overrides a merchant rule, which overrides automatic classification",
    "Requested windows do not prove institution completeness or retention", "Current-month figures are provisional",
    "Excluded accounts do not contribute", "Individual views include joint accounts", "List filters do not change totals",
    "Merchant rules match the cleaned merchant name (dates, store and reference numbers removed) on the same account and currency",
    "a category you set on one row by hand always wins over a rule", "posted date (UTC)", "local-midnight stamp east of UTC",
    "month filters use the same date", "ticks are last month", "card credits under review are excluded",
    "Click a category to filter the list", "balance sync quota")
NEW_SUBSTANCE = ("the matching bank debit still counts as spending until it is matched or marked Transfer too",
                 "partial = an account has no or late data for this month", "so far, through D Mon",
                 "on a pending row it is lost if the bank changes the transaction id", "↺ resets it")


class Text(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("a", "2026-12-10", "3000", "Payroll"), tx("b", "2026-12-11", "-1500.00", "Rent Co"),
                 tx("c", "2026-12-11", "-500.00", "Green Market"), tx("d", "2026-12-12", "-300.00", "Cafe"),
                 tx("e", "2026-12-12", "-50.00", "Odd Shop"))
        self.add("Card", tx("p", "2026-12-12", "40.00", "Payment thank you"))
        self.sync()
        self.cat("Housing", "Groceries", "Dining")
        for desc, cat in (("Rent Co", "Housing"), ("Green Market", "Groceries"), ("Cafe", "Dining")):
            self.post(self.row(desc, "2026-12")["row_id"], action="category", category=cat, month="2026-12")

    def html(self, query="month=2026-12", can_sync=False):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=date(2026, 12, 15),
                                   can_sync=can_sync)

    def info(self, html):
        return re.search(r'<details class="tx-info">.*?</details>', html, re.S).group(0)

    def test_every_old_sentence_lives_in_the_info_box_and_not_above_the_list(self):
        html = self.html(can_sync=True)
        info = self.info(html)
        for phrase in (*OLD_SUBSTANCE, *NEW_SUBSTANCE):
            self.assertIn(phrase, info.replace("&#x27;", "'"), phrase)
        outside = re.sub(r' title="[^"]*"', "", html.replace(info, ""))      # tooltips are not visible text
        for gone in ("Pending, transfers and explicit", "Requested windows", "Merchant rules match", "Automatic card matching",
                     "net saved estimate", "Net saved estimate", "Bar = this month", "Shares the balance sync quota.</p>",
                     "Individual views include", "Coverage and dates"):
            self.assertNotIn(gone, outside, gone)
        self.assertEqual(html.count('<details class="tx-info">'), 1)
        self.assertIn("<summary>ℹ️ How these numbers work</summary>", info)
        self.assertNotIn(" open", info[:40])
        self.assertIn("Bank (USD)", info.replace("Fictional institution · ", ""))     # the per-account coverage list is in the box

    def test_default_page_has_at_most_three_short_text_lines_outside_rows_and_cards(self):
        html = self.html()
        section = html[html.index('<section class="history"'):html.index("<section><h2>Transactions")]
        bare = re.sub(r"<details.*?</details>", "", section, flags=re.S)       # collapsed boxes and the cards' bars
        bare = re.sub(r'<div class="cards">.*?</div></section></article></div>', "", bare, flags=re.S)
        paragraphs = re.findall(r"<p[ >]", bare)
        notes = re.findall(r"<li>(.*?)</li>", re.search(r'<ul class="tx-notes">.*?</ul>', bare, re.S).group(0))
        self.assertEqual(paragraphs, [])
        self.assertEqual(len(notes), 2)
        for note in notes:
            self.assertLessEqual(len(note), 80)            # one line at 1280 px
        self.assertEqual(html.count("ℹ️ How these numbers work"), 1)
        self.assertNotIn("Refreshes all linked accounts</p>", html)

    def test_bars_are_one_collapsed_line_with_the_top_three_and_open_with_a_category_filter(self):
        html = self.html()
        bars = re.search(r'<details class="bars-det"(?P<open> open)?>(?P<body>.*?)</details>', html, re.S)
        self.assertIsNone(bars.group("open"))
        summary = re.search(r"<summary[^>]*>(.*?)</summary>", bars.group("body"), re.S).group(1)
        self.assertIn("📊 Spending by category", summary)
        spans = re.findall(r"<span>([^<]*) <strong>([^<]*)</strong></span>", summary)
        self.assertEqual(spans, [("Housing", "$1,500.00"), ("Groceries", "$500.00"), ("Dining", "$300.00")])   # not "Uncategorized $50"
        self.assertEqual(len(re.findall(r'<a class="bar-row', bars.group("body"))), 4)     # all four are inside, one click away
        self.assertIn("last month", bars.group("body"))
        opened = self.html("month=2026-12&cat=Dining&currency=USD")
        self.assertIn('<details class="bars-det" open>', opened)
        self.assertIn("Showing Dining only (USD)", opened)
        self.assertIn('<details class="bars-det" open>', self.html("month=2026-12&cat=Dining"))      # no currency: all open
        self.assertNotIn('<details class="bars-det" open>', self.html("month=2026-12&cat=Dining&currency=EUR"))

    def test_empty_states_keep_their_wording(self):
        self.assertIn("No posted transactions reported", self.html("month=2026-10"))
        self.assertIn("No posted data with sufficient coverage", self.html("month=2024-10"))
        self.assertIn("ℹ️ How these numbers work", self.html("month=2024-10"))


if __name__ == "__main__":
    unittest.main()

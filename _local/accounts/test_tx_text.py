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
        maple, odd = (self.row(d, "2026-12")["row_id"] for d in ("SQ *MAPLE CAFE 04/12 #1234", "***"))
        html = self.html("month=2026-12&edit=%d" % maple)
        self.assertEqual(html.count("Always treat “maple cafe” on this account as a transfer"), 1)
        self.assertEqual(html.count("Transfer = money between your own accounts; not counted as spending."), 1)   # the open drawer only
        other = self.html("month=2026-12&edit=%d" % odd)
        self.assertIn("Always treat “***” on this account as a transfer", other)       # no cleaned key: the description
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
    "month filters use the same date", "Hover a chip for the comparison with last month", "unmatched card payments are excluded",
    "Click a chip to filter the list", "balance sync quota")
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

    def test_one_stacked_bar_per_currency_and_a_legend_of_filter_chips(self):
        html = self.html()
        self.assertEqual(html.count('<svg class="stack"'), html.count('class="card tx-card"'))
        usd = html[html.index('aria-label="Spending by category in USD"'):]
        usd = usd[:usd.index("</section>")]
        chips = re.findall(r'<a class="tx-leg ([^" ]+)[^>]*>.*?<span class="amt">([^<]*)</span> · ([^<]*)</a>', usd, re.S)
        self.assertEqual([(c[1], c[2]) for c in chips],       # 1500 / 500 / 300 / 50 of 2,350, exact then rounded
                         [("$1,500", "64%"), ("$500", "21%"), ("$300", "13%"), ("$50", "2%")])
        names = re.findall(r'<i class="dot" aria-hidden="true"></i>(\S+) ([^<$]*?) <span class="amt">', usd)
        self.assertEqual([n[1] for n in names], ["Housing", "Groceries", "Dining", "Uncategorized"])
        self.assertEqual(len(re.findall(r'<rect class="seg', usd)), 4)
        self.assertEqual(len({c[0] for c in chips}), 4)                                  # a colour class per category
        self.assertNotIn("<details class=\"bars-det\"", html)
        self.assertNotIn("includes", usd)
        opened = self.html("month=2026-12&cat=Dining&currency=USD")
        self.assertIn("✕ Clear", opened)
        self.assertNotIn("Showing Dining only", opened)

    def test_empty_states_keep_their_wording(self):
        self.assertIn("No posted transactions reported", self.html("month=2026-10"))
        self.assertIn("No posted data with sufficient coverage", self.html("month=2024-10"))
        self.assertIn("ℹ️ How these numbers work", self.html("month=2024-10"))


class Rows(Fixture, unittest.TestCase):
    """Part 5: one-line rows with tooltip provenance, lazy drawer, small pages."""
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.account("Card", "Credit card")
        self.add("Bank", tx("a", "2026-12-10", "3000", "PAYROLL Fictional"), tx("b", "2026-12-11", "-12.00", "Corner Cafe"),
                 tx("c", "2026-12-12", "-30.00", "Online transfer to savings"), tx("m", "2026-12-12", "-5.00", "Maple Shop"))
        self.add("Card", tx("p", "2026-12-12", "40.00", "Payment thank you"), tx("x", "2026-12-13", "-8.00", "Totally Unknown"))
        self.sync()
        self.cat("Dining")
        self.post(self.row("Corner Cafe", "2026-12")["row_id"], action="category", category="Dining", month="2026-12")

    def html(self, query="month=2026-12"):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=date(2026, 12, 15))

    def tr(self, html, description):
        start = html.index('<tr class="acct-row"', html.index(description) - 900)
        return html[start:html.index("</tr>", start)]

    def test_provenance_is_a_tooltip_and_only_transfer_review_and_suggested_stay_visible(self):
        html = self.html()
        payroll, cafe, unknown = (self.tr(html, d) for d in ("PAYROLL Fictional", "Corner Cafe", "Totally Unknown"))
        self.assertIn('title="Change category · (built-in default)"', payroll)
        self.assertIn('title="Change category · (set by you)"', cafe)
        for tr in (payroll, cafe, unknown):
            self.assertNotIn(">Posted<", tr)
            self.assertNotIn("SimpleFIN ·</", tr)
            self.assertNotRegex(tr, r">[^<]*(?:Cash flow|set by you|built-in default|merchant rule)[^<]*<")   # tooltip only
        self.assertIn('title="SimpleFIN · Fictional institution · Bank"', payroll)    # full account text in the tooltip
        self.assertIn('<td class="date-cell" title="Posted">', payroll)
        credit = self.tr(html, "Payment thank you")
        self.assertIn('<span class="acct-sub tx-flow">⚠️ to confirm</span>', credit)      # short visible label
        self.assertEqual(html.count('class="badge"'), 0)
        self.assertEqual(html.count("overridden"), 0)

    def test_cash_flow_label_is_gone_everywhere_on_rows_and_only_transfers_carry_one(self):
        html = self.html()
        rows = html[html.index("<tbody>"):html.index("</tbody>")]
        self.assertNotIn("Cash flow", rows)
        self.assertNotIn("Cash flow", self.html("month=2026-12&q=cash"))          # not even a hidden search term
        self.assertIn("No transactions match", self.html("month=2026-12&q=cash+flow"))
        self.post(self.row("Maple Shop", "2026-12")["row_id"], action="transfer", transfer="yes", month="2026-12")
        marked = self.tr(self.html(), "Maple Shop")
        self.assertIn("↔️ Transfer ▾", marked)                                     # the chip names it, once
        self.assertNotIn("tx-flow", marked)
        self.assertNotRegex(self.tr(html, "Corner Cafe"), r"tx-flow")             # an ordinary row has no label

    def test_every_row_has_a_visible_default_emoji_and_the_old_faint_one_is_gone(self):
        from tx_categories import CUSTOM_EMOJI, UNCATEGORIZED_EMOJI
        html = self.html()
        self.assertEqual(UNCATEGORIZED_EMOJI, "🏷️")
        self.assertNotEqual(UNCATEGORIZED_EMOJI, CUSTOM_EMOJI)
        self.assertNotIn("❔", html)
        self.assertEqual(html.count('class="tx-emoji"'), html.count('class="acct-row"'))
        self.assertIn(f">{UNCATEGORIZED_EMOJI}</span> Totally Unknown", html)

    def test_account_label_shows_the_nickname_once(self):
        html = self.html()
        self.assertIn('class="acct-sub tx-acct" title="SimpleFIN · Fictional institution · Card">Card</span>', html)
        self.assertNotIn("SimpleFIN · Fictional institution · Card</div>", html)

    def test_possible_transfer_marker_is_short_and_keeps_the_tooltip(self):
        self.account("Savings", "Savings")
        self.add("Savings", tx("s", "2026-12-12", "30.00", "Transfer from checking"))
        self.sync()
        html = self.html()
        tr = self.tr(html, "Online transfer to savings")
        self.assertIn("↔️ possible transfer", tr)
        self.assertIn("Possible internal transfer · still counted", tr)

    def test_drawer_is_lazy_one_per_page_and_pages_stay_small(self):
        rows = self.snap("2026-12").rows
        plain = self.html()
        self.assertEqual(plain.count("<details class=\"inline-pop tx-drawer\""), 0)
        self.assertEqual(plain.count('class="sec-chip tx-edit"'), len(rows))
        one = self.html("month=2026-12&view=Household&edit=%d" % rows[0]["row_id"])
        self.assertEqual(one.count("<details class=\"inline-pop tx-drawer\" open>"), 1)
        self.assertEqual(one.count(">Edit</a>"), len(rows) - 1)
        self.assertEqual(one.count(">Close</a>"), 1)                                       # the open row offers Close
        self.assertEqual(one.count('<tr class="tx-drawer-row"><td colspan="6">'), 1)       # the drawer is a row below its row
        self.assertIn(f'href="/transactions?month=2026-12&amp;view=Household#tx-{rows[0]["row_id"]}">Cancel</a>', one)
        self.assertRegex(plain, r'href="/transactions\?month=2026-12&amp;view=Household&amp;edit=\d+#tx-\d+">Edit</a>')
        self.assertEqual(self.html("month=2026-12&edit=abc").count("tx-drawer"), 0)          # a junk id is ignored
        self.assertEqual(self.html("month=2026-12&edit=99999").count("tx-drawer"), 0)        # an id outside the list too

    def test_page_size_at_300_rows_stays_near_400_kb(self):
        from tx_fixture import put_row, source_of
        account = self.db.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        for i in range(300):
            put_row(self.db, account, source_of(self.db, account), "USD", f"2026-12-{1 + i % 14:02d}", f"-{10 + i}.{i % 100:02d}",
                    f"Merchant {i % 40} #{i}")
        html = self.html()
        self.assertGreaterEqual(html.count('class="acct-row"'), 300)
        per_row = len(html.encode()) / html.count('class="acct-row"')
        self.assertLess(per_row, 1700, per_row)                 # was ~4,200 B/row before the shared form and lazy drawer


if __name__ == "__main__":
    unittest.main()

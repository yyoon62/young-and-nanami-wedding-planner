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


if __name__ == "__main__":
    unittest.main()

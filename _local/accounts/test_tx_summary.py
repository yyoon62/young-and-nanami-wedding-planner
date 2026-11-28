"""L26 part 2: compact per-currency summary card, card payments to confirm, money display."""
from datetime import date
from decimal import Decimal
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from transactions_ui import render_transactions
from tx_fixture import Fixture, TOKEN, VIEWS, tx
from tx_money import fmt, sign_of

AMOUNT = re.compile(r"^[-+]?(?:[€$£¥]|[A-Z]{3}\u00a0)?\d{1,3}(?:,\d{3})*(?:\.\d+)?$")


class Money(unittest.TestCase):
    def test_symbols_separators_and_half_up_rounding(self):
        D = Decimal
        for value, currency, expected in (
                ("3000", "USD", "$3,000.00"), ("1234567.891", "USD", "$1,234,567.89"), ("-1852.32", "USD", "-$1,852.32"),
                ("0.005", "USD", "$0.01"), ("2.675", "USD", "$2.68"), ("-0.004", "USD", "$0.00"), ("0", "USD", "$0.00"),
                ("42.0031", "EUR", "€42.00"), ("1234.5", "GBP", "£1,234.50"), ("1234.5", "JPY", "¥1,235"),
                ("-2000", "JPY", "-¥2,000"), ("1234.5", "CHF", "CHF\u00a01,234.50"), ("1234.5678", "KWD", "KWD\u00a01,234.568")):
            self.assertEqual(fmt(D(value), currency), expected, (value, currency))
        self.assertEqual(fmt(D("-1852.32"), "USD", absolute=True), "$1,852.32")
        big = "9" * 60 + ".995"                          # beyond the default Decimal context: still exact half-up
        self.assertEqual(fmt(D(big), "USD"), "$" + f"{int('9' * 60) + 1:,}" + ".00")
        self.assertEqual([sign_of(D(v), "USD") for v in ("0.004", "-0.004", "0.005", "-0.005", "0")], [0, 0, 1, -1, 0])


class Summary(Fixture, unittest.TestCase):
    TODAY = date(2026, 12, 15)

    def html(self, query="month=2026-12", views=VIEWS):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=views, today=self.TODAY)

    def card(self, html, currency):
        start = html.index(f'<article class="card tx-card"><div class="tx-card-head"><small>{currency}</small>')
        return html[start:html.index("</article>", start)]

    def seed_usd(self, credits=("3000.00",), spends=("-4852.32",), reviews=("1200.00",)):
        self.account("Bank")
        self.account("Card", "Credit card")
        for i, v in enumerate(credits):
            self.add("Bank", tx(f"in{i}", "2026-12-10", v, f"Payroll {i}"))
        for i, v in enumerate(spends):
            self.add("Bank", tx(f"out{i}", "2026-12-11", v, f"Big purchase {i}"))
        for i, v in enumerate(reviews):
            self.add("Card", tx(f"pay{i}", "2026-12-12", v, "Payment thank you"))
        self.sync()

    def test_overspent_card_with_confirm_line_and_chips(self):
        self.seed_usd()
        card = self.card(self.html(), "USD")
        self.assertIn("💰 Income <strong>$3,000.00</strong>", card)
        self.assertIn("💸 Spending <strong>$4,852.32</strong>", card)
        self.assertIn("🔴 Overspent <strong>$1,852.32</strong>", card)
        self.assertNotIn("-$1,852.32", card)
        self.assertIn("⚠️ 1 card payment to confirm · $1,200.00</a>", card)
        self.assertNotIn("payments to confirm", card)
        self.assertRegex(card, r'<span class="partial-flag"[^>]*>so far, through 15 Dec</span>')
        self.assertNotIn(">partial</span>", card)             # both accounts are covered through today
        for old in ("Net saved estimate", "Card credits under review", "Income / credits"):
            self.assertNotIn(old, card)

    def test_partial_chip_means_missing_coverage_not_just_an_unfinished_month(self):
        self.seed_usd()
        self.assertNotIn(">partial</span>", self.html())
        start = self.db.execute("SELECT start_at FROM transaction_coverage LIMIT 1").fetchone()[0] + 80 * 86400
        with self.db:
            self.db.execute("UPDATE transaction_coverage SET start_at=? WHERE account_id=2", (start,))   # card fetched later
        card = self.card(self.html(), "USD")
        self.assertRegex(card, r'<span class="partial-flag"[^>]*>so far, through 15 Dec</span>')
        self.assertRegex(card, r'<span class="partial-flag"[^>]*>partial</span>')
        with self.db:
            self.db.execute("DELETE FROM transaction_coverage WHERE account_id=2")                      # never fetched
        self.assertRegex(self.card(self.html(), "USD"), r'<span class="partial-flag"[^>]*>partial</span>')

    def test_past_month_without_full_coverage_says_partial_and_no_so_far_chip(self):
        self.account("Bank")
        self.add("Bank", tx("n", "2026-11-20", "-12.00", "Nov purchase"), tx("o", "2026-11-25", "5.00", "Nov refund"))
        self.sync()
        card = self.card(self.html("month=2026-11"), "USD")
        self.assertNotIn("so far", card)
        self.assertNotIn(">partial</span>", card)                   # fetched from Sep 16 to Dec 15
        with self.db:
            self.db.execute("UPDATE transaction_coverage SET start_at=?", (self.db.execute(
                "SELECT end_at FROM transaction_coverage").fetchone()[0] - 40 * 86400,))                  # starts Nov 5
        card = self.card(self.html("month=2026-11"), "USD")
        self.assertRegex(card, r'<span class="partial-flag"[^>]*>partial</span>')
        self.assertNotIn("so far", card)

    def test_saved_even_and_plural(self):
        self.seed_usd(credits=("3000.00",), spends=("-1000.00",), reviews=("10.00", "20.50"))
        card = self.card(self.html(), "USD")
        self.assertIn("🟢 Saved <strong>$2,000.00</strong>", card)
        self.assertIn("⚠️ 2 card payments to confirm · $30.50</a>", card)

    def test_even_and_no_confirm_line_when_nothing_to_confirm(self):
        self.seed_usd(credits=("100.00",), spends=("-100.00",), reviews=())
        card = self.card(self.html(), "USD")
        self.assertIn("⚪ Even <strong>$0.00</strong>", card)
        self.assertNotIn("to confirm", card)
        self.assertNotIn("tx-confirm", card)

    def test_confirm_link_filters_to_exactly_those_rows_with_a_clear_chip_and_resolving_removes_it(self):
        self.seed_usd(reviews=("1200.00", "300.00"))
        html = self.html()
        link = re.search(r'<a href="([^"]+)"[^>]*>⚠️ 2 card payments', html).group(1).replace("&amp;", "&")
        query = parse_qs(urlsplit(link).query)
        self.assertEqual((query["review"], query["currency"], query["month"]), (["1"], ["USD"], ["2026-12"]))
        filtered = self.html(urlsplit(link).query)
        self.assertEqual(filtered.count('class="acct-row"'), 2)
        self.assertEqual(filtered.count("Payment thank you"), 2)
        self.assertIn("Showing card payments to confirm only", filtered)
        clear = re.search(r'Showing card payments to confirm only · <a href="([^"]+)">Clear filter</a>', filtered)
        self.assertNotIn("review=1", clear.group(1))
        self.assertEqual(self.html(urlsplit(clear.group(1).replace("&amp;", "&")).query).count('class="acct-row"'), 4)
        # Marking one credit as a transfer takes it out of the set; the list and the line follow.
        first = self.row("Payment thank you", "2026-12")["row_id"]
        self.post(first, action="transfer", transfer="yes", month="2026-12")
        after = self.html(urlsplit(link).query)
        self.assertEqual(after.count("Payment thank you"), 1)
        self.assertIn("1 card payment to confirm", self.card(after, "USD"))
        # The matching bank debit is a separate row: spending is unchanged until it is matched/marked too.
        self.assertIn("Spending <strong>$4,852.32</strong>", self.card(after, "USD"))
        # Filter state is carried by every form (a category pick or owner click keeps the review filter).
        self.assertIn('<input type="hidden" name="review" value="1">', filtered)

    def test_review_filter_validation(self):
        self.seed_usd()
        from transactions_ui import RouteProblem
        with self.assertRaises(RouteProblem) as caught:
            self.html("month=2026-12&review=2")
        self.assertEqual(caught.exception.status, 400)

    def test_each_currency_has_its_own_card_and_symbols(self):
        self.account("Bank")
        self.account("Euro", currency="EUR")
        self.account("Yen", currency="JPY")
        self.account("Franc", currency="CHF")
        self.add("Bank", tx("a", "2026-12-10", "1234567.89", "Payroll"), tx("b", "2026-12-11", "-9876.54", "Rent"))
        self.add("Euro", tx("c", "2026-12-10", "-1500.5", "Euro shop"))
        self.add("Yen", tx("d", "2026-12-10", "-123456", "Yen shop"))
        self.add("Franc", tx("e", "2026-12-10", "-4321.1", "Franc shop"))
        self.sync()
        html = self.html()
        self.assertIn("💰 Income <strong>$1,234,567.89</strong>", self.card(html, "USD"))
        self.assertIn("💸 Spending <strong>€1,500.50</strong>", self.card(html, "EUR"))
        self.assertIn("🔴 Overspent <strong>€1,500.50</strong>", self.card(html, "EUR"))
        self.assertIn("💸 Spending <strong>¥123,456</strong>", self.card(html, "JPY"))
        self.assertIn("💸 Spending <strong>CHF\u00a04,321.10</strong>", self.card(html, "CHF"))
        self.assertNotIn("¥123,456.00", html)
        self.assertEqual(html.count('class="card tx-card"'), 4)     # never summed across currencies

    def test_every_visible_amount_has_a_symbol_or_code_and_thousands_separators(self):
        self.seed_usd(credits=("12345.67", "1000000"), spends=("-1234.5", "-20.00", "-98765.43"), reviews=("4321.00",))
        self.cat("Big")
        html = self.html()
        cards = html[html.index('<div class="cards">'):html.index("<section><h2>Transactions")]
        amounts = re.findall(r"<strong>([^<]*)</strong>", cards)
        amounts += re.findall(r'<span class="amt-(?:in|out|zero|neutral)">([^<]*)</span>', html)
        amounts += re.findall(r'<span class="amt">([^<]*)</span>', html)
        amounts += re.findall(r"aria-label=\"[^\"]*: ([^\"]+)\"", re.sub(r'aria-label="Spending[^"]*"', "", html))
        amounts = [a for a in amounts if re.search(r"\d", a)]
        self.assertGreater(len(amounts), 8)
        for a in amounts:
            self.assertRegex(a, AMOUNT, a)
        self.assertIn('<span class="amt-in">+1,000,000</span>', html)       # list rows keep exact amounts + separators
        self.assertNotRegex(html, r"(?<![\d,.#-])\d{4,}\.\d{2}(?![\d])(?=[^<]*</(?:strong|span)>)")


if __name__ == "__main__":
    unittest.main()

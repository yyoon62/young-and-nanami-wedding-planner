"""Slice B: filters without a button, category bars vs last month / 3-month mean, amounts, logos."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from tx_fixture import Fixture, TOKEN, VIEWS, block_network, cover, epoch, put_row, source_of, tx
from transactions import monthly_snapshot
from transactions_ui import RouteProblem, render_transactions
from tx_periods import month_add, spending_comparison

TODAY = date(2026, 11, 28)
ROOT = Path(__file__).resolve().parent


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return .2126 * lin[0] + .7152 * lin[1] + .0722 * lin[2]


def contrast(fg, bg="#ffffff"):
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + .05) / (b + .05)


class Months(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.account("Bank")
        self.savings = self.account("Savings", "Savings")
        self.euro = self.account("Euro bank", currency="EUR")
        self.src = {n: source_of(self.db, a) for n, a in (("USD", self.bank), ("SAV", self.savings), ("EUR", self.euro))}

    def put(self, which, day, amount, desc, currency="USD", **kw):
        account = {"USD": self.bank, "SAV": self.savings, "EUR": self.euro}[which]
        put_row(self.db, account, self.src[which], currency, day, amount, desc, **kw)

    def seed_history(self):
        for month, dining in (("2026-08", "300"), ("2026-09", "250"), ("2026-10", "200")):
            self.put("USD", f"{month}-10", f"-{dining}", "Maple Cafe")
        self.put("USD", "2026-11-10", "-300", "Maple Cafe")
        self.put("USD", "2026-11-12", "-500", "Green Market")
        self.put("USD", "2026-11-13", "-700", "Move to savings", transfer=True)
        self.put("SAV", "2026-11-13", "700", "Move from savings", transfer=True)
        self.put("EUR", "2026-11-14", "-50", "Maple Cafe", currency="EUR")
        self.put("USD", "2026-11-15", "2500.00", "Payroll")
        for desc, cat in (("Maple Cafe", "Dining"), ("Green Market", "Groceries")):
            self.post(next(r for r in self.snap("2026-11").rows if r["description"] == desc and r["currency"] == "USD")["row_id"],
                      action="save", category=cat, apply="1", transfer="auto")
        cover(self.db, "2026-08-01", "2026-11-29")

    def html(self, query="month=2026-11", **kw):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=TODAY, **kw)

    def test_bars_current_vs_last_month_vs_three_month_mean_partial_and_per_currency(self):
        self.seed_history()
        cmp = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)
        usd = cmp["USD"]
        self.assertEqual((usd.current["Dining"], usd.prev["Dining"], usd.avg["Dining"]),
                         (Decimal("300"), Decimal("200"), Decimal("250.00")))
        self.assertEqual(usd.avg_months, ("2026-10", "2026-09", "2026-08"))
        self.assertEqual(usd.current["Groceries"], Decimal("500"))
        self.assertEqual(usd.prev.get("Groceries", 0), 0)  # covered months with no spend are zero, not absent
        self.assertEqual(usd.avg.get("Groceries", Decimal(0)), 0)  # page shows 0.00
        self.assertEqual(sum(usd.current.values()), Decimal("800"))  # transfers excluded, EUR not summed in
        self.assertEqual(cmp["EUR"].current, {"Uncategorized": Decimal("50")})
        self.assertEqual(usd.partial, "so far, through 28 Nov")
        html = self.html()
        self.assertRegex(html, r'<span class="partial-flag"[^>]*>so far, through 28 Nov</span>')
        self.assertEqual(html.count("so far, through 28 Nov"), 2)     # chip on each currency card (USD, EUR)
        dining = re.search(r'<li><a class="bar-row"[^>]*title="Show only Dining \(USD\)".*?</a></li>', html, re.S).group(0)
        for text in ("$300.00", "last month $200.00", "3-mo avg $250.00"):
            self.assertIn(text, dining)
        self.assertIn("Spending <strong>$800.00</strong>", html)
        self.assertIn("Spending <strong>€50.00</strong>", html)
        # Click-to-filter: a plain, reversible GET link; the list shows only that category and currency.
        link = re.search(r'href="([^"]+)"[^>]*title="Show only Dining \(USD\)"', html).group(1).replace("&amp;", "&")
        self.assertEqual(parse_qs(urlsplit(link).query)["cat"], ["Dining"])
        filtered = self.html(urlsplit(link).query)
        self.assertIn("Showing Dining only", filtered)
        self.assertEqual(filtered.count('class="acct-row"'), 1)
        self.assertNotIn("Green Market</span>", filtered)
        self.assertIn("Clear filter", filtered)
        clear = re.search(r'<a href="([^"]+)">Clear filter</a>', filtered).group(1).replace("&amp;", "&")
        self.assertNotIn("cat=Dining", clear)
        self.assertGreater(self.html(urlsplit(clear).query).count('class="acct-row"'), 3)
        # Uncategorized is its own reversible filter value.
        self.assertIn("Showing Uncategorized only", self.html("month=2026-11&cat=Uncategorized&currency=EUR"))

    def test_partial_coverage_is_not_averaged_as_if_complete(self):
        """The 90-day first sync starts on 2026-08-30: August has 2 of 31 days and must not enter the mean."""
        self.seed_history()
        cover(self.db, "2026-08-30", "2026-11-28")
        usd = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)["USD"]
        self.assertEqual(usd.avg_months, ("2026-10", "2026-09"))
        self.assertEqual(usd.avg["Dining"], Decimal("225.00"))
        self.assertIn("2-mo avg $225.00", self.html())
        cover(self.db, "2026-09-16", "2026-11-28")  # only October is complete: no average, previous still shown
        usd = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)["USD"]
        self.assertIsNone(usd.avg)
        self.assertEqual(usd.prev["Dining"], Decimal("200"))
        html = self.html()
        self.assertIn("last month $200.00 · 3-mo avg —", html)
        cover(self.db, "2026-10-20", "2026-11-28")  # not even October: no comparison at all
        usd = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)["USD"]
        self.assertIsNone(usd.prev)
        self.assertIn("last month — · 3-mo avg —", self.html())

    def test_zero_spend_month_counts_and_unlinked_is_reported(self):
        self.put("USD", "2026-09-10", "-300", "Maple Cafe")
        cover(self.db, "2026-08-01", "2026-11-28")
        self.put("USD", "2026-11-10", "-30", "Maple Cafe")
        usd = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)["USD"]
        self.assertEqual(usd.avg["Uncategorized"], Decimal("100.00"))  # (0 + 300 + 0) / 3
        self.account("Manual only", linked=False)
        usd = spending_comparison(self.db, "2026-11", "Household", household_matching=True, today=TODAY)["USD"]
        self.assertEqual(usd.unlinked, 1)

    def test_month_buckets_on_the_displayed_posted_date(self):
        self.add("Bank", tx("w", "2026-10-31T23:30:00-08:00", "-4.00", "Late PST evening"),
                 tx("j", "2026-12-01T00:00:00+09:00", "-5.00", "JST local midnight"))
        self.sync()
        nov = {r["description"]: r["posted_date"] for r in self.snap("2026-11").rows}
        self.assertEqual(nov["Late PST evening"], "2026-11-01")   # displayed date == bucket month (documented)
        self.assertEqual(nov["JST local midnight"], "2026-11-30")
        self.assertNotIn("Late PST evening", {r["description"] for r in self.snap("2026-10").rows})
        html = self.html()
        self.assertIn('<time datetime="2026-11-01">2026-11-01</time>', html)
        self.assertIn("posted date (UTC)", html)

    def test_filters_have_no_button_no_utc_label_and_month_links(self):
        self.seed_history()
        html = self.html()
        self.assertNotIn("(UTC)</label>", html)
        self.assertNotIn("Month (UTC)", html)
        self.assertIn("<label>Month<input", html)
        self.assertEqual(html.count("<button>Filter</button>"), 0)
        self.assertRegex(html, r"<noscript><div class=\"drawer-left\"><button>Apply filters</button></div></noscript>")
        self.assertIn("data-autofilter", html)
        self.assertIn('<script defer src="/dashboard.js"></script>', html)
        self.assertRegex(html, r'href="/transactions\?month=2026-10&amp;[^"]*" rel="prev">← Oct 2026')
        self.assertIn("Current month", html)  # no "next" past the current month
        self.assertRegex(self.html("month=2026-10"), r'href="/transactions\?month=2026-11&amp;[^"]*" rel="next">Nov 2026')
        self.assertEqual(month_add("2026-01", -1), "2025-12")
        self.assertEqual(month_add("2026-12", 1), "2027-01")
        self.assertIn('<option value="Pets"', self.pets())

    def pets(self):
        self.cat("Pets")
        return self.html()

    def test_amount_column_sign_and_colour(self):
        self.put("USD", "2026-11-15", "2500.00", "Payroll")
        self.put("USD", "2026-11-16", "-12.40", "Maple Cafe")
        self.put("USD", "2026-11-17", "0.00", "Zero hold")
        html = self.html()
        self.assertIn("<th class=\"num\">Amount</th>", html)
        self.assertIn('<span class="amt-in">+2,500.00</span>', html)
        self.assertIn('<span class="amt-out">-12.40</span>', html)
        self.assertIn('<span class="amt-zero">0.00</span>', html)
        css = (ROOT / "style.css").read_text()
        var = lambda name: re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", css).group(1)
        for name in ("sage", "accent", "text-muted"):
            self.assertGreaterEqual(contrast(var(name)), 4.5, name)
        self.assertNotEqual(var("sage"), var("accent"))
        self.assertRegex(css, r"\.amt-in\s*\{\s*color:\s*var\(--sage\)")
        self.assertRegex(css, r"\.amt-out\s*\{\s*color:\s*var\(--accent\)")

    def test_logos_use_cache_only_and_never_touch_the_network(self):
        self.seed_history()
        with self.db:
            self.db.execute("UPDATE accounts SET institution='Chase' WHERE id=?", (self.bank,))
        with tempfile.TemporaryDirectory() as tmp, block_network():
            logo_dir = Path(tmp)
            html = self.html(logo_dir=logo_dir)
            self.assertNotIn("<img", html)             # known institution, but nothing cached: monogram, no fetch
            self.assertIn('class="inst-logo"', html)
            (logo_dir / "chase.com.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
            html = self.html(logo_dir=logo_dir)
            self.assertIn('<img class="inst-logo-img" src="/logo/chase.com"', html)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("https://", html)
            self.assertIn('class="inst-logo"', html)   # other institutions still monogram

    def test_category_filter_value_is_escaped_and_bounded(self):
        hostile = '"><img src=x onerror=alert(1)>'
        html = self.html("month=2026-11&cat=" + "%22%3E%3Cimg+src%3Dx+onerror%3Dalert%281%29%3E")
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)
        with self.assertRaises(RouteProblem):
            self.html("month=2026-11&cat=" + "a" * 49)
        self.assertEqual(hostile, hostile)


if __name__ == "__main__":
    unittest.main()

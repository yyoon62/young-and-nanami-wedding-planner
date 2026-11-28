"""L26 part 14: period tiles (30d 90d 6m YTD 1y All) replace the month stepper; the old month= link still works."""
from datetime import date
from decimal import Decimal
import re
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import transactions_ui
from transactions_ui import RouteProblem, render_transactions
from tx_fixture import Fixture, TOKEN, VIEWS, cover, put_row, source_of
from tx_range import PERIODS, add_months, previous_span, resolve_span

TODAY = date(2026, 11, 28)


class Spans(unittest.TestCase):
    def f(self, **kw):
        return dict(period="30d", month="") | kw

    def test_add_months_clips_the_day(self):
        self.assertEqual(add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 11, 29), -12), date(2025, 11, 29))
        self.assertEqual(add_months(date(2026, 1, 15), -2), date(2025, 11, 15))

    def test_each_period_is_inclusive_of_today_and_utc(self):
        got = {key: resolve_span(self.f(period=key), TODAY, date(2026, 2, 3)) for key in PERIODS}
        self.assertEqual((got["30d"].start, got["30d"].end), (date(2026, 10, 30), date(2026, 11, 29)))
        self.assertEqual(got["30d"].days, 30)
        self.assertEqual((got["90d"].start, got["90d"].days), (date(2026, 8, 31), 90))
        self.assertEqual(got["6m"].start, date(2026, 5, 29))
        self.assertEqual(got["ytd"].start, date(2026, 1, 1))
        self.assertEqual(got["1y"].start, date(2025, 11, 29))
        self.assertEqual(got["all"].start, date(2026, 2, 3))                     # the earliest synced day
        self.assertEqual(resolve_span(self.f(period="all"), TODAY, None).start, TODAY)   # nothing synced yet

    def test_month_param_wins_and_maps_to_that_calendar_month(self):
        span = resolve_span(self.f(period="90d", month="2026-10"), TODAY)
        self.assertEqual((span.start, span.end, span.key, span.label), (date(2026, 10, 1), date(2026, 11, 1), "month", "Oct 2026"))

    def test_previous_period_has_the_same_length_and_all_has_none(self):
        cur = resolve_span(self.f(period="90d"), TODAY)
        before = previous_span(cur)
        self.assertEqual((before.end, before.days), (cur.start, 90))
        self.assertEqual(before.start, date(2026, 6, 2))
        jan = previous_span(resolve_span(self.f(month="2026-01"), TODAY))
        self.assertEqual((jan.start, jan.end, jan.label), (date(2025, 12, 1), date(2026, 1, 1), "Dec 2025"))
        self.assertIsNone(previous_span(resolve_span(self.f(period="all"), TODAY)))
        ytd = resolve_span(self.f(period="ytd"), TODAY)
        self.assertEqual(previous_span(ytd).days, ytd.days)


class Page(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.account("Bank")
        self.card = self.account("Card", "Credit card", owner="Nanami")
        self.src = source_of(self.db, self.bank)
        for day, amount, text in (("2026-11-20", "-100.00", "Nov Shop"), ("2026-10-20", "-200.00", "Oct Shop"),
                                  ("2026-09-10", "-300.00", "Sep Shop"), ("2026-06-10", "-400.00", "Jun Shop"),
                                  ("2026-03-10", "-500.00", "Mar Shop"), ("2026-11-01", "3000.00", "Payroll Fictional"),
                                  ("2025-12-20", "-600.00", "Dec Last Year Shop")):
            put_row(self.db, self.bank, self.src, "USD", day, amount, text)
        cover(self.db, "2025-01-01", "2026-11-29")

    def html(self, q="", **kw):
        return render_transactions(self.db, q, token=TOKEN, allowed_views=VIEWS, today=TODAY, **kw)

    def cards(self, html):
        return html[html.index('<div class="cards">'):html.index("<section><h2>Transactions")]

    def spending(self, html):
        return re.search(r"💸 Spending <strong>([^<]*)</strong>", html).group(1)

    def rows(self, html):
        return re.findall(r'<span class="acct-name"[^>]*><span class="tx-emoji"[^>]*>[^<]*</span> ([^<]*)</span>', html)

    def test_default_is_30_days_with_six_tiles_in_the_accounts_tab_style_and_no_stepper(self):
        html = self.html()
        nav = re.search(r'<nav class="owner-tabs tx-tabs tx-periods" aria-label="Period">(.*?)</nav>', html, re.S).group(1)
        self.assertEqual(re.findall(r">([^<]+)</a>", nav), ["30d", "90d", "6m", "YTD", "1y", "All"])
        self.assertRegex(nav, r'aria-current="page"[^>]*>30d</a>')
        self.assertEqual(nav.count("aria-current"), 1)
        self.assertNotIn('class="month-nav"', html)                              # no ← month → stepper
        self.assertNotIn("month-step", html)
        hrefs = re.findall(r'<a href="([^"]+)"', nav)
        self.assertEqual([parse_qs(urlsplit(h.replace("&amp;", "&")).query)["period"][0] for h in hrefs], list(PERIODS))
        self.assertEqual(self.rows(html), ["Nov Shop", "Payroll Fictional"])
        self.assertEqual(self.spending(html), "$100.00")

    def test_totals_bar_legend_and_list_all_follow_the_period(self):
        for period, spending, rows in (("30d", "$100.00", {"Nov Shop", "Payroll Fictional"}),
                                       ("90d", "$600.00", {"Nov Shop", "Oct Shop", "Sep Shop", "Payroll Fictional"}),
                                       ("6m", "$1,000.00", {"Nov Shop", "Oct Shop", "Sep Shop", "Jun Shop", "Payroll Fictional"}),
                                       ("ytd", "$1,500.00", {"Nov Shop", "Oct Shop", "Sep Shop", "Jun Shop", "Mar Shop",
                                                              "Payroll Fictional"}),
                                       ("1y", "$2,100.00", {"Nov Shop", "Oct Shop", "Sep Shop", "Jun Shop", "Mar Shop",
                                                             "Payroll Fictional", "Dec Last Year Shop"}),
                                       ("all", "$2,100.00", {"Nov Shop", "Oct Shop", "Sep Shop", "Jun Shop", "Mar Shop",
                                                              "Payroll Fictional", "Dec Last Year Shop"})):
            html = self.html(f"period={period}")
            self.assertEqual(self.spending(html), spending, period)
            self.assertEqual(set(self.rows(html)), rows, period)
            self.assertRegex(html, rf'aria-current="page"[^>]*>{"YTD" if period == "ytd" else period.capitalize() if period == "all" else period}</a>')
            self.assertEqual(html.count('<svg class="stack"'), 1)
            self.assertIn(f"{spending[:-3].replace('.', '')}", html)             # the legend chip shows whole dollars

    def test_deltas_compare_with_the_previous_period_of_the_same_length_in_the_tooltip(self):
        html = self.html("period=30d")
        chip = re.search(r'<a class="tx-leg [^"]*"[^>]*title="Uncategorized: [^"]*"', html).group(0)
        self.assertIn("vs the previous 30 days: $200.00 (▼ $100.00)", chip)        # Oct 20 (-200) is in Sep 30 - Oct 29
        html = self.html("period=90d")
        chip = re.search(r'<a class="tx-leg [^"]*"[^>]*title="Uncategorized: [^"]*"', html).group(0)
        self.assertIn("vs the previous 90 days: $400.00 (▲ $200.00)", chip)         # Jun 2 - Aug 30 holds Jun 10 only
        self.assertNotIn("vs", re.search(r'<a class="tx-leg [^"]*"[^>]*title="Uncategorized: [^"]*"', self.html("period=all")).group(0))

    def test_data_from_note_replaces_misleading_comparisons_when_the_period_starts_before_the_data(self):
        cover(self.db, "2026-09-05", "2026-11-29")
        html = self.html("period=6m")
        self.assertIn("📅 Data from 5 Sep only", self.cards(html))
        chip = re.search(r'<a class="tx-leg [^"]*"[^>]*title="Uncategorized: [^"]*"', html).group(0)
        self.assertNotIn("vs", chip)                                               # the previous 6 months were never fetched
        self.assertNotIn("Data from", self.cards(self.html("period=30d")))                      # the last 30 days are fully covered
        self.assertIn("vs the previous 30 days", self.html("period=30d"))

    def test_old_month_link_maps_to_that_month_with_a_removable_chip_and_a_stepper(self):
        html = self.html("month=2026-10")
        self.assertEqual(self.rows(html), ["Oct Shop"])
        self.assertEqual(self.spending(html), "$200.00")
        self.assertEqual(html.count('class="month-nav"'), 1)
        self.assertRegex(html, r'rel="prev">← Sep 2026</a><strong>Oct 2026</strong><a class="month-step" href="[^"]+" rel="next">Nov 2026 →')
        chip = re.search(r'<a class="filter-chip tx-monthchip" href="([^"]+)"[^>]*>📅 Oct 2026 ✕</a>', html)
        self.assertNotIn("month=", chip.group(1))
        self.assertNotIn('aria-current="page">30d', html)                         # no tile is lit
        self.assertIn("vs Sep 2026: $300.00 (▼ $100.00)", html)
        again = self.html(urlsplit(chip.group(1).replace("&amp;", "&")).query)
        self.assertNotIn("month-nav", again)
        self.assertEqual(self.spending(again), "$100.00")

    def test_every_link_carries_the_period_so_a_shared_url_reproduces_the_page(self):
        html = self.html("period=90d&view=Young&currency=USD")
        for href in re.findall(r'href="(/transactions\?[^"#]+)"', html):
            q = parse_qs(urlsplit(href.replace("&amp;", "&")).query, keep_blank_values=True)
            if "period" in q:
                continue
            self.assertIn("month", q, href)
        for nav in ("Whose transactions", "Pending"):
            part = re.search(rf'aria-label="{nav}">(.*?)</nav>', html, re.S).group(1)
            for href in re.findall(r'href="([^"]+)"', part):
                self.assertEqual(parse_qs(urlsplit(href.replace("&amp;", "&")).query)["period"], ["90d"])
        self.assertIn('<input type="hidden" name="period" value="90d">', html)

    def test_unknown_period_is_a_400_and_the_old_defaults_still_parse(self):
        with self.assertRaises(RouteProblem) as caught:
            self.html("period=2w")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("Nov Shop", self.html("period=30d&month="))                  # an empty month is no month

    def test_a_pick_inside_a_long_period_saves_and_returns_to_that_period(self):
        row = next(r["row_id"] for r in self.snap_rows("90d") if r["description"] == "Sep Shop")
        result = self.post(row, period="90d", month="", pick="Dining")
        self.assertIn("period=90d", result.location)
        self.assertEqual(self.db.execute("SELECT category FROM account_transactions WHERE row_id=?", (row,)).fetchone(), ("Dining",))
        with self.assertRaises(RouteProblem) as caught:                            # outside the 30 days it is not editable here
            self.post(row, period="30d", month="", pick="Travel")
        self.assertEqual(caught.exception.status, 404)

    def snap_rows(self, period):
        from tx_range import resolve_span
        from transactions import period_snapshot
        span = resolve_span(dict(period=period, month=""), TODAY)
        return period_snapshot(self.db, span.start, span.end, "Household", household_matching=True).rows

    def test_long_lists_are_capped_and_say_so_but_totals_use_every_row(self):
        with mock.patch.object(transactions_ui, "ROW_CAP", 2):
            html = self.html("period=all")
        self.assertEqual(len(self.rows(html)), 2)
        self.assertIn("Showing the newest 2 transactions; 5 older ones are in the totals but not listed", html)
        self.assertEqual(self.spending(html), "$2,100.00")


if __name__ == "__main__":
    unittest.main()

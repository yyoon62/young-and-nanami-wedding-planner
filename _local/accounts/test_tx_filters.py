"""L26 part 11: filters are click targets (owner tabs, currency tabs, pending toggle, search), never dropdowns."""
from datetime import date
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from transactions_ui import render_transactions
from tx_fixture import Fixture, TOKEN, VIEWS, tx
from tx_filters import view_after_click


def query(href):
    return {k: v[0] for k, v in parse_qs(urlsplit(href.replace("&amp;", "&")).query, keep_blank_values=True).items()}


class Tabs(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Young bank", owner="Young")
        self.account("Nanami bank", owner="Nanami")
        self.account("Joint card", "Credit card", owner="Joint")
        self.account("Euro bank", owner="Young", currency="EUR")
        self.add("Young bank", tx("a", "2026-11-03", "-10.00", "Young Shop"))
        self.add("Nanami bank", tx("b", "2026-11-04", "-20.00", "Nanami Shop"))
        self.add("Joint card", tx("c", "2026-11-05", "-30.00", "Joint Shop"))
        self.add("Euro bank", tx("d", "2026-11-06", "-40.00", "Euro Shop", ))
        self.sync()

    def html(self, q="month=2026-11", **kw):
        return render_transactions(self.db, q, token=TOKEN, allowed_views=kw.pop("allowed", VIEWS),
                                   today=date(2026, 11, 28), **kw)

    def tabs(self, html, label):
        nav = re.search(rf'<nav class="owner-tabs tx-tabs[^"]*" aria-label="{label}">(.*?)</nav>', html, re.S)
        return nav.group(1) if nav else ""

    def test_view_click_rule_is_the_accounts_one_both_or_neither_is_household(self):
        for view, person, expected in (("Household", "Young", "Young"), ("Household", "Nanami", "Nanami"),
                                       ("Young", "Young", "Household"), ("Young", "Nanami", "Household"),
                                       ("Nanami", "Young", "Household"), ("Nanami", "Nanami", "Household")):
            self.assertEqual(view_after_click(view, person), expected, (view, person))

    def test_no_dropdown_and_no_month_input_anywhere_in_the_filters(self):
        html = self.html()
        top = html[:html.index("<section><h2>Transactions")]
        for gone in ("<select", 'type="month"', "<option", "All categories", "All currencies", "Hide</option>"):
            self.assertNotIn(gone, top)

    def test_owner_tabs_filter_on_the_effective_owner_and_toggle_back(self):
        html = self.html()
        views = self.tabs(html, "Whose transactions")
        self.assertNotIn("aria-current", views)                                     # Household: neither pill is lit
        young = query(re.search(r'<a href="([^"]+)"[^>]*>Young</a>', views).group(1))
        nanami = query(re.search(r'<a href="([^"]+)"[^>]*>Nanami</a>', views).group(1))
        self.assertEqual((young["view"], nanami["view"]), ("Young", "Nanami"))
        shown = self.html(f"month=2026-11&view={young['view']}")
        self.assertIn("Young Shop", shown)
        self.assertIn("Joint Shop", shown)                                          # joint accounts belong to both
        self.assertNotIn("Nanami Shop", shown)
        lit = self.tabs(shown, "Whose transactions")
        self.assertRegex(lit, r'aria-current="page"[^>]*>Young</a>')
        back = query(re.search(r'<a href="([^"]+)"[^>]*>Young</a>', lit).group(1))
        self.assertEqual(back["view"], "Household")                                 # clicking the lit pill clears it
        both = query(re.search(r'<a href="([^"]+)"[^>]*>Nanami</a>', lit).group(1))
        self.assertEqual(both["view"], "Household")                                 # Young + Nanami = Household
        # An override on one row moves it between the views (effective owner, part 1).
        row = self.row("Joint Shop")["row_id"]
        self.post(row, action="owner", owner="Young")                              # Joint -> Nanami only
        self.assertNotIn("Joint Shop", self.html("month=2026-11&view=Young"))
        self.assertIn("Joint Shop", self.html("month=2026-11&view=Nanami"))

    def test_currency_tabs_only_with_more_than_one_currency_and_never_a_select(self):
        html = self.html()
        cur = self.tabs(html, "Currency")
        self.assertEqual(re.findall(r">([A-Za-z]+)</a>", cur), ["All", "EUR", "USD"])
        eur = query(re.search(r'<a href="([^"]+)"[^>]*>EUR</a>', cur).group(1))
        self.assertEqual(eur["currency"], "EUR")
        only = self.html("month=2026-11&currency=EUR")
        self.assertIn("Euro Shop", only)
        self.assertNotIn("Young Shop", only)
        self.assertRegex(self.tabs(only, "Currency"), r'aria-current="page"[^>]*>EUR</a>')
        self.assertEqual(html.count('class="card tx-card"'), 2)                     # a card per currency regardless
        # one currency in the data: no chip row at all
        with self.db:
            self.db.execute("DELETE FROM account_transactions WHERE currency='EUR'")
            self.db.execute("UPDATE accounts SET excluded=1 WHERE currency='EUR'")
        self.assertEqual(self.tabs(self.html(), "Currency"), "")

    def test_pending_is_a_toggle_link_and_every_state_is_in_the_query_string(self):
        html = self.html()
        pending = self.tabs(html, "Pending")
        link = re.search(r'<a href="([^"]+)"[^>]*>⏳ Pending</a>', pending).group(1)
        self.assertEqual(query(link)["pending"], "1")
        on = self.tabs(self.html("month=2026-11&pending=1"), "Pending")
        self.assertRegex(on, r'aria-current="page"[^>]*>⏳ Pending</a>')
        self.assertEqual(query(re.search(r'<a href="([^"]+)"', on).group(1))["pending"], "0")
        # Links keep every other filter, so a shared URL reproduces the page.
        shown = self.html("month=2026-10&view=Young&currency=USD&q=shop&pending=1")
        for href in re.findall(r'<a href="(/transactions\?[^"]+)"', self.tabs(shown, "Pending")):
            q = query(href)
            self.assertEqual((q["month"], q["view"], q["currency"], q["q"]), ("2026-10", "Young", "USD", "shop"))

    def test_search_is_a_compact_get_form_carrying_the_other_filters(self):
        html = self.html("month=2026-11&view=Young&currency=USD&q=shop")
        form = re.search(r'<form method="get" action="/transactions" class="tx-find" data-autofilter[^>]*>(.*?)</form>',
                         html, re.S).group(1)
        self.assertEqual(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form),
                         [("period", "30d"), ("month", "2026-11"), ("view", "Young"), ("currency", "USD"),
                          ("pending", "0"), ("cat", ""), ("review", "")])
        self.assertIn('<input type="search" name="q" value="shop"', form)

    def test_old_query_strings_still_work(self):
        html = self.html("month=2026-11&view=Nanami&currency=USD&cat=Uncategorized&q=&pending=0&review=")
        self.assertIn("Nanami Shop", html)
        self.assertNotIn("Young Shop", html)
        off_legend = self.html("month=2026-11&cat=Income")                        # a category that has no legend chip
        self.assertIn("Category: Income", off_legend)
        self.assertIn("✕ Clear", off_legend)


if __name__ == "__main__":
    unittest.main()

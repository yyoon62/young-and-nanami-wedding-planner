"""L26 part 6: the row category chip's one-click menu (fictional data, temp database)."""
from datetime import date
from decimal import Decimal
import re
import unittest
from urllib.parse import urlencode

from transactions_ui import RouteProblem, post_transactions, render_transactions
from tx_fixture import TOKEN, VIEWS, Fixture, tx


class Pick(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.account("Bank")
        self.add("Bank", tx("a", "2026-11-05", "-41.30", "Harbor Fuel"), tx("b", "2026-11-12", "-18.75", "Harbor Fuel"),
                 tx("c", "2026-11-19", "-27.00", "Corner Coffee"), tx("d", "2026-11-20", "-9.00", "Pending Shop", pending=True))
        self.sync(include_pending=True)
        self.cat("Dining")
        self.fuel = next(r["row_id"] for r in self.snap().rows if r["amount"] == "-41.30")

    def html(self, query="month=2026-11", **kw):
        return render_transactions(self.db, query, token=TOKEN, allowed_views=VIEWS, today=date(2026, 11, 28), **kw)

    def me(self):
        return next(r for r in self.snap().rows if r["row_id"] == self.fuel)

    def raw(self, fields):
        """Post exactly these fields (a list of pairs, so a field can repeat)."""
        data = urlencode([("token", TOKEN), ("month", "2026-11"), ("view", "Household"), ("row", str(self.fuel)), *fields])
        return post_transactions(self.db, "/transactions/edit", data.encode(), token=TOKEN, allowed_views=VIEWS)

    def test_one_click_pick_changes_only_that_row_and_is_manual(self):
        result = self.post(self.fuel, pick="Transport")
        self.assertEqual(result.location, "/transactions?month=2026-11&view=Household&currency=&q=&pending=0&cat=&review="
                                          f"#tx-{self.fuel}")
        rows = [r for r in self.snap().rows if r["description"] == "Harbor Fuel"]
        self.assertEqual(sorted((r["category"], r["category_source"]) for r in rows),
                         [("", "none"), ("Transport", "manual")])
        self.assertEqual(self.db.execute("SELECT count(*) FROM transaction_merchant_rules").fetchone()[0], 0)

    def test_pick_with_the_toggle_writes_one_rule_and_a_later_sync_row_arrives_categorized(self):
        self.post(self.fuel, pick="Transport", apply="1")
        self.assertEqual(self.db.execute("SELECT merchant, category FROM transaction_merchant_rules").fetchall(),
                         [("harbor fuel", "Transport")])
        self.add("Bank", tx("e", "2026-11-25", "-33.00", "Harbor Fuel"))
        self.db.execute("DELETE FROM transaction_coverage")          # the next sync re-fetches the whole window
        self.db.commit()
        self.sync()
        fuel = [r for r in self.snap().rows if r["description"] == "Harbor Fuel"]
        self.assertEqual({(r["category"], r["category_source"]) for r in fuel}, {("Transport", "rule")})
        self.assertEqual(len(fuel), 3)

    def test_picking_the_current_category_pins_it_so_a_suggestion_can_never_change_it(self):
        self.db.execute("INSERT INTO classifier_cache(clean_key, category, emoji, confidence, status) "
                        "VALUES ('corner coffee','Dining','☕',0.9,'suggested')")
        self.db.commit()
        row = self.row("Corner Coffee")
        self.assertEqual(row["category_source"], "suggested")
        self.post(row["row_id"], pick="Dining")
        self.assertEqual(self.row("Corner Coffee")["category_source"], "manual")
        self.db.execute("UPDATE classifier_cache SET category='Housing' WHERE clean_key='corner coffee'")
        self.db.commit()
        self.assertEqual(self.row("Corner Coffee")["category"], "Dining")

    def test_uncategorized_pick_clears_and_pins_and_cannot_be_applied(self):
        self.post(self.fuel, pick="Transport")
        self.post(self.fuel, pick="")
        self.assertEqual((self.me()["category"], self.me()["category_source"]), ("", "manual"))
        with self.assertRaises(RouteProblem) as ctx:
            self.post(self.fuel, pick="", apply="1")
        self.assertEqual(ctx.exception.status, 422)

    def test_new_category_is_added_and_applied_and_blank_or_duplicate_is_refused_with_the_menu_open(self):
        self.post(self.fuel, pick_new="1", new_category="Pets")
        self.assertIn("Pets", [r[0] for r in self.db.execute("SELECT name FROM transaction_categories")])
        self.assertEqual(self.me()["category"], "Pets")
        for typed, message in (("", "Enter a category name."), ("dining", "already exists"), ("pets", "already exists")):
            with self.assertRaises(RouteProblem) as ctx:
                self.post(self.fuel, pick_new="1", new_category=typed)
            self.assertEqual(ctx.exception.status, 422)
            self.assertIn(message, str(ctx.exception))
            body = ctx.exception.body
            self.assertRegex(body, rf'<details class="inline-pop tx-pick[^"]*" open (?:data-up="1" )?data-row="{self.fuel}"')
            self.assertEqual(body.count('class="tx-menu-err" role="alert"'), 1)
            self.assertEqual(body.count('<p class="notice" role="alert">'), 0)          # one message, inside the menu
            self.assertEqual(len(re.findall(r"<details class=\"inline-pop tx-pick[^>]* open ", body)), 1)

    def test_enter_in_the_name_field_adds_a_category_never_picks_the_first_one(self):
        page = self.html()
        template = page[page.index('<template id="tx-pick-tpl">'):page.index("</template>")]
        first = re.search(r'<button type="submit"[^>]*>', template[template.index('name="row"'):]).group(0)
        self.assertIn('name="pick_new"', first)
        self.assertNotIn('name="pick"', first)
        self.assertIn('class="sr"', first)

    def test_transfer_item_marks_this_row_or_the_merchant_and_a_category_pick_undoes_it(self):
        before = self.snap().totals["USD"].spending
        self.post(self.fuel, pick_transfer="1")
        self.assertEqual(self.snap().totals["USD"].spending, before - Decimal("41.30"))
        self.post(self.fuel, pick="Transport")                     # choosing a category undoes the manual transfer mark
        self.assertEqual(self.snap().totals["USD"].spending, before)
        self.post(self.fuel, pick_transfer="1", apply="1")          # with the toggle: the merchant on this account
        self.assertEqual(self.db.execute("SELECT merchant, is_transfer FROM transaction_merchant_rules").fetchall(),
                         [("harbor fuel", 1)])
        fuel = [r for r in self.snap().rows if r["description"] == "Harbor Fuel"]
        self.assertTrue(all(r["effective_transfer"] for r in fuel))

    def test_pending_rows_cannot_make_rules_and_do_not_offer_the_toggle(self):
        pending = next(r for r in self.snap(show_pending=True).rows if r["pending"])
        with self.assertRaises(RouteProblem) as ctx:
            self.post(pending["row_id"], pick="Dining", apply="1", pending="1")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn('data-rule="0"', self.html("month=2026-11&pending=1"))

    def test_tampered_posts_are_rejected_and_nothing_changes(self):
        for fields in ([("pick", "Dining"), ("pick_new", "1")], [("pick", "Dining"), ("action", "save")],
                       [("pick_new", "2")], [("pick_transfer", "1"), ("pick", "Dining")],
                       [("pick", "Dining"), ("pick", "Travel")]):
            with self.assertRaises(RouteProblem) as ctx:
                self.raw(fields)
            self.assertEqual(ctx.exception.status, 400, fields)
        with self.assertRaises(RouteProblem) as ctx:
            self.post(self.fuel, pick="Not a category")
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual((self.me()["category"], self.me()["category_source"]), ("", "none"))

    def test_the_chip_shows_the_category_with_its_emoji_and_the_last_rows_open_upward(self):
        self.post(self.fuel, pick="Transport")
        page = self.html()
        self.assertIn("🚇 Transport ▾</summary>", page)
        self.assertEqual(page.count('class="inline-pop tx-pick up"'), 3)         # three rows in this month
        self.assertIn('<div class="table-wrap tx-wrap">', page)


if __name__ == "__main__":
    unittest.main()

"""L29 real-Chrome checks: History | Projection tabs on Accounts, one context, one chart, phone and no-JS. Fictional DB only."""
from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from browser_tx import launch
from l29_fixture import running, seed
from tx_fixture import block_network


class BrowserProjection(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="fictional-l29-", dir=Path(tempfile.gettempdir()).resolve()))
        self.path = Path(tmp) / "accounts.sqlite3"
        seed(self.path)
        self.enterContext(block_network(allow_loopback=True))
        self.origin = f"http://127.0.0.1:{self.enterContext(running(self.path)).server_port}"
        self.chrome = launch(self.enterContext(sync_playwright()))
        self.addCleanup(self.chrome.close)

    @contextmanager
    def page(self, width=1366, height=900, javascript=True, refused=False):
        context = self.chrome.new_context(viewport={"width": width, "height": height}, java_script_enabled=javascript,
                                          service_workers="block", is_mobile=width < 500, has_touch=width < 500)
        outside, errors = [], []
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/")
                      else (outside.append(route.request.url), route.abort()))
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text)
                if message.type == "error" and not (refused and "status of 422" in message.text) else None)
        try:
            yield page
            self.assertEqual((outside, errors), ([], []))
        finally:
            context.close()

    def saved(self):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(((v, c), a) for v, c, a in db.execute("SELECT view, currency, assumptions FROM projection_assumptions"))

    def tab(self, page, label):
        return page.locator("nav[aria-label='Net worth chart']").get_by_role("link", name=label, exact=True)

    # ---- examples 4, 5: switch and one context -------------------------------------------------------------------
    def test_example_4_switch_marks_the_tab_keeps_the_view_and_is_a_plain_shareable_link(self):
        with self.page() as page:
            page.goto(self.origin + "/")
            expect(self.tab(page, "History")).to_have_attribute("aria-current", "page")
            self.assertEqual(page.locator("#projection-heading").count(), 0)
            self.tab(page, "Projection").click()
            expect(self.tab(page, "Projection")).to_have_attribute("aria-current", "page")
            self.assertRegex(page.url, r"/\?chart=projection#net-worth$")
            expect(page.locator("#projection-assumptions")).to_be_visible()
            expect(page.get_by_text("Monthly take-home income:").first).to_be_visible()
            self.tab(page, "History").click()
            expect(self.tab(page, "History")).to_have_attribute("aria-current", "page")
            expect(page.locator("#history-heading")).to_be_visible()

    def test_example_4_and_11_without_javascript_both_tabs_work_as_plain_links(self):
        with self.page(javascript=False) as page:
            page.goto(self.origin + "/?chart=projection")
            expect(page.locator("#projection-assumptions")).to_be_visible()
            self.tab(page, "History").click()
            expect(page.locator("#history-heading")).to_be_visible()
            self.tab(page, "Projection").click()
            expect(page.locator("#projection-assumptions")).to_be_visible()
            page.get_by_label("Monthly spending").fill("2500")
            page.get_by_role("button", name="Save assumptions").click()
            expect(page.locator("input[name=spending]")).to_have_value("2500")
            expect(self.tab(page, "Projection")).to_have_attribute("aria-current", "page")

    def test_example_5_one_context_owner_view_and_currency_carry_across_tabs_and_actions(self):
        with self.page() as page:
            page.goto(self.origin + "/")
            page.locator("nav[aria-label='Inventory views']").get_by_role("link", name="Nanami").click()
            page.locator("nav[aria-label='Currency']").get_by_role("link", name="EUR").click()
            self.assertIn("view=Nanami", page.url)
            self.assertIn("currency=EUR", page.url)
            history = page.locator("section[aria-labelledby=history-heading]")
            expect(history.get_by_role("heading", name="EUR net worth")).to_be_visible()
            self.assertEqual(history.get_by_role("heading", name="USD net worth").count(), 0)
            self.tab(page, "Projection").click()
            self.assertEqual(page.locator("select[name=currency]").count(), 0)          # no second picker
            self.assertEqual(page.locator("nav[aria-label='Currency']").count(), 1)
            expect(page.locator("form.projection-form input[name=currency]")).to_have_value("EUR")
            expect(page.locator("form.projection-form input[name=view]")).to_have_value("Nanami")
            expect(page.get_by_text("Start: EUR 9,000.00").first).to_be_visible()
            # an Accounts-table action keeps the tab, the owner view and the currency
            row = page.locator("tr.acct-row", has_text="Nanami EUR Checking")
            row.get_by_role("button", name="Young").click()
            expect(self.tab(page, "Projection")).to_have_attribute("aria-current", "page")
            self.assertIn("view=Nanami", page.url)
            self.assertIn("currency=EUR", page.url)
            self.tab(page, "History").click()
            expect(page.locator("nav[aria-label='Currency']").get_by_role("link", name="EUR")).to_have_attribute("aria-current", "page")

    # ---- example 6: the first screen -----------------------------------------------------------------------------
    def test_example_6_first_screen_has_the_tab_bar_and_the_whole_chart_and_at_most_three_short_lines_above_it(self):
        with self.page(1366, 900) as page:
            page.goto(self.origin + "/?chart=projection")
            bar = page.locator(".nw-bar").bounding_box()
            chart = page.locator(".net-chart").bounding_box()
            self.assertLessEqual(bar["y"] + bar["height"], 900)
            self.assertLessEqual(chart["y"] + chart["height"], 900, "the whole chart (axis and zone labels included)")
            lines = page.evaluate("""() => [...document.querySelectorAll('.nw-proj > *')]
                .filter(el => el.compareDocumentPosition(document.querySelector('.net-chart')) & Node.DOCUMENT_POSITION_FOLLOWING
                              && el.getClientRects().length && !el.classList.contains('sr'))
                .map(el => [el.tagName, Math.round(el.getBoundingClientRect().height)])""")
            self.assertLessEqual(len(lines), 3, lines)
            self.assertTrue(all(height <= 52 for _, height in lines), lines)          # each is one short line (two at most)
            expect(page.locator("details.nw-top-info")).not_to_have_attribute("open", "")
            expect(page.locator("svg.nw-plot")).to_be_visible()
            expect(page.locator(".nw-zone-saved")).to_be_visible()
            expect(page.locator(".nw-zone-proj")).to_be_visible()
            # saved zone is legible: a visible, non-trivial width at a fixed share of the chart
            zone = page.locator("svg.nw-plot").bounding_box()
            self.assertGreater(zone["width"], 800)
            saved = page.locator("svg .nw-saved").first.bounding_box()
            self.assertGreater(saved["width"], 120)
            # the suggestion card is directly below the chart, before the start/projected cards
            ys = page.evaluate("""() => ['.net-chart', '#projection-suggestion', '.projection-cards']
                .map(s => document.querySelector(s).getBoundingClientRect().top)""")
            self.assertEqual(ys, sorted(ys))
            page.locator("details.nw-top-info > summary").click()
            expect(page.get_by_text("not financial advice or a forecast")).to_be_visible()   # nothing dropped: one click away

    def test_example_6_chart_pixels_the_saved_line_is_solid_and_the_projection_is_dashed_and_lighter(self):
        with self.page() as page:
            page.goto(self.origin + "/?chart=projection")
            style = lambda selector: page.evaluate(
                "s => { const c = getComputedStyle(document.querySelector(s)); return [c.strokeDasharray, c.stroke]; }", selector)
            saved, projected = style("svg .history-trace.nw-saved"), style("svg .nw-projected")
            self.assertEqual(saved[0], "none")
            self.assertNotEqual(projected[0], "none")
            self.assertNotEqual(saved[1], projected[1])

    # ---- examples 7, 8: save and errors --------------------------------------------------------------------------
    def test_example_7_saving_returns_to_the_same_tab_view_and_currency_and_changes_nothing_else(self):
        with self.page() as page:
            page.goto(self.origin + "/?view=Young&currency=USD&chart=projection")
            page.get_by_label("Monthly take-home income").fill("4321")
            page.get_by_role("button", name="Save assumptions").click()
            expect(self.tab(page, "Projection")).to_have_attribute("aria-current", "page")
            self.assertIn("view=Young", page.url)
            expect(page.locator("input[name=income]")).to_have_value("4321")
            expect(page.locator("form.projection-form input[name=view]")).to_have_value("Young")
            page.goto(self.origin + "/?view=Nanami&currency=EUR&chart=projection")
            expect(page.locator("input[name=income]")).to_have_value("0")
        self.assertEqual(sorted(self.saved()), [("Household", "USD"), ("Young", "USD")])

    def test_example_8_an_invalid_value_shows_inside_the_projection_tab_keeps_my_values_and_saves_nothing(self):
        before = self.saved()
        with self.page(refused=True) as page:     # the refused save is a deliberate 422 response
            page.goto(self.origin + "/?chart=projection")
            page.locator("input[name=years]").fill("-1")
            page.locator("input[name=income]").fill("12,5")
            page.get_by_role("button", name="Save assumptions").click()
            expect(self.tab(page, "Projection")).to_have_attribute("aria-current", "page")
            expect(page.locator("#error-years")).to_be_visible()
            expect(page.locator("input[name=years]")).to_have_value("-1")
            expect(page.locator("input[name=income]")).to_have_value("12,5")
            expect(page.locator(".nw-failed")).to_be_visible()
            self.assertTrue(page.evaluate("""() => { const r = document.querySelector('#error-years').getBoundingClientRect();
                return r.top >= 0 && r.bottom <= innerHeight; }"""))                  # the form anchor scrolled it into view
            self.assertEqual(page.locator("table.acct-table").count() > 0, True)      # still the Accounts page
        self.assertEqual(self.saved(), before)

    # ---- example 11: phone ---------------------------------------------------------------------------------------
    def test_example_11_phone_no_horizontal_page_scroll_tabs_tall_enough_and_a_readable_chart(self):
        with self.page(390, 844) as page:
            for target in ("/", "/?chart=projection", "/?view=Nanami&currency=EUR&chart=projection"):
                page.goto(self.origin + target)
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), 390, target)
                for link in page.locator(".nw-bar a").all():
                    self.assertGreaterEqual(link.bounding_box()["height"], 28, target)
            page.goto(self.origin + "/?chart=projection")
            plot = page.locator("svg.nw-plot").bounding_box()
            self.assertLessEqual(plot["x"] + plot["width"], 390)
            self.assertGreater(plot["width"], 280)
            self.assertGreater(page.locator("svg .nw-saved").first.bounding_box()["width"], 60)   # the saved zone is not a dot
            labels = page.locator(".nw-axis .nw-cell").first
            first, last = labels.locator("time").all()
            a, b = first.bounding_box(), last.bounding_box()
            self.assertLess(a["x"] + a["width"], b["x"])                               # saved-date labels do not overlap
            expect(page.locator(".nw-zone-proj")).to_be_visible()


if __name__ == "__main__":
    unittest.main()

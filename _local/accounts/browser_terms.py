"""Real Chrome: account terms drawer, flags and the Projections debt table (L23). Fictional DB, loopback only."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from browser_tx import launch, running
from terms_demo import seed_terms_demo
from tx_fixture import block_network


class BrowserTerms(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp).resolve() / "fictional.sqlite3"
        self.requests, self.errors, self.console = [], [], []

    def session(self, pw, width=1280):
        chrome = launch(pw)
        page = chrome.new_page(viewport={"width": width, "height": 900})
        page.on("request", lambda r: self.requests.append(r.url))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.console.append(m.text) if m.type == "error" else None)
        return chrome, page

    def finish(self, expected_422=0):
        self.assertEqual(self.errors, [])
        self.assertEqual([m for m in self.console if "status of 422" not in m], [])   # the one deliberate validation 422
        self.assertEqual(len(self.console), expected_422)
        self.assertTrue(all(u.startswith("http://127.0.0.1:") for u in self.requests), self.requests)

    def terms(self, nickname):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT t.rate, t.planned_payment, t.min_payment, t.paid_in_full FROM account_terms t
                JOIN accounts a ON a.id=t.account_id WHERE a.nickname=?""", (nickname,)).fetchone()

    def test_drawer_terms_flags_validation_and_toggle_persist(self):
        seed_terms_demo(self.path)
        with block_network(allow_loopback=True), running(self.path) as origin, sync_playwright() as pw:
            chrome, page = self.session(pw)
            page.goto(origin + "/")
            loan = page.locator("tr.acct-row", has_text="Maple Student Loan B")
            expect(loan).to_contain_text("rate missing")
            expect(loan).to_contain_text("payment missing")
            expect(page.locator("tr.acct-row", has_text="Oak Everyday Card")).to_contain_text("paid in full")
            expect(page.locator("tr.acct-row", has_text="Birch Checking")).not_to_contain_text("rate missing")
            expect(page.locator("tr.acct-row", has_text="Birch Savings")).to_contain_text("rate missing")
            loan.get_by_role("link", name="Edit").click()
            drawer = page.locator("tr.edit-drawer:not([hidden])")
            expect(drawer.locator("input[name=t_rate]")).to_be_visible()
            expect(drawer.locator("input[name=t_pif]")).not_to_be_checked()
            drawer.locator("input[name=t_rate]").fill("abc")
            with page.expect_response(lambda r: r.request.method == "POST") as response:
                drawer.get_by_role("button", name="Save changes").click()
            self.assertEqual(response.value.status, 422)
            drawer = page.locator("tr.edit-drawer:not([hidden])")
            expect(drawer).to_have_count(1)                                   # the drawer is kept open
            expect(page.locator("[role=alert]").first).to_contain_text("Interest rate (APR)")
            drawer.locator("input[name=t_rate]").fill("7.2")
            drawer.locator("input[name=t_min]").fill("60.00")
            with page.expect_navigation():
                drawer.get_by_role("button", name="Save changes").click()
            self.assertEqual(self.terms("Maple Student Loan B"), ("7.2", "", "60.00", None))
            expect(page.locator("tr.acct-row", has_text="Maple Student Loan B")).not_to_contain_text("rate missing")
            # A card is paid in full by default; unticking it persists across a reload.
            card = page.locator("tr.acct-row", has_text="Oak Everyday Card")
            card.get_by_role("link", name="Edit").click()
            drawer = page.locator("tr.edit-drawer:not([hidden])")
            expect(drawer.locator("input[name=t_pif]")).to_be_checked()
            drawer.locator("input[name=t_pif]").uncheck()
            with page.expect_navigation():
                drawer.get_by_role("button", name="Save changes").click()
            self.assertEqual(self.terms("Oak Everyday Card")[3], 0)
            page.reload()
            expect(page.locator("tr.acct-row", has_text="Oak Everyday Card")).to_contain_text("rate missing")
            chrome.close()
        self.finish(expected_422=1)

    def test_projections_debt_table_schedule_observed_and_phone_layout(self):
        seed_terms_demo(self.path, complete=True)
        with block_network(allow_loopback=True), running(self.path) as origin, sync_playwright() as pw:
            for width in (1366, 390):
                chrome, page = self.session(pw, width)
                for target in ("/", "/projections"):
                    page.goto(origin + target)
                    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    self.assertLessEqual(overflow, 1, f"{target} overflows the {width}px viewport by {overflow}px")
                section = page.locator("section[aria-labelledby=debt-heading]")
                expect(section.locator("tbody tr").first).to_be_visible()
                expect(section).to_contain_text("Birch Auto Loan")
                expect(section).to_contain_text("0% promo through Apr 2027, then 24.99%")
                expect(section).to_contain_text("avg of 3 months")
                expect(section).to_contain_text("1 card(s) assumed paid in full")
                cedar = section.locator("tbody tr", has_text="Cedar Rewards Card").first
                expect(cedar).to_contain_text("planned + extra")
                expect(cedar).to_contain_text("With extra:")
                expect(cedar).to_contain_text("Planned only:")
                expect(cedar).to_contain_text("Mar 2028")            # the planned-only date beside the with-extra one
                expect(cedar).to_contain_text("Aug 2027")
                expect(section).to_contain_text("Avalanche vs snowball")
                expect(section).to_contain_text("Stale")
                section.get_by_text("Schedule: Cedar Rewards Card").click()
                expect(section).to_contain_text("rate changes to 24.99%")
                chrome.close()
        self.finish()


if __name__ == "__main__":
    unittest.main()

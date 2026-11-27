"""Browser-level layout/accessibility regressions (Chrome, synthetic data, temp DB).

Checks *actual rendered* above-the-fold content at 390x844 and 1366x850, no
silent owner on create, and responsive no-overflow widths. No backend change.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

from demo_batch import Demo, seed
from server import serve


def in_viewport(loc):
    box = loc.bounding_box()
    if not box:
        return False
    return box["y"] >= 0 and (box["y"] + box["height"]) <= loc.page.viewport_size["height"] + 1


class LayoutFlow(unittest.TestCase):
    def run(self, *a, **kw):
        with tempfile.TemporaryDirectory() as tmp, serve(Path(tmp) / "x.db", 0, Demo()) as s, sync_playwright() as pw:
            s.demo = True
            with closing(sqlite3.connect(s.db)) as db, db:
                seed(db)
            thread = Thread(target=s.serve_forever, daemon=True)
            thread.start()
            try:
                self.url = f"http://127.0.0.1:{s.server_port}"
                self.s = s
                browser = pw.chromium.launch(channel="chrome")
                self.browser = browser
                try:
                    return super().run(*a, **kw)
                finally:
                    browser.close()
            finally:
                s.shutdown()
                thread.join()

    def page(self, w, h):
        page = self.browser.new_page(viewport={"width": w, "height": h})
        page.set_default_timeout(5000)
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(self.url + "/") else r.abort())
        return page

    def overflow(self, page, w):
        return page.evaluate("document.documentElement.scrollWidth") - w

    def apply_all(self, page):
        page.goto(self.url + "/batch")
        page.get_by_text("Discover accounts for review").click()
        page.get_by_role("button", name="Select all review rows").click()
        page.locator("[name=owner]").select_option("Young")
        for i in range(12):
            m = page.locator(f"[name=m{i}]")
            for d in m.locator("xpath=ancestor::details[not(@open)]").all():
                d.locator(":scope > summary").click()
            m.select_option("1" if i == 0 else "new")
            page.locator(f"[name=d{i}]").select_option("debt" if i == 0 else "asset")
        page.locator("[name=o1]").select_option("Nanami")
        page.get_by_role("button", name="Apply reviewed selections").click()
        page.get_by_text("Linked balances", exact=True).wait_for()

    # 1) At scrollY=0, phone shows BOTH populated currency net + breakdowns.
    def test_390_totals_first_both_currencies_above_fold(self):
        page = self.page(390, 844)
        self.apply_all(page)
        page.goto(self.url)
        page.evaluate("scrollTo(0,0)")
        cards = page.locator(".card")
        self.assertEqual(cards.count(), 2)
        self.assertEqual(page.locator(".card strong").all_text_contents(), ["JPY 2,004", "USD 50.69"])
        for i in range(2):
            card = cards.nth(i)
            self.assertTrue(in_viewport(card.locator("strong")), f"net {i} above fold")
            self.assertTrue(in_viewport(card.locator("small")), f"label {i} above fold")
            self.assertTrue(in_viewport(card.locator("div").first), f"breakdown {i} above fold")
            self.assertIn("Assets", card.inner_text())
            self.assertIn("Debts", card.inner_text())
        self.assertLessEqual(self.overflow(page, 390), 0)

    # 2) /batch review first full account/amount/date/disclosure on phone + desktop.
    def test_review_above_fold_phone_and_desktop(self):
        for w, h, min_rows in [(390, 844, 1), (1366, 850, 3)]:
            page = self.page(w, h)
            page.goto(self.url + "/batch")
            page.get_by_text("Discover accounts for review").click()
            page.evaluate("scrollTo(0,0)")
            first = page.locator(".review-row").first
            self.assertTrue(in_viewport(first.locator(".row-include")), f"first row include @{w}")
            self.assertTrue(in_viewport(first.locator(".row-amount")), f"first row amount @{w}")
            self.assertTrue(in_viewport(first.locator(".row-date")), f"first row date @{w}")
            self.assertTrue(in_viewport(first.locator(".row-src")), f"first row account @{w}")
            self.assertTrue(in_viewport(first.locator("summary").first), f"first row disclosure @{w}")
            # active primary action reachable without scrolling 12 rows first
            apply = page.get_by_role("button", name="Apply reviewed selections")
            self.assertTrue(in_viewport(apply), f"apply reachable @{w}")
            # full account name + signed amount + date present for first row
            src = first.locator(".row-src").inner_text()
            self.assertIn("Cedar Credit", src)
            self.assertIn(first.locator(".row-amount").inner_text().split()[1], first.inner_text())
            self.assertRegex(first.inner_text(), r"as of 20\d\d-\d\d-\d\d")
            # count complete collapsed rows visible at scrollY=0
            visible = page.evaluate("""() => { const H=innerHeight; let n=0;
              for (const li of document.querySelectorAll('.review-row')) { const r=li.getBoundingClientRect();
                if (r.top>=0 && r.bottom<=H+1) n++; } return n; }""")
            self.assertGreaterEqual(visible, min_rows, f">= {min_rows} complete rows @{w}")
            self.assertLessEqual(self.overflow(page, w), 0)
            page.close()

    # 3) linked-first after apply; first row + prominent Refresh reachable.
    def test_linked_first_after_apply(self):
        page = self.page(390, 844)
        self.apply_all(page)
        page.evaluate("scrollTo(0,0)")
        self.assertTrue(in_viewport(page.locator(".linked-row").first.locator(".check")), "first linked row above fold")
        self.assertTrue(in_viewport(page.get_by_role("button", name="Refresh selected balances")), "refresh reachable")
        self.assertIn("Balance as of", page.locator(".linked-row").first.inner_text())
        self.assertIn("Last operation", page.locator(".linked-row").first.inner_text())
        self.assertIn("Checked (UTC)", page.locator(".linked-row").first.inner_text())
        self.assertLessEqual(self.overflow(page, 390), 0)

    # 4) no silent owner on create; intentional Unspecified; nothing pre-selected.
    def test_no_silent_owner_on_create(self):
        page = self.page(1366, 850)
        page.goto(self.url + "/batch")
        page.get_by_text("Discover accounts for review").click()
        # nothing pre-checked
        self.assertEqual(page.locator("input[type=checkbox]:checked").count(), 0)
        # batch owner has explicit blank placeholder; Unspecified available intentionally
        owner = page.locator("[name=owner]")
        self.assertEqual(owner.input_value(), "")
        self.assertIn("Unspecified", owner.inner_text())
        # choose one new row + side but no owner anywhere -> rejected, nothing created
        page.locator("[name=s1]").check()
        m = page.locator("[name=m1]")
        for d in m.locator("xpath=ancestor::details[not(@open)]").all():
            d.locator(":scope > summary").click()
        m.select_option("new")
        page.locator("[name=d1]").select_option("asset")
        page.get_by_role("button", name="Apply reviewed selections").click()
        page.locator("[role=alert]").wait_for()
        with sqlite3.connect(self.s.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 1)
        # per-row owner override exists; blank means "use default"
        self.assertTrue(page.locator("[name=o1]").count() == 1)

    # 5) responsive widths incl 320 / 390 / 1366 no page overflow.
    def test_responsive_widths_no_overflow(self):
        for w, h in [(320, 740), (390, 844), (1366, 850)]:
            page = self.page(w, h)
            for route in ("/", "/batch", "/new", "/servicers", "/federal-preview"):
                page.goto(self.url + route)
                self.assertLessEqual(self.overflow(page, w), 0, f"{route} @{w}")
            page.close()


if __name__ == "__main__":
    unittest.main()

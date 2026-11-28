"""Optional real headless Chrome check; writes PNGs only if L15_SCREENSHOT_DIR is set."""
import os
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from test_history import BASE, DAY, populate, request, running


class BrowserHistory(unittest.TestCase):
    def test_dated_chart_is_local_and_accessible_on_desktop_and_mobile(self):
        with tempfile.TemporaryDirectory() as tmp, running(Path(tmp) / "synthetic.sqlite") as server:
            origin = f"http://127.0.0.1:{server.server_port}"
            populate(server)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=True)
                for label, width, height in [("desktop", 1280, 980), ("mobile", 390, 844)]:
                    with self.subTest(viewport=label):
                        context = browser.new_context(viewport={"width": width, "height": height},
                                                      service_workers="block", is_mobile=label == "mobile",
                                                      has_touch=label == "mobile")
                        try:
                            self.exercise(context, origin, label)
                        finally:
                            context.close()
                browser.close()

    def exercise(self, context, origin, label):
        requests, blocked, errors = [], [], []

        def intercept(route):
            if route.request.url.startswith(origin + "/"):
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        context.route("**/*", intercept)
        page = context.new_page()
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        response = page.goto(origin + "/")
        self.assertEqual(response.headers["content-security-policy"],
                         "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                         "img-src 'self'; script-src 'self'; connect-src 'none'; form-action 'self'")
        history = page.get_by_role("region", name="Net worth history")
        expect(history.locator(".history-backdated")).to_have_count(0)
        usd = history.locator(".history-series").filter(has=page.get_by_role("heading", name="USD net worth"))
        expect(usd.get_by_role("img", name="USD net worth trend.", exact=False)).to_be_visible()
        expect(history.get_by_role("img", name="JPY net worth trend.", exact=False)).to_be_visible()
        expect(usd.locator(".history-dates").get_by_text("2024-11-01")).to_be_visible()
        expect(usd.locator(".history-latest").get_by_text("USD 100.00", exact=True)).to_be_visible()
        self.assertEqual(usd.locator(".history-trace").get_attribute("d"),
                         "M6.00 10.00 H300.00 V118.00 H594.00 V10.00")
        panel = history.bounding_box()
        self.assertLess(panel["y"] + panel["height"], page.locator(".acct-table:visible").bounding_box()["y"])
        self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"])
        rows = history.get_by_role("table", name="USD net worth by saved date").get_by_role("row")
        summary = history.get_by_text("Show USD dated totals", exact=True)
        expect(rows).to_have_count(0)
        summary.focus()
        page.keyboard.press("Enter")
        expect(rows).to_have_count(4)
        expect(rows.nth(2).get_by_role("rowheader")).to_have_text("2024-11-03")
        expect(rows.nth(2).get_by_role("cell")).to_have_text("USD 80.00")
        if label == "desktop":
            page.keyboard.press("Enter")
            expect(rows).to_have_count(0)
        page.evaluate("scrollTo(0, 0)")
        if output := os.environ.get("L15_SCREENSHOT_DIR"):
            page.screenshot(path=Path(output) / f"l15-history-{label}.png", full_page=True)

        page.get_by_role("link", name="Nanami").click()
        expect(history.get_by_role("img")).to_have_count(0)
        expect(history.get_by_text("Only one dated total; no trend yet.")).to_have_count(2)
        self.assertTrue(requests)
        self.assertEqual(blocked, [])
        self.assertTrue(all(url.startswith(origin + "/") for url in requests))
        self.assertEqual(errors, [])

    def test_backdated_edit_and_permanent_removal_in_browser(self):
        with tempfile.TemporaryDirectory() as tmp, running(Path(tmp) / "synthetic.sqlite") as server:
            origin = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(request(server, "/new", **BASE)[0], 303)
            self.assertEqual(request(server, "/edit/1", **dict(BASE, asof=DAY + "05", balance="120"))[0], 303)
            warning = "This account and all its saved balance history will be permanently deleted."
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page()
                page.goto(origin + "/remove/1")
                expect(page.get_by_text(warning, exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Confirm remove")).to_be_visible()
                page.get_by_role("link", name="Cancel").click()
                page.get_by_role("link", name="Young").click()
                history = page.get_by_role("region", name="Net worth history")
                note = history.get_by_text("After a backdated edit, history can show later saved balances "
                                           "than the account table.")
                expect(note).to_have_count(0)
                edit = page.get_by_role("link", name="Edit Harbor savings")
                edit.click()
                drawer = page.locator(".edit-drawer:visible")
                drawer.get_by_label("Balance as of").fill(DAY + "01")
                drawer.get_by_label("Balance", exact=True).fill("90")
                drawer.get_by_role("button", name="Save changes").click()
                expect(note).to_be_visible()
                row = page.locator(".acct-row")
                expect(row.locator("time")).to_have_text(DAY + "01")
                expect(row.locator(".bal-cell")).to_contain_text("USD 90.00")
                history.get_by_text("Show USD dated totals", exact=True).click()
                dated_rows = history.get_by_role("table").get_by_role("row")
                expect(dated_rows).to_have_count(3)
                expect(dated_rows.nth(2).get_by_role("rowheader")).to_have_text(DAY + "05")
                expect(dated_rows.nth(2).get_by_role("cell")).to_have_text("USD 120.00")
                edit.click()
                prompt = drawer.get_by_text(warning, exact=True)
                confirm = drawer.get_by_role("button", name="Confirm remove")
                expect(prompt).to_be_hidden()
                expect(confirm).to_be_hidden()
                drawer.get_by_text("Remove account…", exact=True).click()
                expect(prompt).to_be_visible()
                expect(confirm).to_be_visible()
                with page.expect_response(lambda r: r.url == origin + "/remove/1" and r.request.method == "POST") as submitted:
                    confirm.click()
                self.assertEqual(submitted.value.status, 303)
                expect(history.get_by_text("No dated balances in this view yet.")).to_be_visible()
                expect(history.locator(".history-series")).to_have_count(0)
                expect(row).to_have_count(0)
                browser.close()


if __name__ == "__main__":
    unittest.main()

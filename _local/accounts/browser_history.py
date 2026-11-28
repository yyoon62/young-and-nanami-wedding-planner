"""Optional real headless Chrome check; writes PNGs only if L15_SCREENSHOT_DIR is set."""
import os
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from test_history import populate, running


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


if __name__ == "__main__":
    unittest.main()

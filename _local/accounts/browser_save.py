"""Optional Chrome regression check: submit the real ECSI form to a temporary DB."""
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from playwright.sync_api import sync_playwright

from server import serve


class LocalSave(unittest.TestCase):
    def test_submit_from_browser(self):
        with tempfile.TemporaryDirectory() as tmp, serve(Path(tmp) / "test.db", 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel="chrome")
                    page = browser.new_page()
                    origin = f"http://127.0.0.1:{server.server_port}"
                    page.goto(origin + "/new?servicer=heartland-ecsi")
                    for name, value in dict(nickname="Example loan", currency="USD",
                                            balance="12.34", asof="2024-06-30").items():
                        page.locator(f"[name={name}]").fill(value)
                    with page.expect_response(lambda r: r.request.method == "POST") as submitted:
                        page.get_by_role("button", name="Save account").click()
                    response = submitted.value
                    self.assertEqual(response.status, 303, f"POST Origin: {response.request.header_value('origin')}")
                    page.wait_for_url(origin + "/")
                    self.assertEqual(page.get_by_role("article", name="Example loan").count(), 1)
                    self.assertIn("USD -12.34", page.locator("main").inner_text())
                    browser.close()
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()

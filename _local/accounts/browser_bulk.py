"""Real Chrome + local HTTP server + injectable offline Bridge. No external requests."""
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest

from playwright.sync_api import sync_playwright

from bridge import Bridge
from demo_batch import Demo, seed
from server import serve


@contextmanager
def running(path, bridge):
    with serve(path, 0, bridge) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class BrowserBulk(unittest.TestCase):
    def test_household(self):
        self.household(exclude_one=False)

    def test_select_all_then_exclude_configured_account(self):
        self.household(exclude_one=True)

    def household(self, exclude_one):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            root = Path(tmp).resolve()
            path = root / "fictional.db"
            payload = Demo().fetch()
            marker = "PRIVATE-FICTIONAL-MARKER"
            path_marker = "FICTIONAL-PATH-SECRET"
            access_root = "/simplefin/v2/" + path_marker if exclude_one else "/simplefin"
            access = root / "fictional.access"
            access.write_text(f"https://fiction:{marker}@beta-bridge.simplefin.org{access_root}")
            access.chmod(0o600)
            calls = []
            def offline(method, host, target, headers):
                calls.append((method, target))
                return 200, json.dumps(payload).encode()
            provider = Bridge(access, offline)
            browser = pw.chromium.launch(channel="chrome")
            ctx = browser.new_context(service_workers="block")
            page = ctx.new_page()
            requests, output = [], StringIO()
            ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin+"/")
                      else route.abort())
            ctx.on("request", lambda r: requests.append((r.url, r.post_data)))
            page.on("console", lambda msg: output.write(msg.text))
            with redirect_stderr(output), redirect_stdout(output):
                with running(path, provider) as origin:
                    with closing(sqlite3.connect(path)) as db, db:
                        seed(db)
                    page.goto(origin)
                    page.get_by_role("link", name="Batch balances").click()
                    page.get_by_text("Discover accounts for review").click()
                    self.assertIn("Review 12 balances", page.locator("main").inner_text())
                    self.assertNotIn(path_marker, page.content())
                    page.get_by_label("Default owner for new accounts").select_option("Young")
                    for i in range(12):
                        expose(page, f"[name=m{i}]")
                        page.locator(f"[name=m{i}]").select_option("1" if i == 0 else "new")
                        page.locator(f"[name=d{i}]").select_option("debt" if i == 0 else "asset")
                    page.locator("[name=o1]").select_option("Nanami")
                    page.get_by_role("button", name="Select all review rows").click()
                    self.assertEqual(page.locator(".review-row input[name^=s]:checked").count(), 12)
                    if exclude_one:
                        page.locator("[name=s11]").uncheck()
                        self.assertEqual(page.locator("[name=m11]").input_value(), "new")
                        self.assertEqual(page.locator(".review-row input[name^=s]:checked").count(), 11)
                    count = 11 if exclude_one else 12
                    yen = "JPY 1,503" if exclude_one else "JPY 2,004"
                    page.get_by_text("Apply reviewed selections").click()
                    page.get_by_text("Linked balances", exact=True).wait_for()
                    self.assertNotIn(path_marker, page.content())
                    page.get_by_role("link", name="Accounts").first.click()
                    rows = page.get_by_role("article")
                    self.assertEqual(page.locator(".lane.debt article").count(), 1)
                    self.assertEqual(page.locator(".lane.asset article").count(), count - 1)
                    self.assertEqual(rows.count(), count)
                    self.assertEqual(rows.filter(has_text="Maple Trust").filter(has_text="Cash").count(),
                                     0 if exclude_one else 1)
                    self.assertIn("Joint", rows.filter(has_text="Household card").inner_text())
                    self.assertIn("Nanami", rows.filter(has_text="Cedar Credit").filter(has_text="Savings").inner_text())
                    self.assertEqual(page.locator(".card strong").all_text_contents(), [yen, "USD 50.69"])
                    self.assertNotIn(marker, page.content())
                # Restart the server, reorder upstream rows and change two; refresh only one.
                for a in payload["accounts"][:2]:
                    a.update(balance="-22.89" if a["id"] == "0" else "345.67")
                    a["balance-date"] += 86400
                payload["accounts"].reverse()
                payload["errlist"] = [{"code":"con.auth", "conn_id":"2", "msg":marker + path_marker}]
                with running(path, provider) as origin:
                    page.goto(origin+"/batch")
                    # Nothing is checked for refresh; choose explicitly, all but r2.
                    for link in page.locator("input[name^=r]").all():
                        if link.get_attribute("name") != "r2":
                            link.check()
                    page.get_by_text("Refresh selected balances").click()
                    page.get_by_text("Institution sign-in required", exact=False).wait_for()
                    self.assertIn("provider error; retained", page.locator("main").inner_text())
                    self.assertNotIn(marker, page.content())
                    self.assertNotIn(path_marker, page.content())
                    page.get_by_role("link", name="Accounts").first.click()
                    self.assertEqual(page.get_by_role("article").count(), count)
                    self.assertEqual(page.locator(".card strong").all_text_contents(), [yen, "USD 47.81"])
            browser.close()
            self.assertEqual(calls, [("GET", access_root + "/accounts?version=2&balances-only=1")]*2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(marker, str(requests))
            self.assertTrue(all(url.startswith("http://127.0.0.1:") and "?" not in url for url, _ in requests))
            self.assertNotIn(marker.encode(), path.read_bytes())
            self.assertNotIn(path_marker, str(requests))
            self.assertNotIn(path_marker.encode(), path.read_bytes())


def expose(page, selector):
    """Open any ancestor native <details> so a control is visible (keyboard-accessible)."""
    loc = page.locator(selector)
    if loc.is_visible():
        return
    for d in loc.locator("xpath=ancestor::details[not(@open)]").all():
        d.locator(":scope > summary").click()


if __name__ == "__main__":
    unittest.main()

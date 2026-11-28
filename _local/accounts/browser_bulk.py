"""Real Chrome + local HTTP server + injectable offline Bridge. No external requests."""
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
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
def running(path, provider):
    with serve(path, 0, provider) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class BrowserBulk(unittest.TestCase):
    def test_one_click_sync_inline_tags_exclude_and_refresh(self):
        marker, path_marker = "FICTIONAL-BRIDGE-SECRET", "FICTIONAL-NESTED-ROOT"
        payload, calls = Demo().fetch(), []
        with tempfile.TemporaryDirectory() as tmp:
            path, access = Path(tmp) / "fictional.db", Path(tmp).resolve() / "access"
            access_root = "/simplefin/v2/" + path_marker
            access.write_text(f"https://user:{marker}@beta-bridge.simplefin.org{access_root}")
            access.chmod(0o600)
            def transport(method, host, target, headers):
                calls.append((method, target))
                return 200, json.dumps(payload).encode()
            provider = Bridge(access, transport)
            output, requests = StringIO(), []
            with sync_playwright() as pw, redirect_stdout(output), redirect_stderr(output):
                browser = pw.chromium.launch(channel=os.environ.get("BROWSER_CHANNEL", "chrome"))
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("request", lambda r: requests.append((r.url, r.post_data)))
                with running(path, provider) as origin:
                    with closing(sqlite3.connect(path)) as db, db:
                        seed(db)
                    page.goto(origin)
                    # Old /batch URL cleanly redirects to /
                    page.goto(origin + "/batch")
                    page.wait_for_url(origin + "/")
                    # One-click sync pulls all 12 accounts directly into the compact table
                    page.get_by_role("button", name="↻ Sync balances").click()
                    page.get_by_text("Synced: 12 added", exact=False).wait_for()
                    self.assertNotIn(path_marker, page.content())
                    self.assertNotIn(marker, page.content())
                    rows = page.locator("tbody tr.acct-row")
                    self.assertEqual(rows.count(), 13)
                    # Sections exist (Credit cards, Savings, Other)
                    self.assertGreaterEqual(page.locator("tbody[data-section='Savings']").count(), 1)
                    self.assertGreaterEqual(page.locator("tbody[data-section='Credit cards']").count(), 1)
                    # Exclude the seeded manual duplicate card so net worth reflects the 12 synced accounts
                    rows.filter(has_text="Household card").get_by_role("button", name="Exclude").click()
                    page.get_by_text("Excluded accounts (1)").wait_for()
                    self.assertEqual(page.locator(".card strong").all_text_contents(), ["JPY 2,004", "USD 50.69"])
                    # Inline owner tagging: click BOTH Young and Nanami on Card -> becomes Joint
                    def find_row(inst, name):
                        return page.locator("tr.acct-row", has=page.locator(".acct-sub", has_text=inst)).filter(
                            has=page.locator(".acct-name", has_text=name))
                    find_row("Cedar Credit", "Card").get_by_role("button", name="Young").click()
                    find_row("Cedar Credit", "Card").get_by_role("button", name="Nanami").click()
                    # Inline rename via edit icon and move section to Retirement
                    find_row("Cedar Credit", "Savings").get_by_role("button", name="Nanami").click()
                    sav_row = find_row("Cedar Credit", "Savings")
                    sav_row.locator("summary.icon-btn").click()
                    sav_row.locator("input[name=nickname]").fill("Emergency Fund")
                    sav_row.get_by_role("button", name="Save").click()
                    ef_row = find_row("Cedar Credit", "Emergency Fund")
                    ef_row.locator("summary.sec-chip").click()
                    ef_row.get_by_role("button", name="Retirement").click()
                    self.assertEqual(page.locator("tbody[data-section='Retirement']").filter(has_text="Emergency Fund").count(), 1)
                    # Institution SVG badge is rendered locally with zero external requests
                    self.assertGreaterEqual(page.locator("svg.inst-logo").count(), 12)
                    # In-place Edit drawer opens without leaving "/" and saves in place
                    ef_row = find_row("Cedar Credit", "Emergency Fund")
                    ef_row.locator("a.edit-trigger").click()
                    self.assertEqual(page.url, origin + "/")
                    drawer = page.locator("tr.edit-drawer:not([hidden])")
                    self.assertEqual(drawer.count(), 1)
                    drawer.locator("input[name=update_method]").fill("Auto sync")
                    drawer.get_by_role("button", name="Save changes").click()
                    page.wait_for_url(origin + "/")
                    self.assertEqual(page.locator("tr.edit-drawer:not([hidden])").count(), 0)
                    # Nanami view shows her Emergency Fund (+10.10) + Joint Card (-20.01) = -9.91
                    page.locator(".owner-tabs").get_by_role("link", name="Nanami").click()
                    self.assertEqual(page.locator(".card strong").all_text_contents(), ["USD -9.91"])
                    page.locator(".owner-tabs").get_by_role("link", name="Household").click()
                    # Check zero horizontal page overflow at desktop
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - innerWidth"), 0)
                # Restart server, update two balances + upstream name, add a provider error, and click Sync again
                for a in payload["accounts"][:2]:
                    a.update(balance="-22.89" if a["id"] == "0" else "20.10")
                    if a["id"] == "1":
                        a["name"] = "Upstream Savings Name"
                    a["balance-date"] += 86400
                payload["accounts"].reverse()
                payload["errlist"] = [{"code": "con.auth", "conn_id": "2", "msg": marker + path_marker}]
                with running(path, provider) as origin:
                    page.goto(origin)
                    page.get_by_role("button", name="↻ Sync balances").click()
                    page.get_by_text("Institution sign-in required", exact=False).wait_for()
                    self.assertNotIn(marker, page.content())
                    self.assertNotIn(path_marker, page.content())
                    # Custom name and Retirement section survived re-sync!
                    self.assertEqual(page.locator("tbody[data-section='Retirement']").filter(has_text="Emergency Fund").count(), 1)
                    self.assertNotIn("Upstream Savings Name", page.content())
                    # Card went -20.01 -> -22.89 (-2.88); Emergency Fund went +10.10 -> +20.10 (+10.00); net +7.12 -> 57.81
                    self.assertEqual(page.locator(".card strong").all_text_contents(), ["JPY 2,004", "USD 57.81"])
                    # Manual excluded row stayed excluded across sync
                    self.assertIn("Excluded accounts (1)", page.locator("main").inner_text())
                browser.close()
            self.assertEqual(calls, [("GET", access_root + "/accounts?version=2&balances-only=1")] * 2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(marker, str(requests))
            self.assertNotIn(marker.encode(), path.read_bytes())
            self.assertNotIn(path_marker, str(requests))


if __name__ == "__main__":
    unittest.main()

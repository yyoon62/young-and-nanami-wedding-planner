"""Optional Playwright acceptance check. Starts only an ephemeral fictional dashboard."""
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import os
import sqlite3
import tempfile
from threading import Thread
import unittest

from playwright.sync_api import sync_playwright
from server import serve


MARKER = "FICTIONAL-PRIVATE-FIELD"
FILE = f"""Student First Name:{MARKER}
Student Total All Loans Outstanding Principal:$888.88
Type Code:D1
Total <D1> Outstanding Interest:$777.77
Loan Type Code:D1
Loan Type Description:DIRECT STAFFORD SUBSIDIZED
Loan Award ID:{MARKER}
Loan Outstanding Principal Balance:$1,001.10
Loan Outstanding Principal Balance as of Date:04/30/2024
Loan Outstanding Interest Balance:$0.01
Loan Outstanding Interest Balance as of Date:05/01/2024
Loan Status:RP
Loan Contact Name:{MARKER}
Loan Type Code:D1
Loan Type Description:DIRECT STAFFORD SUBSIDIZED
Loan Outstanding Principal Balance as of Date:05/03/2024
Loan Outstanding Interest Balance:$0.00
Grant Type Code:XX
"""


class BrowserFlow(unittest.TestCase):
    def test_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fictional.db"
            logs = StringIO()
            with serve(db, 0) as server, redirect_stderr(logs), redirect_stdout(logs):
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.executemany("INSERT INTO accounts VALUES (NULL, 'Example', ?, 'Custom', ?, ?, ?, '2024-01-01')",
                                     [("Cedar", "asset", "USD", "11.10"), ("Birch", "debt", "JPY", "500")])
                original = db.read_bytes(), db.stat().st_mtime_ns
                thread = Thread(target=server.serve_forever)
                thread.start()
                try:
                    self.exercise(f"http://127.0.0.1:{server.server_port}")
                finally:
                    server.shutdown()
                    thread.join()
                self.assertEqual(original, (db.read_bytes(), db.stat().st_mtime_ns))
            self.assertEqual(logs.getvalue(), "")

    def exercise(self, origin):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.environ.get("BROWSER_CHANNEL", "chrome"))
            ctx = browser.new_context(service_workers="block")
            requests, console = [], []
            # Record every method/URL, including localhost, not just upload endpoints.
            ctx.on("request", lambda r: requests.append((r.method, r.url, r.post_data_buffer)))
            page = ctx.new_page()
            page.on("console", lambda m: console.append(m.text))
            page.on("pageerror", lambda e: console.append(str(e)))
            page.goto(origin)
            before = page.locator("main").inner_text()
            for field in ["Cedar", "Birch", "USD 11.10", "JPY -500"]:
                self.assertIn(field, before)
            open_link = page.get_by_role("link", name="Preview federal My Aid Data")
            open_link.click()
            page.wait_for_load_state()
            select, result = page.locator("#loan"), page.locator("#result")

            def inspect():
                self.assertNotIn(MARKER, page.content())
                self.assertEqual(page.locator("#file").input_value(), "")
                self.assertEqual(page.evaluate("""async () => [
                    localStorage.length, sessionStorage.length, (await indexedDB.databases()).length,
                    (await caches.keys()).length, document.cookie]"""), [0, 0, 0, 0, ""])

            def choose(text):
                with page.expect_file_chooser() as chooser:
                    page.get_by_role("button", name="Choose My Aid Data file").click()
                chooser.value.set_files({"name": MARKER + ".txt", "mimeType": "text/plain",
                                         "buffer": text.encode()})
                page.locator("#status:not(:empty)").wait_for()
                inspect()

            start = len(requests)
            choose(FILE.replace("\n", "\r\n"))
            self.assertEqual(select.locator("option").all_text_contents(), ["Choose a loan", "Loan 1", "Loan 2"])
            self.assertEqual(select.input_value(), "")
            self.assertEqual(result.inner_text(), "")
            for i, expected in enumerate([
                ["$1,001.10", "04/30/2024", "$0.01", "05/01/2024"],
                ["Not provided", "05/03/2024", "$0.00", "Not provided"],
            ]):
                select.select_option(str(i))
                self.assertEqual(result.locator("p").all_text_contents(), [
                    "Dated snapshot — not a payoff quote.",
                    f"Outstanding principal: {expected[0]}", f"Principal as of: {expected[1]}",
                    f"Outstanding interest: {expected[2]}", f"Interest as of: {expected[3]}"])
                inspect()
            self.assertEqual(requests[start:], [])
            page.get_by_role("link", name="Cancel and return to accounts").click()
            self.assertEqual(page.locator("main").inner_text(), before)
            open_link.click()
            page.wait_for_load_state()
            inspect()
            self.assertTrue(select.is_hidden())
            self.assertEqual(select.input_value(), "")
            self.assertEqual(result.inner_text(), "")
            start = len(requests)
            # Smallest date-shift trap: only the second loan supplies an interest date.
            date_line = "Loan Outstanding Interest Balance as of Date:05/01/2024"
            choose(FILE.replace(date_line, "").replace("Grant Type Code:", date_line + "\nGrant Type Code:"))
            select.select_option("0")
            self.assertIn("Interest as of: Not provided", result.inner_text())
            self.assertNotIn("05/01/2024", result.inner_text())
            for malformed in [
                FILE.replace("Loan Type Code:D1", "", 1),
                FILE.replace("Loan Outstanding Interest Balance:$0.01",
                             "Loan Outstanding Interest Balance:$0.01\nLoan Outstanding Interest Balance:$0.02"),
                FILE + "Loan Outstanding Interest Balance as of Date:05/04/2024",
            ]:
                choose(malformed)
                self.assertIn("Cannot safely separate", page.locator("#status").inner_text())
                self.assertTrue(select.is_hidden())
                self.assertEqual(result.inner_text(), "")
            choose(FILE.replace("$1,001.10", MARKER).replace("04/30/2024", "02/30/2024"))
            select.select_option("0")
            self.assertIn("Unrecognized amount", result.inner_text())
            self.assertIn("Unrecognized date", result.inner_text())
            inspect()
            self.assertEqual(requests[start:], [])
            self.assertEqual(ctx.cookies(), [])
            self.assertEqual(console, [])
            self.assertTrue(all(m == "GET" and data is None and url in
                                {origin + p for p in ["/", "/style.css", "/preview.js", "/federal-preview"]}
                                for m, url, data in requests))
            browser.close()


if __name__ == "__main__":
    unittest.main()

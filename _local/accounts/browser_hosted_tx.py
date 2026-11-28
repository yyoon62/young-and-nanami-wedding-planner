"""L30b: real headless Chrome on the hosted Transactions page (`--hosted --transactions`), fictional data only.

Only the Google library is stubbed (no external requests). Run it with the hosted packages on the path:
    PYTHONPATH=<hosted venv site-packages> python -m unittest -q browser_hosted_tx
Set L30B_SHOTS=/some/dir to also write desktop and phone screenshots.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from browser_tx import launch
from test_hosted import NOW, RSA, csrf, login, request, running
from tx_demo import frozen_today, seed_fictional

STUB = """
window.google = {accounts: {id: {
  initialize(options) { window.testGoogle = options; },
  renderButton(button) { button.textContent = 'Fictional Google popup stub'; },
}}};
"""


class BrowserHostedTransactions(unittest.TestCase):
    def test_member_sees_picks_a_category_and_signs_out(self):
        shots = Path(os.environ["L30B_SHOTS"]) if os.environ.get("L30B_SHOTS") else None
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            rsa = RSA(root)
            path = root / "state" / "accounts.sqlite3"
            path.parent.mkdir(mode=0o700)
            seed_fictional(path, categorize=True, joint_card=True)
            path.chmod(0o600)
            with frozen_today(), running(path, rsa, lambda: NOW, http=True, transactions=True) as server, \
                    sync_playwright() as pw:
                origin = server.auth.config.origin
                chrome = launch(pw)
                context = chrome.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
                external = []

                def intercept(route):
                    url = route.request.url
                    if url == "https://accounts.google.com/gsi/client":
                        route.fulfill(content_type="application/javascript", body=STUB)
                    elif url.startswith(origin + "/"):
                        route.continue_()
                    else:
                        external.append(url)
                        route.abort()

                context.route("**/*", intercept)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

                # Signed out: only the sign-in page, never a transaction.
                page.goto(origin + "/transactions")
                self.assertNotIn("Metro Transit", page.content())
                self.assertEqual(len(errors), 1)                                 # Chrome logs the refusal itself: 401
                self.assertIn("401", errors[0])
                errors.clear()

                page.goto(origin + "/")
                expect(page.get_by_text("Fictional Google popup stub")).to_be_visible()
                nonce = page.evaluate("window.testGoogle.nonce")
                with page.expect_response(lambda r: r.url.endswith("/auth/exchange")) as response:
                    page.evaluate("credential => { window.testGoogle.callback({credential}); }", rsa.make(nonce))
                self.assertEqual(response.value.status, 204)

                page.goto(origin + "/")
                page.get_by_role("link", name="Transactions").click()           # the nav link is present when opted in
                expect(page.locator("tr.acct-row").first).to_be_visible()
                row = page.locator("tr.acct-row", has_text="Metro Transit")
                expect(row).to_have_count(1)
                self.assertIn("Uncategorized", row.inner_text())                # left open by the fixture
                if shots:
                    page.screenshot(path=str(shots / "hosted-transactions-desktop-top.png"))
                    page.screenshot(path=str(shots / "hosted-transactions-desktop-full.png"), full_page=True)

                # One click on the chip, one click on a category: saves through the hosted POST (session + CSRF + origin).
                row.locator("details.tx-pick > summary").click()
                page.wait_for_timeout(250)
                if shots:
                    page.screenshot(path=str(shots / "hosted-transactions-desktop-menu.png"))
                page.locator("details.tx-pick[open] .sec-menu").get_by_role("button", name="Transport", exact=False).first.click()
                page.wait_for_load_state()
                expect(page.locator("tr.acct-row", has_text="Metro Transit").locator("details.tx-pick > summary")) \
                    .to_contain_text("Transport")
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT category FROM account_transactions "
                                                "WHERE description LIKE 'Metro Transit%' AND category_manual=1").fetchall(), [("Transport",)])   # that row only

                if shots:
                    phone = context.new_page()
                    phone.set_viewport_size({"width": 390, "height": 844})
                    phone.goto(origin + "/transactions")
                    phone.screenshot(path=str(shots / "hosted-transactions-phone-top.png"))
                    phone.close()

                # Sign out ends the session; the page is gone again.
                with page.expect_response(lambda r: r.url == origin + "/logout" and r.request.method == "POST") as logout:
                    page.get_by_role("button", name="Sign out").click()
                self.assertEqual(logout.value.status, 303)
                self.assertEqual(context.request.get(origin + "/transactions").status, 401)
                self.assertEqual(errors, [])
                self.assertEqual(external, [])
                chrome.close()


if __name__ == "__main__":
    unittest.main()

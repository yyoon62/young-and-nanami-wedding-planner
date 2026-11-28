"""Real headless Chrome, with only the Google library stubbed (no external requests)."""
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright

from test_history import BASE, DAY
from test_hosted import NOW, RSA, csrf, login, request, running


class BrowserHosted(unittest.TestCase):
    def test_popup_callback_editor_chart_and_signout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()
            rsa = RSA(path)
            with running(path / "fixture.db", rsa, lambda: NOW, http=True) as server, sync_playwright() as pw:
                origin = server.auth.config.origin
                jar = {}
                self.assertEqual(login(server, rsa, jar)[0], 204)
                token = csrf(server, jar)
                for target, account in (
                    ("/new", BASE), ("/edit/1", dict(BASE, balance="120.10", asof=DAY + "05")),
                ):
                    self.assertEqual(request(server, target, dict(account, token=token), jar=jar, origin=origin)[0], 303)
                chrome = pw.chromium.launch(channel="chrome")
                context = chrome.new_context(service_workers="block")
                external = []

                def intercept(route):
                    url = route.request.url
                    if url == "https://accounts.google.com/gsi/client":
                        route.fulfill(content_type="application/javascript", body="""
window.google = {accounts: {id: {
  initialize(options) { window.testGoogle = options; },
  renderButton(button) { button.textContent = 'Fictional Google popup stub'; },
}}};
""")
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
                page.goto(origin + "/")
                expect(page.get_by_text("Fictional Google popup stub")).to_be_visible()
                expect(page.get_by_text("Harbor savings")).to_have_count(0)
                nonce = page.evaluate("window.testGoogle.nonce")
                self.assertEqual(page.evaluate("document.cookie"), "")
                self.assertTrue(context.cookies()[0]["httpOnly"])
                # Simulate the callback's signed result; all subsequent network and DOM are real.
                with page.expect_response(lambda r: r.url.endswith("/auth/exchange")) as response:
                    page.evaluate("credential => { window.testGoogle.callback({credential}); }", rsa.make(nonce))
                self.assertEqual(response.value.status, 204)
                expect(page.get_by_role("img", name="USD net worth trend.", exact=False)).to_be_visible()
                self.assertEqual(page.evaluate("document.cookie"), "")
                page.get_by_role("link", name="Edit Harbor savings").click()
                drawer = page.locator(".edit-drawer:visible")
                expect(drawer).to_be_visible()
                drawer.get_by_label("Balance", exact=True).fill("120.25")
                drawer.get_by_role("button", name="Save changes").click()
                expect(page.locator(".card strong")).to_have_text("USD 120.25")
                page.get_by_role("link", name="Federal preview").click()
                expect(page.get_by_role("button", name="Choose My Aid Data file")).to_be_visible()
                page.get_by_role("button", name="Sign out").click()
                expect(page.get_by_role("heading", name="Sign in to the private dashboard")).to_be_visible()
                self.assertNotIn("accounts-session", [c["name"] for c in context.cookies()])
                self.assertEqual(context.request.get(origin + "/new").status, 401)
                self.assertEqual(errors, [])
                self.assertEqual(external, [])
                chrome.close()

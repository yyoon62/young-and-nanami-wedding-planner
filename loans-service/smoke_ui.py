"""Optional headless browser acceptance test. Route the fictional origin to ASGI in-process."""
import os
import unittest

from playwright.sync_api import expect, sync_playwright

import test_household as fixtures


class BrowserFlow(unittest.TestCase):
    def test_share_then_withdraw(self):
        # Reuse the HTTP suite's disposable backend and synthetic credential issuer.
        fixture = fixtures.Household()
        fixture.setUpClass()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM"),
                                         args=["--host-resolver-rules=MAP * ~NOTFOUND"])

            def route(request_route):
                r = request_route.request
                if not r.url.startswith(fixtures.ORIGIN + "/"):
                    return request_route.abort()
                fixture.client.cookies.clear()
                response = fixture.client.request(r.method, r.url, headers=r.all_headers(), content=r.post_data)
                request_route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            def member(who):
                ctx = browser.new_context(service_workers="block")
                ctx.route("**/*", route)
                page = ctx.new_page()
                page.goto(fixtures.ORIGIN)
                page.evaluate("credential => openDashboard({credential})", fixture.token(who))
                expect(page.locator("#panel")).to_be_visible()
                return page

            a, b = member(fixtures.A), member(fixtures.B)
            a.get_by_label("Name", exact=True).fill(fixtures.BODY["name"])
            a.get_by_label("Amount").fill("123.40")
            a.get_by_label("Currency").select_option("CAD")
            a.get_by_label("As of").fill("2024-02-29")
            a.get_by_role("button", name="Save", exact=True).click()
            expect(a.locator("#records")).to_contain_text("CAD 123.40 as of 2024-02-29 · Private")
            b.reload()
            expect(b.locator("#status")).to_contain_text("0 visible")
            a.get_by_role("button", name="Share read-only").click()
            expect(a.get_by_text("Unshare", exact=True)).to_be_visible()
            b.get_by_text("Refresh", exact=True).click()
            expect(b.locator("#records")).to_contain_text(fixtures.BODY["name"])
            expect(b.locator("#records")).to_contain_text("Shared with you · read-only")
            expect(b.locator("#records button, #records img")).to_have_count(0)
            a.get_by_text("Unshare", exact=True).click()
            expect(a.locator("#records")).to_contain_text("Private")
            b.get_by_text("Refresh", exact=True).click()
            expect(b.locator("#records li")).to_have_count(0)
            a.get_by_text("Sign out", exact=True).click()
            expect(a.locator("#panel")).to_be_hidden()
            a.reload()
            expect(a.locator("#status")).to_have_text("Please sign in.")
            browser.close()


if __name__ == "__main__":
    unittest.main()

"""L29 hosted: the Projection tab on Accounts and the redirect from the old address (fictional data, no network).

Run with the hosted venv: python -m unittest -q test_hosted test_hosted_projection
Reuses the harness of test_hosted (read-only) and seeds the same fictional database.
"""
from contextlib import closing
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_hosted import HAS_CRYPTO, NOW, ORIGIN, RSA, login, request, running
import seed_fictional                      # test_hosted puts deploy/ on sys.path
from hosted_auth import SESSION_TTL

FORM = dict(years="10", income="5000", spending="3000", debt_target="", retirement_target="", investment_target="",
            checking_apy="0", hysa_apy="4", investment_return="7", retirement_return="7", debt_apr="6",
            inflation="2.5", mode="nominal", view="Household", currency="USD")


@unittest.skipUnless(HAS_CRYPTO, "install requirements-hosted.txt for hosted acceptance")
class HostedProjectionTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.rsa = RSA(tmp.name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name).resolve() / "fictional.db"
        seed_fictional.seed(self.db)
        self.now = NOW
        ctx = running(self.db, self.rsa, lambda: self.now)
        self.server = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.jar = {}
        assert login(self.server, self.rsa, self.jar, email="member.alpha@gmail.com")[0] == 204

    def saved(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT view, currency, assumptions FROM projection_assumptions ORDER BY 1, 2").fetchall()

    def get(self, target, jar=None):
        status, headers, body = request(self.server, target, jar=self.jar if jar is None else jar)
        return status, headers, body.decode()

    def token(self):
        return re.search(r'name="token" value="([^"]+)"', self.get("/?chart=projection")[2])[1]

    def post(self, token, origin=ORIGIN, jar=None, **fields):
        return request(self.server, "/projections", dict(FORM, token=token, **fields),
                       jar=self.jar if jar is None else jar, origin=origin)

    def test_example_10_the_sub_tabs_work_and_hide_transaction_derived_data(self):
        status, headers, html = self.get("/?chart=projection")
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
        self.assertRegex(html, r'<a href="[^"]*" aria-current="page">Projection</a>')
        self.assertIn('id="projection-assumptions"', html)
        self.assertNotIn("projection-suggestion", html)
        self.assertNotIn("Suggested from your transactions", html)
        self.assertNotIn("Observed:", html.replace("observed payments", ""))
        for sentinel in (seed_fictional.SENTINEL_MERCHANT, seed_fictional.SENTINEL_DESCRIPTION,
                         *seed_fictional.SENTINEL_AMOUNTS):
            self.assertNotIn(sentinel, html)
        self.assertNotIn("/transactions", html)
        self.assertEqual(self.get("/transactions")[0], 404)
        self.assertEqual(self.get("/?chart=history")[0], 200)

    def test_example_2_and_10_the_old_address_redirects_only_for_a_signed_in_session(self):
        for target in ("/projections", "/projections?view=Young&currency=USD"):
            status, headers, body = request(self.server, target)
            self.assertEqual((status, headers["Cache-Control"], body), (401, "no-store", b"Sign in to continue."), target)
        status, headers, body = self.get("/projections?view=Young&currency=USD")
        self.assertEqual((status, headers["Location"]), (308, "/?view=Young&currency=USD&chart=projection#net-worth"))
        self.assertEqual(self.get("/projections?view=Young&currency=EUR")[0], 400)
        self.assertEqual(self.get("/projections?view=Mallory")[0], 400)
        self.assertEqual(request(self.server, "/?chart=projection")[0], 401)       # shareable link: session first

    def test_example_10_the_save_rejects_missing_csrf_wrong_origin_and_expired_session_and_saves_nothing(self):
        token = self.token()
        before = self.saved()
        for bad in ("", "wrong"):
            self.assertEqual(self.post(bad)[0], 403, bad)
        self.assertEqual(request(self.server, "/projections", dict(FORM), jar=self.jar, origin=ORIGIN)[0], 403)
        for origin in ("https://unapproved.test", ORIGIN + "/", ORIGIN.replace("https", "http"), None):
            self.assertEqual(self.post(token, origin=origin)[0], 403, origin)
        self.assertEqual(self.post(token, jar={})[0], 401)
        self.now += SESSION_TTL
        response = self.post(token)
        self.assertEqual((response[0], response[1]["Cache-Control"]), (401, "no-store"))
        self.assertEqual(self.saved(), before)

    def test_example_7_and_8_valid_save_returns_to_the_tab_and_invalid_renders_inline(self):
        token = self.token()
        status, headers, _ = self.post(token, view="Young", income="777")
        self.assertEqual((status, headers["Location"]), (303, "/?view=Young&currency=USD&chart=projection#net-worth"))
        self.assertRegex(self.get("/?view=Young&currency=USD&chart=projection")[2], r'name="income"[^>]*value="777"')
        before = self.saved()
        status, _, body = self.post(token, years="-1")
        self.assertEqual(status, 422)
        html = body.decode()
        self.assertIn('id="net-worth"', html)
        self.assertIn('id="error-years"', html)
        self.assertIn('value="-1"', html)
        self.assertNotIn("/transactions", html)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.post(token, currency="EUR")[0], 422)           # unavailable currency
        apply = self.post(token, apply="suggestion", suggest_income="1", suggest_spending="1")
        self.assertEqual(apply[0], 422)
        self.assertIn("not available", apply[2].decode())
        self.assertEqual(self.saved(), before)

    def test_account_table_actions_keep_the_tab_when_hosted_too(self):
        token = self.token()
        html = self.get("/?view=Nanami&currency=JPY&chart=projection")[2]
        self.assertIn('name="nw_chart" value="projection"', html)
        account = int(re.search(r'action="/owner/(\d+)"', html)[1])
        status, headers, _ = request(self.server, f"/owner/{account}", dict(
            token=token, owner="Young", view="Nanami", nw_chart="projection", nw_currency="JPY"),
            jar=self.jar, origin=ORIGIN)
        self.assertEqual((status, headers["Location"]), (303, "/?view=Nanami&currency=JPY&chart=projection"))


if __name__ == "__main__":
    unittest.main()

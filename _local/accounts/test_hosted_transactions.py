"""L30b: transactions on an opted-in host (`--hosted --transactions`). Fictional data, no network."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from test_hosted import HAS_CRYPTO, NOW, RSA, csrf, login, request, running   # also puts deploy/ on sys.path
import seed_fictional
from hosted_guard import Refused

MARKER = seed_fictional.SENTINEL_DESCRIPTION
THIRD = dict(email="stranger@gmail.com", sub="999")


@unittest.skipUnless(HAS_CRYPTO, "needs the hosted environment (requirements-hosted.txt)")
class HostedTransactions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.rsa = RSA(tmp.name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name).resolve() / "state" / "accounts.sqlite3"
        seed_fictional.seed(self.db)          # balances + transaction rows + coverage + a merchant rule

    def signed_in(self, server):
        jar = {}
        self.assertEqual(login(server, self.rsa, jar)[0], 204)
        return jar, csrf(server, jar)

    def row(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT rowid, category FROM account_transactions ORDER BY rowid LIMIT 1").fetchone()

    def test_default_host_still_refuses_to_start_with_transaction_rows(self):
        with self.assertRaises(Refused) as caught:
            with running(self.db, self.rsa):
                self.fail("must not start")
        self.assertIn("account_transactions", str(caught.exception))

    def test_opted_in_host_starts_with_transactions_and_unknown_tables_are_still_refused(self):
        with running(self.db, self.rsa, lambda: NOW, transactions=True):
            pass
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE surprise_table (x TEXT)")
            db.execute("INSERT INTO surprise_table VALUES ('not on the allowlist')")
        with self.assertRaises(Refused) as caught:
            with running(self.db, self.rsa, transactions=True):
                self.fail("must not start")
        self.assertIn("surprise_table", str(caught.exception))

    def test_a_local_server_cannot_be_told_it_is_hosted_transactions(self):
        from server import serve
        with self.assertRaises(ValueError):
            serve(self.db, 0, transactions=True)

    def test_signed_out_and_non_members_get_nothing(self):
        with running(self.db, self.rsa, lambda: NOW, transactions=True) as server:
            for target in ("/transactions", "/transactions?period=all"):
                status, _, body = request(server, target)
                self.assertEqual(status, 401, target)
                self.assertNotIn(MARKER.encode(), body)
            status, _, body = request(server, "/transactions/edit", {"token": "x", "view": "Household"},
                                      origin=server.auth.config.origin)
            self.assertEqual(status, 401)
            jar = {}
            self.assertNotEqual(login(server, self.rsa, jar, **THIRD)[0], 204)      # a third Google account is refused
            status, _, body = request(server, "/transactions", jar=jar)
            self.assertEqual(status, 401)
            self.assertNotIn(MARKER.encode(), body)

    def test_members_see_transactions_with_hosted_headers_and_a_nav_link(self):
        with running(self.db, self.rsa, lambda: NOW, transactions=True) as server:
            jar, _ = self.signed_in(server)
            status, headers, body = request(server, "/transactions?period=all", jar=jar)
            html = body.decode()
            self.assertEqual(status, 200)
            self.assertIn(MARKER, html)
            self.assertIn('href="/transactions"', html)                    # nav link present
            self.assertEqual(headers["Cache-Control"], "no-store")
            csp = headers["Content-Security-Policy"]
            self.assertIn("connect-src 'none'", csp)
            self.assertIn("form-action 'self'", csp)
            self.assertNotIn("http", csp.replace("http://", "").split("script-src")[0])   # nothing external
            home = request(server, "/", jar=jar)[2].decode()
            self.assertIn('<a href="/transactions"', home)

    def test_without_the_opt_in_the_page_and_posts_stay_404_even_signed_in(self):
        # Rows are removed so the default host can start; the routes must still be closed.
        with closing(sqlite3.connect(self.db)) as db, db:
            for table in ("account_transactions", "transaction_coverage", "transaction_merchant_rules"):
                db.execute(f"DELETE FROM {table}")
        with running(self.db, self.rsa, lambda: NOW) as server:
            jar, token = self.signed_in(server)
            self.assertEqual(request(server, "/transactions", jar=jar)[0], 404)
            self.assertNotIn('<a href="/transactions"', request(server, "/", jar=jar)[2].decode())
            status, *_ = request(server, "/transactions/edit", {"token": token, "view": "Household"},
                                 jar=jar, origin=server.auth.config.origin)
            self.assertEqual(status, 404)

    def test_pick_needs_session_origin_and_csrf_then_changes_the_category(self):
        with running(self.db, self.rsa, lambda: NOW, transactions=True) as server:
            origin = server.auth.config.origin
            jar, token = self.signed_in(server)
            row, before = self.row()
            form = lambda **kw: dict({"token": token, "view": "Household", "row": str(row), "pick": "Dining"}, **kw)
            # wrong origin, missing or wrong token, extra duplicate-style junk: nothing changes
            self.assertEqual(request(server, "/transactions/edit", form(), jar=jar, origin="https://evil.example")[0], 403)
            self.assertEqual(request(server, "/transactions/edit", form(token="wrong"), jar=jar, origin=origin)[0], 403)
            self.assertEqual(request(server, "/transactions/edit", {k: v for k, v in form().items() if k != "token"},
                                     jar=jar, origin=origin)[0], 403)
            self.assertEqual(self.row()[1], before)
            status, headers, _ = request(server, "/transactions/edit", form(), jar=jar, origin=origin)
            self.assertEqual(status, 303, headers)
            self.assertIn("/transactions", headers["Location"])
            self.assertEqual(self.row()[1], "Dining")
            # and the change is what the page shows afterwards
            page = request(server, "/transactions?period=all", jar=jar)[2].decode()
            self.assertRegex(page, r"Dining")

    def test_sync_post_without_a_bridge_is_a_clean_refusal_not_an_error(self):
        with running(self.db, self.rsa, lambda: NOW, transactions=True) as server:
            jar, token = self.signed_in(server)
            status, *_ = request(server, "/transactions/sync", {"token": token, "view": "Household"},
                                 jar=jar, origin=server.auth.config.origin)
            self.assertEqual(status, 403)

    def test_the_classifier_and_routes_follow_the_same_switch(self):
        import server as srv

        def fake(auth, transactions):
            class Fake:
                class server:
                    pass
            Fake.server.auth, Fake.server.transactions = auth, transactions
            return Fake

        self.assertTrue(srv.Handler.tx_enabled(fake(None, False)))          # local: always
        self.assertTrue(srv.Handler.tx_enabled(fake(object(), True)))       # hosted + --transactions
        self.assertFalse(srv.Handler.tx_enabled(fake(object(), False)))     # hosted, not opted in


if __name__ == "__main__":
    unittest.main()

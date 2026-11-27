from contextlib import contextmanager
from datetime import date, timedelta
from http.client import HTTPConnection
from pathlib import Path
import re
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlencode

from server import serve


@contextmanager
def running(db):
    with serve(db, 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def request(path, values=None, headers=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    conn.request("GET" if values is None else "POST", path,
                                 None if values is None else urlencode(values), headers or {})
                    response = conn.getresponse()
                    return response.status, response.read().decode()
                finally:
                    conn.close()
            yield request
        finally:
            server.shutdown()
            thread.join()


class AccountFlow(unittest.TestCase):
    def test_manual_snapshots_persist_and_total(self):
        today = date.today()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fictional.sqlite3"
            with running(db) as request:
                token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
                base = dict(token=token, nickname="Cedar", institution="Example", kind="Checking",
                            owner="Unspecified", update_method="Manual entry",
                            side="asset", balance="100.10", currency="USD", asof=str(today - timedelta(days=31)))
                self.assertEqual(request("/new", base)[0], 303)
                self.assertEqual(request("/new", dict(base, nickname="Birch", side="debt", kind="Credit card",
                                     balance="20.20", asof=str(today - timedelta(days=30))))[0], 303)
                self.assertEqual(request("/new", dict(base, nickname="Maple", kind="Custom investment",
                                     balance="500", currency="JPY", asof=str(today)))[0], 303)
                overview = request("/")[1]
                self.assertIn("USD 79.90</strong>", overview)
                self.assertIn("JPY 500</strong>", overview)
                self.assertEqual(overview.count(">Stale<"), 1)
                for change in [dict(balance="-1"), dict(balance="1.005"), dict(balance="no"),
                               dict(currency=""), dict(asof=str(today + timedelta(days=1)))]:
                    self.assertEqual(request("/edit/1", dict(base, **change))[0], 422)
                self.assertEqual(request("/")[1], overview)
                self.assertEqual(request("/edit/1", dict(base, balance="90.05", asof=str(today)))[0], 303)
            with running(db) as request:
                overview = request("/")[1]
                self.assertIn("USD 69.85</strong>", overview)
                self.assertEqual(overview.count('href="/edit/'), 3)
                self.assertNotIn(">Stale<", overview)
                self.assertIn('value="90.05"', request("/edit/1")[1])
                confirm = request("/remove/2")[1]
                self.assertIn("Remove Birch?", confirm)
                self.assertEqual(request("/")[1], overview)
                token = re.search(r'name="token" value="([^"]+)"', confirm)[1]
                self.assertEqual(request("/edit/1", dict(base, token=token, balance="0"))[0], 303)
                self.assertIn("USD -20.20</strong>", request("/")[1])
                self.assertEqual(request("/remove/2", {"token": token})[0], 303)
                self.assertNotIn("Birch", request("/")[1])
                self.assertIn("JPY 500</strong>", request("/")[1])

    def test_local_access_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, running(Path(tmp) / "test.db") as request:
            self.assertEqual(request("/", headers={"Host": "outside.example"})[0], 403)
            self.assertEqual(request("/new", {"token": "invalid"})[0], 403)
            self.assertEqual(request("/new", {}, {"Origin": "https://elsewhere.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()

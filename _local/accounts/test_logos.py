"""Offline tests for local cached official institution logos."""
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from logos import cached_logo, domain_for, sanitize_domain
from server import serve


FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class LogoCacheTests(unittest.TestCase):
    def test_sanitize_and_lookup(self):
        self.assertEqual(sanitize_domain("https://www.BankOfAmerica.com/login"), "bankofamerica.com")
        self.assertEqual(sanitize_domain("chase.com"), "chase.com")
        self.assertEqual(sanitize_domain("../etc/passwd"), "")
        self.assertEqual(sanitize_domain("http://localhost:8000"), "")
        self.assertEqual(domain_for("Bank of America"), "bankofamerica.com")
        self.assertEqual(domain_for("Custom Credit Union", "https://mycu.example.org/bank"), "mycu.example.org")
        self.assertEqual(domain_for("Unknown Bank"), "")

    def test_caches_valid_image_once_and_rejects_non_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "logos"
            calls = []
            def fake_ok(url):
                calls.append(url)
                return FAKE_PNG
            res1 = cached_logo(cache, "chase.com", fetcher=fake_ok)
            self.assertEqual(res1, (FAKE_PNG, "image/png"))
            self.assertEqual(len(calls), 1)
            # Second call hits disk cache with zero fetch
            res2 = cached_logo(cache, "chase.com", fetcher=lambda _: self.fail("should hit disk cache"))
            self.assertEqual(res2, (FAKE_PNG, "image/png"))
            # HTML/SVG payload from remote server is rejected
            self.assertIsNone(cached_logo(cache, "sofi.com", fetcher=lambda _: b"<svg><script>alert(1)</script></svg>"))

    def test_logo_route_allowlist_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            logos = Path(tmp) / "logos"
            with serve(db, 0, logo_dir=logos) as srv:
                srv.logo_fetcher = lambda _: FAKE_PNG
                t = Thread(target=srv.serve_forever, daemon=True)
                t.start()
                try:
                    def get(path):
                        c = HTTPConnection("127.0.0.1", srv.server_port, timeout=3)
                        try:
                            c.request("GET", path)
                            r = c.getresponse()
                            return r.status, r.getheader("Content-Type"), r.read()
                        finally:
                            c.close()
                    # Known domain served from local cache
                    status, ctype, body = get("/logo/bankofamerica.com")
                    self.assertEqual((status, ctype, body), (200, "image/png", FAKE_PNG))
                    # Arbitrary unknown domain rejected (no open proxy)
                    self.assertEqual(get("/logo/evil.example")[0], 404)
                    self.assertEqual(get("/logo/..%2fetc")[0], 404)
                finally:
                    srv.shutdown()
                    t.join()


if __name__ == "__main__":
    unittest.main()

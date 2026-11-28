import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from auth import verified as verify_identity


class AccessTests(unittest.TestCase):
    cfg = SimpleNamespace(client="test-client", members={"one@gmail.com", "two@example.org"})

    @patch("auth.verify_oauth2_token")
    def test_access_boundary(self, verify):
        for email, verified, domain, allowed in [
            ("one@gmail.com", True, None, True),
            ("two@example.org", True, "example.org", True),
            ("two@example.org", True, None, False),
            ("one@gmail.com", False, None, False),
            ("other@gmail.com", True, None, False),
        ]:
            with self.subTest(email=email, verified=verified, domain=domain):
                verify.return_value = dict(email=email, email_verified=verified, hd=domain, sub="user", exp=42)
                if allowed:
                    self.assertEqual(verify_identity("token", self.cfg), (email, "user", 42))
                else:
                    with self.assertRaises(HTTPException) as ctx:
                        verify_identity("token", self.cfg)
                    self.assertEqual(ctx.exception.status_code, 403)
                self.assertEqual(verify.call_args.kwargs["audience"], "test-client")

    @patch("auth.verify_oauth2_token", side_effect=ValueError("invalid"))
    def test_bad_token(self, verify):
        with self.assertRaises(HTTPException) as ctx:
            verify_identity("bad", self.cfg)
        self.assertEqual(ctx.exception.status_code, 401)

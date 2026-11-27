import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from main import require_member


@patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "test-client", "LOANS_MEMBERS": "one@gmail.com,two@example.org"})
class AccessTests(unittest.TestCase):
    @patch("main.verify_oauth2_token")
    def test_access_boundary(self, verify):
        token = HTTPAuthorizationCredentials(scheme="Bearer", credentials="id-token")
        for email, verified, domain, allowed in [
            ("one@gmail.com", True, None, True),
            ("two@example.org", True, "example.org", True),
            ("two@example.org", True, None, False),
            ("one@gmail.com", False, None, False),
            ("other@gmail.com", True, None, False),
        ]:
            with self.subTest(email=email, verified=verified, domain=domain):
                verify.return_value = dict(email=email, email_verified=verified, hd=domain, sub="user")
                if allowed:
                    self.assertEqual(require_member(token), "user")
                else:
                    with self.assertRaises(HTTPException) as ctx:
                        require_member(token)
                    self.assertEqual(ctx.exception.status_code, 403)
                self.assertEqual(verify.call_args.kwargs["audience"], "test-client")

    def test_no_token(self):
        with self.assertRaises(HTTPException) as ctx:
            require_member(None)
        self.assertEqual(ctx.exception.status_code, 401)

    @patch("main.verify_oauth2_token", side_effect=ValueError("invalid"))
    def test_bad_token(self, verify):
        with self.assertRaises(HTTPException) as ctx:
            require_member(HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad"))
        self.assertEqual(ctx.exception.status_code, 401)

import os
import sys
import unittest
from datetime import datetime, timedelta

# Add app directory to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from mcp_auth import verify_mcp_token, AuthenticationError


class TestTokenVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        import oauth
        from cryptography.hazmat.primitives.asymmetric import rsa
        if "priv" not in oauth._KEY_CACHE:
            oauth._KEY_CACHE["kid"] = "test-kid-1"
            oauth._KEY_CACHE["priv"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.oauth = oauth
        cls.kid, cls.priv = oauth.signing_key()
        cls.public_jwks = oauth.public_jwks()

    def test_valid_citizen_token(self):
        token, _ = self.oauth.mint_access_token(
            subject="citizen:9876543210",
            role="citizen",
            scope="citizen",
            audience="http://localhost:8000/mcp/citizen",
            client_id="test-client"
        )
        claims = verify_mcp_token(
            token=token,
            expected_audience="http://localhost:8000/mcp/citizen",
            required_scope="citizen",
            required_role="citizen"
        )
        self.assertIsNotNone(claims)
        self.assertEqual(claims["sub"], "citizen:9876543210")
        self.assertEqual(claims["mobile"], "9876543210")

    def test_valid_officer_token(self):
        token, _ = self.oauth.mint_access_token(
            subject="officer:admin",
            role="officer",
            scope="officer",
            audience="http://localhost:8000/mcp/officer",
            client_id="test-client"
        )
        claims = verify_mcp_token(
            token=token,
            expected_audience="http://localhost:8000/mcp/officer",
            required_scope="officer",
            required_role="officer"
        )
        self.assertIsNotNone(claims)
        self.assertEqual(claims["sub"], "officer:admin")
        self.assertEqual(claims["username"], "admin")

    def test_citizen_token_rejected_at_officer_endpoint(self):
        # A citizen token with citizen audience/scope trying to access officer endpoint
        token, _ = self.oauth.mint_access_token(
            subject="citizen:9876543210",
            role="citizen",
            scope="citizen",
            audience="http://localhost:8000/mcp/citizen",
            client_id="test-client"
        )
        with self.assertRaises(AuthenticationError):
            verify_mcp_token(
                token=token,
                expected_audience="http://localhost:8000/mcp/officer",
                required_scope="officer",
                required_role="officer"
            )

    def test_officer_token_rejected_at_citizen_endpoint(self):
        token, _ = self.oauth.mint_access_token(
            subject="officer:admin",
            role="officer",
            scope="officer",
            audience="http://localhost:8000/mcp/officer",
            client_id="test-client"
        )
        with self.assertRaises(AuthenticationError):
            verify_mcp_token(
                token=token,
                expected_audience="http://localhost:8000/mcp/citizen",
                required_scope="citizen",
                required_role="citizen"
            )

    def test_invalid_issuer_rejected(self):
        import jwt
        # Mint token with fraudulent issuer
        now = int(datetime.now().timestamp())
        fake_claims = {
            "iss": "http://evil.com",
            "sub": "citizen:9876543210",
            "aud": "http://localhost:8000/mcp/citizen",
            "scope": "citizen",
            "role": "citizen",
            "exp": now + 3600,
            "iat": now
        }
        token = jwt.encode(fake_claims, self.priv, algorithm="RS256", headers={"kid": self.kid})
        with self.assertRaises(AuthenticationError):
            verify_mcp_token(
                token=token,
                expected_audience="http://localhost:8000/mcp/citizen",
                required_scope="citizen",
                required_role="citizen"
            )

    def test_expired_token_rejected(self):
        token, _ = self.oauth.mint_access_token(
            subject="citizen:9876543210",
            role="citizen",
            scope="citizen",
            audience="http://localhost:8000/mcp/citizen",
            client_id="test-client",
            ttl=-10  # already expired
        )
        with self.assertRaises(AuthenticationError):
            verify_mcp_token(
                token=token,
                expected_audience="http://localhost:8000/mcp/citizen",
                required_scope="citizen",
                required_role="citizen"
            )

    def test_malformed_sub_rejected(self):
        # sub is not citizen:<10 digits>
        token, _ = self.oauth.mint_access_token(
            subject="invalid_sub_format",
            role="citizen",
            scope="citizen",
            audience="http://localhost:8000/mcp/citizen",
            client_id="test-client"
        )
        with self.assertRaises(AuthenticationError):
            verify_mcp_token(
                token=token,
                expected_audience="http://localhost:8000/mcp/citizen",
                required_scope="citizen",
                required_role="citizen"
            )


if __name__ == "__main__":
    unittest.main()

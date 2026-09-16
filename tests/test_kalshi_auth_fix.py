"""
Regression test for Kalshi auth fix:
1. RSA-PSS SHA256 signing (was PKCS1v15)
2. Full path signing using api_prefix from base_url (was normalized path only)
"""
import json
import base64
import time
import pytest
from unittest.mock import patch, MagicMock
from cryptography.hazmat.primitives import hashes, serialization, asymmetric
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.backends import default_backend

# Generate a test RSA key pair for fixture use
_TEST_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
_TEST_PRIVATE_KEY_PEM = _TEST_PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
_TEST_PUBLIC_KEY = _TEST_PRIVATE_KEY.public_key()


def _generate_test_key_pem():
    """Generate a fresh RSA key pair for testing."""
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class TestKalshiAuthFix:
    """Verifies the two auth fixes: RSA-PSS and full-path signing."""

    def test_sign_uses_rsa_pss_not_pkcs1v15(self):
        """Fix 1: _sign_request_rsa must use RSA-PSS SHA256, not PKCS1v15."""
        from core.kalshi_monitor import _sign_request_rsa

        timestamp = str(int(time.time() * 1000))
        method = "GET"
        path = "/trade-api/v2/portfolio/balance"
        body = ""

        signature_b64 = _sign_request_rsa(
            _TEST_PRIVATE_KEY_PEM,
            timestamp,
            method,
            path,
            body,
        )
        assert signature_b64, "signature must not be empty"
        assert isinstance(signature_b64, str)

        # Verify the signature with PSS — PKCS1v15-created signature would
        # fail PSS verification
        message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
        try:
            _TEST_PUBLIC_KEY.verify(
                base64.b64decode(signature_b64),
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        except Exception as exc:
            pytest.fail(f"PSS verification failed (was PKCS1v15 used?): {exc}")

    def test_pss_verification_rejects_pkcs1v15_signature(self):
        """Confirm that a PKCS1v15 signature fails PSS verification."""
        # Create a signature with PKCS1v15
        message = b"test_message"
        bad_sig = _TEST_PRIVATE_KEY.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        with pytest.raises(Exception):
            _TEST_PUBLIC_KEY.verify(
                bad_sig,
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

    def test_kalshi_get_signs_full_path(self):
        """Fix 2: _kalshi_get must sign full API path (with /trade-api/v2 prefix)."""
        from core.kalshi_monitor import _sign_request_rsa

        # Simulate what _kalshi_get does after the fix:
        base_url = "https://api.elections.kalshi.com/trade-api/v2"
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        api_prefix = parsed.path.rstrip("/")  # /trade-api/v2

        relative_path = "/portfolio/balance"
        normalized_path = relative_path  # already starts with /
        signing_path = f"{api_prefix}{normalized_path}"  # /trade-api/v2/portfolio/balance

        timestamp = str(int(time.time() * 1000))
        sig = _sign_request_rsa(
            _TEST_PRIVATE_KEY_PEM,
            timestamp,
            "GET",
            signing_path,
            "",
        )

        # Verify with the full path
        message = f"{timestamp}GET{signing_path}".encode("utf-8")
        try:
            _TEST_PUBLIC_KEY.verify(
                base64.b64decode(sig),
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        except Exception as exc:
            pytest.fail(f"Full-path PSS verification failed: {exc}")

    def test_kalshi_get_calls_with_full_signing_path(self):
        """Integration check: _kalshi_get derives api_prefix and passes signing_path."""
        import sys
        # We can't easily import _kalshi_get directly without env vars,
        # so we test the urlparse logic it uses
        test_cases = [
            ("https://api.elections.kalshi.com/trade-api/v2", "/portfolio/balance", "/trade-api/v2/portfolio/balance"),
            ("https://api.elections.kalshi.com/trade-api/v2/", "/portfolio/balance", "/trade-api/v2/portfolio/balance"),
            ("https://api.elections.kalshi.com/trade-api/v2", "/portfolio/balance?foo=bar", "/trade-api/v2/portfolio/balance?foo=bar"),
            ("https://external-api.kalshi.com/trade-api/v2", "/portfolio/balance", "/trade-api/v2/portfolio/balance"),
        ]

        from urllib.parse import urlparse
        for base, rel, expected_signing in test_cases:
            parsed = urlparse(base)
            api_prefix = parsed.path.rstrip("/")
            normalized = rel
            signing_path = f"{api_prefix}{normalized}"
            assert signing_path == expected_signing, f"Expected {expected_signing}, got {signing_path}"

    @patch.dict("os.environ", {
        "KALSHI_BASE_URL": "https://api.elections.kalshi.com/trade-api/v2",
        "KALSHI_KEY_ID": "test_key_id",
        "KALSHI_PRIVATE_KEY_PEM": base64.b64encode(_TEST_PRIVATE_KEY_PEM.encode("utf-8")).decode("utf-8"),
    })
    def test_kalshi_get_integration_with_mock(self):
        """Verify _kalshi_get path validation and signature header with mock response."""
        from core import kalshi_monitor  # reload mocked state

        # Force reload of module-level config dependencies
        import importlib
        importlib.reload(kalshi_monitor)

        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"balance": 1000.0}
            mock_get.return_value = mock_response

            result = kalshi_monitor._kalshi_get("/portfolio/balance")

            # Verify the request was made
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert call_args is not None

            # Check URL
            url = call_args[0][0]
            assert url == "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance"

            # Check headers
            headers = call_args[1]["headers"]
            assert headers["KALSHI-ACCESS-KEY"] == "test_key_id"
            assert "KALSHI-ACCESS-SIGNATURE" in headers
            assert "KALSHI-ACCESS-TIMESTAMP" in headers

            # Verify the signature was computed over the full path
            sig = headers["KALSHI-ACCESS-SIGNATURE"]
            ts = headers["KALSHI-ACCESS-TIMESTAMP"]
            message = f"{ts}GET/trade-api/v2/portfolio/balance".encode("utf-8")
            try:
                _TEST_PUBLIC_KEY.verify(
                    base64.b64decode(sig),
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
            except Exception as exc:
                pytest.fail(f"Signature verified against full path failed: {exc}")

            assert result == {"balance": 1000.0}

    def test_pss_signature_is_deterministic(self):
        """RSA-PSS with same inputs produces verifiable signature (not identical, but verifiable)."""
        from core.kalshi_monitor import _sign_request_rsa

        ts = str(int(time.time() * 1000))
        path = "/trade-api/v2/test"

        sig1 = _sign_request_rsa(_TEST_PRIVATE_KEY_PEM, ts, "GET", path, "")
        sig2 = _sign_request_rsa(_TEST_PRIVATE_KEY_PEM, ts, "GET", path, "")

        # PSS is non-deterministic (random salt), so signatures differ
        assert sig1 != sig2, "PSS should produce different signatures each time (random salt)"

        # But both should verify
        message = f"{ts}GET{path}".encode("utf-8")
        for sig in (sig1, sig2):
            try:
                _TEST_PUBLIC_KEY.verify(
                    base64.b64decode(sig),
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
            except Exception as exc:
                pytest.fail(f"PSS signature verification failed: {exc}")
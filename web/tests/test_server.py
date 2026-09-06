"""Unit tests for web/server.py.

All calls to the AgentCore Runtime are mocked (via
agent_client.stream_agent_events) — no live network access, no real AWS
credentials required. Most test classes short-circuit real auth by
installing a valid, pre-encrypted session cookie via _authed_client()
(backed by a fake in-memory KMS client, not a real one) — exercising the
actual OIDC dance (redirect/callback/token exchange/refresh) end-to-end is
a separate, narrowly-scoped concern covered by OAuthFlowTests and
SessionRefreshTests instead.
"""
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt as pyjwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

_WEB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_WEB_DIR))

_SERVER_PATH = _WEB_DIR / "server.py"
_spec = importlib.util.spec_from_file_location("web_server", _SERVER_PATH)
web_server = importlib.util.module_from_spec(_spec)
sys.modules["web_server"] = web_server
_spec.loader.exec_module(web_server)

import auth as web_auth  # noqa: E402 - must follow the exec_module() sys.path setup above

_TEST_JWT_SECRET = "test-secret-at-least-32-bytes-long!!"

# A syntactically valid 33+ character runtimeSessionId, matching what the
# frontend generates via build_runtime_session_id()-equivalent JS logic.
VALID_SESSION_ID = "web-user___" + "a" * 32
# The bare component AgentCore Memory's ListSessions/ListEvents actually
# use — confirmed live against a real Memory resource: these APIs are
# already actor-scoped and return/accept only the part after "___", not
# the full runtimeSessionId.
BARE_SESSION_ID = "a" * 32


class FakeKmsClient:
    """In-memory stand-in for boto3's KMS client.

    Real envelope-encryption round trip (a genuine AES-256 data key is
    generated and used), just without any network call to AWS — the
    "encrypted" data key returned here is a fixed sentinel bytes value
    rather than a real KMS ciphertext blob, and decrypt() simply returns
    the same plaintext key back given that sentinel, skipping AWS's own
    authenticated-encryption format entirely (which is opaque to callers
    and not this codebase's concern to test).
    """

    _SENTINEL_CIPHERTEXT = b"fake-encrypted-data-key"

    def __init__(self):
        self._plaintext_key = AESGCM.generate_key(bit_length=256)

    def generate_data_key(self, KeyId, KeySpec):
        return {"Plaintext": self._plaintext_key, "CiphertextBlob": self._SENTINEL_CIPHERTEXT}

    def decrypt(self, CiphertextBlob, KeyId=None):
        assert CiphertextBlob == self._SENTINEL_CIPHERTEXT
        return {"Plaintext": self._plaintext_key}


def _make_okta_config() -> web_auth.OktaConfig:
    return web_auth.OktaConfig(
        issuer="https://test.okta.com",
        authorization_endpoint="https://test.okta.com/authorize",
        token_endpoint="https://test.okta.com/token",
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="https://travel-agent.example.com/oauth2/callback",
    )


def _make_runtime_oidc_config() -> web_auth.RuntimeOidcConfig:
    return web_auth.RuntimeOidcConfig(
        issuer="https://test.okta.com/runtime",
        token_endpoint="https://test.okta.com/runtime/token",
        client_id="test-exchange-client-id",
        client_secret="test-exchange-client-secret",
        audience="travel-agent-runtime",
        scope="runtime:invoke",
    )


def _make_access_token(sub: str, expires_in_seconds: int = 3600) -> str:
    """A JWT-shaped Okta access token, matching auth.py's
    _sub_from_access_token() decode-without-verification assumption."""
    return pyjwt.encode(
        {"sub": sub, "exp": int(time.time()) + expires_in_seconds},
        _TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _make_session_cookie(codec: web_auth.SessionCookieCodec, sub: str, expired: bool = False) -> str:
    """Directly build a valid (or already-expired) encrypted session
    cookie for a given sub, bypassing the real OIDC dance — the fast path
    every test that just needs "a logged-in user" uses."""
    tokens = web_auth.SessionTokens(
        access_token=_make_access_token(sub),
        refresh_token="test-refresh-token",
        access_token_expires_at=(time.time() - 10) if expired else (time.time() + 3600),
        sub=sub,
    )
    return codec.encode(tokens)


def _make_runtime_token_cookie(
    codec: web_auth.RuntimeTokenCookieCodec, expired: bool = False
) -> str:
    """Directly build a valid (or already-expired) encrypted Runtime-token
    cookie, bypassing a real token-exchange call — the fast path for
    tests that need "a cached Runtime token already present"."""
    token = web_auth.RuntimeToken(
        access_token="test-runtime-jwt",
        expires_at=(time.time() - 10) if expired else (time.time() + 3600),
    )
    return codec.encode(token)


def _resource_not_found_error(operation_name: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        operation_name,
    )


def _parse_sse(body: str) -> list[dict]:
    """Parse a "data: <json>\\n\\n"-framed SSE response body into event dicts."""
    events = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        if frame.startswith("data: "):
            events.append(json.loads(frame[len("data: "):]))
    return events


class _AppTestCase(unittest.TestCase):
    """Shared setup: a create_app() instance with a fake KMS-backed
    SessionCookieCodec, an authenticated TestClient pre-loaded with a
    valid session cookie for actor "web-user", and boto3.client() mocked
    for AgentCore Memory calls. Subclasses needing --memory-id or a
    different sub should build their own app via _build_app()."""

    def _build_app(self, **extra_create_app_kwargs):
        self.okta_config = _make_okta_config()
        self.codec = web_auth.SessionCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())
        self.runtime_oidc_config = _make_runtime_oidc_config()
        self.runtime_token_codec = web_auth.RuntimeTokenCookieCodec(
            kms_key_id="test-key", kms_client=FakeKmsClient()
        )
        kwargs = dict(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            okta_config=self.okta_config,
            session_codec=self.codec,
            runtime_oidc_config=self.runtime_oidc_config,
            runtime_token_codec=self.runtime_token_codec,
        )
        kwargs.update(extra_create_app_kwargs)
        return web_server.create_app(**kwargs)

    def _authed_client(self, app, sub="web-user", with_runtime_token=True) -> TestClient:
        """A TestClient pre-loaded with a valid session cookie for `sub`.

        base_url is explicitly HTTPS — the session cookie's Secure flag
        means httpx's cookie jar silently drops it under the TestClient's
        default plain-http base_url, which looks like a missing-cookie/401
        failure but is actually a test-harness artifact, not a real auth
        bug (confirmed by comparing behavior with/without this fix).

        with_runtime_token=True (default) also pre-loads a valid Runtime-
        token cookie, so /api/chat tests don't perform a real (mocked)
        exchange call unless they specifically want to exercise that path
        — set False for tests that need to observe the exchange happening.
        """
        client = TestClient(app, base_url="https://testserver")
        client.cookies.set(
            web_auth.SESSION_COOKIE_NAME, _make_session_cookie(self.codec, sub)
        )
        if with_runtime_token:
            client.cookies.set(
                web_auth.RUNTIME_TOKEN_COOKIE_NAME,
                _make_runtime_token_cookie(self.runtime_token_codec),
            )
        return client

    def setUp(self):
        patcher = patch("web_server.boto3.client")
        self.mock_boto_client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = MagicMock()
        self.mock_boto_client_factory.return_value = self.mock_client

        self.app = self._build_app()
        self.client = self._authed_client(self.app)


class SanitizeActorIdTests(unittest.TestCase):
    """Regression tests: an OIDC `sub` claim shaped like an email address
    caused every ListSessions/ListEvents/CreateEvent call to fail with a
    502 (ValidationException on actorId) until sanitization was added —
    confirmed live against a real deployment."""

    def test_leaves_already_valid_id_unchanged(self):
        self.assertEqual(web_server._sanitize_actor_id("web-user"), "web-user")

    def test_replaces_email_special_characters(self):
        self.assertEqual(
            web_server._sanitize_actor_id("kenkitts@amazon.com"), "kenkitts-amazon-com"
        )

    def test_strips_leading_disallowed_characters(self):
        self.assertEqual(web_server._sanitize_actor_id("@user123"), "user123")

    def test_falls_back_to_default_when_fully_sanitized_away(self):
        self.assertEqual(web_server._sanitize_actor_id("@@@"), web_server._DEFAULT_ACTOR_ID)


class SessionCookieCodecTests(unittest.TestCase):
    """Tests for auth.py's envelope-encryption round trip, independent of
    any FastAPI route — the KMS interaction is faked (FakeKmsClient), but
    the actual AES-GCM encrypt/decrypt is real."""

    def setUp(self):
        self.codec = web_auth.SessionCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())

    def test_round_trips_session_tokens(self):
        tokens = web_auth.SessionTokens(
            access_token="access-123",
            refresh_token="refresh-456",
            access_token_expires_at=1234567890.0,
            sub="alice",
        )

        cookie_value = self.codec.encode(tokens)
        decoded = self.codec.decode(cookie_value)

        self.assertEqual(decoded.access_token, "access-123")
        self.assertEqual(decoded.refresh_token, "refresh-456")
        self.assertEqual(decoded.access_token_expires_at, 1234567890.0)
        self.assertEqual(decoded.sub, "alice")

    def test_rejects_malformed_cookie(self):
        with self.assertRaises(web_auth.AuthError):
            self.codec.decode("not-valid-base64url-json!!!")

    def test_rejects_tampered_ciphertext(self):
        tokens = web_auth.SessionTokens(
            access_token="access-123",
            refresh_token="refresh-456",
            access_token_expires_at=1234567890.0,
            sub="alice",
        )
        cookie_value = self.codec.encode(tokens)
        tampered = cookie_value[:-4] + ("A" if cookie_value[-4] != "A" else "B") + cookie_value[-3:]

        with self.assertRaises(web_auth.AuthError):
            self.codec.decode(tampered)


class RuntimeTokenCookieCodecTests(unittest.TestCase):
    """Tests for the Phase 2 Runtime-token cookie's envelope-encryption
    round trip — same _KmsEnvelopeCodec machinery as
    SessionCookieCodecTests above, applied to RuntimeTokenCookieCodec's
    smaller RuntimeToken payload. Kept as a genuinely separate cookie
    from the session cookie (DESIGN.md's Phase 2 decision, driven by a
    real measured cookie-size check) — this class only tests its own
    codec, not any interaction with the session cookie."""

    def setUp(self):
        self.codec = web_auth.RuntimeTokenCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())

    def test_round_trips_runtime_token(self):
        token = web_auth.RuntimeToken(access_token="runtime-jwt-abc", expires_at=1234567890.0)

        cookie_value = self.codec.encode(token)
        decoded = self.codec.decode(cookie_value)

        self.assertEqual(decoded.access_token, "runtime-jwt-abc")
        self.assertEqual(decoded.expires_at, 1234567890.0)

    def test_rejects_malformed_cookie(self):
        with self.assertRaises(web_auth.AuthError):
            self.codec.decode("not-valid-base64url-json!!!")

    def test_rejects_tampered_ciphertext(self):
        token = web_auth.RuntimeToken(access_token="runtime-jwt-abc", expires_at=1234567890.0)
        cookie_value = self.codec.encode(token)
        tampered = cookie_value[:-4] + ("A" if cookie_value[-4] != "A" else "B") + cookie_value[-3:]

        with self.assertRaises(web_auth.AuthError):
            self.codec.decode(tampered)

    def test_uses_a_genuinely_different_cookie_name_from_the_session_cookie(self):
        # Regression guard for the actual design decision this class
        # exists to implement — the two cookies must never collide.
        self.assertNotEqual(web_auth.RUNTIME_TOKEN_COOKIE_NAME, web_auth.SESSION_COOKIE_NAME)


class TokenExchangeTests(unittest.TestCase):
    """Tests for exchange_token_for_runtime() (the raw RFC 8693 call to
    Okta) and get_or_exchange_runtime_token() (the silent-re-exchange-on-
    missing/expired orchestration) — independent of any FastAPI route."""

    def setUp(self):
        self.runtime_oidc_config = _make_runtime_oidc_config()
        self.codec = web_auth.RuntimeTokenCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())

    def test_exchange_sends_rfc8693_grant_and_basic_auth(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "exchanged-jwt",
                "expires_in": 3600,
            }
            result = web_auth.exchange_token_for_runtime(self.runtime_oidc_config, "okta-access-token-abc")

        self.assertEqual(result.access_token, "exchanged-jwt")
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["data"]["grant_type"], "urn:ietf:params:oauth:grant-type:token-exchange")
        self.assertEqual(
            call_kwargs["data"]["subject_token_type"], "urn:ietf:params:oauth:token-type:access_token"
        )
        self.assertEqual(call_kwargs["data"]["subject_token"], "okta-access-token-abc")
        self.assertEqual(call_kwargs["data"]["scope"], "runtime:invoke")
        self.assertEqual(call_kwargs["data"]["audience"], "travel-agent-runtime")
        self.assertEqual(
            call_kwargs["auth"], ("test-exchange-client-id", "test-exchange-client-secret")
        )

    def test_exchange_raises_auth_error_on_non_2xx(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_scope"
            with self.assertRaises(web_auth.AuthError):
                web_auth.exchange_token_for_runtime(self.runtime_oidc_config, "okta-access-token-abc")

    def test_exchange_raises_auth_error_on_network_failure(self):
        import requests

        with patch("auth.requests.post", side_effect=requests.RequestException("boom")):
            with self.assertRaises(web_auth.AuthError):
                web_auth.exchange_token_for_runtime(self.runtime_oidc_config, "okta-access-token-abc")

    def test_exchange_raises_auth_error_on_missing_fields(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {"expires_in": 3600}  # no access_token
            with self.assertRaises(web_auth.AuthError):
                web_auth.exchange_token_for_runtime(self.runtime_oidc_config, "okta-access-token-abc")

    def _fake_request(self, cookie_value: str = None):
        request = MagicMock()
        request.cookies = {web_auth.RUNTIME_TOKEN_COOKIE_NAME: cookie_value} if cookie_value else {}
        return request

    def test_uses_cached_token_when_still_valid(self):
        cookie_value = _make_runtime_token_cookie(self.codec)
        request = self._fake_request(cookie_value)

        with patch("auth.requests.post") as mock_post:
            context = web_auth.get_or_exchange_runtime_token(
                request, self.codec, self.runtime_oidc_config, "okta-access-token-abc"
            )

        mock_post.assert_not_called()
        self.assertFalse(context.exchanged)
        self.assertEqual(context.access_token, "test-runtime-jwt")

    def test_re_exchanges_when_cookie_missing(self):
        request = self._fake_request(cookie_value=None)

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "freshly-exchanged-jwt",
                "expires_in": 3600,
            }
            context = web_auth.get_or_exchange_runtime_token(
                request, self.codec, self.runtime_oidc_config, "okta-access-token-abc"
            )

        mock_post.assert_called_once()
        self.assertTrue(context.exchanged)
        self.assertEqual(context.access_token, "freshly-exchanged-jwt")

    def test_re_exchanges_when_cookie_expired(self):
        cookie_value = _make_runtime_token_cookie(self.codec, expired=True)
        request = self._fake_request(cookie_value)

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "freshly-exchanged-jwt",
                "expires_in": 3600,
            }
            context = web_auth.get_or_exchange_runtime_token(
                request, self.codec, self.runtime_oidc_config, "okta-access-token-abc"
            )

        mock_post.assert_called_once()
        self.assertTrue(context.exchanged)

    def test_re_exchanges_when_cookie_is_malformed(self):
        request = self._fake_request(cookie_value="not-valid-base64url!!!")

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "freshly-exchanged-jwt",
                "expires_in": 3600,
            }
            context = web_auth.get_or_exchange_runtime_token(
                request, self.codec, self.runtime_oidc_config, "okta-access-token-abc"
            )

        # A malformed/undecryptable cookie is never itself an auth
        # failure here (DESIGN.md's Phase 2 decision) — it just triggers
        # a fresh exchange, unlike a bad session cookie which raises.
        mock_post.assert_called_once()
        self.assertTrue(context.exchanged)

    def test_exchange_failure_propagates_as_auth_error(self):
        request = self._fake_request(cookie_value=None)

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 500
            mock_post.return_value.text = "server_error"
            with self.assertRaises(web_auth.AuthError):
                web_auth.get_or_exchange_runtime_token(
                    request, self.codec, self.runtime_oidc_config, "okta-access-token-abc"
                )


class RevokeRefreshTokenTests(unittest.TestCase):
    """Direct unit tests for auth.revoke_refresh_token() — independent of
    the /api/logout route, which has its own tests (LogoutEndpointTests)
    covering the cookie-clearing side of logout."""

    def setUp(self):
        self.okta_config = _make_okta_config()

    def test_sends_refresh_token_type_hint_and_basic_auth(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            web_auth.revoke_refresh_token(self.okta_config, "a-real-refresh-token")

        call_args, call_kwargs = mock_post.call_args
        self.assertEqual(call_args[0], f"{self.okta_config.issuer}/v1/revoke")
        self.assertEqual(call_kwargs["data"]["token"], "a-real-refresh-token")
        self.assertEqual(call_kwargs["data"]["token_type_hint"], "refresh_token")
        self.assertEqual(
            call_kwargs["auth"], (self.okta_config.client_id, self.okta_config.client_secret)
        )

    def test_raises_auth_error_on_non_2xx(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_token"
            with self.assertRaises(web_auth.AuthError):
                web_auth.revoke_refresh_token(self.okta_config, "a-real-refresh-token")

    def test_raises_auth_error_on_network_failure(self):
        import requests

        with patch("auth.requests.post", side_effect=requests.RequestException("boom")):
            with self.assertRaises(web_auth.AuthError):
                web_auth.revoke_refresh_token(self.okta_config, "a-real-refresh-token")


class OAuthFlowTests(unittest.TestCase):
    """Tests for the real OIDC redirect + /oauth2/callback flow — the
    401-vs-redirect split, PKCE/state round-trip, and code exchange."""

    def setUp(self):
        patcher = patch("web_server.boto3.client")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.okta_config = _make_okta_config()
        self.codec = web_auth.SessionCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())
        self.runtime_oidc_config = _make_runtime_oidc_config()
        self.runtime_token_codec = web_auth.RuntimeTokenCookieCodec(
            kms_key_id="test-key", kms_client=FakeKmsClient()
        )
        self.app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            okta_config=self.okta_config,
            session_codec=self.codec,
            runtime_oidc_config=self.runtime_oidc_config,
            runtime_token_codec=self.runtime_token_codec,
            memory_id="mem-123",
        )
        self.client = TestClient(self.app, follow_redirects=False, base_url="https://testserver")

    def test_unauthenticated_fetch_call_gets_401_not_redirect(self):
        response = self.client.get("/api/whoami", headers={"sec-fetch-mode": "cors"})

        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_page_navigation_gets_redirected_to_okta(self):
        response = self.client.get("/api/whoami", headers={"sec-fetch-mode": "navigate"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith(self.okta_config.authorization_endpoint))
        self.assertIn("code_challenge=", response.headers["location"])
        self.assertIn("state=", response.headers["location"])

    def test_index_route_requires_auth(self):
        # Regression test: the / route originally had no auth check at
        # all (a real bug caught via a live curl check immediately after
        # the first Phase 1 deploy, not by the test suite at the time) —
        # under the old ALB model this didn't matter, since the ALB
        # itself gated every request at the network edge before any of
        # them reached the container; now the app must check for itself.
        response = self.client.get("/", headers={"sec-fetch-mode": "navigate"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith(self.okta_config.authorization_endpoint))

    def test_index_route_returns_401_for_fetch_style_call_with_no_session(self):
        response = self.client.get("/", headers={"sec-fetch-mode": "cors"})

        self.assertEqual(response.status_code, 401)

    def test_navigation_without_sec_fetch_mode_falls_back_to_accept_header(self):
        response = self.client.get("/api/whoami", headers={"accept": "text/html"})

        self.assertEqual(response.status_code, 302)

    def test_full_login_round_trip_sets_session_cookie(self):
        redirect = self.client.get("/api/whoami", headers={"sec-fetch-mode": "navigate"})
        import urllib.parse

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect.headers["location"]).query)
        state = qs["state"][0]

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": _make_access_token("alice@example.com"),
                "refresh_token": "fake-refresh-token",
                "expires_in": 3600,
            }
            callback_response = self.client.get(f"/oauth2/callback?code=fakecode&state={state}")

        self.assertEqual(callback_response.status_code, 302)
        self.assertEqual(callback_response.headers["location"], "/api/whoami")
        self.assertIsNotNone(self.client.cookies.get(web_auth.SESSION_COOKIE_NAME))

        whoami_response = self.client.get("/api/whoami")
        self.assertEqual(whoami_response.status_code, 200)
        self.assertEqual(whoami_response.json()["sub"], "alice@example.com")

    def test_callback_rejects_state_mismatch(self):
        self.client.get("/api/whoami", headers={"sec-fetch-mode": "navigate"})

        response = self.client.get("/oauth2/callback?code=fakecode&state=wrong-state")

        self.assertEqual(response.status_code, 400)
        self.assertIn("State mismatch", response.json()["detail"])

    def test_callback_without_pending_login_cookie_fails(self):
        response = self.client.get("/oauth2/callback?code=fakecode&state=anything")

        self.assertEqual(response.status_code, 400)
        self.assertIn("No pending login", response.json()["detail"])

    def test_callback_surfaces_okta_error_param(self):
        self.client.get("/api/whoami", headers={"sec-fetch-mode": "navigate"})

        response = self.client.get("/oauth2/callback?error=access_denied&state=x")

        self.assertEqual(response.status_code, 400)
        self.assertIn("access_denied", response.json()["detail"])

    def test_callback_surfaces_token_exchange_failure(self):
        redirect = self.client.get("/api/whoami", headers={"sec-fetch-mode": "navigate"})
        import urllib.parse

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect.headers["location"]).query)
        state = qs["state"][0]

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_grant"
            response = self.client.get(f"/oauth2/callback?code=badcode&state={state}")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Login failed", response.json()["detail"])


class SessionRefreshTests(unittest.TestCase):
    """Tests for silent access-token refresh on an expired session cookie
    (no refresh-token rotation, per this project's Okta app config —
    DESIGN.md's Phase 1 auth rearchitecture decision)."""

    def setUp(self):
        patcher = patch("web_server.boto3.client")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.okta_config = _make_okta_config()
        self.codec = web_auth.SessionCookieCodec(kms_key_id="test-key", kms_client=FakeKmsClient())
        self.runtime_oidc_config = _make_runtime_oidc_config()
        self.runtime_token_codec = web_auth.RuntimeTokenCookieCodec(
            kms_key_id="test-key", kms_client=FakeKmsClient()
        )
        self.app = web_server.create_app(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
            region="us-east-1",
            okta_config=self.okta_config,
            session_codec=self.codec,
            runtime_oidc_config=self.runtime_oidc_config,
            runtime_token_codec=self.runtime_token_codec,
        )
        self.client = TestClient(self.app, base_url="https://testserver")

    def test_expired_access_token_triggers_silent_refresh(self):
        self.client.cookies.set(
            web_auth.SESSION_COOKIE_NAME,
            _make_session_cookie(self.codec, "alice@example.com", expired=True),
        )

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": _make_access_token("alice@example.com"),
                "expires_in": 3600,
                # No refresh_token in the response — matches this app's
                # Okta config (Q11: same refresh token reused, not rotated).
            }
            response = self.client.get("/api/whoami")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sub"], "alice@example.com")
        self.assertIn("set-cookie", response.headers)
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(mock_post.call_args.kwargs["data"]["refresh_token"], "test-refresh-token")

    def test_refresh_failure_falls_back_to_401_for_fetch_call(self):
        self.client.cookies.set(
            web_auth.SESSION_COOKIE_NAME,
            _make_session_cookie(self.codec, "alice@example.com", expired=True),
        )

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_grant: refresh token expired"
            response = self.client.get("/api/whoami", headers={"sec-fetch-mode": "cors"})

        self.assertEqual(response.status_code, 401)

    def test_valid_unexpired_access_token_does_not_trigger_refresh(self):
        self.client.cookies.set(
            web_auth.SESSION_COOKIE_NAME,
            _make_session_cookie(self.codec, "alice@example.com", expired=False),
        )

        with patch("auth.requests.post") as mock_post:
            response = self.client.get("/api/whoami")

        self.assertEqual(response.status_code, 200)
        mock_post.assert_not_called()


class ChatEndpointTests(_AppTestCase):
    @patch("web_server.stream_agent_events")
    def test_chat_returns_agent_response(self, mock_stream_agent_events):
        mock_stream_agent_events.return_value = iter(
            [
                {"type": "text", "data": "Here's a 2-day "},
                {"type": "text", "data": "Boston itinerary..."},
                {"type": "done", "data": "Here's a 2-day Boston itinerary..."},
            ]
        )

        response = self.client.post(
            "/api/chat",
            json={"prompt": "Plan a trip to Boston", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = _parse_sse(response.text)
        self.assertEqual(
            events,
            [
                {"type": "text", "data": "Here's a 2-day "},
                {"type": "text", "data": "Boston itinerary..."},
                {"type": "done", "data": "Here's a 2-day Boston itinerary..."},
            ],
        )
        mock_stream_agent_events.assert_called_once()
        call_args = mock_stream_agent_events.call_args.args
        # stream_agent_events(agent_runtime_arn, region, runtime_session_id, prompt, actor_id, bearer_token, qualifier)
        self.assertEqual(call_args[0], "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test")
        self.assertEqual(call_args[1], "us-east-1")
        self.assertEqual(call_args[2], VALID_SESSION_ID)
        self.assertEqual(call_args[3], "Plan a trip to Boston")
        self.assertEqual(call_args[4], "web-user")
        self.assertEqual(call_args[5], "test-runtime-jwt")

    @patch("web_server.stream_agent_events")
    def test_chat_strips_whitespace_from_prompt(self, mock_stream_agent_events):
        mock_stream_agent_events.return_value = iter([{"type": "done", "data": "ok"}])

        self.client.post(
            "/api/chat",
            json={"prompt": "  Plan a trip  ", "session_id": VALID_SESSION_ID},
        )

        call_args = mock_stream_agent_events.call_args.args
        self.assertEqual(call_args[3], "Plan a trip")

    def test_chat_rejects_empty_prompt(self):
        response = self.client.post(
            "/api/chat",
            json={"prompt": "   ", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("prompt must not be empty", response.json()["detail"])

    def test_chat_rejects_short_session_id(self):
        response = self.client.post(
            "/api/chat",
            json={"prompt": "hello", "session_id": "too-short"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("33 characters", response.json()["detail"])

    def test_chat_returns_401_when_not_authenticated(self):
        unauthed_client = TestClient(self.app, base_url="https://testserver")

        response = unauthed_client.post(
            "/api/chat",
            json={"prompt": "hello", "session_id": VALID_SESSION_ID},
            headers={"sec-fetch-mode": "cors"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("web_server.stream_agent_events")
    def test_chat_forwards_agent_error_as_in_band_sse_event(self, mock_stream_agent_events):
        # A transport-level failure discovered mid-stream can't become an
        # HTTPException — the response already started with a 200 — so it's
        # forwarded as one more SSE event instead (see _event_stream()).
        def _raising_generator():
            yield {"type": "text", "data": "partial "}
            raise RuntimeError("Agent returned an error: boom")

        mock_stream_agent_events.return_value = _raising_generator()

        response = self.client.post(
            "/api/chat",
            json={"prompt": "hello", "session_id": VALID_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        events = _parse_sse(response.text)
        self.assertEqual(events[0], {"type": "text", "data": "partial "})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("boom", events[-1]["data"]["note"])

    @patch("web_server.stream_agent_events")
    def test_chat_performs_fresh_exchange_when_no_runtime_token_cookie(
        self, mock_stream_agent_events
    ):
        # This test's own client intentionally omits the Runtime-token
        # cookie (unlike self.client from setUp, which _authed_client()
        # pre-loads with one) — exercising the real "first Runtime call
        # after login" path, where get_or_exchange_runtime_token() must
        # perform a genuine exchange.
        client = self._authed_client(self.app, with_runtime_token=False)
        mock_stream_agent_events.return_value = iter([{"type": "done", "data": "ok"}])

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "freshly-exchanged-jwt",
                "expires_in": 3600,
            }
            response = client.post(
                "/api/chat",
                json={"prompt": "hello", "session_id": VALID_SESSION_ID},
            )

        self.assertEqual(response.status_code, 200)
        mock_post.assert_called_once()
        bearer_token_sent = mock_stream_agent_events.call_args.args[5]
        self.assertEqual(bearer_token_sent, "freshly-exchanged-jwt")
        self.assertIn(web_auth.RUNTIME_TOKEN_COOKIE_NAME, response.cookies)

    @patch("web_server.stream_agent_events")
    def test_chat_does_not_re_exchange_when_cached_runtime_token_is_valid(
        self, mock_stream_agent_events
    ):
        # self.client (from setUp) already has a valid Runtime-token
        # cookie pre-loaded via _authed_client()'s default
        # with_runtime_token=True — no exchange call should happen.
        mock_stream_agent_events.return_value = iter([{"type": "done", "data": "ok"}])

        with patch("auth.requests.post") as mock_post:
            response = self.client.post(
                "/api/chat",
                json={"prompt": "hello", "session_id": VALID_SESSION_ID},
            )

        self.assertEqual(response.status_code, 200)
        mock_post.assert_not_called()
        bearer_token_sent = mock_stream_agent_events.call_args.args[5]
        self.assertEqual(bearer_token_sent, "test-runtime-jwt")

    def test_chat_returns_502_when_token_exchange_fails(self):
        client = self._authed_client(self.app, with_runtime_token=False)

        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_scope"
            response = client.post(
                "/api/chat",
                json={"prompt": "hello", "session_id": VALID_SESSION_ID},
            )

        # A token-exchange failure is a downstream-dependency failure
        # (Okta's token endpoint), not a session-invalid failure — 502,
        # not 401/redirect (DESIGN.md's Phase 2 decision).
        self.assertEqual(response.status_code, 502)
        self.assertIn("Runtime access token", response.json()["detail"])

    def test_config_endpoint_reports_history_disabled_by_default(self):
        # /api/config is intentionally unauthenticated (see server.py) —
        # confirmed here by using a client with no session cookie at all.
        unauthed_client = TestClient(self.app, base_url="https://testserver")

        response = unauthed_client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"history_enabled": False})

    def test_index_serves_html(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Travel Planning Agent", response.text)

    def test_favicon_serves_ico(self):
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertIn("image/", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")


class StaticAssetCachingTests(_AppTestCase):
    """Regression tests: a deployed frontend change (the logout button)
    was invisible to a real user because their browser kept serving a
    pre-deploy app.js with no Cache-Control header at all to say
    otherwise — no failed request, no console error, just a stale
    script silently running instead of the new one. Confirmed live by
    disabling the browser cache, which fixed it immediately. These tests
    lock in the fix: every static asset must tell the browser to
    revalidate on each request."""

    def test_index_route_sends_no_cache(self):
        response = self.client.get("/")

        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_static_mount_sends_no_cache(self):
        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache")


class WhoamiEndpointTests(_AppTestCase):
    """Tests for GET /api/whoami against a real (fake-KMS-backed) session
    cookie — unlike ChatEndpointTests' shared fixture, this exercises a
    second, differently-shaped sub to confirm sanitization end-to-end."""

    def test_returns_sanitized_actor_id_and_raw_sub_for_plain_sub(self):
        response = self.client.get("/api/whoami")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sub": "web-user", "actor_id": "web-user"})

    def test_shows_sanitization_applied_to_email_shaped_sub(self):
        client = self._authed_client(self.app, sub="kenkitts@amazon.com")

        response = client.get("/api/whoami")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"sub": "kenkitts@amazon.com", "actor_id": "kenkitts-amazon-com"},
        )

    def test_returns_401_when_session_cookie_missing(self):
        unauthed_client = TestClient(self.app, base_url="https://testserver")

        response = unauthed_client.get("/api/whoami", headers={"sec-fetch-mode": "cors"})

        self.assertEqual(response.status_code, 401)


class LogoutEndpointTests(_AppTestCase):
    """Tests for POST /api/logout — revokes the refresh token at Okta,
    then clears both cookies regardless of whether revocation succeeded."""

    def test_revokes_refresh_token_and_clears_both_cookies(self):
        with patch("auth.requests.post") as mock_post:
            mock_post.return_value.ok = True
            response = self.client.post("/api/logout")

        self.assertEqual(response.status_code, 204)
        call_args, call_kwargs = mock_post.call_args
        self.assertEqual(call_args[0], f"{self.okta_config.issuer}/v1/revoke")
        self.assertEqual(call_kwargs["data"]["token"], "test-refresh-token")
        self.assertEqual(call_kwargs["data"]["token_type_hint"], "refresh_token")
        self.assertEqual(
            call_kwargs["auth"], (self.okta_config.client_id, self.okta_config.client_secret)
        )
        set_cookie_headers = response.headers.get_list("set-cookie")
        cleared_names = {h.split("=", 1)[0] for h in set_cookie_headers}
        self.assertIn(web_auth.SESSION_COOKIE_NAME, cleared_names)
        self.assertIn(web_auth.RUNTIME_TOKEN_COOKIE_NAME, cleared_names)

    def test_still_clears_cookies_when_okta_revoke_call_fails(self):
        # A logout must never leave the user stuck "logged in" just
        # because Okta was unreachable — the browser-visible half of
        # logout (clearing this app's own cookies) always succeeds.
        import requests

        with patch("auth.requests.post", side_effect=requests.RequestException("boom")):
            response = self.client.post("/api/logout")

        self.assertEqual(response.status_code, 204)
        set_cookie_headers = response.headers.get_list("set-cookie")
        cleared_names = {h.split("=", 1)[0] for h in set_cookie_headers}
        self.assertIn(web_auth.SESSION_COOKIE_NAME, cleared_names)
        self.assertIn(web_auth.RUNTIME_TOKEN_COOKIE_NAME, cleared_names)

    def test_logout_with_no_session_cookie_still_succeeds(self):
        unauthed_client = TestClient(self.app, base_url="https://testserver")

        with patch("auth.requests.post") as mock_post:
            response = unauthed_client.post("/api/logout")

        mock_post.assert_not_called()
        self.assertEqual(response.status_code, 204)


def _conversational_event(role: str, text: str, timestamp=None) -> dict:
    """Build a fake AgentCore Memory event as returned by list_events.

    Encodes `text` the way AgentCoreMemorySessionManager actually stores
    it — a JSON-encoded Strands SessionMessage dump, confirmed against a
    real event via a live ListEvents call — rather than a bare string, so
    these tests exercise the same parsing path production data takes.
    """
    wrapped_text = json.dumps({"message": {"role": role, "content": [{"text": text}]}})
    event = {
        "payload": [{"conversational": {"role": role, "content": {"text": wrapped_text}}}]
    }
    if timestamp is not None:
        event["eventTimestamp"] = timestamp
    return event


def _title_marker_event(title: str) -> dict:
    """Build a fake title-marker event, as written by set_conversation_title."""
    return {
        "payload": [
            {
                "conversational": {
                    "role": "USER",
                    "content": {"text": f"Conversation renamed to “{title}”"},
                }
            }
        ],
        "metadata": {"conversationTitle": {"stringValue": title}},
    }


class ConversationHistoryEndpointTests(_AppTestCase):
    """Tests for GET /api/conversations and GET /api/conversations/{session_id}."""

    def setUp(self):
        patcher = patch("web_server.boto3.client")
        self.mock_boto_client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = MagicMock()
        self.mock_boto_client_factory.return_value = self.mock_client

        self.app = self._build_app(memory_id="mem-123")
        self.client = self._authed_client(self.app)

    def test_config_reports_history_enabled_when_memory_id_set(self):
        unauthed_client = TestClient(self.app, base_url="https://testserver")

        response = unauthed_client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["history_enabled"])

    def test_list_conversations_returns_sessions_with_preview(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [
                {"sessionId": BARE_SESSION_ID, "createdAt": None},
            ]
        }
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first, as the real API returns; the second turn
                # (oldest) is the one used for the preview.
                _conversational_event("ASSISTANT", "Sure, tell me more."),
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        conversations = response.json()
        self.assertEqual(len(conversations), 1)
        # The response must be the full runtimeSessionId ("actorId___bare"),
        # not the bare component ListSessions returns — /api/chat and the
        # frontend's InvokeAgentRuntime path both require the full form.
        self.assertEqual(conversations[0]["session_id"], VALID_SESSION_ID)
        self.assertEqual(conversations[0]["preview"], "Plan a trip to Boston")

        self.mock_client.list_sessions.assert_called_once_with(
            memoryId="mem-123", actorId="web-user", maxResults=100
        )
        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
        )

    def test_list_conversations_returns_empty_for_actor_with_no_sessions(self):
        # Confirmed live against a real Memory resource: ListSessions
        # raises ResourceNotFoundException for a brand-new actor rather
        # than returning an empty sessionSummaries list.
        self.mock_client.list_sessions.side_effect = _resource_not_found_error(
            "ListSessions"
        )

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_conversations_skips_sessions_with_no_events(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_conversations_truncates_long_preview(self):
        long_text = "Plan an extremely detailed multi-week trip " * 3
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [_conversational_event("USER", long_text)]
        }

        response = self.client.get("/api/conversations")

        preview = response.json()[0]["preview"]
        self.assertLessEqual(len(preview), 81)  # PREVIEW_MAX_CHARS + ellipsis
        self.assertTrue(preview.endswith("…"))

    def test_list_conversations_returns_404_when_memory_id_not_configured(self):
        app = self._build_app()
        client = self._authed_client(app)

        response = client.get("/api/conversations")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_chronological_transcript(self):
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first from the API; response should be chronological.
                _conversational_event("ASSISTANT", "Here's your itinerary."),
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["session_id"], VALID_SESSION_ID)
        self.assertEqual(
            data["turns"],
            [
                {"role": "user", "text": "Plan a trip to Boston"},
                {"role": "assistant", "text": "Here's your itinerary."},
            ],
        )
        # The full runtimeSessionId in the URL must be stripped to the bare
        # component before calling ListEvents.
        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
        )

    def test_get_conversation_falls_back_to_raw_text_for_unwrapped_events(self):
        # An event whose content.text isn't the JSON SessionMessage wrapper
        # (e.g. an older or different event format) should still render,
        # using the raw string as-is rather than dropping the turn.
        self.mock_client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "USER",
                                "content": {"text": "plain unwrapped text"},
                            }
                        }
                    ]
                }
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["turns"],
            [{"role": "user", "text": "plain unwrapped text"}],
        )

    def test_get_conversation_skips_blob_payload_items(self):
        # "blob" items are Strands session/state snapshots, not
        # conversation turns, and must not appear in the transcript.
        self.mock_client.list_events.return_value = {
            "events": [
                {"payload": [{"blob": '{"agent_id": "default", "state": {}}'}]},
                _conversational_event("USER", "Plan a trip to Boston"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["turns"],
            [{"role": "user", "text": "Plan a trip to Boston"}],
        )

    def test_get_conversation_returns_404_for_unknown_session(self):
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_404_for_resource_not_found_error(self):
        self.mock_client.list_events.side_effect = _resource_not_found_error(
            "ListEvents"
        )

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_404_when_memory_id_not_configured(self):
        app = self._build_app()
        client = self._authed_client(app)

        response = client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_get_conversation_returns_502_on_client_error(self):
        self.mock_client.list_events.side_effect = RuntimeError("boom")

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 502)
        self.assertIn("boom", response.json()["detail"])

    def test_list_conversations_prefers_title_over_preview(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [
                # Newest-first: the marker (latest) comes before the turns.
                _title_marker_event("Seattle Coffee Trip"),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get("/api/conversations")

        conversations = response.json()
        self.assertEqual(conversations[0]["title"], "Seattle Coffee Trip")
        self.assertEqual(conversations[0]["preview"], "Plan a trip to Seattle")

    def test_list_conversations_title_is_none_when_never_renamed(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            "events": [_conversational_event("USER", "Plan a trip to Seattle")]
        }

        response = self.client.get("/api/conversations")

        self.assertIsNone(response.json()[0]["title"])

    def test_list_conversations_uses_latest_title_when_renamed_twice(self):
        self.mock_client.list_sessions.return_value = {
            "sessionSummaries": [{"sessionId": BARE_SESSION_ID, "createdAt": None}]
        }
        self.mock_client.list_events.return_value = {
            # Newest-first: the most recent rename must win.
            "events": [
                _title_marker_event("Seattle Trip v2"),
                _title_marker_event("Seattle Trip v1"),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get("/api/conversations")

        self.assertEqual(response.json()[0]["title"], "Seattle Trip v2")

    def test_get_conversation_excludes_title_marker_from_transcript(self):
        self.mock_client.list_events.return_value = {
            "events": [
                _title_marker_event("Seattle Coffee Trip"),
                _conversational_event("ASSISTANT", "Here's your itinerary."),
                _conversational_event("USER", "Plan a trip to Seattle"),
            ]
        }

        response = self.client.get(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(
            response.json()["turns"],
            [
                {"role": "user", "text": "Plan a trip to Seattle"},
                {"role": "assistant", "text": "Here's your itinerary."},
            ],
        )

    def test_set_title_writes_marker_event_with_metadata(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "Seattle Coffee Trip"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"session_id": VALID_SESSION_ID, "title": "Seattle Coffee Trip"}
        )

        self.mock_client.create_event.assert_called_once()
        call_kwargs = self.mock_client.create_event.call_args.kwargs
        self.assertEqual(call_kwargs["memoryId"], "mem-123")
        self.assertEqual(call_kwargs["actorId"], "web-user")
        # The full runtimeSessionId in the URL must be stripped to the bare
        # component before calling CreateEvent, same as the other endpoints.
        self.assertEqual(call_kwargs["sessionId"], BARE_SESSION_ID)
        self.assertEqual(
            call_kwargs["metadata"],
            {"conversationTitle": {"stringValue": "Seattle Coffee Trip"}},
        )
        self.assertEqual(call_kwargs["extractionMode"], "SKIP")

    def test_set_title_normalizes_whitespace(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "  Seattle   Coffee\nTrip  "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Seattle Coffee Trip")

    def test_set_title_rejects_empty_title(self):
        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "   "},
        )

        self.assertEqual(response.status_code, 400)
        self.mock_client.create_event.assert_not_called()

    def test_set_title_truncates_long_title(self):
        self.mock_client.create_event.return_value = {"event": {"eventId": "e1"}}
        long_title = "A" * 200

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": long_title},
        )

        self.assertEqual(response.status_code, 200)
        returned_title = response.json()["title"]
        self.assertLessEqual(len(returned_title), 80)
        self.assertTrue(returned_title.endswith("…"))

    def test_set_title_returns_404_when_memory_id_not_configured(self):
        app = self._build_app()
        client = self._authed_client(app)

        response = client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "New Title"},
        )

        self.assertEqual(response.status_code, 404)

    def test_set_title_returns_502_on_client_error(self):
        self.mock_client.create_event.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "CreateEvent",
        )

        response = self.client.put(
            f"/api/conversations/{VALID_SESSION_ID}/title",
            json={"title": "New Title"},
        )

        self.assertEqual(response.status_code, 502)

    def test_delete_conversation_deletes_all_events(self):
        self.mock_client.list_events.return_value = {
            "events": [{"eventId": "e1"}, {"eventId": "e2"}, {"eventId": "e3"}]
        }
        self.mock_client.delete_event.return_value = {}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"session_id": VALID_SESSION_ID, "deleted_events": 3, "failed_events": 0},
        )
        self.assertEqual(self.mock_client.delete_event.call_count, 3)
        for call in self.mock_client.delete_event.call_args_list:
            self.assertEqual(call.kwargs["memoryId"], "mem-123")
            self.assertEqual(call.kwargs["actorId"], "web-user")
            # The full runtimeSessionId in the URL must be stripped to the
            # bare component, same as the other endpoints.
            self.assertEqual(call.kwargs["sessionId"], BARE_SESSION_ID)

        self.mock_client.list_events.assert_called_once_with(
            memoryId="mem-123",
            actorId="web-user",
            sessionId=BARE_SESSION_ID,
            maxResults=100,
            includePayloads=False,
        )

    def test_delete_conversation_paginates_through_all_events(self):
        self.mock_client.list_events.side_effect = [
            {"events": [{"eventId": "e1"}], "nextToken": "page2"},
            {"events": [{"eventId": "e2"}]},
        ]
        self.mock_client.delete_event.return_value = {}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_events"], 2)
        self.assertEqual(self.mock_client.list_events.call_count, 2)
        second_call_kwargs = self.mock_client.list_events.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs["nextToken"], "page2")

    def test_delete_conversation_reports_partial_failure(self):
        self.mock_client.list_events.return_value = {
            "events": [{"eventId": "e1"}, {"eventId": "e2"}]
        }
        self.mock_client.delete_event.side_effect = [
            {},
            ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "DeleteEvent",
            ),
        ]

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        # Best-effort: a partial failure is still a 200 with the failure
        # count surfaced, not a 502 — the caller can see what happened and
        # retry, rather than the whole delete aborting on one bad event.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"session_id": VALID_SESSION_ID, "deleted_events": 1, "failed_events": 1},
        )

    def test_delete_conversation_returns_404_for_empty_session(self):
        self.mock_client.list_events.return_value = {"events": []}

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)
        self.mock_client.delete_event.assert_not_called()

    def test_delete_conversation_returns_404_for_resource_not_found_error(self):
        self.mock_client.list_events.side_effect = _resource_not_found_error(
            "ListEvents"
        )

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)

    def test_delete_conversation_returns_502_on_other_client_error(self):
        self.mock_client.list_events.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "ListEvents",
        )

        response = self.client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 502)

    def test_delete_conversation_returns_404_when_memory_id_not_configured(self):
        app = self._build_app()
        client = self._authed_client(app)

        response = client.delete(f"/api/conversations/{VALID_SESSION_ID}")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

"""App-level OIDC authentication for the Travel Planning Agent's web UI.

Replaces the ALB's `authenticate-oidc` listener action (DESIGN.md decision
#37/#38) — this module now runs the entire OAuth 2.0 Authorization Code +
PKCE flow against Okta itself, inside the container, so the ALB in front
of it can go back to being a plain TLS-terminating load balancer with no
identity logic of its own. See DESIGN.md's Phase-1-auth-rearchitecture
decision for the full rationale (supersedes #37/#38's ALB-OIDC framing;
does not change decision #37's Runtime-auth-model, which stays IAM/SigV4).

Session model: a single, KMS-envelope-encrypted cookie carries the user's
Okta access token, refresh token, and access-token expiry — nothing is
stored server-side, so any of this deployment's Fargate tasks can handle
any request (no session affinity, no shared session store). Encryption
(not just signing, unlike the ALB's original `x-amzn-oidc-data` header)
is a deliberate choice: the tokens inside are real bearer credentials,
and a signed-but-plaintext cookie would let anyone who obtains the cookie
(e.g. via browser devtools, a malicious extension, or a log leak) read
the raw access/refresh token values directly.

Envelope encryption via AWS KMS `GenerateDataKey`/`Decrypt` (not a single
static symmetric key managed by this code) means the encrypted data key
travels inside each cookie alongside its ciphertext — so a cookie is
self-contained and remains decryptable regardless of any later key
rotation on the KMS key itself (rotation only affects newly-generated
data keys, never invalidates ones already issued). This also means no
raw key material ever exists in this process's own memory for longer
than a single request, and every decrypt/encrypt call is authorized (and
audit-logged) by the container's own IAM role via KMS, the same general
pattern already used for `bedrock-agentcore:InvokeAgentRuntime` elsewhere
in this project.

Refresh behavior: this Okta app is configured to keep returning the same,
non-rotating refresh token on every refresh (confirmed intentional
project decision — see DESIGN.md) — so there is no "must persist the
newest refresh token" race to handle here, unlike IdPs that rotate
refresh tokens on each use. A request arriving with an expired access
token but a still-valid refresh token transparently calls Okta's token
endpoint inline, gets a new access token, and rewrites the session cookie
on that same response before the caller ever sees a failure.

Phase 2 (auth rearchitecture, added after Phase 1's initial deploy): this
module also implements RFC 8693 OAuth 2.0 Token Exchange
(exchange_token_for_runtime()) — the web server exchanges a live Okta
access token for a second, Runtime-audienced JWT, used to call
AgentCore's InvokeAgentRuntime with JWT bearer-token auth instead of
IAM/SigV4 (see web/agent_client.py). This exchanged token is cached in
its own cookie (RUNTIME_TOKEN_COOKIE_NAME), independently
encrypted/refreshed from the main session cookie — not folded into it —
because a real, measured cookie size check found that stacking a second
full JWT into the existing session cookie would push it past the ~4KB
practical per-cookie browser limit. See DESIGN.md's Phase 2 section for
the full design rationale and the 13 clarifying-question answers behind
it.
"""
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import boto3
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

# --- Cookie names and lifetimes -------------------------------------------

SESSION_COOKIE_NAME = "travel_agent_session"

# Phase 2 (auth rearchitecture): a second, independent cookie carrying the
# Runtime-audienced JWT obtained via RFC 8693 token exchange (see
# RuntimeOidcConfig/exchange_token_for_runtime() below). Kept as a
# separate cookie from SESSION_COOKIE_NAME rather than folded into it —
# a real, measured envelope-encrypted-cookie size check found that
# stacking a second full JWT into the existing session cookie would push
# it past the ~4KB practical per-cookie browser limit; splitting into two
# independently-sized cookies avoids that cliff entirely (DESIGN.md's
# Phase 2 decision covers the actual measurement). Same KMS envelope
# encryption as SESSION_COOKIE_NAME — this token is just as much a real,
# usable bearer credential (it authenticates as this user to the
# Runtime) as the Okta access/refresh tokens are, so it gets the same
# confidentiality treatment.
RUNTIME_TOKEN_COOKIE_NAME = "travel_agent_runtime_token"

# Short-lived, separate cookie that only survives the redirect round-trip
# to Okta and back — carries the CSRF `state` value, the PKCE verifier,
# and the originally-requested URL to return to after login. Deleted as
# soon as the callback consumes it (successfully or not).
PENDING_LOGIN_COOKIE_NAME = "travel_agent_pending_login"
PENDING_LOGIN_COOKIE_MAX_AGE_SECONDS = 600  # 10 minutes — plenty for a real login.

# Okta access tokens are typically short-lived (often ~1h); refresh tokens
# considerably longer. Only the access-token expiry is tracked explicitly
# here — the refresh token's own expiry is discovered implicitly (Okta
# rejects an expired/invalid refresh_token grant with a 400, which is
# treated as "refresh failed, start the OIDC dance over").
ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 30  # Refresh slightly before actual expiry.

_PKCE_VERIFIER_LENGTH = 64  # RFC 7636 allows 43-128 chars; 64 is a safe middle.


@dataclass(frozen=True)
class OktaConfig:
    """Static Okta app configuration, passed in at server startup.

    Mirrors the shape of the *_endpoint values web_stack.py already read
    for the ALB's authenticate-oidc action (DESIGN.md decision #38) —
    kept as explicit endpoints (not a single discovery_url this module
    fetches itself) to avoid adding a live network dependency to every
    container startup; the same tradeoff the ALB config already made.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid offline_access"


@dataclass(frozen=True)
class RuntimeOidcConfig:
    """Static config for exchanging a user's Okta access token for a
    Runtime-audienced JWT via RFC 8693 OAuth 2.0 Token Exchange.

    Deliberately a separate dataclass from OktaConfig, not additional
    fields on it — this describes a *different* Okta authorization
    server (the project's real Okta org uses a dedicated auth server for
    this, distinct from the one that issues the web-login app's tokens),
    with its own client_id/client_secret (a distinct "API Services" app
    configured for the Token Exchange grant type, not the web-login
    app's confidential-client OIDC app) and its own token_endpoint.

    audience/scope are the RFC 8693 `audience`/`scope` request
    parameters — audience must match what the Runtime's own JWT
    authorizer is configured to accept as `allowedAudience`
    (cdk/stacks/runtime_stack.py), and scope must match its
    `allowedScopes`. issuer is used only to build discovery_url-style
    documentation/debugging context, not sent in the exchange request
    itself (Okta identifies the target authorization server purely from
    which token_endpoint URL the request is POSTed to).
    """

    issuer: str
    token_endpoint: str
    client_id: str
    client_secret: str
    audience: str
    scope: str


@dataclass(frozen=True)
class RuntimeToken:
    """The plaintext contents of a decrypted Runtime-token cookie."""

    access_token: str
    expires_at: float  # Unix timestamp (seconds).


@dataclass(frozen=True)
class SessionTokens:
    """The plaintext contents of a decrypted session cookie."""

    access_token: str
    refresh_token: str
    access_token_expires_at: float  # Unix timestamp (seconds).
    sub: str


class AuthError(Exception):
    """Raised for any failure that means "this session is not usable."

    Callers (server.py's FastAPI dependency) catch this and respond with
    either a 401 (for /api/* fetch() calls) or a redirect to Okta (for a
    top-level page load) — see require_session() below for that split.
    """


# --- PKCE + state ----------------------------------------------------------


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE's S256 method.

    Uses only the standard library (hashlib + base64) — no new dependency
    needed for PKCE itself, since S256 is just "base64url(sha256(verifier))"
    per RFC 7636.
    """
    verifier = _b64url_no_pad(secrets.token_bytes(_PKCE_VERIFIER_LENGTH))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state() -> str:
    """A random, unguessable CSRF token for the OAuth `state` parameter."""
    return secrets.token_urlsafe(32)


# --- Building the Okta redirect --------------------------------------------


def build_authorization_url(config: OktaConfig, state: str, code_challenge: str) -> str:
    """Build the URL to redirect the user's browser to for login."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": config.scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.authorization_endpoint}?{urlencode(params)}"


# --- Token exchange ---------------------------------------------------------


def exchange_code_for_tokens(config: OktaConfig, code: str, code_verifier: str) -> SessionTokens:
    """Exchange an authorization code for tokens at Okta's token endpoint.

    Raises AuthError on any failure (network error, non-2xx response,
    missing expected fields) — the callback route treats this the same as
    any other login failure (redirect back to the start of the flow).
    """
    try:
        response = requests.post(
            config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": code_verifier,
            },
            timeout=10.0,
        )
    except requests.RequestException as e:
        raise AuthError(f"Token exchange request failed: {e}") from e

    if not response.ok:
        raise AuthError(f"Token exchange failed ({response.status_code}): {response.text}")

    return _session_tokens_from_token_response(response.json())


def refresh_access_token(config: OktaConfig, refresh_token: str, sub: str) -> SessionTokens:
    """Use a refresh token to obtain a new access token.

    This project's Okta app is configured to return the *same* refresh
    token on every refresh (not a rotating one) — see this module's
    docstring — so the returned SessionTokens always carries the same
    refresh_token that was passed in, even if Okta's response omits a
    refresh_token field entirely (some IdPs only include it on the
    original grant).
    """
    try:
        response = requests.post(
            config.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
            timeout=10.0,
        )
    except requests.RequestException as e:
        raise AuthError(f"Token refresh request failed: {e}") from e

    if not response.ok:
        raise AuthError(f"Token refresh failed ({response.status_code}): {response.text}")

    body = response.json()
    body.setdefault("refresh_token", refresh_token)
    return _session_tokens_from_token_response(body, fallback_sub=sub)


# RFC 8693 defines these as fixed, literal URN strings, not configurable
# per-IdP values.
_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE_URN = "urn:ietf:params:oauth:token-type:access_token"

# Same expiry-skew treatment as ACCESS_TOKEN_EXPIRY_SKEW_SECONDS above,
# applied to the exchanged Runtime token's own expires_in.
RUNTIME_TOKEN_EXPIRY_SKEW_SECONDS = 30


def exchange_token_for_runtime(config: RuntimeOidcConfig, okta_access_token: str) -> RuntimeToken:
    """Exchange a live Okta access token for a Runtime-audienced JWT.

    Real RFC 8693 OAuth 2.0 Token Exchange (not direct token forwarding)
    — this project's Runtime is configured to trust this specific,
    dedicated exchange-app/audience/scope combination (DESIGN.md's Phase
    2 decision), which lets the Runtime's own JWT authorizer validate
    `aud`/`client_id`/`scp` without needing to also trust the web-login
    app's own tokens directly.

    `subject_token` is the caller's live, valid Okta access token (from
    the main session cookie) — this function does not itself check its
    expiry; get_or_exchange_runtime_token() below only calls this once
    it already has a confirmed-valid Okta access token in hand (post
    Phase 1's own inline-refresh-if-needed step).

    Raises AuthError on any failure (network error, non-2xx response,
    missing expected fields) — callers surface this as a 502, distinct
    from a session-invalid 401/redirect (DESIGN.md's Phase 2 decision:
    an Okta-side token-exchange failure is a downstream dependency
    failure, not evidence the caller's own session is bad).
    """
    try:
        response = requests.post(
            config.token_endpoint,
            data={
                "grant_type": _TOKEN_EXCHANGE_GRANT_TYPE,
                "subject_token_type": _ACCESS_TOKEN_TYPE_URN,
                "subject_token": okta_access_token,
                "scope": config.scope,
                "audience": config.audience,
            },
            auth=(config.client_id, config.client_secret),
            timeout=10.0,
        )
    except requests.RequestException as e:
        raise AuthError(f"Runtime token exchange request failed: {e}") from e

    if not response.ok:
        raise AuthError(f"Runtime token exchange failed ({response.status_code}): {response.text}")

    body = response.json()
    access_token = body.get("access_token")
    expires_in = body.get("expires_in")
    if not access_token or expires_in is None:
        raise AuthError(f"Runtime token exchange response missing expected fields: {body!r}")

    expires_at = time.time() + float(expires_in) - RUNTIME_TOKEN_EXPIRY_SKEW_SECONDS
    return RuntimeToken(access_token=access_token, expires_at=expires_at)


def _session_tokens_from_token_response(body: dict, fallback_sub: Optional[str] = None) -> SessionTokens:
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not access_token or not refresh_token or expires_in is None:
        raise AuthError(f"Token response missing expected fields: {body!r}")

    sub = _sub_from_access_token(access_token) or fallback_sub
    if not sub:
        raise AuthError("Could not determine 'sub' claim from access token")

    expires_at = time.time() + float(expires_in) - ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
    return SessionTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=expires_at,
        sub=sub,
    )


def _sub_from_access_token(access_token: str) -> Optional[str]:
    """Best-effort extraction of the `sub` claim from an Okta access token.

    Okta access tokens are JWTs; decoding without signature verification
    is safe here specifically because this value is only ever read
    immediately after this exact token was obtained directly from Okta's
    own token endpoint over TLS (the authorization_code or refresh_token
    grant, both performed by this server, never supplied by an untrusted
    caller) — there is no scenario where an attacker-controlled token
    reaches this function. This is a materially different trust context
    than the old ALB header verification, which had to defend against a
    forged/replayed header reaching the container from an unverified
    source.
    """
    try:
        import jwt as _jwt

        claims = _jwt.decode(access_token, options={"verify_signature": False})
        sub = claims.get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001 - best-effort; caller has a fallback
        return None


# --- KMS envelope encryption -------------------------------------------------


class _KmsEnvelopeCodec:
    """Shared envelope-encryption primitives for KMS-backed cookie codecs.

    Both SessionCookieCodec (Phase 1) and RuntimeTokenCookieCodec (Phase
    2) need the identical envelope scheme — a fresh GenerateDataKey per
    encode(), local AES-GCM using the plaintext data key, only the
    *encrypted* data key ever persisted — applied to a different
    plaintext JSON shape. Factored out here (rather than duplicated)
    once a second cookie needed the same treatment; see this module's
    docstring and SESSION_COOKIE_NAME/RUNTIME_TOKEN_COOKIE_NAME above for
    why two cookies exist instead of one.
    """

    def __init__(self, kms_key_id: str, kms_client=None) -> None:
        self._kms_key_id = kms_key_id
        self._kms = kms_client or boto3.client("kms")

    def _encode_json(self, payload: dict) -> str:
        plaintext = json.dumps(payload).encode("utf-8")

        data_key_response = self._kms.generate_data_key(
            KeyId=self._kms_key_id, KeySpec="AES_256"
        )
        plaintext_key = data_key_response["Plaintext"]
        encrypted_key = data_key_response["CiphertextBlob"]

        nonce = secrets.token_bytes(12)  # 96-bit nonce, standard for AES-GCM.
        ciphertext = AESGCM(plaintext_key).encrypt(nonce, plaintext, associated_data=None)

        envelope = {
            "k": _b64url_no_pad(encrypted_key),
            "n": _b64url_no_pad(nonce),
            "c": _b64url_no_pad(ciphertext),
        }
        return _b64url_no_pad(json.dumps(envelope).encode("utf-8"))

    def _decode_json(self, cookie_value: str, error_label: str) -> dict:
        try:
            envelope = json.loads(_b64url_decode(cookie_value))
            encrypted_key = _b64url_decode(envelope["k"])
            nonce = _b64url_decode(envelope["n"])
            ciphertext = _b64url_decode(envelope["c"])
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise AuthError(f"Malformed {error_label}: {e}") from e

        try:
            decrypt_response = self._kms.decrypt(
                CiphertextBlob=encrypted_key, KeyId=self._kms_key_id
            )
        except Exception as e:  # noqa: BLE001 - surfaced as a clean AuthError
            raise AuthError(f"Failed to decrypt {error_label} data key: {e}") from e
        plaintext_key = decrypt_response["Plaintext"]

        try:
            plaintext = AESGCM(plaintext_key).decrypt(nonce, ciphertext, associated_data=None)
            return json.loads(plaintext)
        except Exception as e:  # noqa: BLE001 - tampered/corrupt cookie
            raise AuthError(f"Failed to decrypt {error_label}: {e}") from e


class SessionCookieCodec(_KmsEnvelopeCodec):
    """Encrypts/decrypts SessionTokens into/from a single cookie value.

    Envelope encryption: each call to encode() asks KMS for a fresh data
    key (GenerateDataKey), uses its plaintext copy to AES-GCM-encrypt the
    token payload locally, then discards the plaintext data key
    immediately — only the *encrypted* data key (returned by KMS
    alongside the plaintext one) is stored in the cookie, next to the
    ciphertext. decode() calls KMS Decrypt on that encrypted data key to
    recover the plaintext data key, then AES-GCM-decrypts the payload.

    This means: (1) the raw AES key is never persisted anywhere, only
    ever held in memory for the duration of one encrypt/decrypt call, and
    (2) a cookie is self-contained — decoding it later never depends on
    which data key was used or whether the KMS key has since been
    rotated, since the encrypted data key needed to decrypt it travels
    with the ciphertext itself.
    """

    def encode(self, tokens: SessionTokens) -> str:
        return self._encode_json(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "access_token_expires_at": tokens.access_token_expires_at,
                "sub": tokens.sub,
            }
        )

    def decode(self, cookie_value: str) -> SessionTokens:
        payload = self._decode_json(cookie_value, error_label="session cookie")
        try:
            return SessionTokens(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                access_token_expires_at=payload["access_token_expires_at"],
                sub=payload["sub"],
            )
        except KeyError as e:
            raise AuthError(f"Session cookie missing field: {e}") from e


class RuntimeTokenCookieCodec(_KmsEnvelopeCodec):
    """Encrypts/decrypts RuntimeToken into/from RUNTIME_TOKEN_COOKIE_NAME.

    Same KMS envelope scheme as SessionCookieCodec (see
    _KmsEnvelopeCodec), applied to the smaller RuntimeToken payload
    (just the exchanged access token + its own expiry) — kept as a
    genuinely separate cookie from the main session, not merged into it;
    see RUNTIME_TOKEN_COOKIE_NAME's docstring for the real cookie-size
    measurement that drove this split.
    """

    def encode(self, token: RuntimeToken) -> str:
        return self._encode_json(
            {"access_token": token.access_token, "expires_at": token.expires_at}
        )

    def decode(self, cookie_value: str) -> RuntimeToken:
        payload = self._decode_json(cookie_value, error_label="runtime-token cookie")
        try:
            return RuntimeToken(
                access_token=payload["access_token"],
                expires_at=payload["expires_at"],
            )
        except KeyError as e:
            raise AuthError(f"Runtime-token cookie missing field: {e}") from e


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# --- Pending-login (state/PKCE) cookie --------------------------------------


def encode_pending_login(state: str, code_verifier: str, return_to: str) -> str:
    """Plain (unencrypted) JSON, base64url'd — no secrets in this payload.

    Unlike the session cookie, nothing here is a bearer credential: state
    is a one-time CSRF nonce, the PKCE verifier is useless without the
    matching authorization code (which only Okta can mint, and only after
    verifying the code_challenge derived from this same verifier), and
    return_to is just a URL path. Signing/encryption would add no real
    protection here — the actual security property (this exact browser
    completed this exact login attempt) comes from `state` matching an
    unpredictable value the server generated and the browser round-tripped
    unmodified, not from the cookie's confidentiality or integrity.
    """
    payload = {"state": state, "code_verifier": code_verifier, "return_to": return_to}
    return _b64url_no_pad(json.dumps(payload).encode("utf-8"))


def decode_pending_login(cookie_value: str) -> dict:
    try:
        return json.loads(_b64url_decode(cookie_value))
    except (ValueError, json.JSONDecodeError) as e:
        raise AuthError(f"Malformed pending-login cookie: {e}") from e


# --- FastAPI integration -----------------------------------------------------


def set_session_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def set_runtime_token_cookie(response: Response, cookie_value: str) -> None:
    """Same cookie flags as set_session_cookie() — see that function's
    rationale (httponly/secure/samesite=lax all apply identically to
    this cookie, since it carries an equally-real bearer credential)."""
    response.set_cookie(
        key=RUNTIME_TOKEN_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_runtime_token_cookie(response: Response) -> None:
    response.delete_cookie(key=RUNTIME_TOKEN_COOKIE_NAME, path="/")


def set_pending_login_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        key=PENDING_LOGIN_COOKIE_NAME,
        value=cookie_value,
        max_age=PENDING_LOGIN_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_pending_login_cookie(response: Response) -> None:
    response.delete_cookie(key=PENDING_LOGIN_COOKIE_NAME, path="/")


def is_browser_navigation(request: Request) -> bool:
    """True if this looks like a top-level page load, not a fetch()/XHR call.

    Used to decide whether an unauthenticated/expired session should get a
    302 redirect to Okta (a real page navigation can follow that
    transparently) or a clean 401 (a fetch() call to /api/* should see a
    real status code it can react to, rather than the CORS-blocked-
    redirect situation decision #47 had to work around under the ALB).

    `Sec-Fetch-Mode: navigate` is sent by all current major browsers for
    top-level navigations and is the most direct signal available; the
    Accept header is used as a fallback for the rare client that omits
    Sec-Fetch-Mode, treating an explicit request for HTML as a page load.
    """
    sec_fetch_mode = request.headers.get("sec-fetch-mode")
    if sec_fetch_mode is not None:
        return sec_fetch_mode == "navigate"
    accept = request.headers.get("accept", "")
    return "text/html" in accept


class AuthContext:
    """Resolved identity for one request, returned by require_session()."""

    def __init__(self, sub: str, tokens: SessionTokens, refreshed: bool) -> None:
        self.sub = sub
        self.tokens = tokens
        self.refreshed = refreshed


def get_or_refresh_session(
    request: Request, codec: SessionCookieCodec, okta_config: OktaConfig
) -> AuthContext:
    """Resolve the caller's session, transparently refreshing if expired.

    Raises AuthError if there is no session cookie, it fails to decrypt,
    or the access token is expired and the refresh attempt also fails
    (e.g. the refresh token itself has expired) — in every such case the
    caller must re-run the full OIDC flow; see is_browser_navigation() for
    how the route layer decides whether to redirect or return a 401 for
    that.
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        raise AuthError("No session cookie present")

    tokens = codec.decode(cookie_value)

    if time.time() < tokens.access_token_expires_at:
        return AuthContext(sub=tokens.sub, tokens=tokens, refreshed=False)

    # Access token expired (or within the skew window) — refresh inline,
    # synchronously, so the caller's own request can proceed with a valid
    # token and the response can carry the rewritten cookie in the same
    # round-trip (DESIGN.md Phase 1 decision: no deferred/best-effort
    # cookie rewrite).
    refreshed_tokens = refresh_access_token(okta_config, tokens.refresh_token, tokens.sub)
    return AuthContext(sub=refreshed_tokens.sub, tokens=refreshed_tokens, refreshed=True)


def apply_refreshed_cookie_if_needed(
    response: Response, context: AuthContext, codec: SessionCookieCodec
) -> None:
    """Write a fresh Set-Cookie if get_or_refresh_session() had to refresh."""
    if context.refreshed:
        set_session_cookie(response, codec.encode(context.tokens))


class RuntimeTokenContext:
    """Resolved Runtime-audienced JWT for one request, plus whether a
    fresh exchange was just performed (mirrors AuthContext's
    `refreshed` flag)."""

    def __init__(self, access_token: str, expires_at: float, exchanged: bool) -> None:
        self.access_token = access_token
        self.expires_at = expires_at
        self.exchanged = exchanged


def get_or_exchange_runtime_token(
    request: Request,
    codec: RuntimeTokenCookieCodec,
    runtime_oidc_config: RuntimeOidcConfig,
    okta_access_token: str,
) -> RuntimeTokenContext:
    """Resolve a valid Runtime-audienced JWT, exchanging fresh if needed.

    Unlike get_or_refresh_session() above, a missing/expired/undecrypt-
    able Runtime-token cookie is never itself an auth failure — it just
    means this specific derived token needs to be (re-)minted, which this
    function always can as long as the caller already has a live,
    valid Okta access token in hand (DESIGN.md's Phase 2 decision: this
    cookie is a derived artifact of the main session, not an independent
    credential the user "logs in" to get, so its absence never forces a
    re-login). Only a genuine failure of the exchange call itself
    (raised as AuthError by exchange_token_for_runtime()) propagates —
    callers surface that as a 502 (a downstream-dependency failure), not
    a 401/redirect.
    """
    cookie_value = request.cookies.get(RUNTIME_TOKEN_COOKIE_NAME)
    if cookie_value:
        try:
            token = codec.decode(cookie_value)
        except AuthError:
            token = None
        else:
            if time.time() < token.expires_at:
                return RuntimeTokenContext(
                    access_token=token.access_token, expires_at=token.expires_at, exchanged=False
                )
    exchanged = exchange_token_for_runtime(runtime_oidc_config, okta_access_token)
    return RuntimeTokenContext(
        access_token=exchanged.access_token, expires_at=exchanged.expires_at, exchanged=True
    )


def apply_runtime_token_cookie_if_needed(
    response: Response, context: RuntimeTokenContext, codec: RuntimeTokenCookieCodec
) -> None:
    """Write a fresh Set-Cookie if get_or_exchange_runtime_token() had to
    perform a new exchange."""
    if context.exchanged:
        set_runtime_token_cookie(
            response,
            codec.encode(RuntimeToken(access_token=context.access_token, expires_at=context.expires_at)),
        )


def redirect_to_login(config: OktaConfig, return_to: str) -> RedirectResponse:
    """Build the full "start the OIDC dance" response: redirect + pending cookie."""
    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()
    authorization_url = build_authorization_url(config, state, code_challenge)

    redirect = RedirectResponse(url=authorization_url, status_code=302)
    set_pending_login_cookie(
        redirect, encode_pending_login(state, code_verifier, return_to)
    )
    return redirect

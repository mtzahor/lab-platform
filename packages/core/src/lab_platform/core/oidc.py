from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from lab_platform.core.errors import (
    OidcConfigurationInvalidError,
    OidcIdentityNotMappedError,
    OidcLoginFailedError,
)
from lab_platform.core.identity import IssuedSession
from lab_platform.models import AuthenticationSource, Organisation, User

_JWT_MAX_BYTES = 128 * 1024
_MAX_PENDING_TRANSACTIONS = 10_000
_RSA_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


@dataclass(frozen=True, slots=True)
class OidcProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class OidcLoginStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingLogin:
    nonce: str
    code_verifier: str
    organisation_slug: str
    redirect_uri: str
    expires_at: datetime


class OidcProvider(Protocol):
    async def metadata(self) -> OidcProviderMetadata: ...

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> str: ...

    async def jwks(self) -> Mapping[str, object]: ...


class OidcIdentityRepository(Protocol):
    async def get_organisation_by_slug(self, slug: str) -> Organisation | None: ...

    async def get_user_by_username(
        self,
        organisation_id: UUID,
        username: str,
    ) -> User | None: ...


class OidcSessionIssuer(Protocol):
    async def login_oidc_user(
        self,
        *,
        user: User,
        organisation: Organisation,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> IssuedSession: ...

    async def record_oidc_login_failure(
        self,
        *,
        organisation_slug: str,
        reason: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> None: ...


class OidcAuthenticationService:
    """Stateful OIDC authorization-code client with PKCE and strict ID-token checks.

    Login transactions are deliberately process-local and short-lived. A state value
    is single-use: it is removed before the authorization code is exchanged, which
    also prevents callback replay if an exchange fails.
    """

    def __init__(
        self,
        repository: OidcIdentityRepository,
        session_issuer: OidcSessionIssuer,
        *,
        enabled: bool,
        issuer_url: str | None,
        client_id: str | None,
        scopes: Sequence[str] = ("openid", "profile", "email"),
        username_claim: str = "preferred_username",
        provider: OidcProvider | None = None,
        transaction_ttl_seconds: int = 600,
        clock_skew_seconds: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if transaction_ttl_seconds <= 0:
            raise ValueError("OIDC transaction lifetime must be positive")
        if clock_skew_seconds < 0:
            raise ValueError("OIDC clock skew must not be negative")
        self._repository = repository
        self._session_issuer = session_issuer
        self._enabled = enabled
        self._issuer_url = issuer_url
        self._client_id = client_id
        self._scopes = tuple(scopes)
        self._username_claim = username_claim
        self._provider = provider
        self._transaction_ttl = timedelta(seconds=transaction_ttl_seconds)
        self._clock_skew = timedelta(seconds=clock_skew_seconds)
        self._clock = clock
        self._random_bytes = random_bytes
        self._pending: dict[str, _PendingLogin] = {}
        self._pending_lock = asyncio.Lock()

    async def start_login(
        self,
        *,
        organisation_slug: str,
        redirect_uri: str,
    ) -> OidcLoginStart:
        provider, issuer, client_id = self._require_configured()
        _validate_redirect_uri(redirect_uri)
        metadata = await self._load_metadata(provider, issuer)
        now = self._clock()
        state = _base64url(self._random_bytes(32))
        nonce = _base64url(self._random_bytes(32))
        verifier = _base64url(self._random_bytes(64))
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        expires_at = now + self._transaction_ttl
        state_digest = _state_digest(state)
        async with self._pending_lock:
            self._discard_expired(now)
            if len(self._pending) >= _MAX_PENDING_TRANSACTIONS:
                self._pending.pop(next(iter(self._pending)))
            self._pending[state_digest] = _PendingLogin(
                nonce=nonce,
                code_verifier=verifier,
                organisation_slug=organisation_slug.strip().casefold(),
                redirect_uri=redirect_uri,
                expires_at=expires_at,
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self._scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if urlsplit(metadata.authorization_endpoint).query else "?"
        return OidcLoginStart(
            authorization_url=f"{metadata.authorization_endpoint}{separator}{query}",
            expires_at=expires_at,
        )

    async def complete_login(
        self,
        *,
        state: str,
        code: str | None,
        provider_error: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> IssuedSession:
        provider, issuer, client_id = self._require_configured()
        pending = await self._consume_state(state)
        try:
            return await self._complete_pending_login(
                pending,
                provider=provider,
                issuer=issuer,
                client_id=client_id,
                code=code,
                provider_error=provider_error,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
            )
        except (OidcLoginFailedError, OidcIdentityNotMappedError) as exc:
            await self._session_issuer.record_oidc_login_failure(
                organisation_slug=pending.organisation_slug,
                reason=exc.message,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
            )
            raise

    async def _complete_pending_login(
        self,
        pending: _PendingLogin,
        *,
        provider: OidcProvider,
        issuer: str,
        client_id: str,
        code: str | None,
        provider_error: str | None,
        ip_address: str | None,
        user_agent: str | None,
        request_id: UUID | None,
    ) -> IssuedSession:
        if provider_error is not None or code is None or not code.strip() or len(code) > 4096:
            raise OidcLoginFailedError("The identity provider did not complete the login.")
        try:
            metadata = await self._load_metadata(provider, issuer)
            id_token = await provider.exchange_code(
                code=code,
                redirect_uri=pending.redirect_uri,
                code_verifier=pending.code_verifier,
            )
            jwks = await provider.jwks()
        except OidcLoginFailedError:
            raise
        except Exception:
            raise OidcLoginFailedError("The identity provider login failed.") from None
        claims = validate_oidc_id_token(
            id_token,
            jwks=jwks,
            issuer=metadata.issuer,
            audience=client_id,
            nonce=pending.nonce,
            now=self._clock(),
            clock_skew=self._clock_skew,
        )
        username = claims.get(self._username_claim)
        if not isinstance(username, str) or not username.strip():
            raise OidcIdentityNotMappedError(
                "The OIDC identity is not mapped to a Lab Platform user."
            )
        organisation = await self._repository.get_organisation_by_slug(pending.organisation_slug)
        if organisation is None:
            raise OidcIdentityNotMappedError(
                "The OIDC identity is not mapped to a Lab Platform user."
            )
        user = await self._repository.get_user_by_username(
            organisation.id,
            username.strip().casefold(),
        )
        if user is None or user.authentication_source is not AuthenticationSource.OIDC:
            raise OidcIdentityNotMappedError(
                "The OIDC identity is not mapped to a Lab Platform user."
            )
        return await self._session_issuer.login_oidc_user(
            user=user,
            organisation=organisation,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )

    async def _consume_state(self, state: str) -> _PendingLogin:
        if not state or len(state) > 1024:
            raise OidcLoginFailedError("The OIDC login state is invalid or expired.")
        now = self._clock()
        async with self._pending_lock:
            pending = self._pending.pop(_state_digest(state), None)
            self._discard_expired(now)
        if pending is None or pending.expires_at <= now:
            raise OidcLoginFailedError("The OIDC login state is invalid or expired.")
        return pending

    async def _load_metadata(
        self,
        provider: OidcProvider,
        issuer: str,
    ) -> OidcProviderMetadata:
        try:
            metadata = await provider.metadata()
        except OidcConfigurationInvalidError:
            raise
        except Exception:
            raise OidcLoginFailedError("The identity provider is unavailable.") from None
        if metadata.issuer != issuer:
            raise OidcConfigurationInvalidError(
                "The identity provider metadata issuer does not match configuration."
            )
        for endpoint in (
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            metadata.jwks_uri,
        ):
            _validate_provider_endpoint(endpoint)
        return metadata

    def _require_configured(self) -> tuple[OidcProvider, str, str]:
        if not self._enabled:
            raise OidcConfigurationInvalidError("OIDC authentication is disabled.")
        if self._provider is None or self._issuer_url is None or self._client_id is None:
            raise OidcConfigurationInvalidError("OIDC authentication is not configured.")
        return self._provider, self._issuer_url, self._client_id

    def _discard_expired(self, now: datetime) -> None:
        expired = [key for key, value in self._pending.items() if value.expires_at <= now]
        for key in expired:
            self._pending.pop(key, None)


def validate_oidc_id_token(
    token: str,
    *,
    jwks: Mapping[str, object],
    issuer: str,
    audience: str,
    nonce: str,
    now: datetime,
    clock_skew: timedelta = timedelta(seconds=60),
) -> Mapping[str, object]:
    """Validate an RS256 OIDC ID token using a provider JWKS document."""

    if now.tzinfo is None or now.utcoffset() is None or clock_skew < timedelta(0):
        raise OidcConfigurationInvalidError("OIDC token validation time is invalid.")
    if len(token.encode("utf-8")) > _JWT_MAX_BYTES:
        raise OidcLoginFailedError("The identity token is invalid.")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise OidcLoginFailedError("The identity token is invalid.")
    try:
        header = _json_object(_base64url_decode(parts[0]))
        claims = _json_object(_base64url_decode(parts[1]))
        signature = _base64url_decode(parts[2])
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise OidcLoginFailedError("The identity token is invalid.") from None

    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise OidcLoginFailedError("The identity token signing algorithm is not allowed.")
    if header.get("typ") not in {None, "JWT"} or header.get("crit") is not None:
        raise OidcLoginFailedError("The identity token header is not supported.")
    key = _select_rsa_signing_key(jwks, str(header["kid"]))
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if not _verify_rs256(signing_input, signature, key):
        raise OidcLoginFailedError("The identity token signature is invalid.")

    _validate_claims(
        claims,
        issuer=issuer,
        audience=audience,
        nonce=nonce,
        now=now,
        clock_skew=clock_skew,
    )
    return claims


def _validate_claims(
    claims: Mapping[str, object],
    *,
    issuer: str,
    audience: str,
    nonce: str,
    now: datetime,
    clock_skew: timedelta,
) -> None:
    if claims.get("iss") != issuer:
        raise OidcLoginFailedError("The identity token issuer is invalid.")
    audiences = claims.get("aud")
    if isinstance(audiences, str):
        audience_values = [audiences]
    elif isinstance(audiences, list) and all(isinstance(item, str) for item in audiences):
        audience_values = audiences
    else:
        raise OidcLoginFailedError("The identity token audience is invalid.")
    if audience not in audience_values:
        raise OidcLoginFailedError("The identity token audience is invalid.")
    authorised_party = claims.get("azp")
    if (len(audience_values) > 1 or authorised_party is not None) and authorised_party != audience:
        raise OidcLoginFailedError("The identity token authorized party is invalid.")

    now_value = now.astimezone(UTC).timestamp()
    skew = clock_skew.total_seconds()
    expires_at = _numeric_claim(claims, "exp", required=True)
    if expires_at is None or expires_at <= now_value - skew:
        raise OidcLoginFailedError("The identity token has expired.")
    not_before = _numeric_claim(claims, "nbf", required=False)
    if not_before is not None and not_before > now_value + skew:
        raise OidcLoginFailedError("The identity token is not yet valid.")
    issued_at = _numeric_claim(claims, "iat", required=False)
    if issued_at is not None and issued_at > now_value + skew:
        raise OidcLoginFailedError("The identity token issue time is invalid.")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise OidcLoginFailedError("The identity token nonce is invalid.")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 1024:
        raise OidcLoginFailedError("The identity token subject is invalid.")


def _numeric_claim(
    claims: Mapping[str, object],
    name: str,
    *,
    required: bool,
) -> float | None:
    value = claims.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OidcLoginFailedError(f"The identity token {name} claim is invalid.")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise OidcLoginFailedError(f"The identity token {name} claim is invalid.")
    return numeric


def _select_rsa_signing_key(
    jwks: Mapping[str, object],
    key_id: str,
) -> Mapping[str, object]:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise OidcLoginFailedError("The identity provider signing keys are invalid.")
    candidates = [
        key
        for key in keys
        if isinstance(key, dict)
        and key.get("kid") == key_id
        and key.get("kty") == "RSA"
        and key.get("use") in {None, "sig"}
        and key.get("alg") in {None, "RS256"}
        and _allows_signature_verification(key.get("key_ops"))
    ]
    if len(candidates) != 1:
        raise OidcLoginFailedError("No unambiguous identity-provider signing key was found.")
    return candidates[0]


def _verify_rs256(
    signing_input: bytes,
    signature: bytes,
    jwk: Mapping[str, object],
) -> bool:
    modulus_value = jwk.get("n")
    exponent_value = jwk.get("e")
    if not isinstance(modulus_value, str) or not isinstance(exponent_value, str):
        return False
    try:
        modulus = int.from_bytes(_base64url_decode(modulus_value), "big")
        exponent = int.from_bytes(_base64url_decode(exponent_value), "big")
    except ValueError:
        return False
    if not 2048 <= modulus.bit_length() <= 8192:
        return False
    if exponent < 3 or exponent % 2 == 0 or exponent.bit_length() > 32:
        return False
    length = (modulus.bit_length() + 7) // 8
    if len(signature) != length:
        return False
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        return False
    decoded = pow(signature_number, exponent, modulus).to_bytes(length, "big")
    digest_info = _RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding_length = length - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    return hmac.compare_digest(decoded, expected)


def _json_object(value: bytes) -> Mapping[str, object]:
    decoded = json.loads(value.decode("utf-8"))
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError("JWT component must be a JSON object")
    return decoded


def _allows_signature_verification(value: object) -> bool:
    return value is None or (
        isinstance(value, list)
        and all(isinstance(operation, str) for operation in value)
        and "verify" in value
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not value or any(character not in alphabet for character in value):
        raise ValueError("invalid base64url value")
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _validate_redirect_uri(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OidcConfigurationInvalidError("The OIDC callback URI is invalid.")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise OidcConfigurationInvalidError("The OIDC callback URI must use HTTPS.")


def _validate_provider_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OidcConfigurationInvalidError("The identity provider metadata is invalid.")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise OidcConfigurationInvalidError("Identity provider endpoints must use HTTPS.")


def _is_loopback_host(value: str) -> bool:
    normalized = value.casefold().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


__all__ = [
    "OidcAuthenticationService",
    "OidcIdentityRepository",
    "OidcLoginStart",
    "OidcProvider",
    "OidcProviderMetadata",
    "OidcSessionIssuer",
    "validate_oidc_id_token",
]

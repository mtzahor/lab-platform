from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from lab_platform.core.errors import OidcLoginFailedError
from lab_platform.core.oidc import validate_oidc_id_token

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
ISSUER = "https://identity.example.test"
AUDIENCE = "lab-platform"
NONCE = "test-nonce"
KEY_ID = "phase6-test-key"
N = int(
    "c8c734f1315577d5a4e4e234688f8677419c492577767143d58a2cc80fb6664f"
    "963d10a5a58ed1a438bc6a3083261eb490a0031d92471cc1c6ab33867211b602"
    "396c4ff94ab8acb844e046b45ce40e1a2e72ede928b17db0fabfa84319e89e59"
    "fb17b96d0ec9ee6e9a4139d505a8c77e7787234024ffd03f01a3c3f6858eb49a"
    "c7302d3ed4b878167c4de7ba069aca2bedd5fb87e5a6bcfc1327d529954abd4c9"
    "d64bea97a52f4023e2f9daa396b3ae773db87c163fcf755f2dfce216f30ed4683"
    "86303373f270e765ffddf07314a1d8ddb2b0ad12850b1fc14d4d27c98d307a340"
    "b92a2b3975febe2071d565a9c963d27b323e2f6f1b46838826252d7453d3f",
    16,
)
D = int(
    "32bf2af1f572b145b61645d8523f4890d6ebfe0ef2b9649a6d1c2e0268fa44b4"
    "c3f7ee3fb7ef37aca6cc74988b6574a855cfa3c9f3217732c1189f7ed15109ac"
    "5940379c7e56dc0cfd17a4b74cad35a5476d3415d4fbbb95a26313cfd5fa16200"
    "f17697e17995162f2291f4968d3468d00000f985461b60c5ec93a2c1288c5bbcf"
    "c1c0349ac9b9068843378b2f981c8ed81533ee14153d23925f2658fd3450de4d3"
    "8ca92af6a12db5945f70d92aea86b10629e536b8c7595e8ad7c031b8720cdc4e8"
    "578c393e6e5463d7db9f25db1be66e74fb3ae4bc65e5c5f2990f8bc82becfaa47"
    "4176d5d6ca8eb67409d0ffea341eea0f4f036c9f4c227b88c818a823659",
    16,
)
E = 65537
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def test_valid_signed_id_token_with_multiple_audiences() -> None:
    claims = _claims(aud=[AUDIENCE, "another-client"], azp=AUDIENCE)

    validated = validate_oidc_id_token(
        _signed_token(claims),
        jwks=_jwks(),
        issuer=ISSUER,
        audience=AUDIENCE,
        nonce=NONCE,
        now=NOW,
        clock_skew=timedelta(0),
    )

    assert validated["sub"] == "subject-123"
    assert validated["preferred_username"] == "alice"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iss": "https://attacker.example.test"}, "issuer"),
        ({"aud": "other-client"}, "audience"),
        ({"aud": [AUDIENCE, "other-client"], "azp": "other-client"}, "authorized party"),
        ({"nonce": "wrong-nonce"}, "nonce"),
        ({"exp": NOW.timestamp()}, "expired"),
        ({"nbf": NOW.timestamp() + 1}, "not yet valid"),
        ({"iat": NOW.timestamp() + 1}, "issue time"),
        ({"sub": ""}, "subject"),
        ({"exp": True}, "exp claim"),
        ({"aud": []}, "audience"),
        ({"nonce": 123}, "nonce"),
    ],
)
def test_rejects_invalid_required_claims(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(OidcLoginFailedError, match=message):
        validate_oidc_id_token(
            _signed_token(_claims(**overrides)),
            jwks=_jwks(),
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=NONCE,
            now=NOW,
            clock_skew=timedelta(0),
        )


def test_rejects_invalid_signature_and_unknown_key() -> None:
    token = _signed_token(_claims())
    encoded_header, encoded_claims, signature = token.split(".")
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = f"{encoded_header}.{encoded_claims}.{signature[:-1]}{replacement}"

    with pytest.raises(OidcLoginFailedError, match="signature"):
        validate_oidc_id_token(
            tampered,
            jwks=_jwks(),
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=NONCE,
            now=NOW,
        )

    with pytest.raises(OidcLoginFailedError, match="signing key"):
        validate_oidc_id_token(
            token,
            jwks={"keys": []},
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=NONCE,
            now=NOW,
        )

    signing_keys = _jwks()["keys"]
    assert isinstance(signing_keys, list)
    signing_key = signing_keys[0]
    assert isinstance(signing_key, dict)
    with pytest.raises(OidcLoginFailedError, match="unambiguous"):
        validate_oidc_id_token(
            token,
            jwks={"keys": [signing_key, dict(signing_key)]},
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=NONCE,
            now=NOW,
        )


def test_rejects_unsigned_or_unsupported_token() -> None:
    unsigned = _signed_token(_claims(), header={"alg": "none", "kid": KEY_ID})

    with pytest.raises(OidcLoginFailedError, match="algorithm"):
        validate_oidc_id_token(
            unsigned,
            jwks=_jwks(),
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=NONCE,
            now=NOW,
        )

    unsupported_headers: tuple[dict[str, object], ...] = (
        {"alg": "RS256", "kid": KEY_ID, "typ": "JOSE"},
        {"alg": "RS256", "kid": KEY_ID, "crit": ["unknown"]},
    )
    for header in unsupported_headers:
        with pytest.raises(OidcLoginFailedError, match="header"):
            validate_oidc_id_token(
                _signed_token(_claims(), header=header),
                jwks=_jwks(),
                issuer=ISSUER,
                audience=AUDIENCE,
                nonce=NONCE,
                now=NOW,
            )


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": NOW.timestamp() + 300,
        "iat": NOW.timestamp(),
        "nonce": NONCE,
        "sub": "subject-123",
        "preferred_username": "alice",
    }
    claims.update(overrides)
    return claims


def _signed_token(
    claims: dict[str, object],
    *,
    header: dict[str, object] | None = None,
) -> str:
    encoded_header = _encode_json(header or {"alg": "RS256", "kid": KEY_ID, "typ": "JWT"})
    encoded_claims = _encode_json(claims)
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    key_bytes = (N.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding = b"\xff" * (key_bytes - len(digest_info) - 3)
    encoded_message = b"\x00\x01" + padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded_message, "big"), D, N).to_bytes(key_bytes, "big")
    return f"{encoded_header}.{encoded_claims}.{_encode(signature)}"


def _jwks() -> dict[str, object]:
    return {
        "keys": [
            {
                "kid": KEY_ID,
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "key_ops": ["verify"],
                "n": _encode(N.to_bytes((N.bit_length() + 7) // 8, "big")),
                "e": _encode(E.to_bytes((E.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _encode_json(value: dict[str, object]) -> str:
    return _encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

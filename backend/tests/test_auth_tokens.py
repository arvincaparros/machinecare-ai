"""Unit tests for HS256 access tokens.

Exercises ``app.core.security`` create/decode directly, including the
verification failures the auth dependency must translate into 401
responses: expiry, tampering, wrong signature, wrong issuer/audience.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.security import (
    InvalidTokenError,
    access_token_expiry,
    create_access_token,
    decode_access_token,
    hash_token,
    new_opaque_token,
)
from app.core.settings import get_settings


def _claims(**overrides):
    now = datetime.now(timezone.utc)

    return {
        "sub": "7",
        "sid": "0f4c2a9e-1111-4c0a-9e2a-4f9d8b7a6c5d",
        "roles": ["viewer"],
        "ver": 0,
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": "machinecare-api",
        "aud": "machinecare-web",
        "jti": "a1b2c3d4e5f6",
        **overrides,
    }


def test_roundtrip_preserves_every_claim():
    expires = access_token_expiry()
    token = create_access_token(
        user_id=7,
        session_id="session-1",
        roles=["maintenance_engineer"],
        token_version=3,
        expires_at=expires,
    )

    claims = decode_access_token(token)

    assert claims["sub"] == "7"
    assert claims["sid"] == "session-1"
    assert claims["roles"] == ["maintenance_engineer"]
    assert claims["ver"] == 3
    assert claims["iss"] == "machinecare-api"
    assert claims["aud"] == "machinecare-web"
    assert claims["jti"]
    assert claims["exp"] == int(expires.timestamp())


def test_expired_token_is_rejected():
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    token = create_access_token(
        user_id=7,
        session_id="session-1",
        roles=["viewer"],
        token_version=0,
        expires_at=expired,
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_malformed_token_is_rejected():
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-jwt")


def test_wrong_signature_is_rejected():
    token = jwt.encode(
        _claims(),
        "a-different-secret-entirely",
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token)


def test_wrong_issuer_is_rejected():
    token = jwt.encode(
        _claims(iss="someone-else"),
        get_settings().jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_wrong_audience_is_rejected():
    token = jwt.encode(
        _claims(aud="someone-else"),
        get_settings().jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tokens_are_signed_with_hs256():
    token = create_access_token(
        user_id=7,
        session_id="session-1",
        roles=["viewer"],
        token_version=0,
        expires_at=access_token_expiry(),
    )

    header = jwt.get_unverified_header(token)

    assert header["alg"] == "HS256"


def test_access_token_expiry_uses_the_configured_ttl():
    settings = get_settings()
    before = datetime.now(timezone.utc)

    expires = access_token_expiry()

    expected_low = before + timedelta(minutes=settings.access_ttl_minutes)
    expected_high = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_ttl_minutes
    )

    assert expected_low <= expires <= expected_high


def test_opaque_tokens_are_high_entropy_and_hash_stably():
    first = new_opaque_token()
    second = new_opaque_token()

    assert first != second
    assert len(first) >= 48
    assert hash_token(first) == hash_token(first)
    assert hash_token(first) != hash_token(second)
    assert len(hash_token(first)) == 64

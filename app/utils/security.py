import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from app.constants import constants
from app.env import get_settings

password_hasher = PasswordHash.recommended()
DUMMY_HASH = password_hasher.hash("dummy-password-never-used")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def access_token(user_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": now,
            "exp": now + timedelta(minutes=constants.ACCESS_TOKEN_MINUTES),
            "type": "access",
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def decode_token(token: str) -> UUID:
    payload = jwt.decode(
        token,
        get_settings().jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        options={"require": ["sub", "exp", "iat"]},
    )
    if payload.get("type") != "access":
        raise ValueError("Wrong token type")
    return UUID(payload["sub"])

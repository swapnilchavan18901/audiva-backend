"""Single cached source of validated process configuration, including the .env file."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/voice_bots"
    )
    jwt_secret: SecretStr = Field(min_length=32)
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    sarvam_api_key: SecretStr = SecretStr("")
    attendee_api_key: SecretStr = SecretStr("")
    public_backend_url: str = Field(default="", max_length=200)
    meeting_operator_user_ids: list[UUID] = []
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    meeting_bot_email: str = ""
    meeting_token_key: SecretStr = SecretStr("")
    metered_domain: str = Field(
        default="", pattern=r"^(?:[a-z0-9][a-z0-9-]*\.metered\.live)?$"
    )
    metered_secret_key: SecretStr = SecretStr("")
    twilio_account_sid: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$")
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    clinic_operator_user_ids: list[UUID] = []

    @field_validator("database_url")
    @classmethod
    def valid_database_url(cls, value):
        try:
            url = make_url(value)
            if url.drivername not in {"postgresql+asyncpg", "sqlite+aiosqlite"}:
                raise ValueError
        except (ArgumentError, ValueError):
            raise ValueError(
                "DATABASE_URL must use postgresql+asyncpg or sqlite+aiosqlite"
            ) from None
        return value

    @field_validator("public_backend_url")
    @classmethod
    def valid_service_url(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        schemes = {"https"}
        if (
            url.scheme not in schemes
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Expected an absolute service URL without credentials, query or fragment"
            )
        try:
            _ = url.port  # Access validates the parsed port.
        except ValueError:
            raise ValueError("Invalid service URL port") from None
        return value

    @field_validator("jwt_secret")
    @classmethod
    def valid_jwt_secret(cls, value):
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError(
                "JWT_SECRET must contain at least 32 non-padding characters"
            )
        return value

    @field_validator("meeting_token_key")
    @classmethod
    def valid_encryption_key(cls, value):
        if value.get_secret_value():
            try:
                Fernet(value.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError(
                    "MEETING_TOKEN_KEY must be a valid Fernet key"
                ) from None
        return value

    @field_validator("cors_origins")
    @classmethod
    def valid_origins(cls, values):
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path
            ):
                raise ValueError(
                    "CORS_ORIGINS must contain HTTP(S) origins without paths"
                )
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()

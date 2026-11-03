"""Single cached source of validated process configuration, including the .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
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
    app_environment: Literal["development", "test", "production"] = "development"
    registration_enabled: bool = True
    auth_max_concurrency: int = Field(default=4, ge=1, le=16)
    demo_enabled: bool | None = None
    api_requests_per_minute: int = Field(default=2400, ge=1, le=100000)
    user_requests_per_minute: int = Field(default=1200, ge=1, le=100000)
    login_requests_per_minute: int = Field(default=10, ge=1, le=1000)
    signup_requests_per_hour: int = Field(default=5, ge=1, le=1000)
    demo_requests_per_hour: int = Field(default=3, ge=1, le=1000)
    write_requests_per_minute: int = Field(default=120, ge=1, le=10000)
    voice_starts_per_minute: int = Field(default=10, ge=1, le=1000)
    phone_calls_per_minute: int = Field(default=10, ge=1, le=1000)
    campaign_starts_per_minute: int = Field(default=3, ge=1, le=1000)
    voice_tokens_per_minute: int = Field(default=60, ge=1, le=10000)
    rate_limit_max_buckets: int = Field(default=20000, ge=100, le=100000)
    max_request_body_bytes: int = Field(default=3_000_000, ge=1024, le=10_000_000)
    request_body_timeout_seconds: int = Field(default=30, ge=1, le=120)

    @property
    def demo_allowed(self):
        return (
            self.demo_enabled
            if self.demo_enabled is not None
            else self.app_environment != "production"
        )

    @model_validator(mode="after")
    def production_configuration(self):
        if self.app_environment != "production":
            return self
        secret = self.jwt_secret.get_secret_value().lower()
        if any(
            marker in secret
            for marker in ("replace-with", "change-me", "test-secret", "demo-test")
        ):
            raise ValueError("Use a randomly generated JWT_SECRET in production")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Production requires PostgreSQL")
        if not self.public_backend_url:
            raise ValueError("Set PUBLIC_BACKEND_URL to the production HTTPS origin")
        if not self.cors_origins or any(
            not origin.startswith("https://") for origin in self.cors_origins
        ):
            raise ValueError(
                "Production CORS_ORIGINS must contain explicit HTTPS origins"
            )
        return self

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

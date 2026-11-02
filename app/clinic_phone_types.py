"""Clinic telephony contracts; configuration does not place calls."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.outreach_types import Contact
from app.types import ORMModel


class PhoneRouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number: str
    enabled: bool = True

    @field_validator("phone_number")
    @classmethod
    def phone(cls, value):
        return Contact(phone_number=value).phone_number


class PhoneRouteOut(ORMModel):
    id: UUID
    bot_id: UUID
    phone_number: str
    enabled: bool
    inbound_answer_url: str | None = None


class PhoneDial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number: str
    # Reuse this identifier when retrying an HTTP request: never dial twice.
    request_id: UUID

    @field_validator("phone_number")
    @classmethod
    def phone(cls, value):
        return Contact(phone_number=value).phone_number


class PhoneCallOut(ORMModel):
    id: UUID
    bot_id: UUID
    direction: str
    phone_number: str
    status: str
    provider_call_sid: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class PhoneReconcile(BaseModel):
    provider_call_sid: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    confirmed_not_placed: Literal[True] | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.provider_call_sid) == bool(self.confirmed_not_placed):
            raise ValueError(
                "Provide a call SID or explicitly confirm no call was placed"
            )
        return self

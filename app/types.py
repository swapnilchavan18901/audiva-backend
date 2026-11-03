"""Validated data contracts for dashboard, clinic, meeting, and operator workflows."""

import re
from datetime import date, datetime, time
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

DisplayName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class IceServerConfig(BaseModel):
    urls: str
    username: str | None = None
    credential: str | None = None


class VoiceIceConfig(BaseModel):
    ice_servers: list[IceServerConfig]
    expires_at: datetime | None


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class Register(Credentials):
    name: DisplayName


class UserOut(ORMModel):
    id: UUID
    email: str
    name: str


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class OrgCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: DisplayName


class OrgOut(ORMModel):
    id: UUID
    name: str


class MemberOut(ORMModel):
    user_id: UUID
    role: str


class InviteCreate(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class InviteOut(BaseModel):
    id: UUID
    token: str
    expires_at: datetime


class InviteAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: DisplayName
    company_context: str = Field(default="", max_length=20000)
    language: Literal[
        "hi-IN",
        "en-IN",
        "bn-IN",
        "gu-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "od-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
    ] = "hi-IN"


class BotCreate(BotConfig):
    template_id: UUID


class BotOut(BotCreate, ORMModel):
    id: UUID
    organization_id: UUID


class Offer(BaseModel):
    sdp: str = Field(min_length=1, max_length=100000)
    type: Literal["offer"] = "offer"


class Answer(BaseModel):
    sdp: str
    type: str
    pc_id: str


class TemplateOut(ORMModel):
    service_type: str
    id: UUID
    slug: str
    name: str
    description: str


class ApiKeyCreate(BaseModel):
    name: DisplayName
    expires_in_days: int = Field(default=90, ge=1, le=365)


class ApiKeyOut(ORMModel):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str


class VoiceGrantOut(BaseModel):
    token: str
    offer_expires_at: datetime
    expires_at: datetime
    bot_id: UUID


class FeatureOut(BaseModel):
    bots: list[BotOut]
    templates: list[TemplateOut]


class WorkspaceOut(BaseModel):
    organization: OrgOut
    role: Literal["owner", "admin", "member"]
    can_manage: bool
    can_invite_admins: bool
    bots: list[BotOut]
    templates: list[TemplateOut]
    features: dict[Literal["clinic", "conversation"], FeatureOut]
    members: list[MemberOut]


class ClinicInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Caller(ClinicInput):
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def phone(cls, value):
        digits = re.sub(r"[\s()+-]", "", value)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if not re.fullmatch(r"[0-9]{10}", digits):
            raise ValueError("Provide a ten-digit Indian phone number")
        return digits


class Booking(Caller):
    doctor_id: int = Field(gt=0)
    patient_name: str = Field(min_length=1, max_length=120)
    start_time: datetime
    reason: str | None = Field(default=None, max_length=2000)


class ChangeAppointment(Caller):
    appointment_id: UUID


class Reschedule(ChangeAppointment):
    new_start_time: datetime
    doctor_id: int | None = Field(default=None, gt=0)


class VisitUpdate(ClinicInput):
    visited: bool


class PatientOut(BaseModel):
    id: UUID
    doctor_id: int
    doctor_name: str
    patient_name: str
    patient_phone: str
    start_time: datetime
    reason: str | None
    status: str
    visited: bool
    can_update_visit: bool


class RegisterOut(BaseModel):
    day: date
    today: date
    patients: list[PatientOut]
    registered_count: int
    visited_count: int


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_id: UUID
    title: str
    meet_url: str = Field(validation_alias="display_meet_url")
    starts_at: datetime | None
    ends_at: datetime | None
    timezone: str
    agenda: str
    status: str
    auto_join: bool
    is_recurring: bool
    next_occurrence_id: str | None
    next_starts_at: datetime | None
    next_ends_at: datetime | None
    next_meet_url: str | None


class MeetingCaptureOut(ORMModel):
    id: UUID
    meeting_id: UUID
    provider_occurrence_id: str
    scheduled_starts_at: datetime
    scheduled_ends_at: datetime
    title: str
    meet_url: str
    status: str
    provider_status: str | None
    memory_status: str
    memory_error: str | None
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    recurring_event_id: str | None
    title: str | None
    meet_url: str | None = Field(validation_alias="display_meet_url")
    starts_at: datetime | None
    ends_at: datetime | None
    cancelled: bool
    meeting_id: UUID | None


class AssignMeeting(BaseModel):
    organization_id: UUID
    agenda: str = Field(default="", max_length=20000)
    auto_join: bool = True
    assign_series: bool = False


class MeetingSchedule(BaseModel):
    auto_join: bool


class ContextDoctor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    specialty: str = Field(min_length=1, max_length=80)
    available_slots: list[datetime] | None = Field(default=None, max_length=2000)

    @field_validator("available_slots")
    @classmethod
    def local_slots(cls, value):
        if value is not None:
            if any(t.tzinfo or t.second or t.microsecond for t in value):
                raise ValueError(
                    "Published slots must use minute-precision clinic-local datetimes"
                )
            return sorted(set(value))
        return value


class ClinicDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=100)
    fees: str | None = Field(default=None, max_length=2000)
    parking: str | None = Field(default=None, max_length=2000)
    booking: str | None = Field(default=None, max_length=2000)
    timezone: str | None = None
    working_days: list[int] | None = None
    open_time: time | None = None
    close_time: time | None = None
    lunch_start: time | None = None
    lunch_end: time | None = None
    slot_minutes: int | None = Field(default=None, ge=5, le=240)
    doctors: list[ContextDoctor] = Field(default_factory=list, max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Provide a valid IANA timezone") from None
        return value

    @field_validator("working_days")
    @classmethod
    def valid_days(cls, value):
        if value is not None and (
            not value or any(day < 0 or day > 6 for day in value)
        ):
            raise ValueError(
                "Working days must contain weekdays from 0 (Monday) to 6 (Sunday)"
            )
        return sorted(set(value)) if value is not None else None

    @field_validator("open_time", "close_time", "lunch_start", "lunch_end")
    @classmethod
    def minute_precision(cls, value):
        if value and (value.second or value.microsecond or value.tzinfo is not None):
            raise ValueError(
                "Use local HH:MM times without seconds or timezone offsets"
            )
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.open_time and self.close_time and self.open_time >= self.close_time:
            raise ValueError("Closing time must follow opening time")
        if bool(self.lunch_start) != bool(self.lunch_end):
            raise ValueError("Provide both lunch times or neither")
        if self.lunch_start and self.lunch_end:
            if self.lunch_start >= self.lunch_end:
                raise ValueError("Lunch end must follow lunch start")
            if self.open_time and self.lunch_start < self.open_time:
                raise ValueError("Lunch must fall within opening hours")
            if self.close_time and self.lunch_end > self.close_time:
                raise ValueError("Lunch must fall within opening hours")
        if len({d.name.casefold() for d in self.doctors}) != len(self.doctors):
            raise ValueError("Doctor names must be unique within a bot")
        return self

    def calendar(self):
        if (
            self.timezone
            and self.slot_minutes
            and self.doctors
            and any(d.available_slots is not None for d in self.doctors)
        ):
            return self
        required = (
            self.timezone,
            self.working_days,
            self.open_time,
            self.close_time,
            self.slot_minutes,
        )
        if any(value is None or value == "" for value in required):
            raise HTTPException(
                422,
                "Configure the clinic timezone, working days, opening/closing "
                "times and slot duration in the agent context before booking",
            )
        return self


class TemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=30000)
    is_active: bool = True
    service_type: Literal["conversation", "clinic"] | None = None


class ScopeDecision(BaseModel):
    in_scope: StrictBool


class SpokenReply(BaseModel):
    spoken_answer: str = Field(min_length=1)

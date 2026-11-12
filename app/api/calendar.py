"""Self-service calendar authorization, always bound to the authenticated Audiva user."""

import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DB, CurrentUser
from app.clients import create_google_client
from app.db.models import (
    CalendarOAuthState,
    Meeting,
    MeetingCalendar,
    MeetingGoogleConnection,
    utcnow,
)
from app.env import get_settings
from app.integrations import google_meet_calendar as google

router = APIRouter(prefix="/calendar", tags=["calendar"])


class ConnectRequest(BaseModel):
    redirect_uri: str = Field(max_length=500)


class CallbackRequest(BaseModel):
    state: str = Field(min_length=20, max_length=200)
    code: str = Field(min_length=1, max_length=4096)


async def user_connection(db, user_id):
    return await db.scalar(
        select(MeetingGoogleConnection).where(
            MeetingGoogleConnection.user_id == user_id
        )
    )


@router.post("/connect")
async def start_connection(data: ConnectRequest, db: DB, user: CurrentUser):
    settings = get_settings()
    parsed = urlsplit(data.redirect_uri)
    if (
        f"{parsed.scheme}://{parsed.netloc}" not in settings.cors_origins
        or parsed.path != "/calendar-inbox"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            400, "Calendar callback must be the dashboard's Calendar inbox."
        )
    if not settings.google_client_secret.get_secret_value():
        raise HTTPException(
            503, "Google Calendar is not configured. Set GOOGLE_CLIENT_SECRET."
        )
    try:
        url, state, verifier = google.oauth_request(
            settings, data.redirect_uri, personal=True
        )
    except google.CalendarError as exc:
        raise HTTPException(503, str(exc)) from None
    await google.lock(db)
    await db.execute(
        delete(CalendarOAuthState).where(
            (CalendarOAuthState.user_id == user.id)
            | (CalendarOAuthState.expires_at <= utcnow())
        )
    )
    db.add(
        CalendarOAuthState(
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            user_id=user.id,
            verifier_ciphertext=google.cipher(settings)
            .encrypt(verifier.encode())
            .decode(),
            redirect_uri=data.redirect_uri,
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    await db.commit()
    return {"authorization_url": url, "state": state}


@router.post("/callback")
async def finish_connection(data: CallbackRequest, db: DB, user: CurrentUser):
    # Atomic consume: state belongs to this dashboard user and can be used only once.
    pending = (
        await db.execute(
            delete(CalendarOAuthState)
            .where(
                CalendarOAuthState.state_hash
                == hashlib.sha256(data.state.encode()).hexdigest(),
                CalendarOAuthState.user_id == user.id,
                CalendarOAuthState.expires_at > utcnow(),
            )
            .returning(CalendarOAuthState)
        )
    ).scalar_one_or_none()
    await db.commit()
    if pending is None:
        raise HTTPException(
            400, "Calendar authorization expired or was already used. Connect again."
        )
    settings = get_settings()
    try:
        async with create_google_client() as client:
            connection = await google.connect(
                db,
                client,
                settings,
                data.code,
                google.decrypt(settings, pending.verifier_ciphertext),
                pending.redirect_uri,
                user_id=user.id,
            )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "This Google account is already connected to another Audiva user."
        ) from None
    except google.CalendarError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from None
    # The UI requests the first sync separately so a provider outage cannot undo consent.
    return {"status": connection.status, "email": connection.email}


@router.get("/status")
async def status(db: DB, user: CurrentUser):
    connection = await user_connection(db, user.id)
    calendars = (
        list(
            await db.scalars(
                select(MeetingCalendar).where(
                    MeetingCalendar.connection_id == connection.id
                )
            )
        )
        if connection
        else []
    )
    settings = get_settings()
    return {
        "identity_ready": bool(connection and connection.status != "disabled"),
        "configured": bool(
            settings.google_client_id
            and settings.google_client_secret.get_secret_value()
            and settings.meeting_token_key.get_secret_value()
        ),
        "status": connection.status if connection else "not_connected",
        "email": connection.email
        if connection and connection.status != "disabled"
        else None,
        "calendars": [
            {"id": c.id, "last_synced_at": c.last_synced_at, "enabled": c.enabled}
            for c in calendars
        ],
    }


@router.post("/sync")
async def sync_calendar(db: DB, user: CurrentUser):
    connection = await user_connection(db, user.id)
    if not connection or connection.status != "connected":
        raise HTTPException(409, "Connect your Google Calendar first.")
    try:
        async with create_google_client() as client:
            return await google.sync(
                db, client, get_settings(), connection_id=connection.id
            )
    except google.CalendarError as exc:
        await db.rollback()
        raise HTTPException(502, str(exc)) from None


@router.delete("/connection", status_code=204)
async def disconnect(db: DB, user: CurrentUser):
    await google.lock(db)
    connection = await user_connection(db, user.id)
    await db.execute(
        delete(CalendarOAuthState).where(CalendarOAuthState.user_id == user.id)
    )
    if connection:
        calendars = select(MeetingCalendar.id).where(
            MeetingCalendar.connection_id == connection.id
        )
        await db.execute(
            delete(Meeting).where(
                Meeting.calendar_id.in_(calendars), Meeting.organization_id.is_(None)
            )
        )
        # Preserve assigned meetings and captures as workspace history, but stop future joins.
        await db.execute(
            update(Meeting)
            .where(Meeting.calendar_id.in_(calendars))
            .values(auto_join=False, calendar_id=None)
        )
        await db.execute(
            delete(MeetingCalendar).where(
                MeetingCalendar.connection_id == connection.id
            )
        )
        # Keep the row's owner, but release the Google identity for a later connection.
        connection.google_subject = f"disconnected:{connection.id}"
        connection.status = "disabled"
        connection.email = ""
        connection.refresh_token_ciphertext = ""
        connection.access_token_ciphertext = None
        connection.access_token_expires_at = None
        connection.granted_scopes = ""
        connection.updated_at = utcnow()
    await db.commit()

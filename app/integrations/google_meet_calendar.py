"""Operator-only, read-only Calendar integration. No Meet browser authentication."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text

from app.business.meeting_calendar import auto_assign_event
from app.constants import constants
from app.constants.constants import (
    CALENDAR_SCOPE,
    EVENTS_URL,
    LOCK_ID,
    SCOPES,
    TOKEN_URL,
)
from app.db.models import (
    Meeting,
    MeetingCalendar,
    MeetingCapture,
    MeetingGoogleConnection,
    utcnow,
)


class CalendarError(Exception):
    """Safe operator-facing message: never include provider bodies or credentials."""


class ReauthorizationRequired(CalendarError):
    pass


def cipher(settings):
    try:
        return Fernet(settings.meeting_token_key.get_secret_value().encode())
    except (ValueError, TypeError):
        raise CalendarError("Set MEETING_TOKEN_KEY to a valid Fernet key.") from None


def validate_configuration(settings):
    if not settings.google_client_id or not settings.meeting_bot_email:
        raise CalendarError("Configure GOOGLE_CLIENT_ID and MEETING_BOT_EMAIL first.")
    cipher(settings)


def decrypt(settings, value):
    try:
        return cipher(settings).decrypt(value.encode()).decode()
    except InvalidToken:
        raise CalendarError(
            "Cannot decrypt credentials; check MEETING_TOKEN_KEY."
        ) from None


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def parse_instant(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CalendarError("Google returned a timestamp without a timezone.")
    return parsed.astimezone(UTC)


def oauth_request(settings, redirect_uri):
    validate_configuration(settings)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=")
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
            "login_hint": settings.meeting_bot_email,
        }
    )
    return url, state, verifier


async def lock(db):
    # Cross-process serialization in production, including first connection creation.
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})


async def request(client, method, url, **kwargs):
    try:
        return await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise CalendarError("Google request failed; retry later.") from None


def body(response):
    if response.status_code >= 400:
        raise CalendarError(
            f"Google request failed (HTTP {response.status_code}); retry or reconnect."
        )
    try:
        result = response.json()
    except ValueError:
        raise CalendarError("Google returned an invalid response.") from None
    if not isinstance(result, dict):
        raise CalendarError("Google returned an invalid response.")
    return result


def token_fields(data):
    try:
        token = data["access_token"]
        seconds = int(data["expires_in"])
        if not isinstance(token, str) or not token or seconds <= 0:
            raise ValueError
        return token, utcnow() + timedelta(seconds=seconds)
    except (KeyError, TypeError, ValueError):
        raise CalendarError("Google did not return a usable access token.") from None


async def connect(db, client, settings, code, verifier, redirect_uri):
    validate_configuration(settings)
    await lock(db)
    existing = await db.scalar(select(MeetingGoogleConnection))
    response = await request(
        client,
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    tokens = body(response)
    access_token, expires = token_fields(tokens)
    scopes = set(tokens.get("scope", "").split())
    if CALENDAR_SCOPE not in scopes:
        raise CalendarError("Read-only calendar permission was not granted; reconnect.")
    identity = body(
        await request(
            client,
            "GET",
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    )
    if (
        identity.get("email_verified") is not True
        or identity.get("email", "").casefold() != settings.meeting_bot_email.casefold()
        or not identity.get("sub")
    ):
        raise CalendarError(
            "Sign in with the configured, verified MEETING_BOT_EMAIL account."
        )
    if existing and existing.google_subject != identity["sub"]:
        raise CalendarError(
            "A different bot account is already connected; account replacement is disabled."
        )
    refresh = tokens.get("refresh_token")
    if not refresh and not existing:
        raise CalendarError(
            "No refresh token returned. Revoke this app grant and reconnect with consent."
        )
    if existing is None:
        existing = MeetingGoogleConnection(
            google_subject=identity["sub"], email=identity["email"]
        )
        db.add(existing)
    f = cipher(settings)
    if refresh:
        existing.refresh_token_ciphertext = f.encrypt(refresh.encode()).decode()
    existing.access_token_ciphertext = f.encrypt(access_token.encode()).decode()
    existing.access_token_expires_at = expires
    existing.granted_scopes = " ".join(sorted(scopes))
    existing.status = "connected"
    existing.updated_at = utcnow()
    await db.flush()
    calendar = await db.scalar(
        select(MeetingCalendar).where(
            MeetingCalendar.connection_id == existing.id,
            MeetingCalendar.google_calendar_id == "primary",
        )
    )
    if calendar is None:
        db.add(MeetingCalendar(connection_id=existing.id, google_calendar_id="primary"))
    await db.commit()


async def access_token(db, client, settings, *, force=False):
    validate_configuration(settings)
    await lock(db)
    connection = await db.scalar(
        select(MeetingGoogleConnection).execution_options(populate_existing=True)
    )
    if connection is None or connection.status != "connected":
        raise ReauthorizationRequired("Run the operator connect command first.")
    if connection.email.casefold() != settings.meeting_bot_email.casefold():
        raise CalendarError(
            "Configured email does not match the connected bot account."
        )
    if (
        not force
        and connection.access_token_ciphertext
        and connection.access_token_expires_at
        and aware(connection.access_token_expires_at) > utcnow() + timedelta(minutes=2)
    ):
        token = decrypt(settings, connection.access_token_ciphertext)
        await db.commit()
        return token
    response = await request(
        client,
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "grant_type": "refresh_token",
            "refresh_token": decrypt(settings, connection.refresh_token_ciphertext),
        },
    )
    if response.status_code == 400:
        try:
            invalid = response.json().get("error") == "invalid_grant"
        except (ValueError, AttributeError):
            invalid = False
        if invalid:
            connection.status = "reauth_required"
            connection.updated_at = utcnow()
            await db.commit()
            raise ReauthorizationRequired(
                "Google consent expired or was revoked; reconnect the bot."
            )
    tokens = body(response)
    token, expires = token_fields(tokens)
    f = cipher(settings)
    connection.access_token_ciphertext = f.encrypt(token.encode()).decode()
    connection.access_token_expires_at = expires
    if tokens.get("refresh_token"):
        connection.refresh_token_ciphertext = f.encrypt(
            tokens["refresh_token"].encode()
        ).decode()
    connection.updated_at = utcnow()
    # Persist rotated credentials even if a later calendar request fails.
    await db.commit()
    return token


def meet_link(event):
    candidates = [event.get("hangoutLink")]
    candidates += [
        e.get("uri")
        for e in event.get("conferenceData", {}).get("entryPoints", [])
        if e.get("entryPointType") == "video"
    ]
    for value in candidates:
        if not value:
            continue
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "meet.google.com"
            and parsed.path.strip("/")
        ):
            return value
    return None


class Unauthorized(CalendarError):
    pass


async def calendar_get(client, url, token, **kwargs):
    response = await request(
        client, "GET", url, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    if response.status_code == 401:
        raise Unauthorized("Google rejected the access token.")
    return response


async def pages(client, url, token, params):
    """Read every page before making scheduling decisions; never truncate exceptions."""
    params = dict(params)
    seen = set()
    while True:
        data = body(await calendar_get(client, url, token, params=params))
        yield data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return
        if page in seen:
            raise CalendarError("Google repeated a pagination token; sync rolled back.")
        seen.add(page)
        params["pageToken"] = page


def clear_next(meeting):
    meeting.next_occurrence_id = None
    meeting.next_starts_at = meeting.next_ends_at = None
    meeting.next_meet_url = meeting.next_title = None


def occurrence(data, now):
    if data.get("status") == "cancelled":
        return None
    start = parse_instant(data.get("start", {}).get("dateTime"))
    end = parse_instant(data.get("end", {}).get("dateTime"))
    url = meet_link(data)
    if not start or not end or not url or end <= now or end <= start:
        return None
    return {
        "id": data["id"],
        "start": start,
        "end": end,
        "url": url,
        "title": (data.get("summary") or "Meeting")[:500],
    }


async def apply_definition(db, client, calendar, token, data, now):
    """Store one master definition and only its next unattempted occurrence."""
    if data.get("recurringEventId"):
        return  # Modified/cancelled instances are resolved by the master's instances API.
    meeting = await db.scalar(
        select(Meeting)
        .where(
            Meeting.calendar_id == calendar.id, Meeting.provider_event_id == data["id"]
        )
        .with_for_update()
    )
    if data.get("status") == "cancelled":
        if meeting:
            meeting.status = "cancelled"
            clear_next(meeting)
            meeting.updated_at = now
        return
    if meeting is None:
        meeting = Meeting(
            calendar_id=calendar.id,
            provider_event_id=data["id"],
            title="Meeting",
            meet_url="",
            agenda="",
            auto_join=False,
        )
        db.add(meeting)
    organizer = (data.get("organizer", {}).get("email") or "").strip().lower() or None
    if (
        meeting.organization_id
        and meeting.organizer_email
        and organizer != meeting.organizer_email
    ):
        # A transferred series must not silently bring a new organizer's calls into
        # the old workspace. Keep its existing assignment/history and stop future joins.
        meeting.auto_join = False
        meeting.status = "cancelled"
        clear_next(meeting)
        meeting.updated_at = now
        return meeting
    meeting.title = (data.get("summary") or "Meeting")[:500]
    meeting.meet_url = meet_link(data) or ""
    meeting.organizer_email = organizer
    meeting.timezone = data.get("start", {}).get("timeZone") or "UTC"
    meeting.recurrence = data.get("recurrence") or []
    meeting.is_recurring = bool(meeting.recurrence)
    meeting.updated_at = now
    await db.flush()
    attempted = set(
        await db.scalars(
            select(MeetingCapture.provider_occurrence_id).where(
                MeetingCapture.meeting_id == meeting.id
            )
        )
    )
    candidates = []
    if meeting.is_recurring:
        url = EVENTS_URL.format(quote(calendar.google_calendar_id, safe=""))
        url += "/" + quote(meeting.provider_event_id, safe="") + "/instances"
        # Google expands the rules/exceptions, but these occurrences stay in memory.
        # A definition with nothing in this window is checked again on later syncs.
        async for items in pages(
            client,
            url,
            token,
            {
                "timeMin": now.isoformat(),
                "timeMax": (
                    now + timedelta(days=constants.MEETING_SYNC_DAYS)
                ).isoformat(),
                "showDeleted": "false",
                "maxResults": 2500,
                "timeZone": "UTC",
            },
        ):
            for item in items:
                candidate = occurrence(item, now)
                if candidate and candidate["id"] not in attempted:
                    candidates.append(candidate)
    else:
        candidate = occurrence(data, now)
        if candidate and candidate["id"] not in attempted:
            candidates.append(candidate)
    clear_next(meeting)
    if candidates:
        selected = min(candidates, key=lambda item: (item["start"], item["id"]))
        meeting.next_occurrence_id = selected["id"]
        meeting.next_starts_at, meeting.next_ends_at = (
            selected["start"],
            selected["end"],
        )
        meeting.next_meet_url, meeting.next_title = selected["url"], selected["title"]
    meeting.status = "scheduled" if meeting.is_recurring or candidates else "completed"
    await auto_assign_event(db, meeting)
    return meeting


async def sync_calendar(db, client, calendar, token, settings, now):
    url = EVENTS_URL.format(quote(calendar.google_calendar_id, safe=""))
    masters = {}
    exception_masters = set()
    async for items in pages(
        client,
        url,
        token,
        {
            "singleEvents": "false",
            "showDeleted": "true",
            "maxResults": 2500,
            "timeMin": (now - timedelta(days=1)).isoformat(),
            "timeMax": (now + timedelta(days=constants.MEETING_SYNC_DAYS)).isoformat(),
            "timeZone": "UTC",
        },
    ):
        for item in items:
            if item.get("recurringEventId"):
                exception_masters.add(item["recurringEventId"])
            else:
                masters[item["id"]] = item
    # A series can move outside the window or end. Refresh known definitions directly
    # instead of mistaking absence for deletion. Also resolve exception-only results.
    known = list(
        await db.scalars(
            select(Meeting.provider_event_id).where(Meeting.calendar_id == calendar.id)
        )
    )
    for provider_id in set(known) | exception_masters:
        if provider_id in masters:
            continue
        response = await calendar_get(
            client, url + "/" + quote(provider_id, safe=""), token
        )
        masters[provider_id] = (
            {"id": provider_id, "status": "cancelled"}
            if response.status_code in (404, 410)
            else body(response)
        )
    for data in masters.values():
        await apply_definition(db, client, calendar, token, data, now)
    calendar.last_synced_at = now
    return len(masters)


async def sync(db, client, settings):
    token = await access_token(db, client, settings)
    for attempt in range(2):
        try:
            await lock(db)
            calendars = (
                await db.scalars(
                    select(MeetingCalendar).where(
                        MeetingCalendar.enabled.is_(True),
                    )
                )
            ).all()
            count = 0
            for calendar in calendars:
                count += await sync_calendar(
                    db, client, calendar, token, settings, utcnow()
                )
            await db.commit()
            return {"calendars": len(calendars), "events": count}
        except Unauthorized:
            await db.rollback()
            if attempt:
                raise CalendarError(
                    "Google rejected refreshed credentials; reconnect the bot."
                ) from None
            token = await access_token(db, client, settings, force=True)
        except Exception:
            await db.rollback()
            raise

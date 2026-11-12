"""Private calendar authorization, isolation, disconnect and duplicate joins."""

import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from test_meeting_definitions import join, sync

from app.api import calendar as api
from app.api.dependencies import current_user
from app.db.models import CalendarOAuthState, Meeting, MeetingGoogleConnection, utcnow
from app.integrations import google_meet_calendar as google

pytest_plugins = ["test_meeting_definitions"]


async def setup(env, monkeypatch):
    env.settings.google_client_id = "web-client"
    env.settings.google_client_secret = SecretStr("secret")
    env.settings.meeting_token_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(api, "get_settings", lambda: env.settings)
    env.app.include_router(api.router)


async def begin(env):
    response = await env.dashboard.post(
        "/calendar/connect",
        json={"redirect_uri": "http://localhost:3000/calendar-inbox"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def mock_google(monkeypatch, email="personal@gmail.com", subject="personal-subject"):
    def handle(request):
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "scope": google.CALENDAR_SCOPE,
                },
            )
        if request.url.path.endswith("userinfo"):
            return httpx.Response(
                200, json={"email": email, "sub": subject, "email_verified": True}
            )
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(
        api,
        "create_google_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


@pytest.mark.asyncio
async def test_oauth_different_email_and_one_time_user_bound_state(env, monkeypatch):
    await setup(env, monkeypatch)
    mock_google(monkeypatch)
    pending = await begin(env)
    query = parse_qs(urlsplit(pending["authorization_url"]).query)
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert "login_hint" not in query
    assert "select_account" in query["prompt"][0]
    data = {"code": "google-code", "state": pending["state"]}
    env.app.dependency_overrides[current_user] = lambda: env.other
    assert (
        await env.dashboard.post("/calendar/callback", json=data)
    ).status_code == 400
    env.app.dependency_overrides[current_user] = lambda: env.user
    response = await env.dashboard.post("/calendar/callback", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["email"] == "personal@gmail.com"
    assert (
        await env.dashboard.post("/calendar/callback", json=data)
    ).status_code == 400
    async with env.sessions() as db:
        connection = await api.user_connection(db, env.user.id)
        assert connection.singleton_key is None
        assert connection.refresh_token_ciphertext != "refresh"
        assert (
            google.decrypt(env.settings, connection.refresh_token_ciphertext)
            == "refresh"
        )
        assert env.user.calendar_email == "owner@example.com"
    assert (await env.dashboard.get("/calendar/status")).json()[
        "email"
    ] == "personal@gmail.com"
    env.app.dependency_overrides[current_user] = lambda: env.other
    assert (await env.dashboard.get("/calendar/status")).json()["email"] is None


@pytest.mark.asyncio
async def test_redirect_validation_expiry_and_duplicate_google_account(
    env, monkeypatch
):
    await setup(env, monkeypatch)
    for uri in [
        "https://evil.example/calendar-inbox",
        "http://localhost:3000/calendar-inbox?next=evil",
        "http://localhost:3000/login",
    ]:
        assert (
            await env.dashboard.post("/calendar/connect", json={"redirect_uri": uri})
        ).status_code == 400
    pending = await begin(env)
    async with env.sessions() as db:
        state = await db.get(
            CalendarOAuthState, hashlib.sha256(pending["state"].encode()).hexdigest()
        )
        state.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (
        await env.dashboard.post(
            "/calendar/callback", json={"code": "code", "state": pending["state"]}
        )
    ).status_code == 400
    mock_google(monkeypatch)
    pending = await begin(env)
    assert (
        await env.dashboard.post(
            "/calendar/callback", json={"code": "code", "state": pending["state"]}
        )
    ).status_code == 200
    env.app.dependency_overrides[current_user] = lambda: env.other
    pending = await begin(env)
    assert (
        await env.dashboard.post(
            "/calendar/callback", json={"code": "code", "state": pending["state"]}
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_invited_meeting_is_private_assigned_and_disconnect_preserves_history(
    env, monkeypatch
):
    await setup(env, monkeypatch)
    async with env.sessions() as db:
        connection = await db.get(MeetingGoogleConnection, env.calendar.connection_id)
        connection.user_id = env.user.id
        connection.singleton_key = None
        await db.commit()
    env.google.master["organizer"] = {"email": "external@example.com"}
    meeting = await sync(env)
    assert meeting.organization_id is None
    assert len((await env.dashboard.get("/calendar/events")).json()) == 1
    env.app.dependency_overrides[current_user] = lambda: env.other
    assert (await env.dashboard.get("/calendar/events")).json() == []
    assert (
        await env.dashboard.post(
            f"/calendar/events/{meeting.id}/assign",
            json={"organization_id": str(env.other_org.id)},
        )
    ).status_code == 404
    env.app.dependency_overrides[current_user] = lambda: env.user
    response = await env.dashboard.post(
        f"/calendar/events/{meeting.id}/assign",
        json={"organization_id": str(env.org.id)},
    )
    assert response.status_code == 200, response.text
    assert (await env.dashboard.get("/calendar/events")).json()[0]["meeting_id"] == str(
        meeting.id
    )
    assert (await env.dashboard.delete("/calendar/connection")).status_code == 204
    assert (await env.dashboard.get("/calendar/events")).json() == []
    assert (await env.dashboard.get("/calendar/status")).json()["email"] is None
    async with env.sessions() as db:
        retained = await db.get(Meeting, meeting.id)
        assert retained.organization_id == env.org.id
        assert not retained.auto_join
        assert retained.calendar_id is None
        connection = await api.user_connection(db, env.user.id)
        assert connection.refresh_token_ciphertext == ""
        assert connection.access_token_ciphertext is None
    mock_google(monkeypatch, email="replacement@gmail.com", subject="replacement")
    pending = await begin(env)
    assert (
        await env.dashboard.post(
            "/calendar/callback", json={"code": "new", "state": pending["state"]}
        )
    ).status_code == 200
    assert (await env.dashboard.get("/calendar/status")).json()[
        "email"
    ] == "replacement@gmail.com"


@pytest.mark.asyncio
async def test_sync_only_uses_requested_connection_and_marks_revocation(
    env, monkeypatch
):
    await setup(env, monkeypatch)
    mock_google(monkeypatch)
    pending = await begin(env)
    await env.dashboard.post(
        "/calendar/callback", json={"code": "code", "state": pending["state"]}
    )
    visited = []

    async def record(db, client, calendar, token, settings, now):
        visited.append(calendar.connection_id)
        return 0

    monkeypatch.setattr(google, "sync_calendar", record)
    async with env.sessions() as db:
        own_id = (await api.user_connection(db, env.user.id)).id
    response = await env.dashboard.post("/calendar/sync")
    assert response.status_code == 200, response.text
    assert visited == [own_id]
    async with env.sessions() as db:
        connection = await api.user_connection(db, env.user.id)
        connection.access_token_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    monkeypatch.setattr(
        api,
        "create_google_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": "invalid_grant"})
            )
        ),
    )
    assert (await env.dashboard.post("/calendar/sync")).status_code == 502
    assert (await env.dashboard.get("/calendar/status")).json()[
        "status"
    ] == "reauth_required"


@pytest.mark.asyncio
async def test_same_meeting_in_two_calendars_cannot_launch_two_bots(env):
    original = await sync(env)
    await join(env, original.id)
    async with env.sessions() as db:
        copy = Meeting(
            organization_id=env.other_org.id,
            provider_event_id="other-copy",
            title="Same call",
            next_occurrence_id="other-occurrence",
            next_starts_at=original.next_starts_at,
            next_ends_at=original.next_ends_at,
            next_meet_url=original.next_meet_url,
            auto_join=True,
        )
        db.add(copy)
        await db.commit()
        copy_id = copy.id
    with pytest.raises(HTTPException) as error:
        await join(env, copy_id)
    assert error.value.status_code == 409
    assert len(env.provider.created) == 1


@pytest.mark.asyncio
async def test_calendar_owner_member_can_control_bot_without_operator_permission(
    env, monkeypatch
):
    from sqlalchemy import select

    from app.api import meetings
    from app.db.models import Membership

    monkeypatch.setattr(meetings, "is_operator", lambda user: False)
    async with env.sessions() as db:
        connection = await db.get(MeetingGoogleConnection, env.calendar.connection_id)
        connection.user_id = env.user.id
        connection.singleton_key = None
        member = await db.scalar(
            select(Membership).where(
                Membership.user_id == env.user.id,
                Membership.organization_id == env.org.id,
            )
        )
        member.role = "member"
        await db.commit()
    meeting = await sync(env)
    await env.dashboard.post(
        f"/calendar/events/{meeting.id}/assign",
        json={"organization_id": str(env.org.id), "auto_join": False},
    )
    path = f"/organizations/{env.org.id}/meetings/{meeting.id}"
    status = await env.dashboard.get(path + "/connection")
    assert status.json()["can_control"] is True
    response = await env.dashboard.post(path + "/join")
    assert response.status_code == 202, response.text
    assert response.json()["can_control"] is True
    assert len(env.provider.created) == 1
    response = await env.dashboard.post(path + "/leave")
    assert response.status_code == 200
    assert (
        await env.dashboard.put(path + "/schedule", json={"auto_join": True})
    ).status_code == 200


@pytest.mark.asyncio
async def test_other_members_cannot_control_but_workspace_admin_can(env, monkeypatch):
    from app.api import meetings
    from app.db.models import Membership

    monkeypatch.setattr(meetings, "is_operator", lambda user: False)
    meeting = await sync(env)
    path = f"/organizations/{env.org.id}/meetings/{meeting.id}"
    env.app.dependency_overrides[current_user] = lambda: env.other
    assert (await env.dashboard.post(path + "/join")).status_code == 404
    async with env.sessions() as db:
        member = Membership(
            user_id=env.other.id, organization_id=env.org.id, role="member"
        )
        db.add(member)
        await db.commit()
    assert (await env.dashboard.get(path + "/connection")).json()[
        "can_control"
    ] is False
    for action in ["join", "leave"]:
        assert (await env.dashboard.post(path + "/" + action)).status_code == 403
    assert (
        await env.dashboard.put(path + "/schedule", json={"auto_join": False})
    ).status_code == 403
    async with env.sessions() as db:
        member = await db.get(Membership, member.id)
        member.role = "admin"
        await db.commit()
    assert (await env.dashboard.get(path + "/connection")).json()["can_control"] is True
    assert (await env.dashboard.post(path + "/join")).status_code == 202


@pytest.mark.asyncio
async def test_manual_join_can_start_tomorrows_meeting_but_auto_join_waits(env):
    env.google.items = [env.google.instance("tomorrow", 1)]
    meeting = await sync(env)
    path = f"/organizations/{env.org.id}/meetings/{meeting.id}"
    response = await env.dashboard.get(path + "/connection")
    assert response.json()["can_join"] is True
    assert response.json()["can_control"] is True
    # The scheduler must still respect the scheduled start time.
    with pytest.raises(HTTPException) as error:
        await join(env, meeting.id)
    assert error.value.status_code == 409
    assert not env.provider.created
    response = await env.dashboard.post(path + "/join")
    assert response.status_code == 202, response.text
    assert len(env.provider.created) == 1
    assert env.provider.created[0]["meeting_url"] == meeting.next_meet_url

"""Calendar definition scheduling and session history, with simulated external providers."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import meetings as api
from app.api.dependencies import current_user
from app.business import meeting_memory
from app.business.meeting_voice import MeetingVoiceService
from app.db.models import (
    Base,
    Meeting,
    MeetingCalendar,
    MeetingCapture,
    MeetingGoogleConnection,
    MeetingMemory,
    Membership,
    Organization,
    User,
    utcnow,
)
from app.db.session import get_db
from app.env import Settings
from app.integrations import google_meet_calendar as google


class Google:
    def __init__(self, now):
        self.now = now
        self.master = {
            "id": "standup",
            "summary": "Daily standup",
            "status": "confirmed",
            "organizer": {"email": "owner@example.com"},
            "recurrence": ["RRULE:FREQ=DAILY"],
            "start": {
                "dateTime": (now - timedelta(days=20)).isoformat(),
                "timeZone": "Asia/Kolkata",
            },
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }
        self.items = [self.instance(f"day{i}", i) for i in range(30)]
        self.calls = []
        self.master_visible = True
        self.deleted = False
        self.page_size = 7
        self.fail_second_page = False

    def instance(self, name, offset):
        start = self.now - timedelta(minutes=1) + timedelta(days=offset)
        return {
            "id": name,
            "recurringEventId": "standup",
            "summary": "Daily standup",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }

    async def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/instances"):
            page = int(request.url.params.get("pageToken", "0"))
            if page and self.fail_second_page:
                return httpx.Response(503, json={"error": "temporary"})
            items = self.items[page : page + self.page_size]
            result = {"items": items}
            if page + self.page_size < len(self.items):
                result["nextPageToken"] = str(page + self.page_size)
            return httpx.Response(200, json=result)
        if path.endswith("/events/standup"):
            return httpx.Response(404 if self.deleted else 200, json=self.master)
        assert path.endswith("/events")
        assert request.url.params["singleEvents"] == "false"
        return httpx.Response(200, json={"items": [self.master] if self.master_visible else []})


class Attendee:
    def __init__(self):
        self.created = []
        self.states = {}

    async def request(self, method, path, **kwargs):
        if method == "POST" and path == "bots":
            self.created.append(kwargs["json"])
            sid = "bot-" + str(len(self.created))
            self.states[sid] = "joined_recording"
            await asyncio.sleep(0.01)
            return {"id": sid, "state": self.states[sid]}
        if path.endswith("participant_events"):
            return []
        if path.endswith("/leave"):
            self.states[path.split("/")[1]] = "ended"
            return {}
        return {"state": self.states[path.split("/")[1]]}

    async def close(self):
        pass


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    url = os.environ.get("MEETING_TEST_POSTGRES_URL")
    admin = None
    schema = "meeting_test_" + uuid4().hex
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as db:
            await db.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meetings.db'}")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        user = User(
            name="Owner",
            email="owner@example.com",
            calendar_email="owner@example.com",
            password_hash="unused",
        )
        other = User(
            name="Other",
            email="other@example.com",
            calendar_email="other@example.com",
            password_hash="unused",
        )
        org = Organization(name="Acme")
        other_org = Organization(name="Other")
        db.add_all([user, other, org, other_org])
        await db.flush()
        db.add_all(
            [
                Membership(organization_id=org.id, user_id=user.id, role="owner"),
                Membership(organization_id=other_org.id, user_id=other.id, role="owner"),
            ]
        )
        connection = MeetingGoogleConnection(
            singleton_key=1,
            google_subject="subject",
            email="bot@example.com",
            refresh_token_ciphertext="encrypted",
            granted_scopes="calendar",
            status="connected",
        )
        db.add(connection)
        await db.flush()
        calendar = MeetingCalendar(connection_id=connection.id, google_calendar_id="primary")
        db.add(calendar)
        await db.commit()
    settings = Settings(
        _env_file=None,
        jwt_secret="test-only-meeting-secret-123456789",
        sarvam_api_key="test",
        attendee_api_key="test",
        public_backend_url="https://backend.example.com",
    )
    service = MeetingVoiceService(settings)
    await service.attendee.close()
    provider = service.attendee = Attendee()
    monkeypatch.setattr(meeting_memory, "SessionFactory", sessions)
    app = FastAPI()
    app.include_router(api.router)
    app.state.meeting_voice = service
    app.dependency_overrides[current_user] = lambda: user

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    remote = Google(utcnow())
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as dashboard,
    ):
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            user=user,
            org=org,
            other=other,
            other_org=other_org,
            calendar=calendar,
            settings=settings,
            service=service,
            provider=provider,
            google=remote,
            client=client,
            app=app,
            dashboard=dashboard,
        )
    await service.close()
    await engine.dispose()
    if admin:
        async with admin.begin() as db:
            await db.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def sync(env, now=None):
    async with env.sessions() as db:
        await google.lock(db)
        calendar = await db.get(MeetingCalendar, env.calendar.id)
        await google.sync_calendar(
            db, env.client, calendar, "test", env.settings, now or env.google.now
        )
        await db.commit()
        return await db.scalar(sa.select(Meeting).where(Meeting.calendar_id == env.calendar.id))


async def join(env, meeting_id):
    async with env.sessions() as db:
        meeting = await db.get(Meeting, meeting_id)
        return await env.service.start(db, meeting, scheduled=True)


@pytest.mark.asyncio
async def test_thirty_occurrences_create_one_definition_and_no_captures(env):
    first = await sync(env)
    second = await sync(env)
    assert first.id == second.id
    assert first.next_occurrence_id == "day0"
    assert first.organization_id == env.org.id
    assert first.is_recurring
    async with env.sessions() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Meeting)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MeetingCapture)) == 0
    assert "meeting_calendar_events" not in Base.metadata.tables
    assert "meeting_series" not in Base.metadata.tables


@pytest.mark.asyncio
async def test_session_snapshot_and_next_advance_independent_of_summary(env):
    meeting = await sync(env)
    result = await join(env, meeting.id)
    assert len(env.provider.created) == 1
    duplicate = await join(env, meeting.id)
    assert result["capture_id"] == duplicate["capture_id"]
    assert len(env.provider.created) == 1
    async with env.sessions() as db:
        capture = await db.get(MeetingCapture, result["capture_id"])
        capture.transcript = [
            {
                "speaker": "Asha",
                "text": "Ship on Friday",
                "role": "user",
                "timestamp": utcnow().isoformat(),
            }
        ]
        capture.memory_status = "failed"
        await db.commit()
    updated = await sync(env)
    assert updated.id == meeting.id
    assert updated.next_occurrence_id == "day1"
    async with env.sessions() as db:
        capture = await db.get(MeetingCapture, result["capture_id"])
        assert capture.provider_occurrence_id == "day0"
        assert capture.meet_url == env.provider.created[0]["meeting_url"]
        assert capture.transcript[0]["text"] == "Ship on Friday"
        assert capture.memory_status == "failed"
        assert (await env.service.capture(db, meeting.id)).id == capture.id
        old = await db.get(Meeting, meeting.id)
        env.service.apply_state(capture, old, "ended")
        await db.commit()
        assert old.status == "scheduled"
        assert old.next_occurrence_id == "day1"


@pytest.mark.asyncio
async def test_second_occurrence_creates_new_capture_not_new_meeting(env):
    meeting = await sync(env)
    first = await join(env, meeting.id)
    async with env.sessions() as db:
        capture = await db.get(MeetingCapture, first["capture_id"])
        capture.status, capture.provider_status = "stopped", "ended"
        await db.commit()
    # Simulate the next scheduled occurrence being due, keeping its Google identity.
    env.google.items = [env.google.instance("day1", 0)]
    updated = await sync(env)
    second = await join(env, updated.id)
    assert second["capture_id"] != first["capture_id"]
    async with env.sessions() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Meeting)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MeetingCapture)) == 2
        history = list(
            await db.scalars(sa.select(MeetingCapture).order_by(MeetingCapture.created_at))
        )
        assert [c.provider_occurrence_id for c in history] == ["day0", "day1"]


@pytest.mark.asyncio
async def test_reschedule_cancel_exception_and_changed_link(env):
    meeting = await sync(env)
    env.google.items[0]["status"] = "cancelled"
    env.google.items[1]["hangoutLink"] = "https://meet.google.com/new-room-abc"
    updated = await sync(env)
    assert updated.id == meeting.id
    assert updated.next_occurrence_id == "day1"
    assert updated.next_meet_url.endswith("new-room-abc")
    env.google.items[1]["start"]["dateTime"] = (env.google.now + timedelta(hours=1)).isoformat()
    env.google.items[1]["end"]["dateTime"] = (env.google.now + timedelta(hours=2)).isoformat()
    updated = await sync(env)
    assert updated.next_occurrence_id == "day1"
    assert google.aware(updated.next_starts_at) == env.google.now + timedelta(hours=1)


@pytest.mark.asyncio
async def test_missed_session_and_next_outside_window_do_not_create_history(env):
    meeting = await sync(env)
    env.google.items = []
    updated = await sync(env, now=env.google.now + timedelta(days=31))
    assert updated.id == meeting.id
    assert updated.next_occurrence_id is None
    assert updated.next_starts_at is None
    assert updated.status == "scheduled"
    env.google.items = [env.google.instance("future", 32)]
    updated = await sync(env, now=env.google.now + timedelta(days=31))
    assert updated.next_occurrence_id == "future"
    async with env.sessions() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(MeetingCapture)) == 0


@pytest.mark.asyncio
async def test_master_deleted_is_cancelled_and_no_more_joins(env):
    meeting = await sync(env)
    env.google.master_visible = False
    env.google.deleted = True
    updated = await sync(env)
    assert updated.status == "cancelled"
    assert updated.next_occurrence_id is None
    with pytest.raises(HTTPException) as error:
        await join(env, meeting.id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_paginated_failure_does_not_replace_existing_schedule(env):
    original = await sync(env)
    env.google.fail_second_page = True
    with pytest.raises(google.CalendarError):
        await sync(env)
    async with env.sessions() as db:
        retained = await db.get(Meeting, original.id)
        assert retained.next_occurrence_id == original.next_occurrence_id


@pytest.mark.asyncio
async def test_google_offsets_are_preserved_across_dst(env):
    env.google.master["start"]["timeZone"] = "America/New_York"
    env.google.items = [
        {
            **env.google.instance("dst", 0),
            "start": {"dateTime": "2027-03-14T11:30:00-04:00"},
            "end": {"dateTime": "2027-03-14T12:00:00-04:00"},
        }
    ]
    meeting = await sync(env, now=datetime(2027, 3, 14, 12, tzinfo=UTC))
    assert google.aware(meeting.next_starts_at) == datetime(2027, 3, 14, 15, 30, tzinfo=UTC)
    assert meeting.timezone == "America/New_York"


@pytest.mark.asyncio
async def test_single_event_joins_once(env):
    env.google.master = {
        **env.google.instance("standup", 0),
        "organizer": {"email": "owner@example.com"},
    }
    env.google.master.pop("recurringEventId")
    meeting = await sync(env)
    assert not meeting.is_recurring
    await join(env, meeting.id)
    updated = await sync(env)
    assert updated.next_occurrence_id is None
    assert updated.status == "completed"
    with pytest.raises(HTTPException):
        await join(env, meeting.id)
    assert len(env.provider.created) == 1


@pytest.mark.asyncio
async def test_private_inbox_assigns_whole_series_and_preserves_preferences(env):
    async with env.sessions() as db:
        db.add(Membership(organization_id=env.other_org.id, user_id=env.user.id, role="member"))
        await db.commit()
    meeting = await sync(env)
    assert meeting.organization_id is None
    inbox = await env.dashboard.get("/operator/meeting-events")
    assert len(inbox.json()) == 1
    assert inbox.json()[0]["recurring_event_id"] == "standup"
    response = await env.dashboard.post(
        f"/operator/meeting-events/{meeting.id}/assign",
        json={
            "organization_id": str(env.org.id),
            "auto_join": False,
            "agenda": "Keep this agenda",
        },
    )
    assert response.status_code == 200
    updated = await sync(env)
    assert updated.organization_id == env.org.id
    assert not updated.auto_join
    assert updated.agenda == "Keep this agenda"
    assert (await env.dashboard.get("/operator/meeting-events")).json() == []
    env.app.dependency_overrides[current_user] = lambda: env.other
    assert (await env.dashboard.get(f"/organizations/{env.org.id}/meetings")).status_code == 404
    assert (
        await env.dashboard.get(f"/organizations/{env.org.id}/meetings/{meeting.id}/captures")
    ).status_code == 404
    assert (
        await env.dashboard.post(
            f"/operator/meeting-events/{meeting.id}/assign",
            json={
                "organization_id": str(env.other_org.id),
            },
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_capture_history_and_memory_still_work(env):
    meeting = await sync(env)
    result = await join(env, meeting.id)
    async with env.sessions() as db:
        capture = await db.get(MeetingCapture, result["capture_id"])
        capture.transcript = [
            {
                "speaker": "Asha",
                "speaker_id": "p1",
                "role": "user",
                "text": "Ship Friday",
                "timestamp": utcnow().isoformat(),
            }
        ]
        await db.commit()

    async def summarize(previous, transcript, today):
        return f"{today.isoformat()}\nAsha will ship Friday."

    env.service.speech.memory = summarize
    await env.service.memory.update(result["capture_id"])
    async with env.sessions() as db:
        assert (await db.get(MeetingCapture, result["capture_id"])).transcript == []
        assert "Asha" in (await db.get(MeetingMemory, env.org.id)).context
    response = await env.dashboard.get(
        f"/organizations/{env.org.id}/meetings/{meeting.id}/captures"
    )
    assert response.status_code == 200
    assert response.json()[0]["provider_occurrence_id"] == "day0"
    assert response.json()[0]["memory_status"] == "ready"


@pytest.mark.asyncio
async def test_competing_workers_join_only_once(env):
    if env.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL required to verify row locks")
    meeting = await sync(env)
    await asyncio.gather(*(join(env, meeting.id) for _ in range(8)))
    assert len(env.provider.created) == 1
    async with env.sessions() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(MeetingCapture)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting_owners", [False, True])
async def test_migration_preserves_history_or_refuses_cross_org_merge(conflicting_owners):
    postgres_url = os.environ.get("MEETING_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("Disposable PostgreSQL server required for full migration verification")
    base_url = sa.engine.make_url(postgres_url)
    database_name = "meeting_migration_" + uuid4().hex
    admin = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as db:
        await db.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
    test_url = base_url.set(database=database_name)
    engine = create_async_engine(test_url)
    environment = {
        **os.environ,
        "DATABASE_URL": test_url.render_as_string(hide_password=False),
        "JWT_SECRET": "migration-test-signing-secret-only-123456",
    }

    async def migrate(revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", revision],
            env=environment,
            text=True,
            capture_output=True,
        )

    try:
        result = await migrate("0011")
        assert result.returncode == 0, result.stderr
        now = utcnow()
        ids = {
            name: uuid4()
            for name in [
                "org",
                "other_org",
                "connection",
                "calendar",
                "m1",
                "m2",
                "m3",
                "m4",
                "legacy",
                "c1",
                "c2",
            ]
        }
        transcript = [
            {
                "speaker": "Asha",
                "text": "Keep this exact transcript",
                "role": "user",
                "timestamp": now.isoformat(),
            }
        ]

        def seed(connection):
            metadata = sa.MetaData()
            metadata.reflect(connection)

            def insert(table, **values):
                values.setdefault("id", uuid4())
                values.setdefault("created_at", now)
                connection.execute(metadata.tables[table].insert().values(**values))

            insert("organizations", id=ids["org"], name="Acme")
            insert("organizations", id=ids["other_org"], name="Other")
            insert(
                "meeting_google_connection",
                id=ids["connection"],
                google_subject="subject",
                email="bot@example.com",
                refresh_token_ciphertext="encrypted",
                granted_scopes="calendar",
                status="connected",
                updated_at=now,
            )
            insert(
                "meeting_calendars",
                id=ids["calendar"],
                connection_id=ids["connection"],
                google_calendar_id="primary",
                enabled=True,
            )
            for n, key in enumerate(["m1", "m2", "m3", "m4"]):
                start = now + timedelta(days=n - 1, minutes=-1)
                org = ids["other_org"] if conflicting_owners and key == "m4" else ids["org"]
                insert(
                    "meetings",
                    id=ids[key],
                    organization_id=org,
                    occurrence_key=key,
                    title="Standup " + key,
                    meet_url="https://meet.google.com/abc-defg-hij",
                    starts_at=start,
                    ends_at=start + timedelta(minutes=30),
                    timezone="UTC",
                    agenda="Old agenda",
                    status="completed" if key == "m1" else "scheduled",
                    auto_join=True,
                    updated_at=now,
                )
                insert(
                    "meeting_calendar_events",
                    calendar_id=ids["calendar"],
                    provider_event_id="day" + str(n),
                    recurring_event_id="standup",
                    organizer_email="owner@example.com",
                    meeting_id=ids[key],
                    original_starts_at=start,
                    starts_at=start,
                    ends_at=start + timedelta(minutes=30),
                    title="Standup",
                    meet_url="https://meet.google.com/abc-defg-hij",
                    cancelled=False,
                )
            insert(
                "meeting_series",
                calendar_id=ids["calendar"],
                provider_series_id="standup",
                organization_id=ids["org"],
                organizer_email="owner@example.com",
                agenda="Series agenda",
                auto_join=False,
            )
            for key, meeting, state in [("c1", "m1", "ended"), ("c2", "m2", "joined_recording")]:
                insert(
                    "meeting_captures",
                    id=ids[key],
                    meeting_id=ids[meeting],
                    organization_id=ids["org"],
                    provider_bot_id="bot-" + key,
                    speaker_names={"p1": "Asha"},
                    status="stopped" if state == "ended" else "connected",
                    detail="Session",
                    provider_status=state,
                    transcript=transcript,
                    memory_status="pending",
                    updated_at=now,
                )
            connection.execute(
                metadata.tables["meeting_memories"]
                .insert()
                .values(organization_id=ids["org"], context="Existing memory", updated_at=now)
            )
            insert(
                "meetings",
                id=ids["legacy"],
                organization_id=ids["org"],
                occurrence_key="legacy",
                title="Unlinked historical meeting",
                meet_url="https://meet.google.com/old-room-abc",
                starts_at=now - timedelta(days=3),
                ends_at=now - timedelta(days=3) + timedelta(minutes=30),
                timezone="UTC",
                agenda="",
                status="completed",
                auto_join=False,
                updated_at=now,
            )
            insert(
                "meeting_calendar_events",
                calendar_id=ids["calendar"],
                provider_event_id="private-event",
                organizer_email="other@example.com",
                title="Private upcoming meeting",
                meet_url="https://meet.google.com/xyz-abcd-efg",
                starts_at=now + timedelta(days=1),
                ends_at=now + timedelta(days=1, minutes=30),
                cancelled=False,
            )

        async with engine.begin() as connection:
            await connection.run_sync(seed)
        result = await migrate("0012")
        if conflicting_owners:
            assert result.returncode != 0
            assert "multiple organizations" in result.stderr
            async with engine.connect() as db:
                assert (
                    await db.scalar(sa.text("SELECT version_num FROM alembic_version"))
                ) == "0011"
                assert await db.scalar(sa.text("SELECT count(*) FROM meetings")) == 5
                assert await db.scalar(sa.text("SELECT count(*) FROM meeting_captures")) == 2
            return
        assert result.returncode == 0, result.stderr
        async with engine.connect() as db:
            assert await db.scalar(sa.text("SELECT count(*) FROM meetings")) == 3
            rows = (
                (
                    await db.execute(
                        sa.text("SELECT * FROM meetings WHERE provider_event_id='standup'")
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 1
            meeting = rows[0]
            assert meeting["next_occurrence_id"] == "day2"
            assert meeting["agenda"] == "Series agenda"
            assert meeting["auto_join"] is False
            captures = (
                (
                    await db.execute(
                        sa.text("SELECT * FROM meeting_captures ORDER BY provider_occurrence_id")
                    )
                )
                .mappings()
                .all()
            )
            assert {row["id"] for row in captures} == {ids["c1"], ids["c2"]}
            assert all(row["meeting_id"] == meeting["id"] for row in captures)
            assert all(row["transcript"] == transcript for row in captures)
            assert {row["title"] for row in captures} == {"Standup m1", "Standup m2"}
            assert (
                await db.scalar(sa.text("SELECT context FROM meeting_memories"))
                == "Existing memory"
            )

            def check_schema(connection):
                from alembic.autogenerate import compare_metadata
                from alembic.migration import MigrationContext

                def include(obj, name, type_, reflected, compare_to):
                    table = obj if type_ == "table" else getattr(obj, "table", None)
                    return table is not None and table.name in {
                        "meetings",
                        "meeting_captures",
                        "meeting_calendar_events",
                        "meeting_series",
                    }

                context = MigrationContext.configure(
                    connection, opts={"include_object": include, "compare_type": True}
                )
                differences = compare_metadata(context, Base.metadata)
                assert not differences, differences
                tables = sa.inspect(connection).get_table_names()
                assert "meeting_calendar_events" not in tables
                assert "meeting_series" not in tables

            await db.run_sync(check_schema)
    finally:
        await engine.dispose()
        async with admin.connect() as db:
            await db.execute(sa.text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_transferred_organizer_cannot_join_old_workspace(env):
    meeting = await sync(env)
    env.google.master["organizer"] = {"email": "other@example.com"}
    updated = await sync(env)
    assert updated.id == meeting.id
    assert updated.organization_id == env.org.id
    assert updated.organizer_email == "owner@example.com"
    assert updated.status == "cancelled"
    assert updated.next_occurrence_id is None
    assert not updated.auto_join


@pytest.mark.asyncio
async def test_auto_join_setting_and_occurrence_link_are_exposed(env):
    env.google.master.pop("hangoutLink")
    env.google.items[0]["hangoutLink"] = "https://meet.google.com/new-room-abc"
    meeting = await sync(env)
    response = await env.dashboard.get(f"/organizations/{env.org.id}/meetings")
    assert response.status_code == 200
    assert response.json()[0]["meet_url"] == "https://meet.google.com/new-room-abc"
    response = await env.dashboard.put(
        f"/organizations/{env.org.id}/meetings/{meeting.id}/schedule", json={"auto_join": False}
    )
    assert response.status_code == 200
    updated = await sync(env)
    assert not updated.auto_join
    # A paused definition does not create a session on automatic dispatch.
    await join(env, meeting.id)
    assert not env.provider.created

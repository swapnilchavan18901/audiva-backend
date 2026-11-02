"""No external calls: exercise persistence, API permissions and provider wire contracts."""

import asyncio
import importlib.util
import json
import logging
import os
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import outreach as api
from app.api.dependencies import current_user
from app.business import outreach
from app.business.outreach_worker import OutreachService
from app.clients.twilio import TwilioClient
from app.clients.twilio_logging import safe_detail
from app.db.models import (
    Base,
    Membership,
    Organization,
    OutreachAgent,
    OutreachCall,
    OutreachCampaign,
    User,
    utcnow,
)
from app.db.session import get_db
from app.env import Settings
from app.outreach_types import CampaignCreate, Contact, parse_contacts_csv


class FakeProvider:
    def __init__(self):
        self.dials = []
        self.states = {}
        self.error = None
        self.inflight = 0
        self.peak = 0

    async def dial(self, number, stream, callback, call_id):
        self.dials.append((number, stream, callback, call_id))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        if self.error:
            raise self.error
        sid = f"sid{call_id.hex}"
        self.states[sid] = {
            "Sid": sid,
            "Status": "in-progress",
            "From": "+918012345678",
            "To": number,
        }
        return self.states[sid]

    async def details(self, sid):
        return self.states[sid]

    async def close(self):
        pass


@pytest_asyncio.fixture
async def env(tmp_path):
    postgres_url = os.environ.get("OUTREACH_TEST_POSTGRES_URL")
    admin = None
    schema = None
    if postgres_url:
        schema = "outreach_test_" + uuid4().hex
        admin = create_async_engine(postgres_url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            postgres_url,
            connect_args={
                "server_settings": {"search_path": schema},
            },
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outreach.db'}")

        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda db: Base.metadata.create_all(
                db,
                tables=[
                    User.__table__,
                    Organization.__table__,
                    Membership.__table__,
                    OutreachAgent.__table__,
                    OutreachCampaign.__table__,
                    OutreachCall.__table__,
                ],
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        jwt_secret="test-secret-for-outreach-only-123456789",
        twilio_account_sid="account",
        twilio_auth_token="token",
        twilio_phone_number="08012345678",
        public_backend_url="https://outreach.example.com",
        sarvam_api_key="test",
    )
    provider = FakeProvider()
    service = OutreachService(settings, sessions=sessions, provider=provider)
    async with sessions() as db:
        user = User(name="Owner", email="owner@example.com", password_hash="unused")
        outsider = User(
            name="Outsider", email="outsider@example.com", password_hash="unused"
        )
        member = User(name="Member", email="member@example.com", password_hash="unused")
        org = Organization(name="Example")
        other = Organization(name="Other")
        db.add_all([user, outsider, member, org, other])
        await db.flush()
        db.add_all(
            [
                Membership(organization_id=org.id, user_id=user.id, role="owner"),
                Membership(organization_id=org.id, user_id=member.id, role="member"),
                Membership(organization_id=other.id, user_id=outsider.id, role="owner"),
            ]
        )
        agent = OutreachAgent(
            organization_id=org.id,
            name="Plans advisor",
            sales_context="Sell the standard plan for 500",
            language="en-IN",
        )
        db.add(agent)
        await db.commit()
    app = FastAPI()
    app.state.outreach = service
    app.include_router(api.router, prefix="/api/v1")
    app.include_router(api.provider_router, prefix="/api/v1")

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            sessions=sessions,
            service=service,
            provider=provider,
            settings=settings,
            user=user,
            outsider=outsider,
            member=member,
            org=org,
            other=other,
            agent=agent,
            app=app,
            client=client,
            base=f"/api/v1/organizations/{org.id}/outreach",
        )
    await service.close()
    await engine.dispose()
    if admin:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def draft(env, count=7, mode="parallel"):
    async with env.sessions() as db:
        return await outreach.create_campaign(
            db,
            env.org.id,
            env.user.id,
            CampaignCreate(
                name="Launch",
                agent_id=env.agent.id,
                mode=mode,
                contacts=[f"+91987654{i:04d}" for i in range(count)],
            ),
        )


async def run(env, campaign):
    async with env.sessions() as db:
        return await outreach.run(db, env.org.id, campaign.id, env.user.id, env.service)


async def calls(env, campaign):
    async with env.sessions() as db:
        return list(
            await db.scalars(
                select(OutreachCall)
                .where(OutreachCall.campaign_id == campaign.id)
                .order_by(OutreachCall.position)
            )
        )


@pytest.mark.parametrize(
    "number", ["9876543210", "+0123456789", "+91123", "=123", "+91abc12345678"]
)
def test_invalid_phone(number):
    with pytest.raises(ValidationError):
        Contact(phone_number=number)


def test_csv_normalizes_and_preserves_context():
    contacts = parse_contacts_csv(
        b'\xef\xbb\xbfphone_number,name,context\n+91 98765-43210,Asha,"Sell plans, answer questions"\n'
    )
    assert contacts[0].phone_number == "+919876543210"
    assert contacts[0].context == "Sell plans, answer questions"


@pytest.mark.parametrize(
    "content",
    [
        b"phone,name\n+919876543210,Test\n",
        b"phone_number\n",
        b"phone_number,phone_number\n+919876543210,+919876543211\n",
        b"phone_number\n9876543210\n",
        b"phone_number\n\xff\n",
        b'phone_number,name\n+919876543210,"unterminated',
        b"phone_number,name\n+919876543210,Name,extra\n",
    ],
)
def test_invalid_csv(content):
    with pytest.raises(ValueError):
        parse_contacts_csv(content)


def test_duplicates_after_normalization():
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="Test", agent_id=uuid4(), contacts=["+919876543210", "+91 98765-43210"]
        )


@pytest.mark.asyncio
async def test_draft_does_not_dial_and_snapshots_agent(env):
    campaign = await draft(env)
    await env.service.tick()
    assert not env.provider.dials
    async with env.sessions() as db:
        agent = await db.get(OutreachAgent, env.agent.id)
        agent.sales_context = "Edited"
        await db.commit()
        stored = await db.get(OutreachCampaign, campaign.id)
        assert stored.agent_snapshot["sales_context"] == env.agent.sales_context


@pytest.mark.asyncio
async def test_sequential_waits_for_terminal_and_finishes(env):
    campaign = await draft(env, count=2, mode="sequential")
    await run(env, campaign)
    await env.service.tick()
    await env.service.tick()
    assert len(env.provider.dials) == 1
    first = (await calls(env, campaign))[0]
    env.provider.states[first.provider_call_sid]["Status"] = "busy"
    await env.service.tick()
    assert len(env.provider.dials) == 2
    last = (await calls(env, campaign))[1]
    env.provider.states[last.provider_call_sid]["Status"] = "completed"
    await env.service.tick()
    async with env.sessions() as db:
        assert (await db.get(OutreachCampaign, campaign.id)).status == "completed"


@pytest.mark.asyncio
async def test_parallel_is_five_concurrent_and_waits_for_whole_batch(env):
    campaign = await draft(env)
    await run(env, campaign)
    await env.service.tick()
    assert len(env.provider.dials) == 5
    assert env.provider.peak == 5
    batch = await calls(env, campaign)
    for call in batch[:4]:
        env.provider.states[call.provider_call_sid]["Status"] = "completed"
    await env.service.tick()
    assert len(env.provider.dials) == 5
    env.provider.states[batch[4].provider_call_sid]["Status"] = "no-answer"
    await env.service.tick()
    assert len(env.provider.dials) == 7


@pytest.mark.asyncio
async def test_multiple_agents_campaigns_run_together(env):
    first = await draft(env, count=2)
    response = await env.client.post(
        f"{env.base}/agents",
        json={
            "name": "Other advisor",
            "sales_context": "Sell another product",
        },
    )
    assert response.status_code == 201
    response = await env.client.post(
        f"{env.base}/campaigns",
        json={
            "name": "Second",
            "agent_id": response.json()["id"],
            "mode": "parallel",
            "contacts": ["+919000000001", "+919000000002"],
        },
    )
    second_id = response.json()["id"]
    await run(env, first)
    assert (
        await env.client.post(f"{env.base}/campaigns/{second_id}/run")
    ).status_code == 200
    await env.service.tick()
    assert len(env.provider.dials) == 4


@pytest.mark.asyncio
async def test_run_is_once_and_requires_provider_configuration(env):
    campaign = await draft(env)
    env.service.settings.twilio_account_sid = ""
    with pytest.raises(HTTPException) as unavailable:
        await run(env, campaign)
    assert unavailable.value.status_code == 503
    env.service.settings.twilio_account_sid = "account"
    await run(env, campaign)
    with pytest.raises(HTTPException) as duplicate:
        await run(env, campaign)
    assert duplicate.value.status_code == 409


@pytest.mark.asyncio
async def test_stop_cancels_pending_and_waits_for_active(env):
    campaign = await draft(env)
    await run(env, campaign)
    await env.service.tick()
    async with env.sessions() as db:
        result = await outreach.stop(db, env.org.id, campaign.id, env.user.id)
        assert result.status == "stopping"
    assert [c.status for c in (await calls(env, campaign))[-2:]] == ["cancelled"] * 2
    for result in env.provider.states.values():
        result["Status"] = "completed"
    await env.service.tick()
    assert len(env.provider.dials) == 5
    async with env.sessions() as db:
        assert (await db.get(OutreachCampaign, campaign.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_status_callbacks_auth_ordering_and_sid_binding(env):
    campaign = await draft(env, count=1)
    await run(env, campaign)
    await env.service.tick()
    call = (await calls(env, campaign))[0]
    path = f"/api/v1/outreach/twilio/{call.id}/status"
    token = env.service.token(call.id, "status")
    body = {"CallSid": call.provider_call_sid, "CallStatus": "completed"}
    assert (await env.client.post(path, data=body)).status_code == 403
    wrong = env.service.token(call.id, "media")
    assert (
        await env.client.post(path, params={"token": wrong}, data=body)
    ).status_code == 403
    assert (
        await env.client.post(
            path, params={"token": token}, data={**body, "CallSid": "wrong"}
        )
    ).status_code == 409
    assert (
        await env.client.post(path, params={"token": token}, data=body)
    ).status_code == 204
    assert (
        await env.client.post(
            path, params={"token": token}, json={**body, "CallStatus": "ringing"}
        )
    ).status_code == 204
    assert (await calls(env, campaign))[0].status == "completed"


@pytest.mark.asyncio
async def test_timeout_is_unknown_persistent_and_not_redialed(env):
    env.provider.error = httpx.ReadTimeout("response lost")
    campaign = await draft(env, count=2, mode="sequential")
    await run(env, campaign)
    await env.service.tick()
    restarted = OutreachService(
        env.settings, sessions=env.sessions, provider=env.provider
    )
    await restarted.tick()
    assert len(env.provider.dials) == 1
    call = (await calls(env, campaign))[0]
    assert call.status == "unknown"
    await restarted.apply_status(call.id, "late-sid", "completed")
    env.provider.error = None
    await restarted.tick()
    assert len(env.provider.dials) == 2


@pytest.mark.asyncio
async def test_recover_interrupted_dispatch_and_manual_resolution(env):
    campaign = await draft(env, count=2, mode="sequential")
    await run(env, campaign)
    call_id = (await env.service.claim_batch(campaign.id))[0]
    async with env.sessions() as db:
        call = await db.get(OutreachCall, call_id)
        call.started_at = utcnow() - timedelta(minutes=2)
        await db.commit()
    await env.service.tick()
    assert not env.provider.dials
    assert (await calls(env, campaign))[0].status == "unknown"
    response = await env.client.post(
        f"{env.base}/campaigns/{campaign.id}/calls/{call_id}/reconcile",
        json={"confirmed_not_placed": True},
    )
    assert response.status_code == 200
    await env.service.tick()
    assert len(env.provider.dials) == 1


@pytest.mark.asyncio
async def test_rejected_call_moves_on_without_retry(env):
    request = httpx.Request("POST", "https://example.com")
    env.provider.error = httpx.HTTPStatusError(
        "rejected", request=request, response=httpx.Response(400, request=request)
    )
    campaign = await draft(env, count=2, mode="sequential")
    await run(env, campaign)
    await env.service.tick()
    assert (await calls(env, campaign))[0].status == "failed"
    env.provider.error = None
    await env.service.tick()
    assert len(env.provider.dials) == 2


@pytest.mark.asyncio
async def test_org_isolation_and_member_read_only(env):
    campaign = await draft(env)
    env.app.dependency_overrides[current_user] = lambda: env.outsider
    assert (await env.client.get(f"{env.base}/agents")).status_code == 404
    assert (
        await env.client.get(f"{env.base}/campaigns/{campaign.id}/calls")
    ).status_code == 404
    env.app.dependency_overrides[current_user] = lambda: env.member
    assert (await env.client.get(f"{env.base}/agents")).status_code == 200
    assert (
        await env.client.post(f"{env.base}/campaigns/{campaign.id}/run")
    ).status_code == 403
    assert (
        await env.client.put(
            f"{env.base}/agents/{env.agent.id}",
            json={
                "name": "Changed",
                "sales_context": "Changed",
            },
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_csv_upload_template_and_validation(env):
    response = await env.client.get(f"{env.base}/contacts-template.csv")
    assert response.status_code == 200
    response = await env.client.post(
        f"{env.base}/campaigns/csv",
        data={
            "agent_id": str(env.agent.id),
            "name": "CSV",
            "mode": "parallel",
        },
        files={
            "file": (
                "contacts.csv",
                b"phone_number,name,context\n+919876543210,Asha,Plans\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 201
    response = await env.client.get(
        f"{env.base}/campaigns/{response.json()['id']}/calls"
    )
    assert response.json()[0]["phone_number"] == "+919876543210"
    response = await env.client.post(
        f"{env.base}/campaigns/csv",
        data={
            "agent_id": str(env.agent.id),
            "name": "CSV",
        },
        files={"file": ("bad.csv", b"phone_number\n+919876543210\n+919876543210\n")},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_twilio_wire_contract(env):
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(201, json={"sid": "CAcall", "status": "queued"})

    provider = TwilioClient(env.settings)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url="https://api.twilio.com/2010-04-01/Accounts/account/",
        auth=("account", "token"),
        transport=httpx.MockTransport(handler),
    )
    result = await provider.dial(
        "+919876543210",
        "https://example.com/answer",
        "https://example.com/status",
        uuid4(),
    )
    body = parse_qs(captured[0].content.decode())
    assert body["From"] == [env.settings.twilio_phone_number]
    assert body["To"] == ["+919876543210"]
    assert body["Url"] == ["https://example.com/answer"]
    assert body["Method"] == ["POST"]
    assert body["StatusCallback"] == ["https://example.com/status"]
    assert body["StatusCallbackEvent"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]
    assert body["TimeLimit"] == ["1800"] and body["Record"] == ["false"]
    assert captured[0].url.path == "/2010-04-01/Accounts/account/Calls.json"
    assert captured[0].headers["authorization"].startswith("Basic ")
    assert (
        captured[0]
        .headers["content-type"]
        .startswith("application/x-www-form-urlencoded")
    )
    assert result == {"Sid": "CAcall", "Status": "queued"}
    await provider.close()


@pytest.mark.asyncio
async def test_migration_roundtrip(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    spec = importlib.util.spec_from_file_location(
        "outreach_migration", "app/db/migrations/versions/0011_outreach.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")

    def check(connection):
        original = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {
                "outreach_agents",
                "outreach_campaigns",
                "outreach_calls",
            }
            migration.downgrade()
            assert not inspect(connection).get_table_names()
        finally:
            migration.op = original

    async with engine.begin() as connection:
        await connection.run_sync(check)
    await engine.dispose()


@pytest.mark.asyncio
async def test_multipart_callback_and_unicode_token(env):
    campaign = await draft(env, count=1)
    await run(env, campaign)
    await env.service.tick()
    call = (await calls(env, campaign))[0]
    path = f"/api/v1/outreach/twilio/{call.id}/status"
    response = await env.client.post(path, params={"token": "é"}, json={})
    assert response.status_code == 403
    response = await env.client.post(
        path,
        params={"token": env.service.token(call.id, "status")},
        files={
            "CallSid": (None, call.provider_call_sid),
            "CallStatus": (None, "completed"),
        },
    )
    assert response.status_code == 204
    assert (await calls(env, campaign))[0].status == "completed"


class FakeWebsocket:
    def __init__(self, app, events):
        self.app = app
        self.events = iter(events)
        self.client_state = SimpleNamespace(name="CONNECTED")
        self.close_code = None

    async def receive_json(self):
        return next(self.events)

    async def accept(self):
        pass

    async def close(self, code):
        self.close_code = code
        self.client_state.name = "DISCONNECTED"


@pytest.mark.asyncio
async def test_media_auth_binding_snapshot_and_one_stream_per_call(env, monkeypatch):
    from app.ai import outreach as voice

    campaign = await draft(env, count=1)
    await run(env, campaign)
    await env.service.tick()
    call = (await calls(env, campaign))[0]
    invoked = []

    async def fake_voice(ws, snapshot, contact, start, settings):
        invoked.append(snapshot)

    monkeypatch.setattr(voice, "run_voice", fake_voice)
    start = {
        "event": "start",
        "start": {
            "accountSid": "account",
            "streamSid": "stream",
            "callSid": call.provider_call_sid,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
        },
    }
    token = env.service.token(call.id, "media")
    bad = FakeWebsocket(env.app, [start])
    await api.twilio_media(bad, call.id, "wrong")
    assert bad.close_code == 1008
    wrong_sid = FakeWebsocket(
        env.app, [{**start, "start": {**start["start"], "callSid": "wrong"}}]
    )
    await api.twilio_media(wrong_sid, call.id, token)
    assert wrong_sid.close_code == 1008
    assert not invoked
    ws = FakeWebsocket(env.app, [{"event": "connected"}, start])
    await api.twilio_media(ws, call.id, token)
    assert invoked[0]["sales_context"] == env.agent.sales_context
    assert ws.close_code == 1000
    # Socket closure alone must not free a slot before provider termination.
    assert (await calls(env, campaign))[0].status == "in-progress"
    duplicate = FakeWebsocket(env.app, [start])
    await api.twilio_media(duplicate, call.id, token)
    assert duplicate.close_code == 1008
    assert len(invoked) == 1


@pytest.mark.asyncio
async def test_real_voice_pipeline_construction_without_external_requests(
    env, monkeypatch
):
    from app.ai.outreach import run_voice

    workers = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        async def add_workers(self, worker):
            workers.append(worker)

        async def run(self):
            pass

        async def cancel(self):
            pass

    monkeypatch.setattr("pipecat.workers.runner.WorkerRunner", Runner)
    await run_voice(
        SimpleNamespace(),
        {
            "language": "en-IN",
            "name": "Test",
            "sales_context": "Test plan",
        },
        {},
        {
            "stream_sid": "stream",
            "call_sid": "call",
            "media_format": {"sample_rate": 8000},
        },
        env.settings,
    )
    assert len(workers) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_reserve_at_most_one_batch(env):
    async with env.sessions() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL required to exercise SELECT FOR UPDATE")
    campaign = await draft(env, count=20)
    await run(env, campaign)
    other_worker = OutreachService(
        env.settings, sessions=env.sessions, provider=env.provider
    )
    results = await asyncio.gather(
        *[
            (env.service if i % 2 else other_worker).claim_batch(campaign.id)
            for i in range(10)
        ]
    )
    ids = [call_id for batch in results for call_id in batch]
    assert len(ids) == len(set(ids)) == 5
    assert (
        len([call for call in await calls(env, campaign) if call.status == "dialing"])
        == 5
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "timeout", "invalid_json"])
async def test_provider_failure_is_logged_without_retry_or_secrets(caplog, failure):
    settings = SimpleNamespace(
        twilio_account_sid="account",
        twilio_auth_token=SecretStr("private-api-token"),
        twilio_phone_number="+911234567890",
    )
    number = "+919876543210"
    stream = "https://example.com/answer?token=private-answer-grant"
    callback = "https://example.com/status?token=private-callback-grant"
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Timed out", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="invalid json")
        return httpx.Response(
            401,
            json={
                "message": f"Authentication failed private-api-token {number} {stream} {callback}",
                "code": 20003,
                "status": 401,
            },
        )

    provider = TwilioClient(settings)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url="https://api.twilio.com/2010-04-01/Accounts/account/",
        transport=httpx.MockTransport(handler),
    )
    call_id = uuid4()
    try:
        with (
            caplog.at_level(logging.INFO, logger="app.twilio"),
            pytest.raises((httpx.HTTPError, ValueError)),
        ):
            await provider.dial(number, stream, callback, call_id)
    finally:
        await provider.close()
    assert len(requests) == 1
    assert "Twilio request failed" in caplog.text
    assert str(call_id) in caplog.text
    assert "elapsed_ms=" in caplog.text
    for secret in (
        "private-api-token",
        number,
        "private-answer-grant",
        "private-callback-grant",
    ):
        assert secret not in caplog.text
    if failure == "rejected":
        assert "http_status=401" in caplog.text
        assert "Authentication failed" in caplog.text
        assert "20003" in caplog.text
    elif failure == "timeout":
        assert "error_type=ReadTimeout" in caplog.text
    else:
        assert "error_type=JSONDecodeError" in caplog.text


def test_safe_detail_bounds_and_cleans_provider_text():
    result = safe_detail(
        "token=private-grant\nFrom: +91 (98765) 43210\rhttps://example.com/private "
        + "x" * 3000
    )
    assert "private-grant" not in result
    assert "98765" not in result
    assert "example.com" not in result
    assert "\n" not in result and "\r" not in result
    assert len(result) <= 2000


@pytest.mark.asyncio
async def test_twilio_queued_call_sid_and_answer_xml(env):
    async def accepted(number, answer, callback, call_id):
        env.provider.dials.append((number, answer, callback, call_id))
        env.provider.states["real-call"] = {"Sid": "real-call", "Status": "queued"}
        return env.provider.states["real-call"]

    env.provider.dial = accepted
    campaign = await draft(env, count=2, mode="sequential")
    await run(env, campaign)
    await env.service.tick()
    call = (await calls(env, campaign))[0]
    assert call.status == "queued" and call.provider_call_sid == "real-call"
    await env.service.tick()
    assert len(env.provider.dials) == 1
    answer, _ = env.service.urls(call.id)
    response = await env.client.post(answer, data={"CallSid": "real-call"})
    assert response.status_code == 200
    from xml.etree.ElementTree import fromstring

    xml = fromstring(response.text)
    stream = xml.find("Connect/Stream")
    assert stream.get("url") == env.service.media_url(call.id)
    assert "?" not in stream.get("url")
    assert xml.find("Hangup") is not None
    call = (await calls(env, campaign))[0]
    assert call.provider_call_sid == "real-call" and call.status == "in-progress"
    wrong = await env.client.post(answer.split("?")[0], data={"CallSid": "fake"})
    assert wrong.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_status,expected",
    [("in-progress", "in-progress"), ("busy", "busy"), ("canceled", "cancelled")],
)
async def test_twilio_details_call_resource(env, provider_status, expected):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "sid": "CAcall",
                "status": provider_status,
                "from": "+918012345678",
                "to": "+919876543210",
            },
        )

    provider = TwilioClient(env.settings)
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url="https://api.twilio.com/2010-04-01/Accounts/account/",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await provider.details("CAcall")
    finally:
        await provider.close()
    assert result["Sid"] == "CAcall" and result["To"] == "+919876543210"
    assert result["Status"] == expected
    assert len(captured) == 1
    assert captured[0].url.path.endswith("/Calls/CAcall.json")
    assert not captured[0].url.query


@pytest.mark.asyncio
async def test_twilio_serializer_audio_and_interruption():
    import base64

    from pipecat.frames.frames import (
        EndFrame,
        InputAudioRawFrame,
        InterruptionFrame,
        OutputAudioRawFrame,
    )

    from app.ai.common.twilio import build_transport

    transport = build_transport(
        SimpleNamespace(), {"call_sid": "call", "stream_sid": "stream"}
    )
    serializer = transport._params.serializer
    await serializer.setup(SimpleNamespace(audio_in_sample_rate=8000))
    incoming = await serializer.deserialize(
        json.dumps(
            {
                "event": "media",
                "media": {
                    "payload": base64.b64encode(b"\xff" * 160).decode(),
                },
            }
        )
    )
    assert isinstance(incoming, InputAudioRawFrame)
    assert incoming.sample_rate == 8000 and len(incoming.audio) == 320
    outgoing = json.loads(
        await serializer.serialize(
            OutputAudioRawFrame(
                audio=b"\x00\x00" * 160,
                sample_rate=8000,
                num_channels=1,
            )
        )
    )
    assert outgoing["event"] == "media"
    assert outgoing["streamSid"] == "stream"
    assert len(base64.b64decode(outgoing["media"]["payload"])) == 160
    cleared = json.loads(await serializer.serialize(InterruptionFrame()))
    assert cleared == {"event": "clear", "streamSid": "stream"}
    assert isinstance(await serializer.deserialize('{"event":"stop"}'), EndFrame)


@pytest.mark.parametrize(
    "body",
    [
        {"message": "Trial accounts have limited parameter access"},
        {"message": "The number is unverified on a trial account", "code": 21219},
        ["unexpected payload"],
    ],
)
def test_twilio_trial_rejection_only_maps_restricted_parameter_error(body):
    from app.clients.twilio import rejection_message

    request = httpx.Request("POST", "https://api.twilio.com/Calls.json")
    exc = httpx.HTTPStatusError(
        "rejected",
        request=request,
        response=httpx.Response(400, json=body, request=request),
    )
    result = rejection_message(exc)
    if isinstance(body, dict) and "limited parameter access" in body["message"]:
        assert "Media Streams" in result
    else:
        assert result == "Twilio rejected the call request"

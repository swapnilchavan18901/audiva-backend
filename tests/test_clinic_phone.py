"""Clinic telephony verification without placing calls or contacting AI providers."""

import importlib.util
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import bots as bots_api
from app.api import clinic_phone as api
from app.api.dependencies import current_user
from app.business import bots
from app.business.clinic_phone import ClinicPhoneService
from app.db.models import (
    Base,
    Bot,
    BotTemplate,
    ClinicAppointment,
    ClinicPhoneCall,
    ClinicPhoneRoute,
    Membership,
    Organization,
    User,
)
from app.db.session import get_db
from app.env import Settings


class Provider:
    def __init__(self):
        self.dials = []
        self.states = {}
        self.error = None

    async def dial(self, number, stream, callback, call_id):
        self.dials.append((number, stream, callback, call_id))
        if self.error:
            raise self.error
        result = {
            "Sid": "sid" + call_id.hex,
            "Status": "in-progress",
            "From": "+918012345678",
            "To": number,
        }
        self.states[result["Sid"]] = result
        return result

    async def details(self, sid):
        return self.states[sid]


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'clinic.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    slot = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0, tzinfo=None
    )
    async with sessions() as db:
        owner = User(name="Owner", email="owner@example.com", password_hash="unused")
        member = User(name="Member", email="member@example.com", password_hash="unused")
        stranger = User(
            name="Stranger", email="stranger@example.com", password_hash="unused"
        )
        org = Organization(name="Clinic")
        template = BotTemplate(
            slug="clinic",
            name="Clinic",
            description="Clinic",
            system_prompt="Clinic",
            service_type="clinic",
        )
        db.add_all([owner, member, stranger, org, template])
        await db.flush()
        db.add_all(
            [
                Membership(organization_id=org.id, user_id=owner.id, role="owner"),
                Membership(organization_id=org.id, user_id=member.id, role="member"),
            ]
        )
        bot = Bot(
            name="Receptionist",
            organization_id=org.id,
            template_id=template.id,
            language="en-IN",
            company_context=json.dumps(
                {
                    "clinic": {
                        "name": "Test Clinic",
                        "timezone": "Asia/Kolkata",
                        "slot_minutes": 30,
                        "doctors": [
                            {
                                "name": "Dr Test",
                                "specialty": "General",
                                "available_slots": [slot.isoformat()],
                            }
                        ],
                    }
                }
            ),
        )
        db.add(bot)
        await db.commit()
    settings = Settings(
        _env_file=None,
        jwt_secret="test-secret-clinic-phone-at-least-32-characters",
        sarvam_api_key="test",
        twilio_account_sid="account",
        twilio_auth_token="token",
        twilio_phone_number="08012345678",
        public_backend_url="https://clinic.example.com",
        clinic_operator_user_ids=[owner.id],
    )
    provider = Provider()
    service = ClinicPhoneService(settings, provider, sessions=sessions)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.include_router(api.provider_router, prefix="/api/v1")
    app.include_router(bots_api.router, prefix="/api/v1")
    app.state.clinic_phone = service
    actor = {"user": owner}

    async def db_dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[current_user] = lambda: actor["user"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            sessions=sessions,
            service=service,
            provider=provider,
            settings=settings,
            app=app,
            client=client,
            actor=actor,
            owner=owner,
            member=member,
            stranger=stranger,
            org=org,
            bot=bot,
            slot=slot,
            path=f"/api/v1/organizations/{org.id}/bots/{bot.id}/phone",
        )
    await service.close()
    await engine.dispose()


def start(sid="incoming", **overrides):
    return {
        "stream_sid": "stream",
        "call_sid": sid,
        "account_sid": "account",
        "from": "+919876543210",
        "to": "+918012345678",
        "media_format": {
            "encoding": "audio/x-mulaw",
            "sample_rate": 8000,
            "bit_rate": 16,
        },
        **overrides,
    }


def wire_start(value):
    return {
        "event": "start",
        "start": {
            "accountSid": value["account_sid"],
            "callSid": value["call_sid"],
            "streamSid": value["stream_sid"],
            "mediaFormat": {
                "encoding": value["media_format"]["encoding"],
                "sampleRate": value["media_format"]["sample_rate"],
            },
        },
    }


async def route(env):
    response = await env.client.put(
        env.path + "/route", json={"phone_number": "+918012345678"}
    )
    assert response.status_code == 200, response.text
    async with env.sessions() as db:
        return await db.get(ClinicPhoneRoute, UUID(response.json()["id"]))


@pytest.mark.asyncio
async def test_route_access_rotation_disable_and_uniqueness(env):
    env.settings.clinic_operator_user_ids = []
    assert (
        await env.client.put(
            env.path + "/route", json={"phone_number": "+918012345678"}
        )
    ).status_code == 403
    env.settings.clinic_operator_user_ids = [env.owner.id]
    previous = await route(env)
    previous_token = env.service.token(previous.token_version, "inbound")
    current = await route(env)
    assert current.token_version != previous.token_version
    with pytest.raises(HTTPException):
        env.service.verify(current.token_version, "inbound", previous_token)
    env.actor["user"] = env.member
    assert (await env.client.get(env.path + "/route")).status_code == 403
    assert (
        await env.client.post(
            env.path + "/calls",
            json={"phone_number": "+919876543210", "request_id": str(uuid4())},
        )
    ).status_code == 403
    env.actor["user"] = env.stranger
    assert (await env.client.get(env.path + "/calls")).status_code == 404
    env.actor["user"] = env.owner
    response = await env.client.put(
        env.path + "/route", json={"phone_number": "+918012345678", "enabled": False}
    )
    assert response.status_code == 200 and response.json()["inbound_answer_url"] is None
    async with env.sessions() as db:
        other = Bot(
            name="Other",
            organization_id=env.org.id,
            template_id=env.bot.template_id,
            company_context="",
        )
        db.add(other)
        await db.flush()
        db.add(ClinicPhoneRoute(bot_id=other.id, phone_number=current.phone_number))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_outbound_idempotency_and_callback_security(env):
    data = {"phone_number": "+919876543210", "request_id": str(uuid4())}
    first = await env.client.post(env.path + "/calls", json=data)
    assert first.status_code == 202, first.text
    call = first.json()
    second = await env.client.post(env.path + "/calls", json=data)
    assert second.json()["id"] == call["id"] and len(env.provider.dials) == 1
    assert (
        await env.client.post(
            env.path + "/calls", json={**data, "phone_number": "+919876543211"}
        )
    ).status_code == 409
    stream, _callback = env.provider.dials[0][1:3]
    assert stream.startswith(
        "https://clinic.example.com/api/v1/clinic-phone/twilio/calls/"
    )
    call_id = UUID(call["id"])
    endpoint = f"/api/v1/clinic-phone/twilio/calls/{call_id}/status"
    assert (
        await env.client.post(
            endpoint,
            json={"CallSid": call["provider_call_sid"], "CallStatus": "completed"},
        )
    ).status_code == 403
    token = env.service.token(call_id, "status")
    assert (
        await env.client.post(
            endpoint,
            params={"token": token},
            json={"CallSid": "wrong", "CallStatus": "completed"},
        )
    ).status_code == 409
    assert (
        await env.client.post(
            endpoint,
            params={"token": token},
            data={"CallSid": call["provider_call_sid"], "CallStatus": "completed"},
        )
    ).status_code == 204
    await env.service.apply_status(call_id, call["provider_call_sid"], "ringing")
    listing = await env.client.get(env.path + "/calls")
    assert listing.json()[0]["status"] == "completed"
    with pytest.raises(HTTPException):
        env.service.verify(call_id, "media", token)


@pytest.mark.asyncio
async def test_unknown_dispatch_recovery_does_not_redial(env):
    env.provider.error = httpx.ReadTimeout("unknown")
    data = {"phone_number": "+919876543210", "request_id": str(uuid4())}
    response = await env.client.post(env.path + "/calls", json=data)
    assert response.json()["status"] == "unknown"
    await env.client.post(env.path + "/calls", json=data)
    assert len(env.provider.dials) == 1
    call_id = response.json()["id"]
    env.provider.states["verified"] = {
        "Sid": "verified",
        "To": data["phone_number"],
        "Status": "completed",
    }
    result = await env.client.post(
        env.path + f"/calls/{call_id}/reconcile", json={"provider_call_sid": "verified"}
    )
    assert result.status_code == 200 and result.json()["status"] == "completed"
    assert len(env.provider.dials) == 1


@pytest.mark.asyncio
async def test_inbound_verifies_provider_and_rejects_replay(env):
    config = await route(env)
    token = env.service.inbound_media_token(config, "incoming")
    env.provider.states["incoming"] = {
        "Sid": "incoming",
        "From": "09876543210",
        "To": "08012345679",
        "Status": "in-progress",
    }
    with pytest.raises(HTTPException):
        await env.service.admit(start(), route_id=config.id, token=token)
    env.provider.states["incoming"]["To"] = "+918012345678"
    call, runtime = await env.service.admit(start(), route_id=config.id, token=token)
    assert call.direction == "inbound" and runtime.service_type == "clinic"
    assert runtime.id == env.bot.id and call.phone_number == "+919876543210"
    with pytest.raises(IntegrityError):
        await env.service.admit(start(), route_id=config.id, token=token)
    await env.service.apply_status(call.id, "incoming", "completed")
    async with env.sessions() as db:
        assert (await db.get(ClinicPhoneCall, call.id)).status == "completed"


@pytest.mark.asyncio
async def test_outbound_stream_is_single_use(env):
    response = await env.client.post(
        env.path + "/calls",
        json={"phone_number": "+919876543210", "request_id": str(uuid4())},
    )
    call_id = UUID(response.json()["id"])
    token = env.service.token(call_id, "media")
    with pytest.raises(HTTPException):
        await env.service.admit(start("wrong"), call_id=call_id, token=token)
    call, _ = await env.service.admit(
        start(response.json()["provider_call_sid"]), call_id=call_id, token=token
    )
    assert call.stream_claimed
    with pytest.raises(HTTPException):
        await env.service.admit(
            start(call.provider_call_sid), call_id=call_id, token=token
        )


@pytest.mark.parametrize(
    "change",
    [
        {"account_sid": "other"},
        {"call_sid": ""},
        {"call_sid": None},
        {"stream_sid": None},
        {"media_format": {"encoding": "mulaw", "sample_rate": 8000}},
        {"media_format": {"encoding": "audio/x-raw", "sample_rate": 44100}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_media_format_rejected(env, change):
    with pytest.raises(ValueError):
        env.service.validate_start(wire_start(start(**change)))


@pytest.mark.asyncio
async def test_inbound_admitted_clinic_can_book_only_after_confirmation(env):
    from app.ai.clinic.flow import handlers

    config = await route(env)
    env.provider.states["incoming"] = {
        "Sid": "incoming",
        "From": "+919876543210",
        "To": config.phone_number,
        "Status": "in-progress",
    }
    call, runtime = await env.service.admit(
        start(),
        route_id=config.id,
        token=env.service.inbound_media_token(config, "incoming"),
    )

    async def queue(_):
        pass

    fm = SimpleNamespace(
        state={
            "bot": runtime,
            "facts": runtime.clinic_facts,
            "session_factory": env.sessions,
            "caller_turn": 1,
        },
        worker=SimpleNamespace(queue_frame=queue),
        current_node="greet_identify",
    )
    result, _ = await handlers.save_caller(
        {"name": "Patient", "phone": "9876543210"}, fm
    )
    assert result["ok"]
    await handlers.choose_book({}, fm)
    doctor_id = runtime.clinic_facts["doctors"][0]["id"]
    selected, _ = await handlers.select_slot(
        {
            "doctor_id": doctor_id,
            "start_time": env.slot.isoformat(),
            "reason": "Checkup",
        },
        fm,
    )
    assert selected["ok"]
    fm.current_node = "confirm_booking"
    denied, _ = await handlers.confirm_booking({}, fm)
    assert not denied["ok"]
    fm.state.update(caller_turn=2, last_caller_text="Yes, book it")
    booked, _ = await handlers.confirm_booking({}, fm)
    assert booked["ok"], booked
    async with env.sessions() as db:
        appointment = await db.scalar(
            select(ClinicAppointment).where(ClinicAppointment.bot_id == call.bot_id)
        )
        assert (
            appointment.patient_name == "Patient"
            and appointment.patient_phone == "9876543210"
        )


@pytest.mark.asyncio
async def test_webrtc_endpoint_remains_available(env):
    seen = []

    async def offer(bot, user_id, data):
        seen.append(bot.service_type)
        return {"sdp": "answer", "type": "answer", "pc_id": "peer"}

    env.app.state.voice = SimpleNamespace(offer=offer)
    url = env.path.removesuffix("/phone") + "/voice/offer"
    result = await env.client.post(url, json={"sdp": "test-offer", "type": "offer"})
    assert result.status_code == 200 and seen == ["clinic"]


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("rate", [8000])
@pytest.mark.asyncio
async def test_real_clinic_phone_pipeline_construction(
    env, direction, rate, monkeypatch
):
    from app.ai.clinic.pipeline import build_pipeline
    from app.ai.common.twilio import build_transport

    async with env.sessions() as db:
        bot = await db.get(Bot, env.bot.id)
        runtime = await bots.runtime_bot(db, bot)
    transport = build_transport(
        SimpleNamespace(), start(media_format={"sample_rate": rate})
    )
    runner, worker = build_pipeline(
        None,
        runtime,
        env.settings,
        transport=transport,
        output_sample_rate=rate,
        session_factory=env.sessions,
        direction=direction,
    )
    flow = worker.app_resources["clinic_flow"]
    nodes = []

    async def initialize(node):
        nodes.append(node)

    monkeypatch.setattr(flow, "initialize", initialize)
    # Pipecat's actual connection event triggers exactly one greeting/flow start.
    await transport._call_event_handler("on_client_connected", None)
    await transport._call_event_handler("on_client_connected", None)
    await transport.cleanup()
    assert len(nodes) == 1
    assert (
        "outbound clinic phone call" in nodes[0]["task_messages"][0]["content"]
    ) == (direction == "outbound")
    assert flow.state["session_factory"] is env.sessions
    await runner.cancel()


@pytest.mark.asyncio
async def test_migration_upgrade_and_downgrade(env):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    spec = importlib.util.spec_from_file_location(
        "clinic_phone_migration", "app/db/migrations/versions/0013_clinic_phone.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with env.sessions() as db:
        engine = db.bind

    def roundtrip(conn):
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert "clinic_phone_calls" not in inspect(conn).get_table_names()
        migration.upgrade()
        assert "clinic_phone_routes" in inspect(conn).get_table_names()

    async with engine.begin() as conn:
        await conn.run_sync(roundtrip)


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.asyncio
async def test_media_endpoint_dispatches_to_clinic_pipeline(
    env, direction, monkeypatch
):
    seen = []
    if direction == "inbound":
        config = await route(env)
        env.provider.states["incoming"] = {
            "Sid": "incoming",
            "From": "+919876543210",
            "To": config.phone_number,
            "Status": "in-progress",
        }
        kwargs = {
            "route_id": config.id,
            "token": env.service.inbound_media_token(config, "incoming"),
            "call_sid": "incoming",
        }
        event = wire_start(start())
    else:
        response = await env.client.post(
            env.path + "/calls",
            json={"phone_number": "+919876543210", "request_id": str(uuid4())},
        )
        call_id = UUID(response.json()["id"])
        kwargs = {"call_id": call_id, "token": env.service.token(call_id, "media")}
        event = wire_start(start(response.json()["provider_call_sid"]))

    class Socket:
        app = env.app
        client_state = SimpleNamespace(name="CONNECTED")
        accepted = False
        close_code = None

        async def accept(self):
            self.accepted = True

        async def receive_json(self):
            return event

        async def close(self, code):
            self.close_code = code

    class Runner:
        async def add_workers(self, worker):
            pass

        async def run(self):
            pass

        async def cancel(self):
            pass

    def build(connection, bot, settings, **options):
        seen.append((bot.id, options["direction"], options["session_factory"]))
        return Runner(), object()

    monkeypatch.setattr("app.ai.clinic.pipeline.build_pipeline", build)
    monkeypatch.setattr("app.ai.common.twilio.build_transport", lambda *args: object())
    socket = Socket()
    await api.media(socket, **{**kwargs, "token": "invalid"})
    assert not socket.accepted and socket.close_code == 1008 and seen == []
    socket = Socket()
    await api.media(socket, **kwargs)
    assert socket.accepted and socket.close_code == 1000
    assert seen == [(env.bot.id, direction, env.sessions)]
    assert not env.service.live


def test_telephony_tokens_redacted_from_uvicorn_logs():
    import logging

    from app.business.meeting_voice import AudioGrantLogFilter

    record = logging.LogRecord(
        "test",
        20,
        "",
        0,
        "WebSocket %s",
        ("/api/v1/clinic-phone/twilio/inbound/route?token=secret&sample-rate=16000",),
        None,
    )
    AudioGrantLogFilter().filter(record)
    assert "secret" not in record.getMessage() and "[redacted]" in record.getMessage()


@pytest.mark.asyncio
async def test_phone_capabilities_follow_membership_and_operator_role(env):
    response = await env.client.get(env.path + "/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "can_view_route": True,
        "can_manage_route": True,
        "can_call": True,
        "inbound_ready": True,
        "outbound_ready": True,
    }
    env.settings.clinic_operator_user_ids = []
    result = (await env.client.get(env.path + "/capabilities")).json()
    assert result["can_call"] and not result["can_manage_route"]
    env.actor["user"] = env.member
    result = (await env.client.get(env.path + "/capabilities")).json()
    assert (
        not result["can_view_route"]
        and not result["can_call"]
        and not result["can_manage_route"]
    )
    env.settings.twilio_phone_number = ""
    result = (await env.client.get(env.path + "/capabilities")).json()
    assert result["inbound_ready"] and not result["outbound_ready"]
    env.settings.sarvam_api_key = type(env.settings.sarvam_api_key)("")
    result = (await env.client.get(env.path + "/capabilities")).json()
    assert not result["inbound_ready"] and not result["outbound_ready"]
    env.actor["user"] = env.stranger
    assert (await env.client.get(env.path + "/capabilities")).status_code == 404


@pytest.mark.asyncio
async def test_context_text_alone_updates_clinic_runtime_and_clears_old_facts(env):
    context = """# Context Clinic

## Clinic details
- Address: Pune
- Phone: +919876543210
- Timezone: Asia/Kolkata
- Hours: Monday–Saturday, 9 AM–6 PM
- Appointment length: 30 minutes
- Fees: ₹500 per consultation
- Parking: Available outside
- Booking policy: Cancel at least 24 hours in advance

## Doctors
| Doctor | Specialty |
| --- | --- |
| Dr. Mehta | General medicine |
"""
    path = f"/api/v1/organizations/{env.org.id}/bots/{env.bot.id}"
    payload = {"name": env.bot.name, "language": "en-IN", "company_context": context}
    response = await env.client.put(path, json=payload)
    assert response.status_code == 200
    assert response.json()["company_context"] == context
    async with env.sessions() as db:
        bot = await db.get(Bot, env.bot.id)
        runtime = await bots.runtime_bot(db, bot)
        facts = runtime.clinic_facts
        assert runtime.company_context == context
        assert facts["clinic_name"] == "Context Clinic"
        assert facts["address"] == "Pune"
        assert facts["phone"] == "+919876543210"
        assert facts["fees"] == "₹500 per consultation"
        assert facts["parking"] == "Available outside"
        assert facts["booking"] == "Cancel at least 24 hours in advance"
        assert facts["booking_ready"] is True
        assert [(d["name"], d["specialty"]) for d in facts["doctors"]] == [
            ("Dr. Mehta", "General medicine")
        ]

    # Clearing context must not reuse the previous form's facts or cached doctors.
    response = await env.client.put(path, json={**payload, "company_context": ""})
    assert response.status_code == 200
    async with env.sessions() as db:
        runtime = await bots.runtime_bot(db, await db.get(Bot, env.bot.id))
        assert runtime.company_context == ""
        assert runtime.clinic_facts["clinic_name"] is None
        assert runtime.clinic_facts["doctors"] == []
        assert runtime.clinic_facts["booking_ready"] is False


@pytest.mark.asyncio
async def test_twilio_answer_xml_and_real_call_sid_binding(env):
    from xml.etree.ElementTree import fromstring

    async def accepted(number, answer, callback, call_id):
        env.provider.dials.append((number, answer, callback, call_id))
        return {"Sid": "actual-call", "Status": "queued"}

    env.provider.dial = accepted
    data = {"phone_number": "+919876543210", "request_id": str(uuid4())}
    result = await env.client.post(env.path + "/calls", json=data)
    assert result.status_code == 202
    assert (
        result.json()["status"] == "queued"
        and result.json()["provider_call_sid"] == "actual-call"
    )
    await env.client.post(env.path + "/calls", json=data)
    assert len(env.provider.dials) == 1
    answer = env.provider.dials[0][1]
    response = await env.client.post(answer, data={"CallSid": "actual-call"})
    assert response.status_code == 200
    xml = fromstring(response.text)
    assert (
        xml.find("Connect/Stream")
        .get("url")
        .startswith("wss://clinic.example.com/api/v1/clinic-phone/twilio/calls/")
    )
    assert "?" not in xml.find("Connect/Stream").get("url")
    listing = await env.client.get(env.path + "/calls")
    assert listing.json()[0]["provider_call_sid"] == "actual-call"
    assert listing.json()[0]["status"] == "in-progress"
    assert (
        await env.client.post(answer.split("?")[0], data={"CallSid": "fake"})
    ).status_code == 403
    assert (await env.client.post(answer, data={"CallSid": "other"})).status_code == 409


@pytest.mark.asyncio
async def test_twilio_inbound_answer_checks_number_and_binds_media_token(env):
    from urllib.parse import urlsplit
    from xml.etree.ElementTree import fromstring

    config = await route(env)
    answer = env.service.inbound_url(config)
    env.provider.states["incoming"] = {
        "Sid": "incoming",
        "Status": "in-progress",
        "From": "+919876543210",
        "To": "+918012345679",
    }
    assert (
        await env.client.post(answer, data={"CallSid": "incoming"})
    ).status_code == 403
    env.provider.states["incoming"]["To"] = config.phone_number
    response = await env.client.post(answer, data={"CallSid": "incoming"})
    assert response.status_code == 200
    stream = fromstring(response.text).find("Connect/Stream").get("url")
    assert not urlsplit(stream).query
    assert urlsplit(stream).path.endswith(
        "/media/incoming/" + env.service.inbound_media_token(config, "incoming")
    )
    with pytest.raises(HTTPException):
        env.service.verify(
            config.token_version, "inbound-media:other", stream.rsplit("/", 1)[-1]
        )
    # Saving a route rotates both its answer grant and its media grants.
    await route(env)
    assert (
        await env.client.post(answer, data={"CallSid": "incoming"})
    ).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/clinic-phone/twilio/calls/call/media/",
        "/api/v1/clinic-phone/twilio/inbound/route/media/CAcall/",
        "/api/v1/outreach/twilio/call/media/",
    ],
)
def test_twilio_media_path_grants_redacted_from_uvicorn_logs(path):
    import logging

    from app.business.meeting_voice import AudioGrantLogFilter

    grant = "a" * 64
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        0,
        "WebSocket %s [accepted]",
        (path + grant,),
        None,
    )
    assert AudioGrantLogFilter().filter(record)
    assert grant not in record.getMessage()
    assert "[redacted]" in record.getMessage()


@pytest.mark.asyncio
async def test_restricted_twilio_trial_shows_actionable_failure_without_retry(env):
    request = httpx.Request("POST", "https://api.twilio.com/Calls.json")
    env.provider.error = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(
            400,
            request=request,
            json={
                "code": 0,
                "message": "Invalid or disallowed parameters provided - trial accounts have limited parameter access, upgrade your account to unlock full functionality",
            },
        ),
    )
    data = {"phone_number": "+919876543210", "request_id": str(uuid4())}
    result = await env.client.post(env.path + "/calls", json=data)
    assert result.status_code == 202
    assert result.json()["status"] == "failed"
    assert "trial restrictions" in result.json()["error"]
    assert "browser voice" in result.json()["error"]
    await env.client.post(env.path + "/calls", json=data)
    assert len(env.provider.dials) == 1

"""Exercise launch protections without providers, real accounts, or network calls."""

import asyncio
import re
from types import SimpleNamespace

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api import auth, health
from app.clients.database import create_database_engine
from app.core.launch_check import check_database, production_config
from app.db.session import get_db
from app.env import Settings
from app.types import ApiKeyCreate, BotConfig, Credentials, OrgCreate, Register
from app.utils.auth_capacity import auth_work_slot
from app.utils.http_protection import HTTPProtectionMiddleware, validation_error
from app.utils.rate_limit import RateLimiter, request_policy


def settings(**values):
    config = {
        "jwt_secret": "launch-test-random-secret-1234567890",
        "database_url": "postgresql+asyncpg://unused:unused@localhost/unused",
        **values,
    }
    return Settings(_env_file=None, **config)


def protected_app(config=None, *, clock=None):
    config = config or settings()
    app = FastAPI()
    limiter = RateLimiter(config, **({"clock": clock} if clock else {}))
    app.state.rate_limiter = limiter
    app.add_middleware(HTTPProtectionMiddleware, settings=config, limiter=limiter)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://frontend.example"],
        expose_headers=["Retry-After", "X-Request-ID"],
    )

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def route(path: str, request: Request):
        if path == "crash":
            raise RuntimeError("private-provider-key")
        return {"size": len(await request.body())}

    return app


def test_token_bucket_boundary_and_recovery():
    now = [0.0]
    limiter = RateLimiter(settings(login_requests_per_minute=2), clock=lambda: now[0])
    limiter.check("login", "peer")
    limiter.check("login", "peer")
    with pytest.raises(HTTPException) as error:
        limiter.check("login", "peer")
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "30"
    now[0] = 29.99
    with pytest.raises(HTTPException):
        limiter.check("login", "peer")
    now[0] = 30
    limiter.check("login", "peer")
    limiter.check("login", "another-peer")


def test_limiter_state_is_bounded_and_expires():
    now = [0.0]
    limiter = RateLimiter(settings(rate_limit_max_buckets=100), clock=lambda: now[0])
    for index in range(100):
        limiter.check("api", f"peer-{index}")
    with pytest.raises(HTTPException):
        limiter.check("api", "new-peer")
    assert len(limiter.buckets) == 100
    now[0] = 61
    limiter.check("api", "new-peer")
    assert len(limiter.buckets) == 1


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/auth/login/", "login"),
        ("POST", "/api/v1/auth/register", "register"),
        ("POST", "/api/v1/auth/demo", "demo"),
        ("POST", "/api/v1/public/bots/id/session-token", "grant"),
        ("POST", "/api/v1/public/bots/id/webrtc/offer", "voice"),
        ("POST", "/api/v1/organizations/id/meetings/id/join", "voice"),
        ("POST", "/api/v1/organizations/id/bots/id/phone/calls", "phone"),
        ("POST", "/api/v1/organizations/id/outreach/campaigns/id/run", "campaign"),
        ("POST", "/api/v1/organizations/id/outreach/campaigns/id/stop", "cleanup"),
        ("DELETE", "/api/v1/public/bots/id/webrtc/session", "cleanup"),
    ],
)
def test_expensive_actions_and_cleanup_have_distinct_budgets(method, path, expected):
    assert request_policy(method, path) == expected


@pytest.mark.asyncio
async def test_login_limit_cannot_be_reset_by_spoofing_forwarded_headers():
    app = protected_app(settings(login_requests_per_minute=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/api/v1/auth/login", json={})
        limited = await client.post(
            "/api/v1/auth/login",
            json={},
            headers={
                "X-Forwarded-For": "203.0.113.2",
                "Origin": "https://frontend.example",
            },
        )
    assert first.status_code == 200
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.headers["access-control-allow-origin"] == "https://frontend.example"
    assert "Retry-After" in limited.headers["access-control-expose-headers"]


@pytest.mark.asyncio
async def test_provider_callbacks_and_cleanup_survive_exhausted_read_budget():
    app = protected_app(settings(api_requests_per_minute=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/anything")).status_code == 200
        assert (await client.get("/api/v1/anything")).status_code == 429
        for _ in range(3):
            assert (
                await client.post("/api/v1/outreach/twilio/id/status")
            ).status_code == 200
            assert (
                await client.post("/api/v1/clinic-phone/twilio/calls/id/status")
            ).status_code == 200
        assert (
            await client.delete("/api/v1/public/bots/id/webrtc/session")
        ).status_code == 200
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_body_limits_cover_declared_and_chunked_requests():
    app = protected_app(settings(max_request_body_bytes=1024))

    async def chunks():
        yield b"x" * 600
        yield b"y" * 600

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/write", content=b"x" * 1025)).status_code == 413
        assert (await client.post("/write", content=chunks())).status_code == 413
        accepted = await client.post("/write", content=b"x" * 1024)
        assert accepted.json()["size"] == 1024
        assert (
            await client.post(
                "/write", content=b"abc", headers={"Content-Length": "bad"}
            )
        ).status_code == 400
        assert (
            await client.post("/write", content=b"abc", headers={"Content-Length": "9"})
        ).status_code == 400


@pytest.mark.asyncio
async def test_errors_and_response_headers_do_not_expose_provider_details():
    app = protected_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/crash", headers={"X-Request-ID": "attacker-controlled"}
        )
        again = await client.get("/api/v1/me")
    assert response.status_code == 500
    assert "private-provider-key" not in response.text
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["x-request-id"])
    assert response.headers["x-request-id"] != again.headers["x-request-id"]
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert again.headers["cache-control"] == "no-store"
    assert again.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_slow_request_body_times_out_before_dispatch():
    config = settings().model_copy(update={"request_body_timeout_seconds": 0.001})
    called = False
    messages = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        await asyncio.sleep(0.1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = HTTPProtectionMiddleware(
        downstream, settings=config, limiter=RateLimiter(config)
    )
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/write",
            "headers": [],
            "client": ("127.0.0.1", 1),
        },
        receive,
        send,
    )
    assert not called
    assert messages[0]["status"] == 408


@pytest.mark.asyncio
async def test_websocket_streams_are_not_buffered_or_http_throttled():
    observed = []

    async def downstream(scope, receive, send):
        observed.append(scope["type"])

    middleware = HTTPProtectionMiddleware(
        downstream, settings=settings(), limiter=RateLimiter(settings())
    )
    await middleware({"type": "websocket", "path": "/meeting-audio/id"}, None, None)
    assert observed == ["websocket"]


def production_settings(**values):
    config = {
        "app_environment": "production",
        "public_backend_url": "https://api.example.com",
        "cors_origins": ["https://app.example.com"],
    }
    config.update(values)
    return settings(**config)


def test_production_disables_demo_by_default_and_keeps_it_configurable():
    assert settings().demo_allowed
    assert not production_settings().demo_allowed
    assert production_settings(demo_enabled=True).demo_allowed


@pytest.mark.parametrize(
    "values",
    [
        {"database_url": "sqlite+aiosqlite:///local.db"},
        {"cors_origins": ["http://app.example.com"]},
        {"cors_origins": []},
        {"public_backend_url": ""},
        {"jwt_secret": "replace-with-a-random-secret-at-least-32-characters"},
        {"login_requests_per_minute": 0},
    ],
)
def test_invalid_production_configuration_fails_at_startup(values):
    with pytest.raises(ValidationError):
        production_settings(**values)


@pytest.mark.parametrize(
    "model, values",
    [
        (Register, {"email": "owner@example.com", "password": "not-a-real-password"}),
        (OrgCreate, {}),
        (BotConfig, {}),
        (ApiKeyCreate, {}),
    ],
)
def test_whitespace_only_names_are_rejected(model, values):
    with pytest.raises(ValidationError):
        model(name="   ", **values)


def test_display_name_normalization_never_trims_passwords():
    account = Register(
        name="  Owner  ", email="Owner@example.com", password="  keep spaces  "
    )
    assert account.name == "Owner"
    assert account.password == "  keep spaces  "


@pytest.mark.asyncio
async def test_auth_options_and_disabled_routes_match(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: settings(registration_enabled=False, demo_enabled=False),
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/auth/options")).json() == {
            "registration_enabled": False,
            "demo_enabled": False,
        }
        assert (await client.post("/auth/demo")).status_code == 403
        response = await client.post(
            "/auth/register",
            json={
                "name": "Owner",
                "email": "owner@example.com",
                "password": "test-password-123",
            },
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_readiness_checks_the_database_without_revealing_connection_errors():
    app = FastAPI()
    app.include_router(health.router)

    class Database:
        async def execute(self, query):
            raise OperationalError("private-database-host", {}, Exception("secret"))

    app.dependency_overrides[get_db] = Database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        response = await client.get("/ready")
    assert response.status_code == 503
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_auth_hash_slot_is_released_after_failure_and_cancellation():
    slots = asyncio.Semaphore(1)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_slots=slots))
    )
    with pytest.raises(ValueError):
        async with auth_work_slot(request):
            raise ValueError("simulated failure")
    with pytest.raises(asyncio.CancelledError):
        async with auth_work_slot(request):
            raise asyncio.CancelledError
    assert not slots.locked()


@pytest.mark.asyncio
async def test_public_mount_errors_keep_the_public_cors_policy():
    config = settings(voice_tokens_per_minute=1)
    limiter = RateLimiter(config)
    root = FastAPI()
    root.add_middleware(
        HTTPProtectionMiddleware, settings=config, limiter=limiter, skip_public=True
    )
    public = FastAPI()
    public.add_middleware(HTTPProtectionMiddleware, settings=config, limiter=limiter)
    public.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        expose_headers=["Retry-After", "X-Request-ID"],
    )

    @public.post("/bots/id/session-token")
    async def grant():
        return {"ok": True}

    root.mount("/api/v1/public", public)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root), base_url="http://test"
    ) as client:
        headers = {"Origin": "https://customer.example"}
        assert (
            await client.post("/api/v1/public/bots/id/session-token", headers=headers)
        ).status_code == 200
        blocked = await client.post(
            "/api/v1/public/bots/id/session-token", headers=headers
        )
    assert blocked.status_code == 429
    assert blocked.headers["access-control-allow-origin"] == "*"
    assert "x-request-id" in blocked.headers


@pytest.mark.asyncio
async def test_validation_errors_do_not_echo_the_submitted_password():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error)

    @app.post("/validate")
    async def validate(data: Credentials):
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/validate", json={"email": "owner@example.com", "password": "secret!"}
        )
    assert response.status_code == 422
    assert "secret!" not in response.text
    assert "input" not in response.json()["detail"][0]


def test_production_preflight_does_not_mutate_runtime_settings():
    config = settings(
        app_environment="development",
        jwt_secret="a-generated-production-secret-1234567890",
        cors_origins=["https://dashboard.example"],
        public_backend_url="https://api.example",
    )
    checked = production_config(config)
    assert checked.app_environment == "production"
    assert not checked.demo_allowed
    assert config.app_environment == "development"
    assert config.demo_allowed


@pytest.mark.asyncio
async def test_preflight_requires_current_migration_without_writing(tmp_path):
    config = settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'preflight.db'}")
    engine = create_database_engine(config)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE alembic_version (version_num TEXT)")
            )
            await connection.execute(
                text("INSERT INTO alembic_version VALUES ('outdated')")
            )
        assert not await check_database(config)
        expected = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            for revision in expected:
                await connection.execute(
                    text("INSERT INTO alembic_version VALUES (:revision)"),
                    {"revision": revision},
                )
        assert await check_database(config)
        async with engine.connect() as connection:
            actual = list(
                (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).scalars()
            )
        assert set(actual) == set(expected)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_failure", [False, True])
async def test_lifespan_closes_resources_even_after_partial_startup(
    monkeypatch, startup_failure
):
    from app import main

    events = []

    def service(label):
        class Stub:
            def __init__(self, *args):
                self.provider = object()

            def start(self):
                if label == "outreach" and startup_failure:
                    raise RuntimeError("startup failed")

            async def close(self):
                events.append(label)

        return Stub

    for name, label in [
        ("MeetingVoiceService", "meeting"),
        ("TurnCredentials", "turn"),
        ("VoiceSessions", "voice"),
        ("OutreachService", "outreach"),
        ("ClinicPhoneService", "clinic"),
    ]:
        monkeypatch.setattr(main, name, service(label))

    async def dispose():
        events.append("database")

    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(auth_max_concurrency=4)
    )
    monkeypatch.setattr(main, "public_app", FastAPI())
    monkeypatch.setattr(main, "start_meeting_scheduler", lambda service: None)
    app = FastAPI()
    if startup_failure:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with main.lifespan(app):
                pytest.fail("Failed startup must not admit requests")
        assert events == ["outreach", "voice", "turn", "meeting", "database"]
    else:
        async with main.lifespan(app):
            assert events == []
        assert events == ["clinic", "outreach", "voice", "turn", "meeting", "database"]

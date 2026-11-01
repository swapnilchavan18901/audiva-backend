"""Exercise passwordless demo sessions through the normal authenticated API."""

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth, organizations
from app.db.models import Base, BotTemplate, ClinicDoctor
from app.db.session import get_db
from app.env import Settings
from app.utils import security


@pytest_asyncio.fixture
async def demo_api(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None, jwt_secret="demo-test-secret-at-least-32-characters"
    )
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(organizations.router)

    async def database():
        async with sessions() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, sessions
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_templates", [True, False])
async def test_demo_signin_and_workspace_isolation(demo_api, with_templates):
    client, sessions = demo_api
    if with_templates:
        async with sessions() as db:
            for slug, feature, active in [
                ("clinic", "clinic", True),
                ("general", "conversation", True),
                ("sales", "conversation", True),
                ("retired", "clinic", False),
            ]:
                db.add(
                    BotTemplate(
                        slug=slug,
                        name=slug,
                        service_type=feature,
                        description="Demo template",
                        system_prompt="Assist the caller.",
                        is_active=active,
                    )
                )
            await db.commit()

    first = await client.post("/auth/demo")
    second = await client.post("/auth/demo")
    assert first.status_code == second.status_code == 201
    assert first.json()["user"]["id"] != second.json()["user"]["id"]
    first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}
    me = await client.get("/auth/me", headers=first_headers)
    assert me.status_code == 200
    assert me.json()["name"] == "Demo Visitor"
    orgs = await client.get("/organizations", headers=first_headers)
    assert len(orgs.json()) == 1
    org_id = orgs.json()[0]["id"]
    path = f"/organizations/{org_id}/workspace"
    workspace = await client.get(path, headers=first_headers)
    assert workspace.status_code == 200
    assert workspace.json()["can_manage"] is True
    assert len(workspace.json()["bots"]) == (2 if with_templates else 0)
    assert (await client.get(path, headers=second_headers)).status_code == 404
    assert (await client.get(path)).status_code == 401
    if with_templates:
        async with sessions() as db:
            doctors = (await db.scalars(select(ClinicDoctor))).all()
            assert len(doctors) == 2
            assert all(d.name == "Dr. Alex Demo" for d in doctors)


@pytest.mark.asyncio
async def test_regular_auth_still_works_and_real_workspace_is_private(demo_api):
    client, _ = demo_api
    credentials = {"email": "owner@example.com", "password": "test-password-123"}
    registered = await client.post(
        "/auth/register", json={**credentials, "name": "Owner"}
    )
    assert registered.status_code == 201
    login = await client.post("/auth/login", json=credentials)
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    orgs = await client.get("/organizations", headers=headers)
    demo = await client.post("/auth/demo")
    demo_headers = {"Authorization": f"Bearer {demo.json()['access_token']}"}
    path = f"/organizations/{orgs.json()[0]['id']}/workspace"
    assert (await client.get(path, headers=demo_headers)).status_code == 404
    assert (await client.get(path, headers=headers)).status_code == 200
    invalid = await client.post(
        "/auth/login", json={**credentials, "password": "wrong-password"}
    )
    assert invalid.status_code == 401

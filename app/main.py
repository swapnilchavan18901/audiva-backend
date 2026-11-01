from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.benchmark import configure_logging as configure_benchmark_logging
from app.ai.sessions import VoiceSessions
from app.api import (
    auth,
    bots,
    clinic,
    clinic_phone,
    health,
    meetings,
    organizations,
    outreach,
    platform,
    public,
)
from app.business.clinic_phone import ClinicPhoneService
from app.business.meeting_scheduler import start as start_meeting_scheduler
from app.business.meeting_voice import MeetingVoiceService
from app.business.outreach_worker import OutreachService
from app.business.turn_credentials import TurnCredentials
from app.constants.constants import PRODUCT_NAME
from app.db.session import SessionFactory, engine
from app.env import get_settings
from app.utils.cors import DashboardCORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_benchmark_logging()
    settings = get_settings()
    app.state.meeting_voice = MeetingVoiceService(settings)
    app.state.turn = TurnCredentials(settings, SessionFactory)
    app.state.turn.start()
    app.state.voice = VoiceSessions(settings, app.state.turn)
    app.state.outreach = OutreachService(settings)
    app.state.outreach.start()
    app.state.clinic_phone = ClinicPhoneService(settings, app.state.outreach.provider)
    app.state.clinic_phone.start()
    public_app.state.voice = app.state.voice
    public_app.state.turn = app.state.turn
    start_meeting_scheduler(app.state.meeting_voice)
    yield
    await app.state.clinic_phone.close()
    await app.state.outreach.close()
    await app.state.meeting_voice.close()
    await app.state.voice.close()
    await app.state.turn.close()
    await engine.dispose()


app = FastAPI(title=f"{PRODUCT_NAME} API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    DashboardCORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(health.router)
for router in (
    auth.router,
    organizations.router,
    bots.router,
    clinic.router,
    platform.router,
    meetings.router,
    outreach.router,
    outreach.provider_router,
    clinic_phone.router,
    clinic_phone.provider_router,
):
    app.include_router(router, prefix="/api/v1")


# Browser integration uses short-lived bearer grants, never cookies or the customer API key.
public_app = FastAPI(title=f"{PRODUCT_NAME} Public Voice API", version="0.2.0")
public_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
public_app.include_router(public.router)
app.mount("/api/v1/public", public_app)

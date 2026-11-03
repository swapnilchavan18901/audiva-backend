import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
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
from app.clients.twilio_logging import configure_logging as configure_twilio_logging
from app.constants.constants import PRODUCT_NAME
from app.db.session import SessionFactory, engine
from app.env import get_settings
from app.utils.cors import DashboardCORSMiddleware
from app.utils.http_protection import HTTPProtectionMiddleware, validation_error
from app.utils.rate_limit import RateLimiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_benchmark_logging()
    configure_twilio_logging()
    settings = get_settings()
    app.state.auth_slots = asyncio.Semaphore(settings.auth_max_concurrency)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(engine.dispose)
        app.state.meeting_voice = MeetingVoiceService(settings)
        cleanup.push_async_callback(app.state.meeting_voice.close)
        app.state.turn = TurnCredentials(settings, SessionFactory)
        cleanup.push_async_callback(app.state.turn.close)
        app.state.turn.start()
        app.state.voice = VoiceSessions(settings, app.state.turn)
        cleanup.push_async_callback(app.state.voice.close)
        app.state.outreach = OutreachService(settings)
        cleanup.push_async_callback(app.state.outreach.close)
        app.state.outreach.start()
        app.state.clinic_phone = ClinicPhoneService(
            settings, app.state.outreach.provider
        )
        cleanup.push_async_callback(app.state.clinic_phone.close)
        app.state.clinic_phone.start()
        public_app.state.voice = app.state.voice
        public_app.state.turn = app.state.turn
        start_meeting_scheduler(app.state.meeting_voice)
        yield


settings = get_settings()
app = FastAPI(
    title=f"{PRODUCT_NAME} API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.app_environment == "production" else "/docs",
    redoc_url=None if settings.app_environment == "production" else "/redoc",
    openapi_url=None if settings.app_environment == "production" else "/openapi.json",
)
limiter = RateLimiter(settings)


app.add_exception_handler(RequestValidationError, validation_error)
app.state.rate_limiter = limiter
app.add_middleware(
    HTTPProtectionMiddleware, settings=settings, limiter=limiter, skip_public=True
)
app.add_middleware(
    DashboardCORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Retry-After", "X-Request-ID"],
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
public_app.add_exception_handler(RequestValidationError, validation_error)
public_app.state.rate_limiter = limiter
public_app.add_middleware(HTTPProtectionMiddleware, settings=settings, limiter=limiter)
public_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    expose_headers=["Retry-After", "X-Request-ID"],
)
public_app.include_router(public.router)
app.mount("/api/v1/public", public_app)

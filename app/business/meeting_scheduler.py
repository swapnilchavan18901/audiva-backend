"""Small independent loops for scheduling, provider status, memory and retention."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select

from app.clients import create_google_client
from app.constants import constants
from app.constants.constants import TERMINAL
from app.db.models import Meeting, MeetingCapture, MeetingGoogleConnection, utcnow
from app.db.session import SessionFactory
from app.integrations.google_meet_calendar import sync

logger = logging.getLogger(__name__)


async def repeat(action, seconds):
    while True:
        try:
            await action()
        except Exception:  # noqa: BLE001 - isolate failed background work and retry
            # Do not log provider bodies, transcript contents or credentials.
            logger.warning(
                "Meeting background operation %s failed; retrying", action.__name__
            )
        await asyncio.sleep(seconds)


def start(service):
    async def calendar():
        async with SessionFactory() as db, create_google_client() as client:
            connection = await db.scalar(select(MeetingGoogleConnection))
            if connection and connection.status == "connected":
                await sync(db, client, service.settings)

    async def dispatch():
        if not service.ready:
            return
        async with SessionFactory() as db:
            now = utcnow()
            ids = list(
                await db.scalars(
                    select(Meeting.id).where(
                        Meeting.auto_join.is_(True),
                        Meeting.organization_id.is_not(None),
                        Meeting.status == "scheduled",
                        Meeting.next_starts_at <= now,
                        Meeting.next_starts_at >= now - timedelta(minutes=10),
                        Meeting.next_ends_at > now,
                        ~select(MeetingCapture.id)
                        .where(
                            MeetingCapture.meeting_id == Meeting.id,
                            MeetingCapture.provider_occurrence_id
                            == Meeting.next_occurrence_id,
                        )
                        .exists(),
                    )
                )
            )
        for meeting_id in ids:
            try:
                async with SessionFactory() as db:
                    meeting = await db.get(Meeting, meeting_id)
                    await service.start(db, meeting, scheduled=True)
            except Exception:  # noqa: BLE001 - isolate failed background work and retry
                logger.warning("Automatic join failed for meeting %s", meeting_id)

    async def statuses():
        if not service.ready:
            return
        async with SessionFactory() as db:
            ids = list(
                await db.scalars(
                    select(MeetingCapture.id).where(
                        MeetingCapture.provider_bot_id.is_not(None),
                        MeetingCapture.provider_status.not_in(TERMINAL),
                    )
                )
            )
        for capture_id in ids:
            try:
                async with SessionFactory() as db:
                    capture = await db.get(MeetingCapture, capture_id)
                    meeting = await db.get(Meeting, capture.meeting_id)
                    await service.status(db, meeting, capture=capture)
            except Exception:  # noqa: BLE001 - isolate failed background work and retry
                logger.warning("Meeting status refresh failed for %s", capture_id)

    async def memories():
        async with SessionFactory() as db:
            ids = list(
                await db.scalars(
                    select(MeetingCapture.id).where(
                        MeetingCapture.provider_status.in_(TERMINAL),
                        func.json_array_length(MeetingCapture.transcript) > 0,
                    )
                )
            )
        for capture_id in ids:
            if capture_id in service.live:
                continue
            try:
                await service.memory.update(capture_id)
            except Exception:  # noqa: BLE001 - isolate failed background work and retry
                async with SessionFactory() as db:
                    capture = await db.get(MeetingCapture, capture_id)
                    if capture:
                        capture.memory_status = "failed"
                        capture.memory_error = (
                            "Memory update failed; automatic retry is pending."
                        )
                        await db.commit()

    jobs = [(service.memory.cleanup, constants.MEETING_RETENTION_CLEANUP_SECONDS)]
    settings = service.settings
    if (
        settings.google_client_id
        and settings.meeting_bot_email
        and settings.meeting_token_key.get_secret_value()
    ):
        jobs.append((calendar, constants.MEETING_CALENDAR_SYNC_SECONDS))
    if service.ready:
        jobs.extend(((dispatch, 10), (statuses, 15)))
    if settings.sarvam_api_key.get_secret_value():
        jobs.append((memories, constants.MEETING_MEMORY_PROCESS_SECONDS))
    service.tasks = [
        asyncio.create_task(repeat(action, seconds)) for action, seconds in jobs
    ]

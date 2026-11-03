"""Clinic phone admission, durable dial attempts, and provider status reconciliation."""

import asyncio
import hashlib
import hmac
import logging
import re
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.business import bots
from app.business.call_status import (
    PROVIDER_CALL_STATUSES,
    TERMINAL_CALL_STATUSES,
    should_advance_call_status,
)
from app.clients.twilio import rejection_message
from app.clients.twilio_logging import logger as twilio_logger
from app.constants import constants
from app.db.models import Bot, ClinicPhoneCall, ClinicPhoneRoute, utcnow
from app.db.session import SessionFactory
from app.outreach_types import Contact

logger = logging.getLogger(__name__)


def network_phone(value):
    """Normalize provider numbers; API inputs still require E.164."""
    value = re.sub(r"[\s().-]", "", str(value))
    if not value.startswith("+"):
        if len(value) == 11 and value.startswith("0"):
            value = "+91" + value[1:]
        elif len(value) == 10:
            value = "+91" + value
        else:
            value = "+" + value
    return Contact(phone_number=value).phone_number


async def clinic_bot(db, org_id, bot_id, user_id, *, write=False):
    bot = await bots.get_bot(db, org_id, bot_id, user_id, write=write)
    template = await bots.active_template(db, bot.template_id)
    if template.service_type != "clinic":
        raise HTTPException(422, "Phone access requires a clinic bot")
    return bot


class ClinicPhoneService:
    def __init__(self, settings, provider, *, sessions=SessionFactory):
        # Share Twilio's account-wide request limiter with Outreach.
        self.settings, self.provider, self.sessions = settings, provider, sessions
        self.task = None
        self.live = set()
        self.closing = False

    def require_ready(self):
        if not all(
            (
                self.settings.public_backend_url,
                self.settings.twilio_account_sid,
                self.settings.twilio_auth_token.get_secret_value(),
                self.settings.sarvam_api_key.get_secret_value(),
            )
        ):
            raise HTTPException(503, "Configure Twilio, Sarvam and PUBLIC_BACKEND_URL")

    def token(self, identity, purpose):
        return hmac.new(
            self.settings.jwt_secret.get_secret_value().encode(),
            f"clinic-phone:{purpose}:{identity}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, identity, purpose, token):
        if not token.isascii() or not hmac.compare_digest(
            self.token(identity, purpose), token
        ):
            raise HTTPException(403, "Invalid clinic phone token")

    def url(self, path, identity, purpose, *, media=False):
        base = self.settings.public_backend_url.rstrip("/")
        if media:
            base = base.replace("https://", "wss://", 1)
        endpoint = f"{base}/api/v1/clinic-phone/twilio/{path}"
        grant = self.token(identity, purpose)
        return f"{endpoint}/{grant}" if media else f"{endpoint}?token={grant}"

    def inbound_url(self, route):
        return self.url(f"inbound/{route.id}/answer", route.token_version, "inbound")

    def inbound_media_token(self, route, sid):
        return self.token(route.token_version, f"inbound-media:{sid}")

    async def apply_status(self, call_id, sid, status):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise HTTPException(400, "Invalid call SID")
        if status not in PROVIDER_CALL_STATUSES:
            raise HTTPException(400, "Invalid call status")
        async with self.sessions() as db:
            call = await db.scalar(
                select(ClinicPhoneCall)
                .where(ClinicPhoneCall.id == call_id)
                .with_for_update()
            )
            if not call:
                raise HTTPException(404, "Call not found")
            if call.provider_call_sid and call.provider_call_sid != sid:
                raise HTTPException(409, "Call SID mismatch")
            if call.status in TERMINAL_CALL_STATUSES:
                return
            previous_status = call.status
            call.provider_call_sid = sid
            if should_advance_call_status(call.status, status):
                call.status, call.error = status, None
            if status in TERMINAL_CALL_STATUSES:
                call.finished_at = utcnow()
            await db.commit()
            if call.status != previous_status:
                twilio_logger.info(
                    "Twilio status changed service=%s call_id=%s sid=%s previous=%s status=%s",
                    "clinic_phone",
                    call_id,
                    sid,
                    previous_status,
                    call.status,
                )

    async def dial(self, db, bot, data):
        self.require_ready()
        if not self.settings.twilio_phone_number:
            raise HTTPException(
                503, "Configure TWILIO_PHONE_NUMBER before outbound calls"
            )
        previous = await db.scalar(
            select(ClinicPhoneCall).where(
                ClinicPhoneCall.bot_id == bot.id,
                ClinicPhoneCall.request_id == data.request_id,
            )
        )
        if previous:
            if previous.phone_number != data.phone_number:
                raise HTTPException(
                    409, "request_id already belongs to a different destination"
                )
            return previous
        # Validate the real clinic configuration before placing a phone call.
        await bots.runtime_bot(db, bot)
        call = ClinicPhoneCall(
            bot_id=bot.id,
            request_id=data.request_id,
            direction="outbound",
            phone_number=data.phone_number,
            status="dialing",
        )
        db.add(call)
        try:
            await db.commit()  # Deduplicate before the irreversible provider request.
        except IntegrityError:
            await db.rollback()
            previous = await db.scalar(
                select(ClinicPhoneCall).where(
                    ClinicPhoneCall.bot_id == bot.id,
                    ClinicPhoneCall.request_id == data.request_id,
                )
            )
            if previous and previous.phone_number == data.phone_number:
                return previous
            raise HTTPException(
                409, "Call request conflicts with an existing attempt"
            ) from None
        call_id = call.id
        answer = self.url(f"calls/{call_id}/answer", call_id, "answer")
        callback = self.url(f"calls/{call_id}/status", call_id, "status")
        try:
            result = await self.provider.dial(
                data.phone_number, answer, callback, call_id
            )
            await self.apply_status(call_id, result["Sid"], result["Status"])
        except Exception as exc:  # noqa: BLE001 - preserve uncertain external side effects
            rejected = (
                isinstance(exc, httpx.HTTPStatusError)
                and 400 <= exc.response.status_code < 500
            )
            twilio_logger.error(
                "Twilio dispatch failed service=%s call_id=%s error_type=%s outcome=%s",
                "clinic_phone",
                call_id,
                type(exc).__name__,
                "rejected"
                if rejected
                else "unknown; awaiting callback or reconciliation",
            )
            # Never overwrite a successful concurrent callback or redial an uncertain attempt.
            await db.execute(
                update(ClinicPhoneCall)
                .where(
                    ClinicPhoneCall.id == call_id,
                    ClinicPhoneCall.status == "dialing",
                    ClinicPhoneCall.provider_call_sid.is_(None),
                )
                .values(
                    status="failed" if rejected else "unknown",
                    error=rejection_message(exc)
                    if rejected
                    else "Dispatch unknown; verify with Twilio before reconciliation",
                    finished_at=utcnow() if rejected else None,
                )
            )
            await db.commit()
        await db.refresh(call)
        return call

    def validate_start(self, event):
        from app.ai.common.twilio import validate_start

        return validate_start(event, self.settings.twilio_account_sid)

    async def admit(self, start, *, route_id=None, call_id=None, token=""):
        async with self.sessions() as db:
            if route_id:
                route = await db.get(ClinicPhoneRoute, route_id)
                if not route or not route.enabled:
                    raise HTTPException(403, "Inbound route unavailable")
                self.verify(
                    route.token_version, f"inbound-media:{start['call_sid']}", token
                )
                # Check the actual provider call, not just untrusted WebSocket metadata.
                details = await self.provider.details(start["call_sid"])
                if (
                    details["Sid"] != start["call_sid"]
                    or details["Status"] != "in-progress"
                    or network_phone(details["To"]) != route.phone_number
                ):
                    raise HTTPException(403, "Inbound number or caller mismatch")
                call = ClinicPhoneCall(
                    bot_id=route.bot_id,
                    direction="inbound",
                    phone_number=network_phone(details["From"]),
                    status="in-progress",
                    provider_call_sid=start["call_sid"],
                    stream_claimed=True,
                )
                db.add(call)
                await db.commit()  # Global unique SID rejects replayed calls.
            else:
                self.verify(call_id, "media", token)
                call = await db.get(ClinicPhoneCall, call_id)
                if (
                    not call
                    or call.direction != "outbound"
                    or call.status in TERMINAL_CALL_STATUSES
                    or (
                        call.provider_call_sid
                        and call.provider_call_sid != start["call_sid"]
                    )
                ):
                    raise HTTPException(403, "Outbound call unavailable")
                changed = await db.execute(
                    update(ClinicPhoneCall)
                    .where(
                        ClinicPhoneCall.id == call_id,
                        ClinicPhoneCall.stream_claimed.is_(False),
                        ClinicPhoneCall.status.not_in(TERMINAL_CALL_STATUSES),
                        or_(
                            ClinicPhoneCall.provider_call_sid.is_(None),
                            ClinicPhoneCall.provider_call_sid == start["call_sid"],
                        ),
                        ClinicPhoneCall.created_at
                        > utcnow() - timedelta(seconds=constants.VOICE_SESSION_SECONDS),
                    )
                    .values(
                        stream_claimed=True,
                        provider_call_sid=start["call_sid"],
                        status="in-progress",
                    )
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise HTTPException(409, "Phone stream already claimed or expired")
                await db.commit()
                await db.refresh(call)
            bot = await db.get(Bot, call.bot_id)
            template = await bots.active_template(db, bot.template_id)
            if template.service_type != "clinic":
                raise HTTPException(422, "Not a clinic bot")
            runtime = await bots.runtime_bot(db, bot)
            return call, runtime

    async def refresh(self):
        async with self.sessions() as db:
            calls = list(
                await db.scalars(
                    select(ClinicPhoneCall).where(
                        ClinicPhoneCall.status.not_in(TERMINAL_CALL_STATUSES)
                    )
                )
            )
        for call in calls:
            try:
                if call.provider_call_sid:
                    result = await self.provider.details(call.provider_call_sid)
                    await self.apply_status(call.id, result["Sid"], result["Status"])
                elif call.status in {"dialing", "queued"}:
                    async with self.sessions() as db:
                        await db.execute(
                            update(ClinicPhoneCall)
                            .where(
                                ClinicPhoneCall.id == call.id,
                                ClinicPhoneCall.status.in_({"dialing", "queued"}),
                                ClinicPhoneCall.provider_call_sid.is_(None),
                                ClinicPhoneCall.created_at
                                < utcnow() - timedelta(seconds=60),
                            )
                            .values(
                                status="unknown",
                                error="Interrupted dispatch; reconcile with Twilio",
                            )
                        )
                        await db.commit()
            except (httpx.HTTPError, ValueError, KeyError, TypeError, HTTPException):
                logger.warning("Clinic phone status refresh failed for %s", call.id)

    def start(self):
        # Deployment settings are fixed until restart; avoid idle polling when disabled.
        try:
            self.require_ready()
        except HTTPException as exc:
            twilio_logger.warning(
                "Twilio worker disabled service=clinic_phone reason=%s", exc.detail
            )
            return

        async def loop():
            while True:
                try:
                    self.require_ready()
                    await self.refresh()
                except HTTPException:
                    pass
                except Exception:  # noqa: BLE001 - keep periodic recovery running
                    logger.warning(
                        "Clinic phone status refresh failed; retrying next cycle"
                    )
                await asyncio.sleep(5)

        self.task = asyncio.create_task(loop())

    async def close(self):
        self.closing = True
        tasks = [*self.live, *([self.task] if self.task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

"""Persistent batch dispatch with row locks and conservative ambiguous-request handling."""

import asyncio
import hashlib
import hmac
import logging
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.business.call_status import (
    ACTIVE_CALL_STATUSES,
    PROVIDER_CALL_STATUSES,
    TERMINAL_CALL_STATUSES,
    should_advance_call_status,
)
from app.clients.twilio import TwilioClient, rejection_message
from app.clients.twilio_logging import logger as twilio_logger
from app.db.models import OutreachCall, OutreachCampaign, utcnow
from app.db.session import SessionFactory

logger = logging.getLogger(__name__)


class OutreachService:
    def __init__(self, settings, *, sessions=SessionFactory, provider=None):
        self.settings = settings
        self.sessions = sessions
        self.provider = provider or TwilioClient(settings)
        self.tasks = []

    def require_ready(self):
        if not all(
            [
                self.settings.twilio_account_sid,
                self.settings.twilio_auth_token.get_secret_value(),
                self.settings.twilio_phone_number,
                self.settings.public_backend_url,
                self.settings.sarvam_api_key.get_secret_value(),
            ]
        ):
            raise HTTPException(
                503,
                "Configure Twilio, PUBLIC_BACKEND_URL (HTTPS) and Sarvam before running",
            )

    def token(self, call_id, purpose):
        return hmac.new(
            self.settings.jwt_secret.get_secret_value().encode(),
            f"outreach:{purpose}:{call_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, call_id, purpose, token):
        if not token.isascii() or not hmac.compare_digest(
            self.token(call_id, purpose), token
        ):
            raise HTTPException(403, "Invalid call token")

    def urls(self, call_id):
        base = self.settings.public_backend_url.rstrip("/")
        path = f"/api/v1/outreach/twilio/{call_id}"
        return (
            base + path + f"/answer?token={self.token(call_id, 'answer')}",
            base + path + f"/status?token={self.token(call_id, 'status')}",
        )

    def media_url(self, call_id):
        base = self.settings.public_backend_url.rstrip("/").replace(
            "https://", "wss://", 1
        )
        return (
            base
            + f"/api/v1/outreach/twilio/{call_id}/media/{self.token(call_id, 'media')}"
        )

    async def apply_status(self, call_id, sid, status):
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            raise HTTPException(400, "Invalid Twilio call SID")
        if status not in PROVIDER_CALL_STATUSES:
            raise HTTPException(400, "Unsupported Twilio call status")
        async with self.sessions() as db:
            call = await db.scalar(
                select(OutreachCall).where(OutreachCall.id == call_id).with_for_update()
            )
            if not call or call.status == "pending" or call.started_at is None:
                raise HTTPException(404, "Dispatched call not found")
            if call.provider_call_sid and call.provider_call_sid != sid:
                raise HTTPException(409, "Call SID does not match")
            if call.status in TERMINAL_CALL_STATUSES:
                return  # Duplicate / out-of-order callbacks never reopen a finished call.
            previous_status = call.status
            call.provider_call_sid = sid
            if should_advance_call_status(call.status, status):
                call.status = status
                # Provider confirmation resolves an ambiguous dispatch.
                call.error = None
            if status in TERMINAL_CALL_STATUSES:
                call.finished_at = utcnow()
            await db.commit()
            if call.status != previous_status:
                twilio_logger.info(
                    "Twilio status changed service=%s call_id=%s sid=%s previous=%s status=%s",
                    "outreach_worker",
                    call_id,
                    sid,
                    previous_status,
                    call.status,
                )

    async def claim_batch(self, campaign_id):
        async with self.sessions() as db:
            campaign = await db.scalar(
                select(OutreachCampaign)
                .where(OutreachCampaign.id == campaign_id)
                .with_for_update(skip_locked=True)
            )
            if not campaign or campaign.status not in {"running", "stopping"}:
                return []
            active = await db.scalar(
                select(OutreachCall.id)
                .where(
                    OutreachCall.campaign_id == campaign.id,
                    OutreachCall.status.in_(ACTIVE_CALL_STATUSES),
                )
                .limit(1)
            )
            if active:
                return []  # A batch must fully finish before another batch starts.
            pending = list(
                await db.scalars(
                    select(OutreachCall)
                    .where(
                        OutreachCall.campaign_id == campaign.id,
                        OutreachCall.status == "pending",
                    )
                    .order_by(OutreachCall.position)
                    .limit(5 if campaign.mode == "parallel" else 1)
                )
            )
            if not pending or campaign.status == "stopping":
                campaign.status = (
                    "cancelled" if campaign.status == "stopping" else "completed"
                )
                campaign.finished_at = utcnow()
                await db.commit()
                return []
            for call in pending:
                call.status = "dialing"
                call.started_at = utcnow()
            await db.commit()  # Persist reservations BEFORE making external requests.
            return [call.id for call in pending]

    async def dispatch(self, call_id):
        async with self.sessions() as db:
            call = await db.get(OutreachCall, call_id)
            number = call.phone_number
        try:
            stream, callback = self.urls(call_id)
            result = await self.provider.dial(number, stream, callback, call_id)
            await self.apply_status(call_id, result["Sid"], result["Status"])
        except Exception as exc:  # noqa: BLE001 - preserve ambiguous outcomes on any dispatch failure
            # A timeout/5xx/malformed response can still have placed a real phone call.
            # Keep its slot reserved, accept a late callback and NEVER auto-redial it.
            rejected = (
                isinstance(exc, httpx.HTTPStatusError)
                and 400 <= exc.response.status_code < 500
            )
            twilio_logger.error(
                "Twilio dispatch failed service=%s call_id=%s error_type=%s outcome=%s",
                "outreach_worker",
                call_id,
                type(exc).__name__,
                "rejected"
                if rejected
                else "unknown; awaiting callback or reconciliation",
            )
            async with self.sessions() as db:
                call = await db.scalar(
                    select(OutreachCall)
                    .where(OutreachCall.id == call_id)
                    .with_for_update()
                )
                if call.status == "dialing" and not call.provider_call_sid:
                    call.status = "failed" if rejected else "unknown"
                    call.error = (
                        rejection_message(exc)
                        if rejected
                        else "Dispatch outcome unknown; awaiting callback or provider reconciliation"
                    )
                    if rejected:
                        call.finished_at = utcnow()
                    await db.commit()

    async def refresh_statuses(self):
        async with self.sessions() as db:
            active = list(
                await db.scalars(
                    select(OutreachCall).where(
                        OutreachCall.status.in_(ACTIVE_CALL_STATUSES)
                    )
                )
            )
        for call in active:
            if call.provider_call_sid:
                try:
                    result = await self.provider.details(call.provider_call_sid)
                    await self.apply_status(call.id, result["Sid"], result["Status"])
                except (
                    httpx.HTTPError,
                    ValueError,
                    KeyError,
                    TypeError,
                    HTTPException,
                ):
                    logger.warning(
                        "Outreach call status refresh failed for %s", call.id
                    )
            elif call.status in {"dialing", "queued"}:
                # Recover a process that died between reservation and storing the response.
                async with self.sessions() as db:
                    stuck = await db.scalar(
                        select(OutreachCall)
                        .where(
                            OutreachCall.id == call.id,
                            OutreachCall.status.in_({"dialing", "queued"}),
                            OutreachCall.started_at < utcnow() - timedelta(seconds=60),
                        )
                        .with_for_update()
                    )
                    if stuck:
                        stuck.status = "unknown"
                        stuck.error = "Interrupted dispatch; reconcile with Twilio before continuing"
                        await db.commit()

    async def dispatch_pending(self):
        async with self.sessions() as db:
            ids = list(
                await db.scalars(
                    select(OutreachCampaign.id)
                    .where(OutreachCampaign.status.in_({"running", "stopping"}))
                    .order_by(OutreachCampaign.created_at)
                )
            )
        reserved = []
        for campaign_id in ids:
            reserved.extend(await self.claim_batch(campaign_id))
            if len(reserved) >= 10:
                break  # Bound provider dial rate; remaining campaigns get a later tick.
        await asyncio.gather(*(self.dispatch(call_id) for call_id in reserved))

    async def tick(self):
        await self.refresh_statuses()
        await self.dispatch_pending()

    def start(self):
        # Deployment settings are fixed until restart; avoid idle polling when disabled.
        try:
            self.require_ready()
        except HTTPException as exc:
            twilio_logger.warning(
                "Twilio worker disabled service=outreach_worker reason=%s", exc.detail
            )
            return

        async def loop(action):
            while True:
                try:
                    self.require_ready()
                    await action()
                except HTTPException:
                    pass
                except Exception:  # noqa: BLE001 - keep the durable queue alive after a failed tick
                    logger.warning(
                        "Outreach worker failed; persisted queue will be retried"
                    )
                await asyncio.sleep(5)

        self.tasks = [
            asyncio.create_task(loop(action))
            for action in (self.refresh_statuses, self.dispatch_pending)
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.provider.close()

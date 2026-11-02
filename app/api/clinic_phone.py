"""Authenticated clinic phone controls and scoped Twilio media endpoints."""

import asyncio
import re
from contextlib import suppress
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app.ai.common.twilio import stream_xml
from app.api.dependencies import DB, CurrentUser
from app.api.phone_callbacks import read_payload
from app.business import bots
from app.business.clinic_phone import clinic_bot, network_phone
from app.business.organizations import require_membership
from app.clients.twilio import callback_status
from app.clients.twilio_logging import logger, safe_detail
from app.clinic_phone_types import (
    PhoneCallOut,
    PhoneDial,
    PhoneReconcile,
    PhoneRouteConfig,
    PhoneRouteOut,
)
from app.constants import constants
from app.constants.constants import MANAGE_ROLES
from app.db.models import ClinicPhoneCall, ClinicPhoneRoute, utcnow

router = APIRouter(
    prefix="/organizations/{org_id}/bots/{bot_id}/phone", tags=["Clinic phone"]
)
provider_router = APIRouter(prefix="/clinic-phone/twilio", tags=["Clinic Twilio"])


@router.get("/capabilities")
async def capabilities(
    org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser, request: Request
):
    await clinic_bot(db, org_id, bot_id, user.id)
    member = await require_membership(db, org_id, user.id)
    service = request.app.state.clinic_phone
    manager = member.role in MANAGE_ROLES
    try:
        service.require_ready()
        inbound_ready = True
    except HTTPException:
        inbound_ready = False
    return {
        "can_view_route": manager,
        "can_manage_route": manager
        and user.id in service.settings.clinic_operator_user_ids,
        "can_call": manager,
        "inbound_ready": inbound_ready,
        "outbound_ready": inbound_ready and bool(service.settings.twilio_phone_number),
    }


@router.get("/route", response_model=PhoneRouteOut)
async def get_route(
    org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser, request: Request
):
    await clinic_bot(db, org_id, bot_id, user.id, write=True)
    route = await db.scalar(
        select(ClinicPhoneRoute).where(ClinicPhoneRoute.bot_id == bot_id)
    )
    if not route:
        raise HTTPException(404, "No inbound number configured")
    result = PhoneRouteOut.model_validate(route)
    if route.enabled and request.app.state.clinic_phone.settings.public_backend_url:
        result.inbound_answer_url = request.app.state.clinic_phone.inbound_url(route)
    return result


@router.put("/route", response_model=PhoneRouteOut)
async def set_route(
    org_id: UUID,
    bot_id: UUID,
    data: PhoneRouteConfig,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    bot = await clinic_bot(db, org_id, bot_id, user.id, write=True)
    service = request.app.state.clinic_phone
    # The Twilio account is shared: only trusted operators can allocate its numbers.
    if user.id not in service.settings.clinic_operator_user_ids:
        raise HTTPException(403, "A platform operator must allocate the Twilio number")
    if data.enabled:
        service.require_ready()
        await bots.runtime_bot(db, bot)
    route = await db.scalar(
        select(ClinicPhoneRoute)
        .where(ClinicPhoneRoute.bot_id == bot_id)
        .with_for_update()
    )
    if route is None:
        route = ClinicPhoneRoute(bot_id=bot_id)
        db.add(route)
    route.phone_number, route.enabled, route.token_version = (
        data.phone_number,
        data.enabled,
        uuid4(),
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "This phone number already belongs to a clinic"
        ) from None
    result = PhoneRouteOut.model_validate(route)
    if route.enabled:
        result.inbound_answer_url = service.inbound_url(route)
    return result


@router.post("/calls", response_model=PhoneCallOut, status_code=202)
async def dial(
    org_id: UUID,
    bot_id: UUID,
    data: PhoneDial,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    bot = await clinic_bot(db, org_id, bot_id, user.id, write=True)
    return await request.app.state.clinic_phone.dial(db, bot, data)


@router.get("/calls", response_model=list[PhoneCallOut])
async def calls(
    org_id: UUID,
    bot_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await clinic_bot(db, org_id, bot_id, user.id)
    return list(
        await db.scalars(
            select(ClinicPhoneCall)
            .where(ClinicPhoneCall.bot_id == bot_id)
            .order_by(ClinicPhoneCall.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.post("/calls/{call_id}/reconcile", response_model=PhoneCallOut)
async def reconcile(
    org_id: UUID,
    bot_id: UUID,
    call_id: UUID,
    data: PhoneReconcile,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    await clinic_bot(db, org_id, bot_id, user.id, write=True)
    call = await db.scalar(
        select(ClinicPhoneCall)
        .where(ClinicPhoneCall.id == call_id, ClinicPhoneCall.bot_id == bot_id)
        .with_for_update()
    )
    if not call:
        raise HTTPException(404, "Call not found")
    if call.direction != "outbound" or call.status != "unknown":
        raise HTTPException(409, "Only an unknown outbound call needs reconciliation")
    service = request.app.state.clinic_phone
    if data.confirmed_not_placed:
        call.status, call.finished_at = "failed", utcnow()
        call.error = "Operator confirmed no call was placed"
        await db.commit()
    else:
        try:
            result = await service.provider.details(data.provider_call_sid)
            if (
                result["Sid"] != data.provider_call_sid
                or network_phone(result["To"]) != call.phone_number
            ):
                raise HTTPException(409, "Provider destination does not match")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "Unable to verify provider call") from None
        await db.commit()
        await service.apply_status(call_id, result["Sid"], result["Status"])
        await db.refresh(call)
    return call


@provider_router.api_route("/calls/{call_id}/answer", methods=["GET", "POST"])
async def outbound_answer(call_id: UUID, request: Request, token: str = ""):
    service = request.app.state.clinic_phone
    service.verify(call_id, "answer", token)
    service.require_ready()
    payload = await read_payload(request)
    await service.apply_status(call_id, payload.get("CallSid"), "in-progress")
    url = service.url(f"calls/{call_id}/media", call_id, "media", media=True)
    logger.info("Twilio answer served service=clinic_phone call_id=%s", call_id)
    return Response(stream_xml(url), media_type="application/xml")


@provider_router.api_route("/inbound/{route_id}/answer", methods=["GET", "POST"])
async def inbound_answer(route_id: UUID, request: Request, token: str = ""):
    service = request.app.state.clinic_phone
    service.require_ready()
    async with service.sessions() as db:
        route = await db.get(ClinicPhoneRoute, route_id)
        if not route or not route.enabled:
            raise HTTPException(403, "Inbound route unavailable")
        service.verify(route.token_version, "inbound", token)
        payload = await read_payload(request)
        sid = payload.get("CallSid")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise HTTPException(400, "Invalid Twilio call SID")
        details = await service.provider.details(sid)
        if (
            details["Sid"] != sid
            or details["Status"] != "in-progress"
            or network_phone(details["To"]) != route.phone_number
        ):
            raise HTTPException(403, "Inbound call does not match clinic number")
        url = service.url(
            f"inbound/{route_id}/media/{sid}",
            route.token_version,
            f"inbound-media:{sid}",
            media=True,
        )
    logger.info("Twilio inbound answer served route_id=%s sid=%s", route_id, sid)
    return Response(stream_xml(url), media_type="application/xml")


@provider_router.post("/calls/{call_id}/status", status_code=204)
async def status(call_id: UUID, request: Request, token: str = ""):
    service = request.app.state.clinic_phone
    try:
        service.verify(call_id, "status", token)
    except HTTPException as exc:
        logger.warning(
            "Twilio callback rejected service=clinic_phone call_id=%s http_status=%s",
            call_id,
            exc.status_code,
        )
        raise
    logger.info(
        "Twilio callback received service=clinic_phone call_id=%s content_type=%s",
        call_id,
        request.headers.get("content-type"),
    )
    try:
        payload = await read_payload(request)
        await service.apply_status(
            call_id,
            payload.get("CallSid"),
            callback_status(payload),
        )
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "Twilio callback malformed service=clinic_phone call_id=%s", call_id
        )
        raise HTTPException(400, "Invalid call status callback") from None
    except HTTPException as exc:
        logger.warning(
            "Twilio callback rejected service=clinic_phone call_id=%s http_status=%s reason=%s",
            call_id,
            exc.status_code,
            safe_detail(exc.detail),
        )
        raise
    logger.info("Twilio callback applied service=clinic_phone call_id=%s", call_id)
    return Response(status_code=204)


async def media(websocket, *, route_id=None, call_id=None, token="", call_sid=""):
    service = websocket.app.state.clinic_phone
    task = asyncio.current_task()
    admitted = False
    phase = "authorization"
    logger.info(
        "Twilio media requested service=clinic_phone call_id=%s route_id=%s",
        call_id,
        route_id,
    )
    try:
        service.require_ready()
        if service.closing or len(service.live) >= constants.MAX_VOICE_SESSIONS:
            raise HTTPException(503, "Clinic phone capacity reached")
        if route_id:
            async with service.sessions() as db:
                route = await db.get(ClinicPhoneRoute, route_id)
                if not route or not route.enabled:
                    raise HTTPException(403, "Inbound route unavailable")
                service.verify(route.token_version, f"inbound-media:{call_sid}", token)
        else:
            service.verify(call_id, "media", token)
        # Count pending starts as well as active audio sessions.
        if service.closing or len(service.live) >= constants.MAX_VOICE_SESSIONS:
            raise HTTPException(503, "Clinic phone capacity reached")
        service.live.add(task)
        await websocket.accept()
        phase = "start_event"
        async with asyncio.timeout(15):
            event = await websocket.receive_json()
            if event.get("event") == "connected":
                event = await websocket.receive_json()
            start = service.validate_start(event)
            if route_id and start["call_sid"] != call_sid:
                raise ValueError("Twilio stream call SID mismatch")
            phase = "admission"
            call, runtime = await service.admit(
                start, route_id=route_id, call_id=call_id, token=token
            )
        admitted = True
        call_id = call.id
        phase = "voice_pipeline"
        logger.info(
            "Twilio media admitted service=clinic_phone call_id=%s sid=%s sample_rate=%s",
            call_id,
            start["call_sid"],
            start["media_format"]["sample_rate"],
        )
        from app.ai.clinic.pipeline import build_pipeline
        from app.ai.common.twilio import build_transport

        transport = build_transport(websocket, start)
        runner, worker = build_pipeline(
            None,
            runtime,
            service.settings,
            transport=transport,
            output_sample_rate=int(start["media_format"]["sample_rate"]),
            session_factory=service.sessions,
            direction=call.direction,
        )
        await runner.add_workers(worker)
        try:
            async with asyncio.timeout(constants.VOICE_SESSION_SECONDS):
                await runner.run()
        finally:
            await runner.cancel()
        # Media termination isn't authoritative telephony status: the provider polling
        # loop (and outbound callbacks) owns completion, including across restarts.
    except WebSocketDisconnect as exc:
        logger.info(
            "Twilio media disconnected service=clinic_phone call_id=%s route_id=%s phase=%s code=%s",
            call_id,
            route_id,
            phase,
            exc.code,
        )
    except (
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TimeoutError,
        IntegrityError,
        httpx.HTTPError,
    ) as exc:
        logger.warning(
            "Twilio media failed service=clinic_phone call_id=%s route_id=%s phase=%s error_type=%s reason=%s",
            call_id,
            route_id,
            phase,
            type(exc).__name__,
            safe_detail(exc.detail)
            if isinstance(exc, HTTPException)
            else type(exc).__name__
            if isinstance(exc, IntegrityError)
            else safe_detail(exc),
        )
    except Exception as exc:
        logger.error(
            "Twilio media failed service=clinic_phone call_id=%s route_id=%s phase=%s error_type=%s reason=%s",
            call_id,
            route_id,
            phase,
            type(exc).__name__,
            safe_detail(exc),
        )
        raise
    finally:
        logger.info(
            "Twilio media closed service=clinic_phone call_id=%s route_id=%s admitted=%s",
            call_id,
            route_id,
            admitted,
        )
        service.live.discard(task)
        if websocket.client_state.name != "DISCONNECTED":
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1000 if admitted else 1008)


@provider_router.websocket("/inbound/{route_id}/media/{call_sid}/{token}")
async def inbound_media(
    websocket: WebSocket, route_id: UUID, token: str = "", call_sid: str = ""
):
    await media(websocket, route_id=route_id, token=token, call_sid=call_sid)


@provider_router.websocket("/calls/{call_id}/media/{token}")
async def outbound_media(websocket: WebSocket, call_id: UUID, token: str = ""):
    await media(websocket, call_id=call_id, token=token)

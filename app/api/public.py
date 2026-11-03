from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import DB, bearer
from app.business import api_keys, public_voice
from app.db.models import utcnow
from app.types import Answer, Offer, VoiceGrantOut, VoiceIceConfig
from app.utils.rate_limit import enforce_rate_limit

router = APIRouter(tags=["public voice"])
VoiceCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


@router.post(
    "/bots/{bot_id}/session-token", response_model=VoiceGrantOut, status_code=201
)
async def session_token(
    bot_id: UUID,
    request: Request,
    db: DB,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
):
    key = await api_keys.authenticate_key(db, api_key)
    enforce_rate_limit(request, "grant", f"organization:{key.organization_id}")
    return await public_voice.create_grant(db, key, bot_id)


@router.get(
    "/bots/{bot_id}/webrtc/ice-servers",
    response_model=VoiceIceConfig,
    response_model_exclude_none=True,
)
async def ice_servers(
    bot_id: UUID,
    request: Request,
    response: Response,
    db: DB,
    credentials: VoiceCredentials,
):
    grant, _bot = await public_voice.resolve_grant(
        db, bot_id, credentials.credentials if credentials else None
    )
    # Only an unused grant within its offer window may retrieve relay credentials.
    expires = grant.offer_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=utcnow().tzinfo)
    if grant.used_at is not None or expires <= utcnow():
        raise HTTPException(
            409, "Voice token has already been used or its offer window expired"
        )
    request.app.state.turn.throttle(grant.api_key_id)
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.turn.configuration()


@router.post("/bots/{bot_id}/webrtc/offer", response_model=Answer)
async def offer(
    bot_id: UUID, data: Offer, request: Request, db: DB, credentials: VoiceCredentials
):
    _, bot = await public_voice.resolve_grant(
        db, bot_id, credentials.credentials if credentials else None
    )
    enforce_rate_limit(request, "voice", f"organization:{bot.organization_id}")
    return await public_voice.start_voice(
        db,
        bot_id,
        credentials.credentials if credentials else None,
        data,
        request.app.state.voice,
    )


@router.delete("/bots/{bot_id}/webrtc/{pc_id}", status_code=204)
async def stop(
    bot_id: UUID, pc_id: str, request: Request, db: DB, credentials: VoiceCredentials
):
    await public_voice.stop_voice(
        db,
        bot_id,
        credentials.credentials if credentials else None,
        pc_id,
        request.app.state.voice,
    )
    return Response(status_code=204)

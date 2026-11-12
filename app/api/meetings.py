"""Calendar assignment, Attendee voice control, and workspace meeting memory."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from sqlalchemy import and_, or_, select

from app.api.dependencies import DB, CurrentUser
from app.business.meeting_calendar import assign_event, aware
from app.business.organizations import list_organizations, require_membership
from app.constants import constants
from app.db.models import (
    Meeting,
    MeetingCalendar,
    MeetingCapture,
    MeetingGoogleConnection,
    utcnow,
)
from app.env import get_settings
from app.integrations.google_meet_calendar import lock
from app.types import (
    AssignMeeting,
    EventOut,
    MeetingCaptureOut,
    MeetingOut,
    MeetingSchedule,
    OrgOut,
)

router = APIRouter(tags=["meetings"])


def is_operator(user):
    return user.id in get_settings().meeting_operator_user_ids


async def require_operator(user: CurrentUser):
    if not is_operator(user):
        raise HTTPException(
            403, "Calendar inbox is restricted to the platform operator"
        )
    return user


Operator = Annotated[object, Depends(require_operator)]


@router.get("/meeting-capabilities")
async def capabilities(user: CurrentUser, request: Request):
    return {
        "operator": is_operator(user),
        "voice_name": constants.MEETING_VOICE_NAME,
        "manual_join_ready": request.app.state.meeting_voice.ready,
    }


@router.get("/operator/meeting-calendar")
async def connection_status(db: DB, user: CurrentUser):
    connection = await db.scalar(
        select(MeetingGoogleConnection).where(
            MeetingGoogleConnection.user_id == user.id
        )
    )
    calendars = (
        (
            await db.scalars(
                select(MeetingCalendar).where(
                    MeetingCalendar.connection_id == connection.id
                )
            )
        ).all()
        if connection
        else []
    )
    return {
        "identity_ready": bool(connection and connection.status != "disabled"),
        "status": connection.status if connection else "not_connected",
        "email": connection.email if connection else None,
        "calendars": [
            {"id": c.id, "last_synced_at": c.last_synced_at, "enabled": c.enabled}
            for c in calendars
        ],
    }


@router.get("/operator/meeting-organizations", response_model=list[OrgOut])
async def assignment_organizations(db: DB, user: CurrentUser):
    return await list_organizations(db, user.id)


def calendar_ownership(user):
    return or_(
        Meeting.calendar_id.in_(
            select(MeetingCalendar.id)
            .join(MeetingGoogleConnection)
            .where(
                MeetingGoogleConnection.user_id == user.id,
                MeetingGoogleConnection.status != "disabled",
            )
        ),
        # Preserve operator-verified access to the original shared bot calendar.
        and_(
            Meeting.organizer_email.is_not(None),
            Meeting.organizer_email == user.calendar_email,
            Meeting.calendar_id.in_(
                select(MeetingCalendar.id)
                .join(MeetingGoogleConnection)
                .where(MeetingGoogleConnection.singleton_key == 1)
            ),
        ),
    )


@router.get("/operator/meeting-events", response_model=list[EventOut])
async def inbox(
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return (
        await db.scalars(
            select(Meeting)
            .where(
                calendar_ownership(user),
                Meeting.organization_id.is_(None),
                Meeting.status != "cancelled",
                Meeting.next_ends_at > utcnow(),
            )
            .order_by(Meeting.next_starts_at, Meeting.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()


@router.get("/calendar/events", response_model=list[EventOut])
async def personal_inbox(
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return (
        await db.scalars(
            select(Meeting)
            .join(MeetingCalendar)
            .join(MeetingGoogleConnection)
            .where(
                MeetingGoogleConnection.user_id == user.id,
                MeetingGoogleConnection.status != "disabled",
                Meeting.status != "cancelled",
                Meeting.next_ends_at > utcnow(),
            )
            .order_by(Meeting.next_starts_at, Meeting.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()


@router.post("/calendar/events/{event_id}/assign", response_model=MeetingOut)
@router.post("/operator/meeting-events/{event_id}/assign", response_model=MeetingOut)
async def assign(event_id: UUID, data: AssignMeeting, db: DB, user: CurrentUser):
    # Same lock as sync; prevent assignment from racing a cancellation or another assignment.
    await lock(db)
    event = await db.scalar(
        select(Meeting)
        .where(
            Meeting.id == event_id,
            calendar_ownership(user),
        )
        .with_for_update()
    )
    if not event:
        raise HTTPException(404, "Calendar event not found")
    organizations = await list_organizations(db, user.id)
    if not any(org.id == data.organization_id for org in organizations):
        raise HTTPException(404, "Workspace not found")
    meeting = await assign_event(
        db, event, data.organization_id, data.agenda, data.auto_join
    )
    # A recurring definition is always assigned as a whole. The deprecated
    # assign_series input remains accepted for older clients.
    await db.commit()
    return meeting


@router.get("/organizations/{org_id}/meetings", response_model=list[MeetingOut])
async def meetings(
    org_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await require_membership(db, org_id, user.id)
    return (
        await db.scalars(
            select(Meeting)
            .where(Meeting.organization_id == org_id)
            .order_by(Meeting.next_starts_at.desc().nulls_last(), Meeting.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()


async def owned_meeting(db, org_id, meeting_id, user_id):
    await require_membership(db, org_id, user_id)
    meeting = await db.scalar(
        select(Meeting).where(
            Meeting.id == meeting_id, Meeting.organization_id == org_id
        )
    )
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    return meeting


@router.get("/organizations/{org_id}/meetings/{meeting_id}/connection")
async def join_status(
    org_id: UUID, meeting_id: UUID, db: DB, user: CurrentUser, request: Request
):
    meeting = await owned_meeting(db, org_id, meeting_id, user.id)
    service = request.app.state.meeting_voice
    capture = await service.capture(db, meeting.id)
    return {**service.output(capture, meeting), "auto_join": meeting.auto_join}


@router.post("/organizations/{org_id}/meetings/{meeting_id}/join", status_code=202)
async def join_now(
    org_id: UUID, meeting_id: UUID, db: DB, operator: Operator, request: Request
):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    if meeting.status == "cancelled":
        raise HTTPException(409, "Cancelled meetings cannot be joined")
    return await request.app.state.meeting_voice.start(db, meeting)


@router.post("/organizations/{org_id}/meetings/{meeting_id}/leave")
async def leave_now(
    org_id: UUID, meeting_id: UUID, db: DB, operator: Operator, request: Request
):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    return await request.app.state.meeting_voice.stop(db, meeting)


@router.websocket("/meeting-audio/{capture_id}")
async def meeting_audio(websocket: WebSocket, capture_id: UUID, token: str = ""):
    await websocket.app.state.meeting_voice.audio(websocket, capture_id, token)


@router.get("/organizations/{org_id}/meeting-memory")
async def meeting_memory(org_id: UUID, db: DB, user: CurrentUser, request: Request):
    await require_membership(db, org_id, user.id)
    return {"context": await request.app.state.meeting_voice.memory.read(org_id)}


@router.put(
    "/organizations/{org_id}/meetings/{meeting_id}/schedule", response_model=MeetingOut
)
async def schedule_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: MeetingSchedule,
    db: DB,
    operator: Operator,
):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    await db.refresh(meeting, with_for_update=True)
    if meeting.status != "scheduled" or (
        not meeting.is_recurring
        and (not meeting.next_ends_at or aware(meeting.next_ends_at) <= utcnow())
    ):
        raise HTTPException(
            409, "Only upcoming scheduled meetings can change auto-join"
        )
    meeting.auto_join = data.auto_join
    await db.commit()
    return meeting


@router.get(
    "/organizations/{org_id}/meetings/{meeting_id}/captures",
    response_model=list[MeetingCaptureOut],
)
async def captures(
    org_id: UUID,
    meeting_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await owned_meeting(db, org_id, meeting_id, user.id)
    return (
        await db.scalars(
            select(MeetingCapture)
            .where(
                MeetingCapture.meeting_id == meeting_id,
                MeetingCapture.organization_id == org_id,
            )
            .order_by(MeetingCapture.scheduled_starts_at.desc(), MeetingCapture.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()

"""Tenant authorization and persisted Outreach drafts. Workers own call dispatch."""

from fastapi import HTTPException
from sqlalchemy import select, update

from app.business.call_status import ACTIVE_CALL_STATUSES
from app.business.organizations import require_membership
from app.constants.constants import MANAGE_ROLES
from app.db.models import OutreachAgent, OutreachCall, OutreachCampaign, utcnow


async def access(db, org_id, user_id, write=False):
    await require_membership(db, org_id, user_id, MANAGE_ROLES if write else None)


async def agent(db, org_id, agent_id, user_id, write=False):
    await access(db, org_id, user_id, write)
    query = select(OutreachAgent).where(
        OutreachAgent.id == agent_id, OutreachAgent.organization_id == org_id
    )
    if write:
        query = query.with_for_update()
    result = await db.scalar(query)
    if not result:
        raise HTTPException(404, "Outreach agent not found")
    return result


async def campaign(db, org_id, campaign_id, user_id, write=False):
    await access(db, org_id, user_id, write)
    query = select(OutreachCampaign).where(
        OutreachCampaign.id == campaign_id, OutreachCampaign.organization_id == org_id
    )
    if write:
        query = query.with_for_update()
    result = await db.scalar(query)
    if not result:
        raise HTTPException(404, "Campaign not found")
    return result


async def create_campaign(db, org_id, user_id, data):
    selected = await agent(db, org_id, data.agent_id, user_id, write=True)
    record = OutreachCampaign(
        organization_id=org_id,
        agent_id=selected.id,
        name=data.name,
        mode=data.mode,
        agent_snapshot={
            "name": selected.name,
            "sales_context": selected.sales_context,
            "language": selected.language,
        },
    )
    db.add(record)
    await db.flush()
    db.add_all(
        [
            OutreachCall(campaign_id=record.id, position=i, **contact.model_dump())
            for i, contact in enumerate(data.contacts, start=1)
        ]
    )
    await db.commit()
    return record


async def run(db, org_id, campaign_id, user_id, service):
    record = await campaign(db, org_id, campaign_id, user_id, write=True)
    if record.status != "draft":
        raise HTTPException(
            409, "Only a draft campaign can be run; create a new draft to call again"
        )
    service.require_ready()
    record.status = "running"
    record.started_at = utcnow()
    await db.commit()
    return record


async def stop(db, org_id, campaign_id, user_id):
    record = await campaign(db, org_id, campaign_id, user_id, write=True)
    if record.status in {"completed", "cancelled"}:
        return record
    await db.execute(
        update(OutreachCall)
        .where(OutreachCall.campaign_id == record.id, OutreachCall.status == "pending")
        .values(status="cancelled", finished_at=utcnow())
    )
    active = await db.scalar(
        select(OutreachCall.id)
        .where(
            OutreachCall.campaign_id == record.id,
            OutreachCall.status.in_(ACTIVE_CALL_STATUSES),
        )
        .limit(1)
    )
    record.status = "stopping" if active else "cancelled"
    if not active:
        record.finished_at = utcnow()
    await db.commit()
    return record

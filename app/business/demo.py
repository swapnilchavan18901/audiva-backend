"""One-click demo access with a separate workspace for each visitor."""

import json
import secrets
from uuid import uuid4

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.business.clinic_context import sync_doctors
from app.db.models import Bot, BotTemplate, Membership, Organization, User
from app.types import AuthOut, UserOut
from app.utils.security import access_token, password_hasher


async def start_demo(db):
    user = User(
        email=f"demo-{uuid4().hex}@example.com",
        name="Demo Visitor",
        password_hash=await run_in_threadpool(
            password_hasher.hash, secrets.token_urlsafe(32)
        ),
    )
    db.add(user)
    await db.flush()
    org = Organization(name="Audiva Demo", personal_for_user_id=user.id)
    db.add(org)
    await db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="owner"))

    templates = (
        await db.scalars(
            select(BotTemplate)
            .where(BotTemplate.is_active.is_(True))
            .order_by(BotTemplate.name, BotTemplate.id)
        )
    ).all()
    # One ready-to-try agent per feature, using the deployed template catalog.
    seeded = set()
    for template in templates:
        if template.service_type in seeded:
            continue
        seeded.add(template.service_type)
        clinic = template.service_type == "clinic"
        context = (
            json.dumps(
                {
                    "background": "Fictional demo clinic. Use sample details when trying bookings.",
                    "clinic": {
                        "name": "Audiva Demo Clinic",
                        "address": "Demo Plaza, Pune (fictional)",
                        "timezone": "Asia/Kolkata",
                        "working_days": list(range(7)),
                        "open_time": "09:00",
                        "close_time": "18:00",
                        "slot_minutes": 30,
                        "fees": "General consultation: INR 500 (demo)",
                        "doctors": [
                            {"name": "Dr. Alex Demo", "specialty": "General Medicine"}
                        ],
                    },
                }
            )
            if clinic
            else "You are Aira, the assistant for Nova Audio, a fictional demo store in Pune. "
            "Wireless earbuds cost INR 1,999; headphones cost INR 4,999. "
            "Hours: Monday to Saturday, 10 AM to 8 PM. Delivery takes 2–3 business days. "
            "Help visitors explore products and policies. This demo cannot place orders "
            "or issue refunds. Do not ask for payment details."
        )
        bot = Bot(
            organization_id=org.id,
            template_id=template.id,
            name="Demo Clinic Receptionist"
            if clinic
            else "Aira — Demo Store Assistant",
            company_context=context,
            language="en-IN",
        )
        db.add(bot)
        if clinic:
            await db.flush()
            await sync_doctors(db, bot)
    await db.commit()
    return AuthOut(
        access_token=access_token(user.id), user=UserOut.model_validate(user)
    )

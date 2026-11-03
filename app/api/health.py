import asyncio

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DB

router = APIRouter(tags=["health"])


@router.get("/", include_in_schema=False)
@router.head("/", include_in_schema=False)
@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(db: DB):
    try:
        async with asyncio.timeout(3):
            await db.execute(text("SELECT 1"))
    except (SQLAlchemyError, TimeoutError):
        raise HTTPException(503, "Database is not ready") from None
    return {"status": "ready"}

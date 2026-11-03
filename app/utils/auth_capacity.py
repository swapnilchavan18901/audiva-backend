"""Keep memory-hard password hashing within the server's resource budget."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException


@asynccontextmanager
async def auth_work_slot(request):
    slots = getattr(request.app.state, "auth_slots", None)
    if slots is None:
        yield
        return
    try:
        await asyncio.wait_for(slots.acquire(), timeout=2)
    except TimeoutError:
        raise HTTPException(
            503,
            "Sign-in service is busy. Please try again shortly.",
            headers={"Retry-After": "2"},
        ) from None
    try:
        yield
    finally:
        slots.release()

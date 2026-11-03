from fastapi import APIRouter, HTTPException, Request

from app.api.dependencies import DB, CurrentUser
from app.business import auth
from app.business.demo import start_demo
from app.env import get_settings
from app.types import AuthOut, Credentials, Register, UserOut
from app.utils.auth_capacity import auth_work_slot

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/options")
async def options():
    settings = get_settings()
    return {
        "registration_enabled": settings.registration_enabled,
        "demo_enabled": settings.demo_allowed,
    }


@router.post("/demo", response_model=AuthOut, status_code=201)
async def demo(request: Request, db: DB):
    if not get_settings().demo_allowed:
        raise HTTPException(403, "Public demo access is disabled")
    async with auth_work_slot(request):
        return await start_demo(db)


@router.post("/register", response_model=AuthOut, status_code=201)
async def register(data: Register, request: Request, db: DB):
    if not get_settings().registration_enabled:
        raise HTTPException(
            403,
            "New account registration is disabled. Contact the workspace owner for access.",
        )
    async with auth_work_slot(request):
        return await auth.register(db, data)


@router.post("/login", response_model=AuthOut)
async def login(data: Credentials, request: Request, db: DB):
    async with auth_work_slot(request):
        return await auth.login(db, data)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user

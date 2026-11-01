from fastapi import APIRouter

from app.api.dependencies import DB, CurrentUser
from app.business import auth
from app.business.demo import start_demo
from app.types import AuthOut, Credentials, Register, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/demo", response_model=AuthOut, status_code=201)
async def demo(db: DB):
    return await start_demo(db)


@router.post("/register", response_model=AuthOut, status_code=201)
async def register(data: Register, db: DB):
    return await auth.register(db, data)


@router.post("/login", response_model=AuthOut)
async def login(data: Credentials, db: DB):
    return await auth.login(db, data)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user

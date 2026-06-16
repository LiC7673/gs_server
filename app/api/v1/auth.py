from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.schemas.user import UserRegister, UserLogin, TokenResponse, UserResponse
from app.services.auth_service import AuthService
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User, token: str) -> TokenResponse:
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserResponse.from_user(user),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="Register",
    responses={
        401: {"description": "Username or email already exists"},
        422: {"description": "Request validation failed"},
    },
)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
    user = await AuthService.register(db, body.username, body.email, body.password)
    _, token = await AuthService.login(db, body.username, body.password)
    return _token_response(user, token)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login",
    responses={
        401: {"description": "Invalid username or password, expired token, or disabled account"},
        422: {"description": "Request validation failed"},
    },
)
async def login(body: UserLogin, db: AsyncSession = Depends(get_db)):
    user, token = await AuthService.login(db, body.username, body.password)
    return _token_response(user, token)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current authenticated user",
    responses={401: {"description": "Missing, invalid, or expired bearer token"}},
)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_user(current_user)

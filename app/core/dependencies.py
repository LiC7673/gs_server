from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.quota_time import gpu_usage_date
from app.core.security import decode_access_token
from app.core.exceptions import UnauthorizedException, ForbiddenException
from app.models.user import User
from app.services.user_service import UserService

bearer_scheme = HTTPBearer(auto_error=False)


async def get_mock_user(db: AsyncSession = Depends(get_db)) -> User:
    result = await db.execute(
        select(User).where(
            (User.username == settings.mock_auth_username)
            | (User.email == settings.mock_auth_email)
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            username=settings.mock_auth_username,
            email=settings.mock_auth_email,
            hashed_password="mock-auth-disabled-password",
            nickname="Mock User",
            is_active=True,
            is_admin=True,
            storage_quota=10 * 1024 * 1024 * 1024 * 1024,
            task_quota=100000,
            gpu_quota=100000000,
            gpu_concurrency_quota=100000,
            gpu_usage_date=gpu_usage_date(),
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user

    user.is_active = True
    user.is_admin = True
    user.storage_quota = max(user.storage_quota or 0, 10 * 1024 * 1024 * 1024 * 1024)
    user.task_quota = max(user.task_quota or 0, 100000)
    user.gpu_quota = max(user.gpu_quota or 0, 100000000)
    user.gpu_concurrency_quota = max(user.gpu_concurrency_quota or 0, 100000)
    if user.gpu_usage_date is None:
        user.gpu_usage_date = gpu_usage_date()
    await db.flush()
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if settings.mock_auth_enabled:
        return await get_mock_user(db)

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedException("Missing or invalid authorization header")
    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise UnauthorizedException("Invalid or expired token")
    user_id = payload.get("sub")
    if user_id is None:
        raise UnauthorizedException("Token missing subject")
    user = await UserService.get_by_id(db, int(user_id))
    if user is None:
        raise UnauthorizedException("User not found")
    if not user.is_active:
        raise UnauthorizedException("Account is disabled")
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise ForbiddenException("Admin access required")
    return user

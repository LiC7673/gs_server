from typing import Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.core.security import hash_password, verify_password, create_access_token
from app.core.exceptions import UnauthorizedException
from app.core.quota_time import gpu_usage_date
from app.services.user_service import UserService


class AuthService:
    @staticmethod
    async def register(db: AsyncSession, username: str, email: str, password: str) -> User:
        username = username.strip()
        email = email.strip().lower()
        existing = await db.execute(
            select(User).where((User.username == username) | (User.email == email))
        )
        if existing.scalar_one_or_none():
            raise UnauthorizedException("Username or email already exists")
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(password),
            storage_quota=UserService.default_storage_quota(),
            task_quota=UserService.default_active_task_quota(),
            gpu_quota=UserService.default_gpu_daily_quota(),
            gpu_concurrency_quota=UserService.default_gpu_concurrency_quota(),
            gpu_usage_date=gpu_usage_date(),
        )
        db.add(user)
        await db.flush()
        return user

    @staticmethod
    async def login(db: AsyncSession, username: str, password: str) -> Tuple[User, str]:
        username = username.strip()
        user = await UserService.get_by_username(db, username)
        if not user or not verify_password(password, user.hashed_password):
            raise UnauthorizedException("Invalid username or password")
        if not user.is_active:
            raise UnauthorizedException("Account is disabled")
        token = create_access_token({"sub": str(user.id), "username": user.username})
        return user, token

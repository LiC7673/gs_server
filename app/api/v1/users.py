from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.dependencies import get_current_user, get_current_admin
from app.schemas.user import (
    AvatarResponse,
    AvatarUpdate,
    GpuUsageResetResponse,
    QuotaUpdate,
    UserProfileResponse,
    UserResponse,
    UserUsageResponse,
    UserUpdate,
)
from app.core.quota_time import gpu_quota_resets_at
from app.services.user_service import UserService
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserProfileResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    return UserProfileResponse.from_user(current_user)


@router.put("/me", response_model=UserProfileResponse)
async def update_profile(
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await UserService.update_user(
        db, current_user.id,
        nickname=body.nickname,
        email=body.email,
    )
    return UserProfileResponse.from_user(user)


@router.put("/update_avatar", response_model=AvatarResponse)
async def update_avatar(
    body: AvatarUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await UserService.update_avatar(db, current_user.id, body.avatar_file_id)
    return AvatarResponse.from_user(user)


@router.get("/me/usage", response_model=UserUsageResponse)
async def get_usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await UserService.get_usage(db, current_user.id)


@router.put("/{user_id}/quota", response_model=UserResponse)
async def update_quota(
    user_id: int,
    body: QuotaUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = await UserService.update_user(
        db, user_id,
        storage_quota=body.storage_quota,
        task_quota=body.task_quota,
        gpu_quota=body.gpu_quota,
        gpu_concurrency_quota=body.gpu_concurrency_quota,
    )
    return UserResponse.from_user(user)


@router.post("/{user_id}/gpu-usage/reset", response_model=GpuUsageResetResponse)
async def reset_gpu_usage(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = await UserService.reset_gpu_usage(db, user_id)
    return GpuUsageResetResponse(
        user_id=user.id,
        gpu_seconds_used=user.gpu_seconds_used,
        gpu_quota=user.gpu_quota,
        gpu_quota_resets_at=gpu_quota_resets_at(),
    )

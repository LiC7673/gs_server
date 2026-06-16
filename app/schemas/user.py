from datetime import datetime
from typing import Optional
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import NO_VALUE

from app.models.file import FileDerivativeVariant
from app.models.user import User


def _loaded_attr(value, name: str):
    loaded = inspect(value).attrs[name].loaded_value
    if loaded is NO_VALUE:
        return None
    return loaded


def _avatar_thumbnail_id(avatar_file) -> Optional[str]:
    if not avatar_file or avatar_file.is_deleted:
        return None
    links = inspect(avatar_file).attrs.derivatives.loaded_value
    if links is NO_VALUE:
        return None
    for link in links:
        if (
            link.variant == FileDerivativeVariant.THUMBNAIL
            and not link.derivative_file.is_deleted
        ):
            return link.derivative_file.public_id
    return None


def _avatar_ids(user: User) -> tuple[Optional[str], Optional[str]]:
    avatar_file = _loaded_attr(user, "avatar_file")
    avatar_file_id = None
    if avatar_file and not avatar_file.is_deleted:
        avatar_file_id = avatar_file.public_id
    return avatar_file_id, _avatar_thumbnail_id(avatar_file)


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: str = Field(..., min_length=3, max_length=128)
    password: str = Field(..., min_length=6, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("username only supports letters, numbers, and underscore")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("invalid email")
        return value


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip()


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    nickname: str
    is_active: bool
    is_admin: bool
    storage_used: int
    storage_quota: int
    task_count: int
    task_quota: int
    gpu_seconds_used: int
    gpu_quota: int
    gpu_concurrency_quota: int
    avatar_file_id: Optional[str] = None
    avatar_thumbnail_file_id: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        avatar_file_id, avatar_thumbnail_file_id = _avatar_ids(user)
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            nickname=user.nickname,
            is_active=user.is_active,
            is_admin=user.is_admin,
            storage_used=user.storage_used,
            storage_quota=user.storage_quota,
            task_count=user.task_count,
            task_quota=user.task_quota,
            gpu_seconds_used=user.gpu_seconds_used,
            gpu_quota=user.gpu_quota,
            gpu_concurrency_quota=user.gpu_concurrency_quota,
            avatar_file_id=avatar_file_id,
            avatar_thumbnail_file_id=avatar_thumbnail_file_id,
            created_at=user.created_at,
        )


class UserProfileResponse(BaseModel):
    id: int
    username: str
    email: str
    nickname: str
    is_admin: bool
    avatar_file_id: Optional[str] = None
    avatar_thumbnail_file_id: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserProfileResponse":
        avatar_file_id, avatar_thumbnail_file_id = _avatar_ids(user)
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            nickname=user.nickname,
            is_admin=user.is_admin,
            avatar_file_id=avatar_file_id,
            avatar_thumbnail_file_id=avatar_thumbnail_file_id,
            created_at=user.created_at,
        )


class AvatarResponse(BaseModel):
    avatar_file_id: Optional[str] = None
    avatar_thumbnail_file_id: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "AvatarResponse":
        avatar_file_id, avatar_thumbnail_file_id = _avatar_ids(user)
        return cls(
            avatar_file_id=avatar_file_id,
            avatar_thumbnail_file_id=avatar_thumbnail_file_id,
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: Optional[str] = None
    email: Optional[str] = None


class AvatarUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    avatar_file_id: Optional[str] = Field(
        ...,
        max_length=64,
        description="Existing image file_id to use as avatar; pass null to clear avatar.",
    )


class QuotaUpdate(BaseModel):
    storage_quota: Optional[int] = Field(None, ge=0)
    task_quota: Optional[int] = Field(None, ge=0)
    gpu_quota: Optional[int] = Field(None, ge=0)
    gpu_concurrency_quota: Optional[int] = Field(None, ge=0)


class UserUsageResponse(BaseModel):
    storage_used: int
    storage_quota: int
    task_count: int
    task_quota: int
    total_task_count: int
    gpu_running_count: int
    gpu_concurrency_quota: int
    gpu_seconds_used: int
    gpu_quota: int
    gpu_quota_exceeded: bool
    gpu_quota_resets_at: str


class GpuUsageResetResponse(BaseModel):
    user_id: int
    gpu_seconds_used: int
    gpu_quota: int
    gpu_quota_resets_at: str

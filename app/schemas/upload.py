from datetime import datetime
import re
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models.file import MediaProcessingStatus
from app.models.upload import UploadStatus

ALLOWED_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/mov",
    "video/webm",
    "video/x-msvideo",
    "video/x-matroska",
    "video/mpeg",
    "video/x-m4v",
    "video/3gpp",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "model/ply",
    "application/zip",
    "application/json",
    "other/zip",
    "other/json",
}
HEX_32_RE = re.compile(r"^[a-f0-9]{32}$")
HEX_64_RE = re.compile(r"^[a-f0-9]{64}$")


class UploadInitRequest(BaseModel):
    task_id: Optional[str] = Field(None, max_length=64)
    filename: str
    file_size: int = Field(..., gt=0)
    chunk_size: Optional[int] = Field(None, gt=0)
    mime_type: str
    file_hash: str

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MIME_TYPES:
            raise ValueError(
                "mime_type supports common video formats (mp4, quicktime, webm, avi, "
                "mkv, mpeg, m4v, 3gpp), image/jpeg, image/png, model/ply, "
                "application/zip, and application/json"
            )
        return normalized

    @field_validator("file_hash")
    @classmethod
    def validate_file_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HEX_64_RE.fullmatch(normalized):
            raise ValueError("file_hash must be a SHA-256 hex string")
        return normalized


class UploadInitResponse(BaseModel):
    task_id: Optional[str] = None
    upload_id: Optional[str] = None
    chunk_size: int
    total_chunks: int
    expires_at: Optional[datetime] = None
    already_uploaded: bool = False
    file_id: Optional[str] = None
    image_id: Optional[str] = None
    file_hash: Optional[str] = None
    storage_key: Optional[str] = None
    media_processing_status: Optional[MediaProcessingStatus] = None
    thumbnail_id: Optional[str] = None


class UploadPartResponse(BaseModel):
    received: bool
    chunk_index: int
    etag: str


class UploadStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    upload_id: str
    task_id: Optional[str] = None
    filename: str
    file_size: int
    total_chunks: int
    received_chunks: int
    status: UploadStatus
    chunk_statuses: List[int] = Field(default_factory=list)


class UploadMergePart(BaseModel):
    chunk_index: int = Field(..., ge=0)
    etag: str

    @field_validator("etag")
    @classmethod
    def validate_etag(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HEX_32_RE.fullmatch(normalized):
            raise ValueError("etag must be a chunk MD5 hex string")
        return normalized


class UploadMergeRequest(BaseModel):
    expected_hash: str = ""
    expected_size: int = Field(0, ge=0)
    parts: List[UploadMergePart]

    @field_validator("expected_hash")
    @classmethod
    def validate_expected_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and not (
            HEX_32_RE.fullmatch(normalized) or HEX_64_RE.fullmatch(normalized)
        ):
            raise ValueError("expected_hash must be an MD5 or SHA-256 hex string")
        return normalized


class UploadMergeResponse(BaseModel):
    task_id: Optional[str] = None
    file_id: str
    image_id: Optional[str] = None
    file_hash: str
    storage_key: str
    verified: bool
    already_uploaded: bool = False
    media_processing_status: MediaProcessingStatus
    thumbnail_id: Optional[str] = None

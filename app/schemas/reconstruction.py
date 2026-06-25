from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.file import FileCategory, FileType
from app.models.task import TaskStatus, TaskVisibility
from app.schemas.upload import HEX_64_RE, UploadMergePart


def _normalize_input_type(value: Optional[str], allowed: set[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    aliases = {"ply": "ply_model"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in allowed:
        display_allowed = set(allowed)
        if "ply_model" in display_allowed:
            display_allowed.add("ply")
        raise ValueError(f"input_type must be one of {', '.join(sorted(display_allowed))}")
    return normalized


class ReconstructionAlgorithmParamResponse(BaseModel):
    param_name: str
    description: str
    display_name: str
    default_value: Any


class ReconstructionAlgorithmDependencyResponse(BaseModel):
    required_stage: Optional[str] = None
    required_gaussian_algorithms: List[str] = Field(default_factory=list)
    required_input_type: Optional[str] = None
    description: str = ""


class ReconstructionAlgorithmResponse(BaseModel):
    name: str
    display_name: str
    available: bool
    params: List[ReconstructionAlgorithmParamResponse] = Field(default_factory=list)
    dependencies: ReconstructionAlgorithmDependencyResponse = Field(
        default_factory=ReconstructionAlgorithmDependencyResponse
    )


class ReconstructionAlgorithmsResponse(BaseModel):
    algorithms: List[ReconstructionAlgorithmResponse]
    default_algorithm: str


class ReconstructionTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    algorithm: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ReconstructionTaskCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus
    status_code: int
    algorithm: str
    params: Dict[str, Any] = Field(default_factory=dict)
    visibility: TaskVisibility
    current_stage: str
    created_at: str


class ReconstructionStartByImagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_type: Optional[str] = Field(
        default=None,
        description="Optional input declaration: image, image_folder, video, ply, or ply_model.",
    )
    input_file_ids: List[str] = Field(default_factory=list)

    @field_validator("input_type")
    @classmethod
    def validate_input_type(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_input_type(value, {"image", "image_folder", "video", "ply_model"})


class ReconstructionMeshStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: str = Field(..., min_length=1, max_length=64)
    input_file_ids: List[str] = Field(..., min_length=1)
    params: Dict[str, Any] = Field(default_factory=dict)


class ReconstructionStartResponse(BaseModel):
    task_id: str
    status: TaskStatus
    status_code: int
    algorithm: str
    current_stage: str
    input_type: str
    input_file_count: int
    queue_reason: Optional[str] = None


class ReconstructionResultFileResponse(BaseModel):
    file_id: str
    category: FileCategory
    file_type: FileType
    mime_type: str
    filename: str


class ReconstructionTaskResultResponse(BaseModel):
    file_id: str
    filename: str
    file_type: FileType
    category: Literal["render_model", "mesh_model"]
    mime_type: str
    size_bytes: int


class ReconstructionStatusResponse(BaseModel):
    task_id: str
    user_id: int
    title: str
    algorithm: str
    params: Dict[str, Any]
    gaussian_algorithm: str
    gaussian_params: Dict[str, Any]
    mesh_algorithm: Optional[str] = None
    mesh_params: Dict[str, Any]
    visibility: TaskVisibility
    status: TaskStatus
    status_code: int
    current_stage: str
    progress: float
    queue_reason: Optional[str] = None
    input_kind: str
    input_file_ids: List[str] = Field(default_factory=list)
    result_id: Optional[str] = None
    result_file_id: Optional[str] = None
    result_storage_key: Optional[str] = None
    ply_id: Optional[str] = None
    results: List[ReconstructionTaskResultResponse] = Field(default_factory=list)
    result_files: List[ReconstructionResultFileResponse] = Field(default_factory=list)
    preview_ids: List[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_status_code: Optional[int] = None
    error: Optional[str] = None
    worker_node_id: Optional[str] = None
    executor_id: Optional[str] = None
    cuda_device: Optional[str] = None
    execution_attempt: int = 0
    gpu_seconds_cost: int = 0
    gpu_quota_exceeded: bool = False
    cancel_requested: bool
    created_at: str
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None


class ReconstructionTaskListResponse(BaseModel):
    tasks: List[ReconstructionStatusResponse]
    total: int


class ReconstructionDiscoverResponse(BaseModel):
    tasks: List[ReconstructionStatusResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class ReconstructionTaskInputsResponse(BaseModel):
    task_id: str
    input_kind: str
    input_file_ids: List[str] = Field(default_factory=list)
    input_file_count: int


class ReconstructionResultReplaceInitRequest(BaseModel):
    filename: str
    file_size: int = Field(..., gt=0)
    chunk_size: Optional[int] = Field(None, gt=0)
    mime_type: str = "model/ply"
    file_hash: str

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "model/ply":
            raise ValueError("Only model/ply result replacement is supported")
        return normalized

    @field_validator("file_hash")
    @classmethod
    def validate_file_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HEX_64_RE.fullmatch(normalized):
            raise ValueError("file_hash must be a SHA-256 hex string")
        return normalized


class ReconstructionResultReplaceInitResponse(BaseModel):
    task_id: str
    file_id: str
    upload_id: str
    chunk_size: int
    total_chunks: int
    expires_at: Optional[str] = None


class ReconstructionResultReplaceCompleteRequest(BaseModel):
    expected_hash: str = ""
    expected_size: int = Field(0, ge=0)
    parts: List[UploadMergePart]


class ReconstructionResultReplaceCompleteResponse(BaseModel):
    task_id: str
    file_id: str
    filename: str
    mime_type: str
    file_size: int
    file_hash: str
    replaced: bool
    verified: bool


class ReconstructionVisibilityRequest(BaseModel):
    visibility: TaskVisibility


class ReconstructionDeleteResponse(BaseModel):
    task_id: str
    deleted: bool
    status: TaskStatus


class ReconstructionLogsResponse(BaseModel):
    task_id: str
    status: TaskStatus
    error_code: Optional[str] = None
    error_status_code: Optional[int] = None
    error: Optional[str] = None
    stdout_tail: str = ""
    stderr_tail: str = ""


class ReconstructionCancelResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cancelled: bool
    message: str = ""


class ReconstructionDiagnosticCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class ReconstructionDiagnosticsResponse(BaseModel):
    task_id: str
    status: TaskStatus
    algorithm: str
    error_code: Optional[str] = None
    error_status_code: Optional[int] = None
    checks: List[ReconstructionDiagnosticCheck]

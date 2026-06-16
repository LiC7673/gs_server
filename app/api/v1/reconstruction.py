import asyncio
import ast
import json
import mimetypes
import os
import shlex
import shutil
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.algorithm_stages import (
    GAUSSIAN_ALGORITHMS,
    GAUSSIAN_STAGE,
    MESH_ALGORITHMS,
    MESH_STAGE,
)
from app.core.config import settings
from app.core.database import async_session_factory, get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.exceptions import AppException, QuotaExceededException, TaskStateException
from app.core.storage import get_storage_backend
from app.models.file import FileCategory, FileRecord
from app.models.task import TaskFileRecord, TaskFileRole, TaskRecord, TaskStatus, TaskVisibility
from app.models.user import User
from app.schemas.reconstruction import (
    ReconstructionAlgorithmResponse,
    ReconstructionAlgorithmsResponse,
    ReconstructionCancelResponse,
    ReconstructionDeleteResponse,
    ReconstructionDiscoverResponse,
    ReconstructionDiagnosticCheck,
    ReconstructionDiagnosticsResponse,
    ReconstructionLogsResponse,
    ReconstructionMeshStartRequest,
    ReconstructionStartByImagesRequest,
    ReconstructionStartResponse,
    ReconstructionStatusResponse,
    ReconstructionTaskCreateRequest,
    ReconstructionTaskCreateResponse,
    ReconstructionTaskInputsResponse,
    ReconstructionTaskListResponse,
    ReconstructionVisibilityRequest,
)
from app.services.file_service import FileService
from app.services.gpu_scheduler import GPULease, GPUScheduler
from app.services.task_service import TaskService
from app.services.user_service import UserService

router = APIRouter(prefix="/reconstruction", tags=["reconstruction"])

ANYSPLAT_DEFAULT_PARAMS = {
    "frame_nums": 4,
    "crop_quantile": 0.8,
}
ANYSPLAT_ALLOWED_PARAMS = {"algorithm", *ANYSPLAT_DEFAULT_PARAMS}
DASH_GAUSSIAN_DEFAULT_PARAMS = {
    "iterations": 30000,
}
DASH_GAUSSIAN_ALLOWED_PARAMS = {"algorithm", *DASH_GAUSSIAN_DEFAULT_PARAMS}
DASH_GAUSSIAN_MESH_DEFAULT_PARAMS = {
    "radius": 4,
    "cluster_voxel_size": 0.05,
    "keep_largest": True,
    "iteration": 30000,
    "views": "train",
    "voxel_size": 0.02,
    "sdf_trunc": 0.36,
    "alpha_threshold": 0.35,
    "max_depth": 25,
    "depth_quantile": 0.9,
    "mask_erode": 2,
}
DASH_GAUSSIAN_MESH_ALLOWED_PARAMS = {"radius", *DASH_GAUSSIAN_MESH_DEFAULT_PARAMS}
@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    display_name: str
    python_path: str
    algorithm_path: str
    entrypoint: str
    args_template: str
    timeout_seconds: int
    stage: str = GAUSSIAN_STAGE
    command_template: str = ""
    accepted_input_types: Tuple[str, ...] = ("image_folder",)
    output_glob: str = "**/*.ply"
    result_category: str = FileCategory.PLY_MODEL.value
    result_mime_type: str = "model/ply"
    result_filename: str = "result.ply"

    @property
    def available(self) -> bool:
        return bool(self.python_path and self.algorithm_path and self.entrypoint)


@dataclass(frozen=True)
class DashGaussianMeshPipeline:
    commands: List[List[str]]
    model_root: Path
    point_cloud_path: Path
    mesh_output_dir: Path
    mesh_path: Path
    cluster_filtered_path: Path


def _algorithm_specs() -> Dict[str, AlgorithmSpec]:
    return {
        "anysplat": AlgorithmSpec(
            name="anysplat",
            display_name="AnySplat",
            python_path=settings.anysplat_python_path,
            algorithm_path=settings.anysplat_path,
            entrypoint=settings.anysplat_entrypoint,
            args_template=settings.anysplat_args_template,
            timeout_seconds=settings.anysplat_timeout_seconds,
            accepted_input_types=("image_folder", "video"),
            output_glob=settings.reconstruction_output_ply_glob,
        ),
        "dash_gaussian": AlgorithmSpec(
            name="dash_gaussian",
            display_name="DashGaussian",
            python_path=settings.dash_gaussian_conda_path,
            algorithm_path=settings.dash_gaussian_path,
            entrypoint=settings.dash_gaussian_entrypoint,
            args_template=settings.dash_gaussian_args_template,
            timeout_seconds=settings.dash_gaussian_timeout_seconds,
            command_template=settings.dash_gaussian_command_template,
            accepted_input_types=("image_folder", "video"),
            result_filename="point_cloud.ply",
        ),
        "dash_gaussian_mesh": AlgorithmSpec(
            name="dash_gaussian_mesh",
            display_name="DashGaussian TSDF Mesh",
            python_path=settings.dash_gaussian_conda_path,
            algorithm_path=settings.dash_gaussian_path,
            entrypoint="scripts/render_depth_tsdf_mesh.py",
            args_template="",
            timeout_seconds=settings.dash_gaussian_timeout_seconds,
            stage=MESH_STAGE,
            accepted_input_types=("ply_model",),
            result_category=FileCategory.MESH_MODEL.value,
            result_mime_type="model/obj",
            result_filename="dash_gaussian_mesh.obj",
        ),
        "vggt_omega": AlgorithmSpec(
            name="vggt_omega",
            display_name="VGGT Omega",
            python_path=settings.vggt_omega_python_path,
            algorithm_path=settings.vggt_omega_path,
            entrypoint=settings.vggt_omega_entrypoint,
            args_template=settings.vggt_omega_args_template,
            timeout_seconds=settings.vggt_omega_timeout_seconds,
            command_template=settings.vggt_omega_command_template,
            accepted_input_types=("image_folder", "video"),
            output_glob=settings.vggt_omega_output_glob,
            result_category=settings.vggt_omega_result_category,
            result_mime_type=settings.vggt_omega_result_mime_type,
            result_filename=settings.vggt_omega_result_filename,
        ),
        "hunyuan3d": AlgorithmSpec(
            name="hunyuan3d",
            display_name="Hunyuan3D 2.1",
            python_path=settings.hunyuan3d_conda_path,
            algorithm_path=settings.hunyuan3d_path,
            entrypoint=settings.hunyuan3d_entrypoint,
            args_template=settings.hunyuan3d_args_template,
            timeout_seconds=settings.hunyuan3d_timeout_seconds,
            stage=MESH_STAGE,
            command_template=settings.hunyuan3d_command_template,
            accepted_input_types=("image", "image_folder", "video"),
            output_glob="**/*.glb",
            result_category=FileCategory.GLB_MODEL.value,
            result_mime_type="model/gltf-binary",
            result_filename="hunyuan3d_result.glb",
        ),
    }


def _get_algorithm_spec(name: str) -> AlgorithmSpec:
    spec = _algorithm_specs().get(name)
    if not spec:
        raise AppException(f"Unsupported reconstruction algorithm: {name}", status.HTTP_400_BAD_REQUEST)
    if not spec.available:
        raise AppException(f"Algorithm is not configured: {name}", status.HTTP_503_SERVICE_UNAVAILABLE)
    return spec


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _prepare_algorithm_process() -> None:
    os.setsid()
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        libc.prctl(1, signal.SIGTERM)
        if os.getppid() == 1:
            os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        pass


async def _terminate_algorithm_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        if process.returncode is None:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _task_status_code(task: TaskRecord) -> int:
    if task.status == TaskStatus.PENDING:
        return 100
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROCESSING}:
        return 102
    if task.status == TaskStatus.COMPLETED:
        return 200
    if task.status == TaskStatus.PARTIAL_COMPLETED:
        return status.HTTP_206_PARTIAL_CONTENT
    if task.status == TaskStatus.CANCELLED:
        return status.HTTP_409_CONFLICT
    return task.error_status_code or status.HTTP_500_INTERNAL_SERVER_ERROR


def _json_dict(value: Optional[str]) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _params(task: TaskRecord) -> Dict[str, Any]:
    return _json_dict(task.params)


def _queued_stage(algorithm: str) -> str:
    return "mesh_queued" if algorithm in MESH_ALGORITHMS else "gaussian_queued"


def _processing_stage(algorithm: str) -> str:
    return "mesh_processing" if algorithm in MESH_ALGORITHMS else "gaussian_processing"


def _completed_stage(algorithm: str) -> str:
    return "mesh_completed" if algorithm in MESH_ALGORITHMS else "gaussian_completed"


def _processing_progress(algorithm: str) -> float:
    return 10.0


def _normalize_anysplat_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    actual = dict(params or {})
    unknown = sorted(set(actual) - ANYSPLAT_ALLOWED_PARAMS)
    if unknown:
        raise AppException(
            f"Unsupported AnySplat params: {', '.join(unknown)}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    selector = actual.get("algorithm")
    if selector is not None and selector != "anysplat":
        raise AppException(
            "params.algorithm must be 'anysplat'",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    frame_nums = actual.get("frame_nums", ANYSPLAT_DEFAULT_PARAMS["frame_nums"])
    if isinstance(frame_nums, bool) or not isinstance(frame_nums, int):
        raise AppException(
            "params.frame_nums must be an integer",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    crop_quantile = actual.get("crop_quantile", ANYSPLAT_DEFAULT_PARAMS["crop_quantile"])
    if isinstance(crop_quantile, bool) or not isinstance(crop_quantile, (int, float)):
        raise AppException(
            "params.crop_quantile must be a number",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    normalized = {
        "frame_nums": frame_nums,
        "crop_quantile": crop_quantile,
    }
    if selector is not None:
        normalized["algorithm"] = selector
    return normalized


def _normalize_dash_gaussian_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    actual = dict(params or {})
    unknown = sorted(set(actual) - DASH_GAUSSIAN_ALLOWED_PARAMS)
    if unknown:
        raise AppException(
            f"Unsupported DashGaussian params: {', '.join(unknown)}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    selector = actual.get("algorithm")
    if selector is not None and selector != "dash_gaussian":
        raise AppException(
            "params.algorithm must be 'dash_gaussian'",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    iterations = actual.get("iterations", DASH_GAUSSIAN_DEFAULT_PARAMS["iterations"])
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
        raise AppException(
            "params.iterations must be a positive integer",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    normalized = {"iterations": iterations}
    if selector is not None:
        normalized["algorithm"] = selector
    return normalized


def _require_number(name: str, value: Any, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppException(
            f"params.{name} must be a number",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    actual = float(value)
    if positive and actual <= 0:
        raise AppException(
            f"params.{name} must be greater than 0",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return actual


def _require_int(name: str, value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppException(
            f"params.{name} must be an integer",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if value < minimum:
        raise AppException(
            f"params.{name} must be >= {minimum}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return value


def _normalize_dash_gaussian_mesh_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    actual = dict(params or {})
    unknown = sorted(set(actual) - DASH_GAUSSIAN_MESH_ALLOWED_PARAMS)
    if unknown:
        raise AppException(
            f"Unsupported DashGaussian mesh params: {', '.join(unknown)}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    normalized: Dict[str, Any] = dict(DASH_GAUSSIAN_MESH_DEFAULT_PARAMS)
    if "radius" in actual:
        normalized["radius"] = actual["radius"]
    radius = _require_number("radius", normalized["radius"])
    if radius < 4 or radius > 25:
        raise AppException(
            "params.radius must be between 4 and 25",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    for name in (
        "cluster_voxel_size",
        "voxel_size",
        "sdf_trunc",
        "max_depth",
    ):
        if name in actual:
            normalized[name] = actual[name]
        _require_number(name, normalized[name])
    for name in ("alpha_threshold", "depth_quantile"):
        if name in actual:
            normalized[name] = actual[name]
        _require_number(name, normalized[name], positive=False)
    if "keep_largest" in actual:
        if not isinstance(actual["keep_largest"], bool):
            raise AppException(
                "params.keep_largest must be a boolean",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        normalized["keep_largest"] = actual["keep_largest"]
    if "iteration" in actual:
        normalized["iteration"] = _require_int("iteration", actual["iteration"])
    else:
        normalized["iteration"] = _require_int("iteration", normalized["iteration"])
    if "mask_erode" in actual:
        normalized["mask_erode"] = _require_int("mask_erode", actual["mask_erode"], minimum=0)
    else:
        normalized["mask_erode"] = _require_int("mask_erode", normalized["mask_erode"], minimum=0)
    if "views" in actual:
        if not isinstance(actual["views"], str) or not actual["views"].strip():
            raise AppException(
                "params.views must be a non-empty string",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        normalized["views"] = actual["views"].strip()
    return normalized


def _normalize_task_params(algorithm: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if algorithm == "anysplat":
        return _normalize_anysplat_params(params)
    if algorithm == "dash_gaussian":
        return _normalize_dash_gaussian_params(params)
    if algorithm == "dash_gaussian_mesh":
        return _normalize_dash_gaussian_mesh_params(params)
    return dict(params or {})


def _role_file_ids(task: TaskRecord, role: TaskFileRole) -> List[str]:
    return [
        link.file.public_id
        for link in sorted(task.file_links, key=lambda item: item.id or 0)
        if link.role == role and not link.file.is_deleted
    ]


def _current_input_file_ids(task: TaskRecord) -> List[str]:
    if task.algorithm in MESH_ALGORITHMS:
        return [
            link.file.public_id
            for link in sorted(task.file_links, key=lambda item: item.id or 0)
            if link.role == TaskFileRole.INTERMEDIATE
            and link.file
            and not link.file.is_deleted
        ]
    return _original_input_file_ids(task)


def _original_input_file_ids(task: TaskRecord) -> List[str]:
    return [
        link.file.public_id
        for link in sorted(task.file_links, key=lambda item: item.id or 0)
        if link.role == TaskFileRole.INPUT
        and link.file
        and not link.file.is_deleted
        and (
            (link.file.mime_type or "").startswith("image/")
            or (link.file.mime_type or "").startswith("video/")
        )
    ]


def _original_input_kind(task: TaskRecord) -> str:
    files = [
        link.file
        for link in task.file_links
        if link.role == TaskFileRole.INPUT
        and link.file
        and not link.file.is_deleted
        and (
            (link.file.mime_type or "").startswith("image/")
            or (link.file.mime_type or "").startswith("video/")
        )
    ]
    if len(files) == 1 and (files[0].mime_type or "").startswith("video/"):
        return "video"
    if files and all((file.mime_type or "").startswith("image/") for file in files):
        return "image" if len(files) == 1 else "image_folder"
    return ""


def _task_response(task: TaskRecord, *, include_private: bool = False) -> ReconstructionStatusResponse:
    result_ids = _role_file_ids(task, TaskFileRole.RESULT)
    preview_ids = _role_file_ids(task, TaskFileRole.PREVIEW)
    input_ids = _current_input_file_ids(task)
    result_files = sorted(
        [
            link.file
            for link in task.file_links
            if link.role == TaskFileRole.RESULT and not link.file.is_deleted
        ],
        key=lambda file: (file.category != FileCategory.GLB_MODEL, file.id),
    )
    primary_mesh_id = None
    if task.algorithm in MESH_ALGORITHMS:
        primary_mesh_id = next(
            (
                file.public_id
                for file in result_files
                if (file.metainfo or {}).get("generated_by") == task.algorithm
                and bool((file.metainfo or {}).get("primary_result"))
            ),
            None,
        )
    preferred_categories = {
        "hunyuan3d": (FileCategory.GLB_MODEL,),
        "dash_gaussian_mesh": (FileCategory.MESH_MODEL,),
    }.get(task.algorithm, (FileCategory.PLY_MODEL, FileCategory.GLB_MODEL, FileCategory.MESH_MODEL))
    preferred_id = next(
        (
            file.public_id
            for category in preferred_categories
            for file in result_files
            if file.category == category
        ),
        None,
    )
    result_id = primary_mesh_id or preferred_id or (result_ids[0] if result_ids else None)
    ply_id = next(
        (
            link.file.public_id
            for link in task.file_links
            if link.role == TaskFileRole.RESULT
            and not link.file.is_deleted
            and link.file.category == FileCategory.PLY_MODEL
        ),
        None,
    )
    return ReconstructionStatusResponse(
        task_id=task.public_id,
        user_id=task.user_id,
        title=task.title or "",
        algorithm=task.algorithm,
        params=_params(task),
        gaussian_algorithm=task.gaussian_algorithm or task.algorithm,
        gaussian_params=_json_dict(task.gaussian_params or task.params),
        mesh_algorithm=task.mesh_algorithm,
        mesh_params=_json_dict(task.mesh_params),
        visibility=task.visibility,
        status=task.status,
        status_code=_task_status_code(task),
        current_stage=task.current_stage or task.status.value,
        progress=float(task.progress or 0.0),
        queue_reason=task.queue_reason or None,
        input_kind=task.input_kind or "",
        input_file_ids=input_ids if include_private else [],
        result_id=result_id,
        result_file_id=result_id,
        result_storage_key=result_id,
        ply_id=ply_id,
        result_files=[
            {
                "file_id": file.public_id,
                "category": file.category,
                "file_type": file.file_type,
                "mime_type": file.mime_type,
                "filename": file.filename,
            }
            for file in result_files
        ],
        preview_ids=preview_ids,
        error_code=(task.error_code or None) if include_private else None,
        error_status_code=(task.error_status_code or None) if include_private else None,
        error=(task.error_message or None) if include_private else None,
        worker_node_id=(task.worker_node_id or None) if include_private else None,
        executor_id=(task.executor_id or None) if include_private else None,
        cuda_device=(task.cuda_device or None) if include_private else None,
        execution_attempt=int(task.execution_attempt or 0) if include_private else 0,
        gpu_seconds_cost=int(task.gpu_seconds_cost or 0),
        gpu_quota_exceeded=task.error_code == "GPU_DAILY_QUOTA_EXCEEDED",
        cancel_requested=bool(task.cancel_requested),
        created_at=_iso(task.created_at) or "",
        started_at=_iso(task.started_at),
        updated_at=_iso(task.updated_at),
        completed_at=_iso(task.completed_at),
    )


def _has_ply_result(task: TaskRecord) -> bool:
    return any(
        link.role == TaskFileRole.RESULT
        and link.file
        and not link.file.is_deleted
        and _is_ply_model_file(link.file)
        for link in task.file_links
    )


def _set_failed(
    task: TaskRecord,
    code: str,
    message: str,
    status_code: int = 500,
    *,
    stage: str = "failed",
) -> None:
    task.status = TaskStatus.FAILED
    task.current_stage = stage
    task.progress = 100.0
    task.error_code = code
    task.error_status_code = status_code
    task.error_message = message[:1000]
    task.completed_at = _utc_now()
    task.heartbeat_at = _utc_now()
    task.queue_reason = ""


def _set_partial_completed(
    task: TaskRecord,
    code: str,
    message: str,
    status_code: int = 500,
    *,
    stage: str = "mesh_failed",
) -> None:
    task.status = TaskStatus.PARTIAL_COMPLETED
    task.current_stage = stage
    task.progress = 100.0
    task.error_code = code
    task.error_status_code = status_code
    task.error_message = message[:1000]
    task.completed_at = _utc_now()
    task.heartbeat_at = _utc_now()
    task.queue_reason = ""


def _set_stage_failed(task: TaskRecord, code: str, message: str, status_code: int = 500) -> None:
    if task.algorithm in MESH_ALGORITHMS:
        _set_partial_completed(task, code, message, status_code)
        return
    if task.algorithm in GAUSSIAN_ALGORITHMS and _has_ply_result(task):
        _set_partial_completed(task, code, message, status_code, stage="gaussian_failed")
        return
    stage = "gaussian_failed" if task.algorithm in GAUSSIAN_ALGORITHMS else "failed"
    _set_failed(task, code, message, status_code, stage=stage)


def _set_cancelled(task: TaskRecord, message: str = "Task cancelled") -> None:
    task.status = TaskStatus.CANCELLED
    task.current_stage = "cancelled"
    task.progress = 100.0
    task.error_code = "TASK_CANCELLED"
    task.error_status_code = status.HTTP_409_CONFLICT
    task.error_message = message[:1000]
    task.completed_at = _utc_now()
    task.heartbeat_at = _utc_now()
    task.queue_reason = ""


def _tail(path: Path, limit: int = 4000) -> str:
    if not path.exists() or limit <= 0:
        return ""
    with path.open("rb") as handle:
        try:
            handle.seek(-limit, os.SEEK_END)
        except OSError:
            handle.seek(0)
        return handle.read()[-limit:].decode("utf-8", errors="replace")


def _shell_split(value: str) -> List[str]:
    return shlex.split(value, posix=os.name != "nt")


def _build_command(
    spec: AlgorithmSpec,
    task_id: str,
    image_dir: Path,
    input_path: Path,
    output_dir: Path,
    params: Optional[Dict[str, Any]] = None,
) -> List[str]:
    output_glb = output_dir / "hunyuan3d_result.glb"
    values = {
        "image_dir": str(image_dir),
        "input_path": str(input_path),
        "output_folder": str(output_glb) if spec.name == "hunyuan3d" else str(output_dir),
        "output_glb": str(output_glb),
        "task_id": task_id,
        "python_path": spec.python_path,
        "entrypoint": spec.entrypoint,
    }
    values.update(params or {})
    args = _shell_split(spec.args_template.format(**values))
    if spec.command_template:
        prefix = _shell_split(spec.command_template.replace("{args}", "").format(**values))
        return [*prefix, *args] if "{args}" in spec.command_template else prefix
    return [spec.python_path, spec.entrypoint, *args]


def _dash_gaussian_mesh_command(spec: AlgorithmSpec, script: str, args: List[str]) -> List[str]:
    return [spec.python_path, "run", "-n", "DashGaussian", "python", script, *args]


def _build_dash_gaussian_mesh_pipeline(
    spec: AlgorithmSpec,
    input_path: Path,
    scratch_dir: Path,
    output_dir: Path,
    params: Dict[str, Any],
) -> DashGaussianMeshPipeline:
    mesh_dir = scratch_dir / "mesh_pipeline"
    radius_filtered_path = mesh_dir / "radius_filtered.ply"
    cluster_filtered_path = mesh_dir / "cluster_filtered.ply"
    model_root = output_dir / "dash_gaussian_mesh_model"
    iteration = int(params["iteration"])
    point_cloud_path = model_root / "point_cloud" / f"iteration_{iteration}" / "point_cloud.ply"
    mesh_output_dir = output_dir / "dash_gaussian_mesh"
    mesh_path = mesh_output_dir / "dash_gaussian_mesh.obj"

    radius_args = [
        "-i",
        str(input_path),
        "-o",
        str(radius_filtered_path),
        "-r",
        str(params["radius"]),
    ]

    cluster_args = [
        "-i",
        str(radius_filtered_path),
        "-o",
        str(cluster_filtered_path),
        "-v",
        str(params["cluster_voxel_size"]),
    ]
    if params.get("keep_largest", True):
        cluster_args.extend(["--keep", "largest"])

    render_args = [
        "-m",
        str(model_root),
        "--iteration",
        str(iteration),
        "--views",
        str(params["views"]),
        "--voxel_size",
        str(params["voxel_size"]),
        "--sdf_trunc",
        str(params["sdf_trunc"]),
        "--alpha_threshold",
        str(params["alpha_threshold"]),
        "--max_depth",
        str(params["max_depth"]),
        "--depth_quantile",
        str(params["depth_quantile"]),
        "--mask_erode",
        str(params["mask_erode"]),
        "--output",
        str(mesh_path),
    ]
    return DashGaussianMeshPipeline(
        commands=[
            _dash_gaussian_mesh_command(spec, "scripts/filter_gaussians_by_radius.py", radius_args),
            _dash_gaussian_mesh_command(spec, "scripts/filter_gaussians_by_cluster.py", cluster_args),
            _dash_gaussian_mesh_command(spec, "scripts/render_depth_tsdf_mesh.py", render_args),
        ],
        model_root=model_root,
        point_cloud_path=point_cloud_path,
        mesh_output_dir=mesh_output_dir,
        mesh_path=mesh_path,
        cluster_filtered_path=cluster_filtered_path,
    )


def _find_output_file(output_dir: Path, glob_pattern: str) -> Optional[Path]:
    ignored = {"stdout.log", "stderr.log", "run_cmd.txt"}
    candidates = [
        path
        for path in output_dir.glob(glob_pattern)
        if path.is_file() and path.name not in ignored and path.stat().st_size > 0
    ]
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


GENERATED_OUTPUT_IGNORED_NAMES = {"stdout.log", "stderr.log", "run_cmd.txt"}
GENERATED_OUTPUT_IGNORED_SUFFIXES = {".tmp", ".part", ".lock"}


def _generated_result_type(path: Path) -> Tuple[FileCategory, str]:
    if path.name == "cfg_args":
        return FileCategory.OTHER, "text/plain"
    suffix = path.suffix.lower()
    explicit_types = {
        ".glb": (FileCategory.GLB_MODEL, "model/gltf-binary"),
        ".gltf": (FileCategory.GLB_MODEL, "model/gltf+json"),
        ".obj": (FileCategory.MESH_MODEL, "model/obj"),
        ".ply": (FileCategory.PLY_MODEL, "model/ply"),
        ".mtl": (FileCategory.OTHER, "model/mtl"),
        ".json": (FileCategory.OTHER, "application/json"),
        ".zip": (FileCategory.OTHER, "application/zip"),
    }
    if suffix in explicit_types:
        return explicit_types[suffix]
    guessed_type, _ = mimetypes.guess_type(path.name)
    return FileCategory.OTHER, guessed_type or "application/octet-stream"


def _generated_output_files(output_dir: Path) -> List[Path]:
    return sorted(
        (
            path
            for path in output_dir.rglob("*")
            if path.is_file()
            and path.name not in GENERATED_OUTPUT_IGNORED_NAMES
            and path.suffix.lower() not in GENERATED_OUTPUT_IGNORED_SUFFIXES
            and path.stat().st_size > 0
        ),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )


def _safe_relative_output_path(value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value.strip())
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return relative


def _rewrite_dash_gaussian_cfg_args(cfg_path: Path, *, model_path: str, source_path: str) -> None:
    if not cfg_path.is_file():
        return
    raw = cfg_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return
    try:
        tree = ast.parse(raw, mode="eval")
        call = tree.body
        if not isinstance(call, ast.Call):
            raise ValueError("cfg_args is not a call expression")
        existing_args = {keyword.arg for keyword in call.keywords if keyword.arg}
        for keyword in call.keywords:
            if keyword.arg == "model_path":
                keyword.value = ast.Constant(model_path)
            elif keyword.arg == "source_path":
                keyword.value = ast.Constant(source_path)
        if "model_path" not in existing_args:
            call.keywords.append(ast.keyword(arg="model_path", value=ast.Constant(model_path)))
        if "source_path" not in existing_args:
            call.keywords.append(ast.keyword(arg="source_path", value=ast.Constant(source_path)))
        cfg_path.write_text(ast.unparse(call) + "\n", encoding="utf-8")
    except Exception:
        cfg_path.write_text(
            f"Namespace(model_path={model_path!r}, source_path={source_path!r})\n",
            encoding="utf-8",
        )


def _category(value: str) -> FileCategory:
    try:
        return FileCategory(value)
    except ValueError:
        return FileCategory.OTHER


def _is_ply_model_file(record: FileRecord) -> bool:
    names = [record.filename or "", record.original_name or ""]
    return (
        record.category == FileCategory.PLY_MODEL
        or (record.mime_type or "").lower() == "model/ply"
        or any(Path(name).suffix.lower() == ".ply" for name in names)
    )


def _requested_type_matches(requested_type: Optional[str], input_kind: str) -> bool:
    if not requested_type:
        return True
    if requested_type == "image":
        return input_kind in {"image", "image_folder"}
    return requested_type == input_kind


def _selected_start_file_ids(body: ReconstructionStartByImagesRequest) -> List[str]:
    return list(body.input_file_ids or [])


def _new_dash_gaussian_generation_id() -> str:
    return f"dash_gaussian_{uuid4().hex}"


def _select_dash_gaussian_restore_links_for_ply(
    result_links: List[TaskFileRecord],
    ply_file_id: str,
) -> Tuple[List[TaskFileRecord], Optional[str]]:
    selected_link = next(
        (
            link
            for link in result_links
            if link.role == TaskFileRole.RESULT
            and link.file
            and not link.file.is_deleted
            and link.file.public_id == ply_file_id
            and _is_ply_model_file(link.file)
        ),
        None,
    )
    if not selected_link:
        return [], "Selected PLY result is not linked to this task"

    selected_meta = selected_link.file.metainfo or {}
    selected_generator = selected_meta.get("generated_by")
    if selected_generator != "dash_gaussian":
        return [], "Selected PLY was not generated by DashGaussian; rerun the Gaussian stage before Mesh"

    generation_id = selected_meta.get("generation_id")
    if generation_id:
        selected = [
            link
            for link in result_links
            if link.role == TaskFileRole.RESULT
            and link.file
            and not link.file.is_deleted
            and (link.file.metainfo or {}).get("generated_by") == "dash_gaussian"
            and (link.file.metainfo or {}).get("generation_id") == generation_id
        ]
        if not selected:
            return [], "DashGaussian files for the selected PLY generation are missing"
        return selected, None

    # Compatibility path for DashGaussian files created before generation_id existed.
    legacy = [
        link
        for link in result_links
        if link.role == TaskFileRole.RESULT
        and link.file
        and not link.file.is_deleted
        and (link.file.metainfo or {}).get("generated_by") == "dash_gaussian"
    ]
    if not legacy:
        return [], "DashGaussian model directory files are missing; rerun the Gaussian stage before Mesh"
    return legacy, None


def _latest_ply_result_id(task: TaskRecord) -> Optional[str]:
    links = sorted(task.file_links, key=lambda item: item.id or 0, reverse=True)
    return next(
        (
            link.file.public_id
            for link in links
            if link.role == TaskFileRole.RESULT
            and link.file
            and not link.file.is_deleted
            and _is_ply_model_file(link.file)
        ),
        None,
    )


async def _input_links(db: AsyncSession, task: TaskRecord) -> List[TaskFileRecord]:
    roles = [TaskFileRole.INTERMEDIATE] if task.algorithm in MESH_ALGORITHMS else [TaskFileRole.INPUT]
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file).selectinload(FileRecord.storage_object))
        .where(TaskFileRecord.task_id == task.id, TaskFileRecord.role.in_(roles))
        .order_by(TaskFileRecord.id)
    )
    links = list(result.scalars().all())
    if task.input_kind == "ply_model":
        return [link for link in links if _is_ply_model_file(link.file)][:1]
    if task.input_kind in {"image", "image_folder"}:
        return [link for link in links if (link.file.mime_type or "").startswith("image/")]
    if task.input_kind == "video":
        return [link for link in links if (link.file.mime_type or "").startswith("video/")]
    return links


async def _stage_inputs(task: TaskRecord, links: List[TaskFileRecord], scratch_dir: Path) -> Tuple[Path, Path]:
    storage = get_storage_backend()
    image_dir = scratch_dir / "images"
    input_dir = scratch_dir / "input"
    image_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    if task.input_kind == "video":
        file = links[0].file
        video_path = input_dir / Path(file.original_name or file.filename).name
        await storage.download_file(file.storage_object.object_key, video_path)
        return image_dir, video_path
    if task.input_kind == "image":
        file = links[0].file
        extension = Path(file.original_name or file.filename).suffix or ".jpg"
        image_path = input_dir / f"image{extension.lower()}"
        await storage.download_file(file.storage_object.object_key, image_path)
        return image_dir, image_path
    if task.input_kind == "ply_model":
        file = links[0].file
        ply_name = Path(file.original_name or file.filename or "input.ply").name
        if Path(ply_name).suffix.lower() != ".ply":
            ply_name = "input.ply"
        ply_path = input_dir / ply_name
        await storage.download_file(file.storage_object.object_key, ply_path)
        return image_dir, ply_path
    for index, link in enumerate(links):
        file = link.file
        extension = Path(file.original_name or file.filename).suffix or ".jpg"
        await storage.download_file(file.storage_object.object_key, image_dir / f"img_{index:04d}{extension.lower()}")
    return image_dir, image_dir


async def _stage_original_task_source(task_id: str, target_dir: Path) -> Path:
    storage = get_storage_backend()
    target_dir.mkdir(parents=True, exist_ok=True)
    async with async_session_factory() as db:
        result = await db.execute(
            select(TaskFileRecord)
            .join(TaskRecord, TaskRecord.id == TaskFileRecord.task_id)
            .options(selectinload(TaskFileRecord.file).selectinload(FileRecord.storage_object))
            .where(
                TaskRecord.public_id == task_id,
                TaskFileRecord.role == TaskFileRole.INPUT,
            )
            .order_by(TaskFileRecord.id)
        )
        input_links = list(result.scalars().all())
    if not input_links:
        return target_dir
    if len(input_links) == 1 and (input_links[0].file.mime_type or "").startswith("video/"):
        file = input_links[0].file
        video_path = target_dir / Path(file.original_name or file.filename or "input.mp4").name
        await storage.download_file(file.storage_object.object_key, video_path)
        return video_path
    images_dir = target_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for index, link in enumerate(input_links):
        file = link.file
        extension = Path(file.original_name or file.filename or "").suffix or ".jpg"
        filename = f"img_{index:04d}{extension.lower()}"
        await storage.download_file(file.storage_object.object_key, target_dir / filename)
        await storage.download_file(file.storage_object.object_key, images_dir / filename)
    return target_dir


async def _restore_dash_gaussian_model_dir(task_id: str, ply_file_id: str, model_root: Path) -> Optional[str]:
    storage = get_storage_backend()
    model_root.mkdir(parents=True, exist_ok=True)
    async with async_session_factory() as db:
        result = await db.execute(
            select(TaskFileRecord)
            .join(TaskRecord, TaskRecord.id == TaskFileRecord.task_id)
            .options(selectinload(TaskFileRecord.file).selectinload(FileRecord.storage_object))
            .where(
                TaskRecord.public_id == task_id,
                TaskFileRecord.role == TaskFileRole.RESULT,
            )
            .order_by(TaskFileRecord.id)
        )
        result_links = list(result.scalars().all())

    restore_links, selection_error = _select_dash_gaussian_restore_links_for_ply(result_links, ply_file_id)
    if selection_error:
        return selection_error

    restored = 0
    cfg_restored = False
    for link in restore_links:
        file = link.file
        if not file or file.is_deleted:
            continue
        metainfo = file.metainfo or {}
        if metainfo.get("generated_by") != "dash_gaussian":
            continue
        relative_path = _safe_relative_output_path(metainfo.get("relative_path"))
        if relative_path is None:
            continue
        target_path = model_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        await storage.download_file(file.storage_object.object_key, target_path)
        restored += 1
        cfg_restored = cfg_restored or relative_path.as_posix() == "cfg_args"

    if not restored:
        return "DashGaussian model directory files are missing; rerun the Gaussian stage before Mesh"
    if not cfg_restored:
        return "DashGaussian model directory missing cfg_args; rerun the Gaussian stage before Mesh"

    source_path = await _stage_original_task_source(task_id, model_root / "source_input")
    _rewrite_dash_gaussian_cfg_args(
        model_root / "cfg_args",
        model_path=str(model_root),
        source_path=str(source_path),
    )
    return None


async def _register_log(db: AsyncSession, task: TaskRecord, path: Path) -> None:
    if not path.exists():
        return
    record = await FileService.create_record_from_path(
        db=db,
        user_id=task.user_id,
        path=path,
        filename=path.name,
        category=FileCategory.LOG_FILE,
        mime_type="text/plain",
    )
    await TaskService.add_file_link(db, task, record, TaskFileRole.LOG)


async def _replace_result_links_by_category(db: AsyncSession, task: TaskRecord, category: FileCategory) -> None:
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file))
        .where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.RESULT,
        )
    )
    for link in result.scalars().all():
        if link.file and link.file.category == category:
            await db.delete(link)
    await db.flush()


async def _replace_stage_input_links(
    db: AsyncSession,
    task: TaskRecord,
    records: List[FileRecord],
) -> None:
    result = await db.execute(
        select(TaskFileRecord).where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.INTERMEDIATE,
        )
    )
    for link in result.scalars().all():
        await db.delete(link)
    await db.flush()
    for record in records:
        await TaskService.add_file_link(db, task, record, TaskFileRole.INTERMEDIATE)


async def _remove_mesh_result_links(db: AsyncSession, task: TaskRecord) -> None:
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file))
        .where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.RESULT,
        )
    )
    for link in result.scalars().all():
        if not link.file:
            continue
        generated_by = (link.file.metainfo or {}).get("generated_by")
        if link.file.category in {FileCategory.MESH_MODEL, FileCategory.GLB_MODEL} or generated_by in {
            "dash_gaussian_mesh",
            "hunyuan3d",
        }:
            await db.delete(link)
        elif link.file.filename == "hunyuan3d_obj_bundle.zip":
            await db.delete(link)
    await db.flush()


async def _remove_generated_result_links(db: AsyncSession, task: TaskRecord, generated_by: str) -> None:
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file))
        .where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.RESULT,
        )
    )
    for link in result.scalars().all():
        if link.file and (link.file.metainfo or {}).get("generated_by") == generated_by:
            await db.delete(link)
    await db.flush()


async def _remove_hunyuan3d_result_links(db: AsyncSession, task: TaskRecord) -> None:
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file))
        .where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.RESULT,
        )
    )
    for link in result.scalars().all():
        if not link.file:
            continue
        metainfo = link.file.metainfo or {}
        if metainfo.get("generated_by") == "hunyuan3d" or link.file.filename in {
            "hunyuan3d_result.glb",
            "hunyuan3d_obj_bundle.zip",
        }:
            await db.delete(link)
    await db.flush()


async def _remove_dash_gaussian_mesh_result_links(db: AsyncSession, task: TaskRecord) -> None:
    result = await db.execute(
        select(TaskFileRecord)
        .options(selectinload(TaskFileRecord.file))
        .where(
            TaskFileRecord.task_id == task.id,
            TaskFileRecord.role == TaskFileRole.RESULT,
        )
    )
    for link in result.scalars().all():
        if not link.file:
            continue
        metainfo = link.file.metainfo or {}
        if metainfo.get("generated_by") == "dash_gaussian_mesh" or link.file.filename == "dash_gaussian_mesh.obj":
            await db.delete(link)
    await db.flush()


async def _register_result(
    db: AsyncSession,
    task: TaskRecord,
    path: Path,
    filename: str,
    category: FileCategory,
    mime_type: str,
    *,
    replace_existing_category: bool = True,
    metainfo: Optional[Dict[str, Any]] = None,
) -> FileRecord:
    record = await FileService.create_record_from_path(
        db=db,
        user_id=task.user_id,
        path=path,
        filename=filename,
        category=category,
        mime_type=mime_type,
        metainfo=metainfo,
    )
    if replace_existing_category:
        await _replace_result_links_by_category(db, task, category)
    await TaskService.add_file_link(db, task, record, TaskFileRole.RESULT)
    return record


async def _register_hunyuan3d_results(
    db: AsyncSession,
    task: TaskRecord,
    output_dir: Path,
    _scratch_dir: Path,
) -> Optional[str]:
    primary_glb_path = output_dir / "hunyuan3d_result.glb"
    if not primary_glb_path.is_file() or primary_glb_path.stat().st_size <= 0:
        return f"Hunyuan3D output missing: {primary_glb_path}"

    output_files = _generated_output_files(output_dir)
    if primary_glb_path not in output_files:
        output_files.insert(0, primary_glb_path)

    await _remove_hunyuan3d_result_links(db, task)
    for path in output_files:
        category, mime_type = _generated_result_type(path)
        relative_path = path.relative_to(output_dir).as_posix()
        await _register_result(
            db,
            task,
            path,
            path.name,
            category,
            mime_type,
            replace_existing_category=False,
            metainfo={
                "generated_by": "hunyuan3d",
                "relative_path": relative_path,
                "primary_result": path == primary_glb_path,
            },
        )
    return None


async def _register_dash_gaussian_results(
    db: AsyncSession,
    task: TaskRecord,
    output_dir: Path,
) -> Optional[str]:
    params = _normalize_dash_gaussian_params(_params(task))
    iterations = params["iterations"]
    output_file = output_dir / "point_cloud" / f"iteration_{iterations}" / "point_cloud.ply"
    if not output_file.is_file() or output_file.stat().st_size <= 0:
        return f"DashGaussian output missing: {output_file}"
    cfg_path = output_dir / "cfg_args"
    _rewrite_dash_gaussian_cfg_args(
        cfg_path,
        model_path="${MODEL_ROOT}",
        source_path="${SOURCE_PATH}",
    )
    generation_id = _new_dash_gaussian_generation_id()
    await _remove_generated_result_links(db, task, "dash_gaussian")
    for path in _generated_output_files(output_dir):
        is_primary = path == output_file
        category, mime_type = _generated_result_type(path)
        if not is_primary and category == FileCategory.PLY_MODEL:
            category = FileCategory.OTHER
        await _register_result(
            db,
            task,
            path,
            "point_cloud.ply" if is_primary else path.name,
            FileCategory.PLY_MODEL if is_primary else category,
            "model/ply" if is_primary else mime_type,
            replace_existing_category=is_primary,
            metainfo={
                "generated_by": "dash_gaussian",
                "generation_id": generation_id,
                "relative_path": path.relative_to(output_dir).as_posix(),
                "primary_result": is_primary,
            },
        )
    return None


async def _register_dash_gaussian_mesh_results(
    db: AsyncSession,
    task: TaskRecord,
    output_dir: Path,
) -> Optional[str]:
    mesh_output_dir = output_dir / "dash_gaussian_mesh"
    output_file = mesh_output_dir / "dash_gaussian_mesh.obj"
    if not output_file.is_file() or output_file.stat().st_size <= 0:
        return f"DashGaussian mesh output missing: {output_file}"
    await _remove_dash_gaussian_mesh_result_links(db, task)
    for path in _generated_output_files(mesh_output_dir):
        is_primary = path == output_file
        category, mime_type = _generated_result_type(path)
        await _register_result(
            db,
            task,
            path,
            path.name,
            FileCategory.MESH_MODEL if is_primary else category,
            "model/obj" if is_primary else mime_type,
            replace_existing_category=False,
            metainfo={
                "generated_by": "dash_gaussian_mesh",
                "relative_path": path.relative_to(mesh_output_dir).as_posix(),
                "primary_result": is_primary,
            },
        )
    return None


async def _register_algorithm_results(
    db: AsyncSession,
    task: TaskRecord,
    spec: AlgorithmSpec,
    output_dir: Path,
    scratch_dir: Path,
) -> Optional[str]:
    if task.algorithm == "hunyuan3d":
        return await _register_hunyuan3d_results(db, task, output_dir, scratch_dir)
    if task.algorithm == "dash_gaussian":
        return await _register_dash_gaussian_results(db, task, output_dir)
    if task.algorithm == "dash_gaussian_mesh":
        return await _register_dash_gaussian_mesh_results(db, task, output_dir)
    output_file = _find_output_file(output_dir, spec.output_glob)
    if not output_file:
        return f"No result matched {spec.output_glob}"
    await _register_result(
        db,
        task,
        output_file,
        spec.result_filename,
        _category(spec.result_category),
        spec.result_mime_type,
    )
    return None


async def _execute_tracked_command(
    *,
    task_id: str,
    command: List[str],
    cwd: str,
    environment: Dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    lease: GPULease,
    stage: str,
    progress: float,
    timeout_seconds: int,
) -> Tuple[int, bool]:
    if not await GPUScheduler.renew(lease):
        raise RuntimeError("GPU lease expired before algorithm startup")
    process_options: Dict[str, Any]
    if os.name == "nt":
        process_options = {"start_new_session": True}
    else:
        process_options = {"preexec_fn": _prepare_algorithm_process}
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("ab") as marker:
        marker.write(("\n$ " + " ".join(shlex.quote(part) for part in command) + "\n").encode("utf-8"))
    with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            **process_options,
        )
        async with async_session_factory() as db:
            task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
            task.process_id = process.pid
            task.current_stage = stage
            task.progress = progress
            task.heartbeat_at = _utc_now()
            await db.commit()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=settings.reconstruction_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
            async with async_session_factory() as db:
                task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
                if task.cancel_requested:
                    await _terminate_algorithm_process(process)
                    _set_cancelled(task)
                    task.stdout_tail = _tail(stdout_path)
                    task.stderr_tail = _tail(stderr_path)
                    await _register_log(db, task, stdout_path)
                    await _register_log(db, task, stderr_path)
                    await UserService.settle_gpu_usage(db, task)
                    await db.commit()
                    return process.returncode or -1, True
                if not await GPUScheduler.renew(lease):
                    await _terminate_algorithm_process(process)
                    raise RuntimeError("GPU lease lost while algorithm was running")
                if asyncio.get_running_loop().time() > deadline:
                    await _terminate_algorithm_process(process)
                    raise TimeoutError(f"Algorithm timed out after {timeout_seconds}s")
                task.heartbeat_at = _utc_now()
                task.stdout_tail = _tail(stdout_path)
                task.stderr_tail = _tail(stderr_path)
                task.progress = min(99.0, max(progress, float(task.progress or 0.0) + 1.0))
                await db.commit()
    return process.returncode or 0, False


async def _run_dash_gaussian_mesh_pipeline(
    *,
    task_id: str,
    task: TaskRecord,
    links: List[TaskFileRecord],
    scratch_dir: Path,
    output_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
    lease: GPULease,
    spec: AlgorithmSpec,
) -> Tuple[bool, Optional[str], Optional[str]]:
    _, input_path = await _stage_inputs(task, links, scratch_dir)
    params = _normalize_dash_gaussian_mesh_params(_params(task))
    pipeline = _build_dash_gaussian_mesh_pipeline(spec, input_path, scratch_dir, output_dir, params)
    pipeline.cluster_filtered_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.point_cloud_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.mesh_output_dir.mkdir(parents=True, exist_ok=True)
    if not links or not links[0].file:
        return False, "OUTPUT_FILE_NOT_FOUND", "dash_gaussian_mesh requires one selected PLY result"
    restore_error = await _restore_dash_gaussian_model_dir(task_id, links[0].file.public_id, pipeline.model_root)
    if restore_error:
        return False, "OUTPUT_FILE_NOT_FOUND", restore_error

    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = lease.device_id
    environment["PYTHONUNBUFFERED"] = "1"
    stages = [
        ("mesh_processing", 25.0),
        ("mesh_processing", 55.0),
        ("mesh_processing", 85.0),
    ]
    for index, command in enumerate(pipeline.commands):
        stage, progress = stages[index]
        returncode, cancelled = await _execute_tracked_command(
            task_id=task_id,
            command=command,
            cwd=spec.algorithm_path,
            environment=environment,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            lease=lease,
            stage=stage,
            progress=progress,
            timeout_seconds=spec.timeout_seconds,
        )
        if cancelled:
            return True, None, None
        if returncode != 0:
            return False, "ALGORITHM_EXIT_NONZERO", f"{stage} exited with code {returncode}"
        if index == 1:
            if not pipeline.cluster_filtered_path.is_file() or pipeline.cluster_filtered_path.stat().st_size <= 0:
                return False, "OUTPUT_FILE_NOT_FOUND", f"Filtered PLY missing: {pipeline.cluster_filtered_path}"
            shutil.copyfile(pipeline.cluster_filtered_path, pipeline.point_cloud_path)
    return False, None, None


async def _enqueue(task: TaskRecord, db: AsyncSession) -> str:
    from app.core.celery_app import celery_app

    task.status = TaskStatus.QUEUED
    task.current_stage = _queued_stage(task.algorithm)
    task.progress = 0.0
    task.queue_reason = ""
    task.heartbeat_at = _utc_now()
    task.celery_task_id = "dispatching"
    await db.commit()
    try:
        queued = celery_app.send_task(
            "reconstruction.run",
            args=[task.public_id],
            queue=settings.reconstruction_queue_name,
        )
    except Exception as exc:
        _set_stage_failed(task, "QUEUE_ENQUEUE_FAILED", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
        await db.commit()
        raise AppException("Reconstruction queue is unavailable", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    task.celery_task_id = queued.id
    await db.commit()
    return queued.id


@router.get("/algorithms", response_model=ReconstructionAlgorithmsResponse)
async def list_algorithms(current_user: User = Depends(get_current_user)):
    return ReconstructionAlgorithmsResponse(
        algorithms=[
            ReconstructionAlgorithmResponse(name=spec.name, display_name=spec.display_name, available=spec.available)
            for spec in _algorithm_specs().values()
            if spec.stage == GAUSSIAN_STAGE
        ],
        default_algorithm=settings.default_reconstruction_algorithm,
    )


@router.get("/mesh/algorithms", response_model=ReconstructionAlgorithmsResponse)
async def list_mesh_algorithms(current_user: User = Depends(get_current_user)):
    return ReconstructionAlgorithmsResponse(
        algorithms=[
            ReconstructionAlgorithmResponse(name=spec.name, display_name=spec.display_name, available=spec.available)
            for spec in _algorithm_specs().values()
            if spec.stage == MESH_STAGE
        ],
        default_algorithm=settings.default_mesh_algorithm,
    )


@router.post("/tasks", response_model=ReconstructionTaskCreateResponse)
async def create_reconstruction_task(
    body: ReconstructionTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    params = dict(body.params or {})
    algorithm = str(body.algorithm or params.get("algorithm") or settings.default_reconstruction_algorithm)
    if algorithm in MESH_ALGORITHMS:
        raise AppException(
            f"{algorithm} is a Mesh follow-up stage and cannot create a separate task",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    spec = _get_algorithm_spec(algorithm)
    if spec.stage != GAUSSIAN_STAGE:
        raise AppException(
            f"{algorithm} is not a Gaussian reconstruction algorithm",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    params = _normalize_task_params(algorithm, params)
    task = await TaskService.create_task(
        db,
        current_user.id,
        body.title,
        algorithm,
        params,
    )
    return ReconstructionTaskCreateResponse(
        task_id=task.public_id,
        status=task.status,
        status_code=_task_status_code(task),
        algorithm=task.algorithm,
        params=params,
        visibility=task.visibility,
        current_stage=task.current_stage,
        created_at=_iso(task.created_at) or "",
    )


@router.get("/tasks", response_model=ReconstructionTaskListResponse)
async def list_reconstruction_tasks(
    status_filter: Optional[TaskStatus] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tasks, total = await TaskService.list_owned(db, current_user, status_filter, skip, limit)
    return ReconstructionTaskListResponse(tasks=[_task_response(task, include_private=True) for task in tasks], total=total)


@router.get("/discover", response_model=ReconstructionDiscoverResponse)
async def list_discover_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=10),
    skip: Optional[int] = Query(None, ge=0, deprecated=True),
    limit: Optional[int] = Query(None, ge=1, le=10, deprecated=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    using_page_pagination = (
        "page" in request.query_params
        or "page_size" in request.query_params
        or (skip is None and limit is None)
    )
    if using_page_pagination:
        offset = (page - 1) * page_size
        actual_page = page
        actual_page_size = page_size
    else:
        actual_page_size = limit or 10
        offset = skip or 0
        actual_page = offset // actual_page_size + 1
    tasks, total = await TaskService.list_discover(db, offset, actual_page_size)
    total_pages = (total + actual_page_size - 1) // actual_page_size if total else 0
    return ReconstructionDiscoverResponse(
        tasks=[_task_response(task) for task in tasks],
        total=total,
        page=actual_page,
        page_size=actual_page_size,
        total_pages=total_pages,
        has_next=offset + actual_page_size < total,
        has_prev=offset > 0,
    )


@router.get("/tasks/{task_id}", response_model=ReconstructionStatusResponse)
async def get_reconstruction_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_readable(task, current_user)
    return _task_response(task, include_private=current_user.is_admin or task.user_id == current_user.id)


@router.get("/tasks/{task_id}/inputs", response_model=ReconstructionTaskInputsResponse)
async def get_reconstruction_task_inputs(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    input_file_ids = _original_input_file_ids(task)
    return ReconstructionTaskInputsResponse(
        task_id=task.public_id,
        input_kind=_original_input_kind(task),
        input_file_ids=input_file_ids,
        input_file_count=len(input_file_ids),
    )


@router.patch("/tasks/{task_id}/visibility", response_model=ReconstructionStatusResponse)
async def set_reconstruction_visibility(
    task_id: str,
    body: ReconstructionVisibilityRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    await TaskService.set_visibility(db, task, current_user, body.visibility)
    task = await TaskService.get_by_public_id(db, task_id)
    return _task_response(task, include_private=True)


@router.delete("/tasks/{task_id}", response_model=ReconstructionDeleteResponse)
async def delete_reconstruction_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    await TaskService.delete_task(db, task, current_user)
    return ReconstructionDeleteResponse(task_id=task.public_id, deleted=True, status=task.status)


async def _start_reconstruction(
    db: AsyncSession,
    current_user: User,
    task: TaskRecord,
    selected: List[str],
    requested_type: Optional[str] = None,
) -> ReconstructionStartResponse:
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROCESSING}:
        raise TaskStateException(f"Task is already {task.status.value}")
    if task.status not in {
        TaskStatus.PENDING,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_COMPLETED,
        TaskStatus.FAILED,
    }:
        raise TaskStateException(f"Task is already {task.status.value}")
    is_rerun = task.status != TaskStatus.PENDING
    await UserService.ensure_active_task_quota(db, current_user.id, exclude_task_record_id=task.id)
    requested_algorithm = task.gaussian_algorithm or task.algorithm
    if requested_algorithm not in GAUSSIAN_ALGORITHMS:
        raise TaskStateException("This task does not contain a startable Gaussian reconstruction algorithm")
    spec = _get_algorithm_spec(requested_algorithm)
    normalized_params = _normalize_task_params(
        requested_algorithm,
        _json_dict(task.gaussian_params or task.params),
    )
    if not selected:
        selected = _original_input_file_ids(task)
    if not selected or len(selected) != len(set(selected)):
        raise AppException("Provide unique input_file_ids")
    records = [await FileService.get_by_identifier_for_user(db, item, current_user.id) for item in selected]
    if any(record.source_link for record in records):
        raise AppException("Derived files such as thumbnails cannot be reconstruction inputs")
    if is_rerun:
        related_file_ids = {
            link.file.public_id
            for link in task.file_links
            if link.role == TaskFileRole.INPUT
            and link.file
            and not link.file.is_deleted
            and (
                (link.file.mime_type or "").startswith("image/")
                or (link.file.mime_type or "").startswith("video/")
            )
        }
        unlinked = [record.public_id for record in records if record.public_id not in related_file_ids]
        if unlinked:
            raise AppException("Gaussian reruns can only use original files already linked to this task")
    if len(records) == 1 and records[0].mime_type.startswith("video/"):
        input_kind = "video"
    else:
        is_images = all(item.mime_type.startswith("image/") for item in records)
        if is_images and len(records) >= 3:
            input_kind = "image_folder"
        else:
            raise AppException("Provide one video or at least three images")
    if input_kind not in spec.accepted_input_types:
        raise AppException(f"Algorithm {spec.name} does not support {input_kind} input")
    if not _requested_type_matches(requested_type, input_kind):
        raise AppException(f"input_type={requested_type} does not match selected input files ({input_kind})")
    for record in records:
        await TaskService.add_file_link(db, task, record, TaskFileRole.INPUT)
    task.algorithm = requested_algorithm
    task.params = json.dumps(normalized_params, ensure_ascii=False)
    task.gaussian_algorithm = requested_algorithm
    task.gaussian_params = task.params
    task.input_kind = input_kind
    task.cancel_requested = False
    task.error_code = ""
    task.error_status_code = 0
    task.error_message = ""
    task.stdout_tail = ""
    task.stderr_tail = ""
    task.started_at = None
    task.completed_at = None
    task.retry_count = 0
    task.queue_reason = ""
    task.gpu_billing_started_at = None
    task.process_id = None
    task.worker_node_id = None
    task.executor_id = None
    task.cuda_device = None
    await _enqueue(task, db)
    return ReconstructionStartResponse(
        task_id=task.public_id,
        status=task.status,
        status_code=_task_status_code(task),
        algorithm=requested_algorithm,
        current_stage=task.current_stage,
        input_type=input_kind,
        input_file_count=len(records),
        queue_reason=task.queue_reason or None,
    )


@router.post("/start/{task_id}", response_model=ReconstructionStartResponse)
async def start_reconstruction(
    task_id: str,
    body: ReconstructionStartByImagesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    selected = _selected_start_file_ids(body)
    return await _start_reconstruction(
        db,
        current_user,
        task,
        selected,
        requested_type=body.input_type,
    )


@router.post("/mesh/start/{task_id}", response_model=ReconstructionStartResponse)
async def start_mesh_reconstruction(
    task_id: str,
    body: ReconstructionMeshStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROCESSING}:
        raise TaskStateException(f"Task is already {task.status.value}")
    if task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_COMPLETED, TaskStatus.FAILED}:
        raise TaskStateException("Mesh reconstruction requires a completed Gaussian PLY result")
    if not _has_ply_result(task):
        raise TaskStateException("Mesh reconstruction requires a completed Gaussian PLY result")
    await UserService.ensure_active_task_quota(db, current_user.id, exclude_task_record_id=task.id)
    algorithm = body.algorithm.strip()
    if algorithm not in MESH_ALGORITHMS:
        raise AppException(
            f"Mesh algorithm must be one of: {', '.join(sorted(MESH_ALGORITHMS))}",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    spec = _get_algorithm_spec(algorithm)
    if spec.stage != MESH_STAGE:
        raise AppException(f"{algorithm} is not a Mesh algorithm", status.HTTP_422_UNPROCESSABLE_ENTITY)
    selected = list(body.input_file_ids)
    if not selected or len(selected) != len(set(selected)):
        raise AppException("Provide unique input_file_ids", status.HTTP_422_UNPROCESSABLE_ENTITY)
    records = [await FileService.get_by_identifier_for_user(db, item, current_user.id) for item in selected]
    if any(record.source_link for record in records):
        raise AppException("Derived files such as thumbnails cannot be Mesh inputs")

    if algorithm == "dash_gaussian_mesh":
        if len(records) != 1:
            raise AppException("dash_gaussian_mesh requires exactly one PLY result")
        record = records[0]
        is_task_ply_result = any(
            link.role == TaskFileRole.RESULT
            and link.file
            and link.file.id == record.id
            and not link.file.is_deleted
            and _is_ply_model_file(link.file)
            for link in task.file_links
        )
        if not is_task_ply_result:
            raise AppException("input_file_ids must contain a PLY result linked to this task")
        restore_links, restore_error = _select_dash_gaussian_restore_links_for_ply(task.file_links, record.public_id)
        if restore_error:
            raise AppException(restore_error)
        if not any(
            _safe_relative_output_path((link.file.metainfo or {}).get("relative_path")) == Path("cfg_args")
            for link in restore_links
            if link.file
        ):
            raise AppException("Selected DashGaussian PLY has no matching cfg_args; rerun the Gaussian stage before Mesh")
        input_kind = "ply_model"
    else:
        original_input_ids = set(_original_input_file_ids(task))
        if any(record.public_id not in original_input_ids for record in records):
            raise AppException("Hunyuan3D inputs must be original image or video files linked to this task")
        image_records = [record for record in records if (record.mime_type or "").startswith("image/")]
        video_records = [record for record in records if (record.mime_type or "").startswith("video/")]
        if len(video_records) == 1 and len(records) == 1:
            input_kind = "video"
        elif len(image_records) == len(records):
            input_kind = "image" if len(records) == 1 else "image_folder"
        else:
            raise AppException("Hunyuan3D requires images only or one video; mixed inputs are not allowed")

    if input_kind not in spec.accepted_input_types:
        raise AppException(f"Algorithm {spec.name} does not support {input_kind} input")
    normalized_params = _normalize_task_params(algorithm, body.params)
    await _replace_stage_input_links(db, task, records)
    task.algorithm = algorithm
    task.params = json.dumps(normalized_params, ensure_ascii=False)
    task.mesh_algorithm = algorithm
    task.mesh_params = task.params
    task.input_kind = input_kind
    task.cancel_requested = False
    task.error_code = ""
    task.error_status_code = 0
    task.error_message = ""
    task.stdout_tail = ""
    task.stderr_tail = ""
    task.started_at = None
    task.completed_at = None
    task.retry_count = 0
    task.queue_reason = ""
    task.gpu_billing_started_at = None
    task.process_id = None
    task.worker_node_id = None
    task.executor_id = None
    task.cuda_device = None
    await _enqueue(task, db)
    return ReconstructionStartResponse(
        task_id=task.public_id,
        status=task.status,
        status_code=_task_status_code(task),
        algorithm=task.algorithm,
        current_stage=task.current_stage,
        input_type=task.input_kind,
        input_file_count=len(records),
        queue_reason=task.queue_reason or None,
    )


@router.get("/status/{task_id}", response_model=ReconstructionStatusResponse)
async def get_reconstruction_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_readable(task, current_user)
    return _task_response(task, include_private=current_user.is_admin or task.user_id == current_user.id)


@router.post("/cancel/{task_id}", response_model=ReconstructionCancelResponse)
async def cancel_reconstruction(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    await TaskService.request_cancel(db, task)
    return ReconstructionCancelResponse(
        task_id=task.public_id,
        status=task.status,
        cancelled=task.status == TaskStatus.CANCELLED,
        message="Cancellation requested",
    )


@router.get("/logs/{task_id}", response_model=ReconstructionLogsResponse)
async def get_reconstruction_logs(
    task_id: str,
    tail: int = Query(4000, ge=0, le=20000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    return ReconstructionLogsResponse(
        task_id=task.public_id,
        status=task.status,
        error_code=task.error_code or None,
        error_status_code=task.error_status_code or None,
        error=task.error_message or None,
        stdout_tail=task.stdout_tail[-tail:],
        stderr_tail=task.stderr_tail[-tail:],
    )


@router.get("/diagnostics/{task_id}", response_model=ReconstructionDiagnosticsResponse)
async def get_reconstruction_diagnostics(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    task = await TaskService.get_by_public_id(db, task_id)
    spec = _get_algorithm_spec(task.algorithm)
    storage = get_storage_backend()
    checks = [
        ReconstructionDiagnosticCheck(name="python_runtime", ok=bool(shutil.which(spec.python_path) or Path(spec.python_path).exists()), detail="configured"),
        ReconstructionDiagnosticCheck(name="algorithm_directory", ok=Path(spec.algorithm_path).is_dir(), detail="configured"),
        ReconstructionDiagnosticCheck(name="algorithm_entrypoint", ok=(Path(spec.algorithm_path) / spec.entrypoint).exists(), detail="configured"),
        ReconstructionDiagnosticCheck(name="minio_bucket", ok=await storage.bucket_exists(), detail=settings.s3_bucket),
        ReconstructionDiagnosticCheck(name="celery_task_id", ok=bool(task.celery_task_id), detail=task.celery_task_id),
        ReconstructionDiagnosticCheck(name="worker_node", ok=bool(task.worker_node_id), detail=task.worker_node_id or "not assigned"),
        ReconstructionDiagnosticCheck(name="executor", ok=bool(task.executor_id), detail=task.executor_id or "not assigned"),
        ReconstructionDiagnosticCheck(name="cuda_device", ok=bool(task.cuda_device), detail=task.cuda_device or "not assigned"),
    ]
    return ReconstructionDiagnosticsResponse(
        task_id=task.public_id,
        status=task.status,
        algorithm=task.algorithm,
        error_code=task.error_code or None,
        error_status_code=task.error_status_code or None,
        checks=checks,
    )


async def prepare_gpu_dispatch(task_id: str) -> Optional[dict]:
    async with async_session_factory() as db:
        task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
        if task.status not in {TaskStatus.PENDING, TaskStatus.QUEUED}:
            return None
        try:
            quota = await UserService.ensure_gpu_daily_quota_available(db, task.user_id)
        except QuotaExceededException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {
                "code": "GPU_DAILY_QUOTA_EXCEEDED",
                "message": str(exc.detail),
            }
            _set_stage_failed(
                task,
                "GPU_DAILY_QUOTA_EXCEEDED",
                json.dumps(detail, ensure_ascii=False),
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
            task.queue_reason = ""
            await db.commit()
            return None
        await db.commit()
        return {
            "user_id": task.user_id,
            "gpu_concurrency_quota": int(quota["gpu_concurrency_quota"] or 0),
        }


async def mark_waiting_for_gpu(task_id: str, queue_reason: str = "gpu_capacity") -> bool:
    async with async_session_factory() as db:
        task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
        if task.status not in {TaskStatus.PENDING, TaskStatus.QUEUED}:
            return False
        task.status = TaskStatus.QUEUED
        task.current_stage = _queued_stage(task.algorithm)
        task.progress = 0.0
        task.queue_reason = queue_reason or "gpu_capacity"
        task.worker_node_id = None
        task.executor_id = None
        task.cuda_device = None
        task.process_id = None
        task.heartbeat_at = _utc_now()
        await db.commit()
        return True


async def mark_gpu_inspection_failed(task_id: str, message: str) -> None:
    async with async_session_factory() as db:
        task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
        if task.status not in {TaskStatus.PENDING, TaskStatus.QUEUED}:
            return
        _set_stage_failed(task, "GPU_INSPECTION_FAILED", message, status.HTTP_503_SERVICE_UNAVAILABLE)
        await db.commit()


async def run_reconstruction_algorithm(task_id: str, lease: GPULease) -> None:
    process: Optional[asyncio.subprocess.Process] = None
    scratch_dir = Path(settings.reconstruction_scratch_path) / f"{task_id}_{uuid4().hex}"
    output_dir = scratch_dir / "output"
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_session_factory() as db:
        task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
        if task.cancel_requested or task.status == TaskStatus.CANCELLED:
            _set_cancelled(task)
            await db.commit()
            await GPUScheduler.release(lease)
            shutil.rmtree(scratch_dir, ignore_errors=True)
            return
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.MANUAL_REVIEW}:
            await GPUScheduler.release(lease)
            shutil.rmtree(scratch_dir, ignore_errors=True)
            return
        if task.status == TaskStatus.PROCESSING and task.heartbeat_at:
            heartbeat = task.heartbeat_at
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            if (_utc_now() - heartbeat).total_seconds() < settings.reconstruction_stale_processing_seconds:
                await GPUScheduler.release(lease)
                shutil.rmtree(scratch_dir, ignore_errors=True)
                return
        task.status = TaskStatus.PROCESSING
        task.current_stage = _processing_stage(task.algorithm)
        task.progress = _processing_progress(task.algorithm)
        task.worker_node_id = lease.node_id
        task.executor_id = lease.executor_id
        task.cuda_device = lease.device_id
        task.execution_attempt = int(task.execution_attempt or 0) + 1
        task.started_at = task.started_at or _utc_now()
        task.gpu_billing_started_at = _utc_now()
        task.queue_reason = ""
        task.heartbeat_at = _utc_now()
        await db.commit()
        links = await _input_links(db, task)
        spec = _get_algorithm_spec(task.algorithm)

    try:
        if task.algorithm == "dash_gaussian_mesh":
            cancelled, error_code, error_message = await _run_dash_gaussian_mesh_pipeline(
                task_id=task.public_id,
                task=task,
                links=links,
                scratch_dir=scratch_dir,
                output_dir=output_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                lease=lease,
                spec=spec,
            )
            async with async_session_factory() as db:
                task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
                task.stdout_tail = _tail(stdout_path)
                task.stderr_tail = _tail(stderr_path)
                if cancelled:
                    await db.commit()
                    return
                if error_code:
                    _set_stage_failed(task, error_code, task.stderr_tail or error_message or "DashGaussian mesh failed")
                else:
                    output_error = await _register_algorithm_results(
                        db,
                        task,
                        spec,
                        output_dir,
                        scratch_dir,
                    )
                    if output_error:
                        _set_stage_failed(task, "OUTPUT_FILE_NOT_FOUND", output_error)
                    else:
                        task.status = TaskStatus.COMPLETED
                        task.current_stage = "mesh_completed"
                        task.progress = 100.0
                        task.completed_at = _utc_now()
                        task.heartbeat_at = _utc_now()
                        task.queue_reason = ""
                await _register_log(db, task, stdout_path)
                await _register_log(db, task, stderr_path)
                await UserService.settle_gpu_usage(db, task)
                await db.commit()
            return

        image_dir, input_path = await _stage_inputs(task, links, scratch_dir)
        command_params = _normalize_task_params(task.algorithm, _params(task))
        command = _build_command(
            spec,
            task.public_id,
            image_dir,
            input_path,
            output_dir,
            command_params,
        )
        environment = os.environ.copy()
        if not await GPUScheduler.renew(lease):
            raise RuntimeError("GPU lease expired before algorithm startup")
        environment["CUDA_VISIBLE_DEVICES"] = lease.device_id
        environment["PYTHONUNBUFFERED"] = "1"
        process_options: Dict[str, Any]
        if os.name == "nt":
            process_options = {"start_new_session": True}
        else:
            process_options = {"preexec_fn": _prepare_algorithm_process}
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=spec.algorithm_path,
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
                **process_options,
            )
            async with async_session_factory() as db:
                task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
                task.process_id = process.pid
                task.current_stage = _processing_stage(task.algorithm)
                task.progress = _processing_progress(task.algorithm)
                task.heartbeat_at = _utc_now()
                await db.commit()
            deadline = asyncio.get_running_loop().time() + spec.timeout_seconds
            while process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=settings.reconstruction_poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
                async with async_session_factory() as db:
                    task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
                    if task.cancel_requested:
                        await _terminate_algorithm_process(process)
                        _set_cancelled(task)
                        task.stdout_tail = _tail(stdout_path)
                        task.stderr_tail = _tail(stderr_path)
                        await _register_log(db, task, stdout_path)
                        await _register_log(db, task, stderr_path)
                        await UserService.settle_gpu_usage(db, task)
                        await db.commit()
                        return
                    if not await GPUScheduler.renew(lease):
                        await _terminate_algorithm_process(process)
                        raise RuntimeError("GPU lease lost while algorithm was running")
                    if asyncio.get_running_loop().time() > deadline:
                        await _terminate_algorithm_process(process)
                        raise TimeoutError(f"Algorithm timed out after {spec.timeout_seconds}s")
                    task.heartbeat_at = _utc_now()
                    task.stdout_tail = _tail(stdout_path)
                    task.stderr_tail = _tail(stderr_path)
                    task.progress = min(99.0, max(_processing_progress(task.algorithm), task.progress + 1.0))
                    await db.commit()
        async with async_session_factory() as db:
            task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
            task.stdout_tail = _tail(stdout_path)
            task.stderr_tail = _tail(stderr_path)
            if process.returncode != 0:
                _set_stage_failed(task, "ALGORITHM_EXIT_NONZERO", task.stderr_tail or f"Exit code {process.returncode}")
            else:
                output_error = await _register_algorithm_results(
                    db,
                    task,
                    spec,
                    output_dir,
                    scratch_dir,
                )
                if output_error:
                    _set_stage_failed(task, "OUTPUT_FILE_NOT_FOUND", output_error)
                else:
                    if task.algorithm in GAUSSIAN_ALGORITHMS:
                        await _replace_stage_input_links(db, task, [])
                        await _remove_mesh_result_links(db, task)
                        task.status = TaskStatus.COMPLETED
                        task.current_stage = "gaussian_completed"
                        task.progress = 100.0
                        task.completed_at = _utc_now()
                        task.heartbeat_at = _utc_now()
                        task.queue_reason = ""
                    else:
                        task.status = TaskStatus.COMPLETED
                        task.current_stage = _completed_stage(task.algorithm)
                        task.progress = 100.0
                        task.completed_at = _utc_now()
                        task.heartbeat_at = _utc_now()
                        task.queue_reason = ""
            await _register_log(db, task, stdout_path)
            await _register_log(db, task, stderr_path)
            await UserService.settle_gpu_usage(db, task)
            await db.commit()
    except Exception as exc:
        if process is not None:
            await _terminate_algorithm_process(process)
        async with async_session_factory() as db:
            task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
            _set_stage_failed(task, "ALGORITHM_RUNTIME_ERROR", str(exc))
            task.stdout_tail = _tail(stdout_path)
            task.stderr_tail = _tail(stderr_path)
            await _register_log(db, task, stdout_path)
            await _register_log(db, task, stderr_path)
            await UserService.settle_gpu_usage(db, task)
            await db.commit()
    finally:
        try:
            await GPUScheduler.release(lease)
        except Exception:
            pass
        async with async_session_factory() as db:
            task = await TaskService.get_by_public_id(db, task_id, include_deleted=True)
            if task.status != TaskStatus.PROCESSING:
                await UserService.settle_gpu_usage(db, task)
                task.process_id = None
            await db.commit()
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)


async def recover_stale_reconstruction_tasks() -> int:
    async with async_session_factory() as db:
        recovered = await TaskService.recover_stale_tasks(db)
        await db.commit()
        for task in recovered:
            GPUScheduler.terminate_local_process(task.worker_node_id, task.executor_id, task.process_id)
            await GPUScheduler.release_stale(
                task.worker_node_id,
                task.executor_id,
                task.cuda_device,
                task.public_id,
                task.user_id,
            )
            task.worker_node_id = None
            task.executor_id = None
            task.cuda_device = None
            task.process_id = None
            await db.commit()
            if task.status in {TaskStatus.FAILED, TaskStatus.PARTIAL_COMPLETED}:
                continue
            await _enqueue(task, db)
        return len(recovered)

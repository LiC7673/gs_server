"""
Smoke-test every custom HTTP endpoint exposed by the 3DGS backend.

The default run is intentionally lightweight:
1. Check health and generated API documentation.
2. Register a temporary user and test authentication/profile endpoints.
3. Upload valid tiny PNG files through the chunked upload API.
4. Verify duplicate upload reuse, upload cancellation, file listing, archive,
   media retry, chunked download, and file deletion.
5. Create reconstruction tasks, enqueue AnySplat jobs, inspect
   status/logs, and cancel them quickly so the smoke test does not occupy a GPU.
6. Verify that admin-only endpoints reject an ordinary user.

This script verifies API contracts, infrastructure connectivity, and queue
dispatch. It does not wait for real GPU algorithms to finish. Use
scripts/test_reconstruction_algorithms.py for a full algorithm run with real
images or videos.

Examples:
  python scripts/test_all_api_endpoints.py

  python scripts/test_all_api_endpoints.py \
    --base-url http://127.0.0.1:8000 \
    --admin-token "<admin access token>" \
    --report-file api-smoke-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import uuid
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import httpx


CUSTOM_ENDPOINTS: Set[Tuple[str, str]] = {
    ("GET", "/health"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/users/me"),
    ("PUT", "/api/v1/users/me"),
    ("PUT", "/api/v1/users/update_avatar"),
    ("GET", "/api/v1/users/me/usage"),
    ("PUT", "/api/v1/users/{user_id}/quota"),
    ("POST", "/api/v1/users/{user_id}/gpu-usage/reset"),
    ("POST", "/api/v1/upload/init"),
    ("PUT", "/api/v1/upload/{upload_id}/chunk"),
    ("GET", "/api/v1/upload/{upload_id}/progress"),
    ("POST", "/api/v1/upload/{upload_id}/merge"),
    ("POST", "/api/v1/upload/{upload_id}/cancel"),
    ("GET", "/api/v1/files"),
    ("GET", "/api/v1/files/{file_id}"),
    ("POST", "/api/v1/files/{file_id}/download/init"),
    ("GET", "/api/v1/files/{file_id}/download/chunk"),
    ("GET", "/api/v1/files/downloads/{download_id}/progress"),
    ("POST", "/api/v1/files/downloads/{download_id}/complete"),
    ("POST", "/api/v1/files/{file_id}/archive"),
    ("POST", "/api/v1/files/{file_id}/media-processing/retry"),
    ("DELETE", "/api/v1/files/{file_id}"),
    ("GET", "/api/v1/reconstruction/algorithms"),
    ("GET", "/api/v1/reconstruction/mesh/algorithms"),
    ("POST", "/api/v1/reconstruction/tasks"),
    ("GET", "/api/v1/reconstruction/tasks"),
    ("GET", "/api/v1/reconstruction/discover"),
    ("GET", "/api/v1/reconstruction/tasks/{task_id}"),
    ("GET", "/api/v1/reconstruction/tasks/{task_id}/inputs"),
    ("PATCH", "/api/v1/reconstruction/tasks/{task_id}/visibility"),
    ("DELETE", "/api/v1/reconstruction/tasks/{task_id}"),
    ("POST", "/api/v1/reconstruction/start/{task_id}"),
    ("POST", "/api/v1/reconstruction/mesh/start/{task_id}"),
    ("GET", "/api/v1/reconstruction/status/{task_id}"),
    ("POST", "/api/v1/reconstruction/cancel/{task_id}"),
    ("GET", "/api/v1/reconstruction/logs/{task_id}"),
    ("GET", "/api/v1/reconstruction/diagnostics/{task_id}"),
}


class SmokeFailure(RuntimeError):
    pass


@dataclass
class CheckResult:
    status: str
    name: str
    detail: str = ""


class SmokeRunner:
    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.results: List[CheckResult] = []
        self.covered: Set[Tuple[str, str]] = set()

    def _record(self, status: str, name: str, detail: str = "") -> None:
        self.results.append(CheckResult(status=status, name=name, detail=detail))
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}")

    def skip(self, name: str, detail: str) -> None:
        self._record("SKIP", name, detail)

    def require(self, condition: bool, name: str, detail: str = "") -> None:
        if condition:
            self._record("PASS", name, detail)
            return
        self._record("FAIL", name, detail)
        raise SmokeFailure(f"{name}: {detail or 'assertion failed'}")

    def request(
        self,
        method: str,
        endpoint: str,
        path: Optional[str] = None,
        *,
        expected: Iterable[int] = (200,),
        label: str = "",
        **kwargs: Any,
    ) -> httpx.Response:
        actual_method = method.upper()
        actual_path = path or endpoint
        name = label or f"{actual_method} {endpoint}"
        self.covered.add((actual_method, endpoint))
        try:
            response = self.client.request(
                actual_method,
                f"{self.base_url}{actual_path}",
                **kwargs,
            )
        except Exception as exc:
            self._record("FAIL", name, f"request error: {exc}")
            raise SmokeFailure(f"{name}: request error: {exc}") from exc
        expected_codes = set(expected)
        if response.status_code not in expected_codes:
            body = response.text.replace("\n", " ")[:600]
            self._record(
                "FAIL",
                name,
                f"HTTP {response.status_code}, expected {sorted(expected_codes)}; {body}",
            )
            raise SmokeFailure(f"{name}: unexpected HTTP {response.status_code}")
        self._record("PASS", name, f"HTTP {response.status_code}")
        return response

    def json(self, response: httpx.Response, name: str) -> Dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            self._record("FAIL", name, "response is not JSON")
            raise SmokeFailure(f"{name}: response is not JSON") from exc
        if not isinstance(data, dict):
            self._record("FAIL", name, "JSON response is not an object")
            raise SmokeFailure(f"{name}: JSON response is not an object")
        return data

    def audit_coverage(self) -> None:
        missing = sorted(CUSTOM_ENDPOINTS - self.covered)
        if missing:
            detail = ", ".join(f"{method} {path}" for method, path in missing)
            self._record("FAIL", "custom endpoint coverage", f"missing: {detail}")
            return
        self._record("PASS", "custom endpoint coverage", f"{len(CUSTOM_ENDPOINTS)}/{len(CUSTOM_ENDPOINTS)} routes exercised")

    def summary(self, report_file: Optional[Path] = None) -> None:
        self.audit_coverage()
        counts = {
            "PASS": sum(item.status == "PASS" for item in self.results),
            "SKIP": sum(item.status == "SKIP" for item in self.results),
            "FAIL": sum(item.status == "FAIL" for item in self.results),
        }
        print("\n=== API smoke summary ===")
        print(f"PASS={counts['PASS']} SKIP={counts['SKIP']} FAIL={counts['FAIL']}")
        print(f"Custom routes exercised: {len(self.covered & CUSTOM_ENDPOINTS)}/{len(CUSTOM_ENDPOINTS)}")
        if report_file:
            payload = {
                "counts": counts,
                "custom_routes_exercised": len(self.covered & CUSTOM_ENDPOINTS),
                "custom_routes_total": len(CUSTOM_ENDPOINTS),
                "results": [asdict(item) for item in self.results],
            }
            report_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Report written to: {report_file}")

    def has_failures(self) -> bool:
        return any(item.status == "FAIL" for item in self.results)


def bearer_headers(token: str) -> Dict[str, str]:
    actual = token.strip()
    if actual.lower().startswith("bearer "):
        actual = actual[7:].strip()
    return {"Authorization": f"Bearer {actual}"}


def response_json(runner: SmokeRunner, response: httpx.Response, name: str) -> Dict[str, Any]:
    return runner.json(response, name)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def png_bytes(red: int, green: int, blue: int, marker: str = "") -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel_row = b"\x00" + bytes((red, green, blue))
    metadata = chunk(b"tEXt", b"smoke\x00" + marker.encode("ascii")) if marker else b""
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(pixel_row)) + metadata + chunk(b"IEND", b"")


def register_user(
    runner: SmokeRunner,
    username: str,
    email: str,
    password: str,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    register = runner.request(
        "POST",
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    register_data = response_json(runner, register, "register JSON")
    runner.require(bool(register_data.get("access_token")), "register returns access_token")

    login = runner.request(
        "POST",
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    login_data = response_json(runner, login, "login JSON")
    token = str(login_data.get("access_token") or "")
    runner.require(bool(token), "login returns access_token")
    return bearer_headers(token), login_data["user"]


def upload_bytes(
    runner: SmokeRunner,
    headers: Dict[str, str],
    *,
    filename: str,
    mime_type: str,
    content: bytes,
    chunk_size: int = 17,
    check_progress: bool = False,
) -> Dict[str, Any]:
    digest = sha256_bytes(content)
    init_response = runner.request(
        "POST",
        "/api/v1/upload/init",
        headers=headers,
        json={
            "filename": filename,
            "file_size": len(content),
            "chunk_size": chunk_size,
            "mime_type": mime_type,
            "file_hash": digest,
        },
        label=f"POST /api/v1/upload/init ({filename})",
    )
    init_data = response_json(runner, init_response, f"upload init JSON ({filename})")
    if init_data.get("already_uploaded"):
        runner.require(bool(init_data.get("file_id")), f"duplicate init returns file_id ({filename})")
        return init_data

    upload_id = str(init_data["upload_id"])
    actual_chunk_size = int(init_data["chunk_size"])
    total_chunks = int(init_data["total_chunks"])
    parts: List[Dict[str, Any]] = []
    for chunk_index in range(total_chunks):
        start = chunk_index * actual_chunk_size
        body = content[start : start + actual_chunk_size]
        chunk_response = runner.request(
            "PUT",
            "/api/v1/upload/{upload_id}/chunk",
            f"/api/v1/upload/{upload_id}/chunk",
            headers={**headers, "Content-Type": "application/octet-stream"},
            params={"chunk_index": chunk_index},
            content=body,
            label=f"PUT /api/v1/upload/{{upload_id}}/chunk ({filename} #{chunk_index})",
        )
        chunk_data = response_json(runner, chunk_response, f"upload chunk JSON ({filename} #{chunk_index})")
        etag = str(chunk_data.get("etag") or "")
        runner.require(etag == md5_bytes(body), f"chunk etag matches MD5 ({filename} #{chunk_index})")
        parts.append({"chunk_index": chunk_index, "etag": etag})
        if check_progress and chunk_index == 0:
            progress_response = runner.request(
                "GET",
                "/api/v1/upload/{upload_id}/progress",
                f"/api/v1/upload/{upload_id}/progress",
                headers=headers,
                label=f"GET /api/v1/upload/{{upload_id}}/progress ({filename}, partial)",
            )
            progress = response_json(runner, progress_response, f"upload progress JSON ({filename})")
            runner.require(
                int(progress.get("received_chunks", 0)) >= 1,
                f"upload progress records received chunk ({filename})",
            )

    merge_response = runner.request(
        "POST",
        "/api/v1/upload/{upload_id}/merge",
        f"/api/v1/upload/{upload_id}/merge",
        headers=headers,
        json={
            "expected_hash": digest,
            "expected_size": len(content),
            "parts": parts,
        },
        label=f"POST /api/v1/upload/{{upload_id}}/merge ({filename})",
    )
    merged = response_json(runner, merge_response, f"upload merge JSON ({filename})")
    runner.require(bool(merged.get("file_id")), f"merge returns string file_id ({filename})")
    runner.require(str(merged["file_id"]).startswith("file_"), f"file_id prefix is file_ ({filename})")
    runner.require(merged.get("file_hash") == digest, f"merged SHA-256 matches ({filename})")
    return merged


def download_and_verify(
    runner: SmokeRunner,
    headers: Dict[str, str],
    file_id: str,
    expected_content: bytes,
) -> None:
    init_response = runner.request(
        "POST",
        "/api/v1/files/{file_id}/download/init",
        f"/api/v1/files/{file_id}/download/init",
        headers=headers,
        json={"chunk_size": 13},
    )
    init_data = response_json(runner, init_response, "download init JSON")
    download_id = str(init_data["download_id"])
    total_chunks = int(init_data["total_chunks"])
    downloaded = bytearray()
    parts: List[Dict[str, Any]] = []
    for chunk_index in range(total_chunks):
        chunk_response = runner.request(
            "GET",
            "/api/v1/files/{file_id}/download/chunk",
            f"/api/v1/files/{file_id}/download/chunk",
            expected=(206,),
            headers=headers,
            params={"download_id": download_id, "chunk_index": chunk_index},
            label=f"GET /api/v1/files/{{file_id}}/download/chunk (#{chunk_index})",
        )
        downloaded.extend(chunk_response.content)
        etag = chunk_response.headers.get("X-Chunk-Etag", "")
        runner.require(etag == md5_bytes(chunk_response.content), f"download chunk etag matches MD5 (#{chunk_index})")
        parts.append({"chunk_index": chunk_index, "etag": etag})
        if chunk_index == 0:
            progress_response = runner.request(
                "GET",
                "/api/v1/files/downloads/{download_id}/progress",
                f"/api/v1/files/downloads/{download_id}/progress",
                headers=headers,
                label="GET /api/v1/files/downloads/{download_id}/progress (partial)",
            )
            progress = response_json(runner, progress_response, "download progress JSON")
            runner.require(int(progress.get("downloaded_chunks", 0)) >= 1, "download progress records received chunk")

    runner.require(bytes(downloaded) == expected_content, "downloaded bytes equal uploaded bytes")
    complete_response = runner.request(
        "POST",
        "/api/v1/files/downloads/{download_id}/complete",
        f"/api/v1/files/downloads/{download_id}/complete",
        headers=headers,
        json={
            "expected_hash": sha256_bytes(expected_content),
            "expected_size": len(expected_content),
            "parts": parts,
        },
    )
    complete = response_json(runner, complete_response, "download complete JSON")
    runner.require(bool(complete.get("verified")), "download complete response is verified")


def create_task(
    runner: SmokeRunner,
    headers: Dict[str, str],
    *,
    title: str,
    algorithm: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = runner.request(
        "POST",
        "/api/v1/reconstruction/tasks",
        headers=headers,
        json={
            "title": title,
            "algorithm": algorithm,
            "params": params or {},
        },
        label=f"POST /api/v1/reconstruction/tasks ({algorithm})",
    )
    data = response_json(runner, response, f"create task JSON ({algorithm})")
    runner.require(str(data.get("task_id", "")).startswith("recon_"), f"task_id prefix is recon_ ({algorithm})")
    return data


def verify_openapi_routes(runner: SmokeRunner, openapi: Dict[str, Any]) -> None:
    advertised: Set[Tuple[str, str]] = set()
    for path, operations in dict(openapi.get("paths") or {}).items():
        for method in dict(operations or {}):
            normalized = method.upper()
            if normalized in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                advertised.add((normalized, path))
    advertised_custom = {
        item
        for item in advertised
        if item[1] == "/health" or item[1].startswith("/api/v1/")
    }
    missing_on_server = sorted(CUSTOM_ENDPOINTS - advertised_custom)
    runner.require(
        not missing_on_server,
        "OpenAPI contains expected custom routes",
        "all expected routes found"
        if not missing_on_server
        else "missing on server: " + ", ".join(f"{method} {path}" for method, path in missing_on_server),
    )
    untracked = sorted(advertised_custom - CUSTOM_ENDPOINTS)
    runner.require(
        not untracked,
        "smoke script tracks every OpenAPI custom route",
        "no untracked routes"
        if not untracked
        else "add tests for: " + ", ".join(f"{method} {path}" for method, path in untracked),
    )


def run_smoke(runner: SmokeRunner, args: argparse.Namespace) -> None:
    runner.request("GET", "/health")
    runner.request("GET", "/docs", label="GET /docs (Swagger)")
    runner.request("GET", "/redoc", label="GET /redoc")
    openapi_response = runner.request("GET", "/openapi.json", label="GET /openapi.json")
    verify_openapi_routes(runner, response_json(runner, openapi_response, "OpenAPI JSON"))

    suffix = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    username = f"api_smoke_{suffix}"
    password = args.password
    email = f"{username}@example.com"
    headers, user = register_user(runner, username, email, password)
    user_id = int(user["id"])

    runner.request("GET", "/api/v1/auth/me", headers=headers)
    profile_response = runner.request("GET", "/api/v1/users/me", headers=headers)
    profile_data = response_json(runner, profile_response, "profile JSON")
    runner.require(
        set(profile_data) == {
            "id",
            "username",
            "email",
            "nickname",
            "is_admin",
            "avatar_file_id",
            "avatar_thumbnail_file_id",
            "created_at",
        },
        "GET /api/v1/users/me returns compact profile fields",
    )
    runner.request(
        "PUT",
        "/api/v1/users/me",
        headers=headers,
        json={"nickname": "API smoke user"},
    )
    usage_response = runner.request("GET", "/api/v1/users/me/usage", headers=headers)
    usage_data = response_json(runner, usage_response, "usage JSON")
    runner.require(
        {
            "storage_used",
            "storage_quota",
            "task_count",
            "task_quota",
            "total_task_count",
            "gpu_running_count",
            "gpu_concurrency_quota",
            "gpu_seconds_used",
            "gpu_quota",
            "gpu_quota_exceeded",
            "gpu_quota_resets_at",
        }.issubset(usage_data),
        "GET /api/v1/users/me/usage returns active task and GPU quota fields",
    )
    runner.request(
        "PUT",
        "/api/v1/users/{user_id}/quota",
        f"/api/v1/users/{user_id}/quota",
        expected=(403,),
        headers=headers,
        json={},
        label="PUT /api/v1/users/{user_id}/quota (ordinary user denied)",
    )
    runner.request(
        "POST",
        "/api/v1/users/{user_id}/gpu-usage/reset",
        f"/api/v1/users/{user_id}/gpu-usage/reset",
        expected=(403,),
        headers=headers,
        label="POST /api/v1/users/{user_id}/gpu-usage/reset (ordinary user denied)",
    )

    if args.admin_token:
        admin_headers = bearer_headers(args.admin_token)
        runner.request(
            "PUT",
            "/api/v1/users/{user_id}/quota",
            f"/api/v1/users/{user_id}/quota",
            headers=admin_headers,
            json={
                "storage_quota": user.get("storage_quota"),
                "task_quota": user.get("task_quota"),
                "gpu_quota": user.get("gpu_quota"),
                "gpu_concurrency_quota": user.get("gpu_concurrency_quota"),
            },
            label="PUT /api/v1/users/{user_id}/quota (admin positive test)",
        )
        runner.request(
            "POST",
            "/api/v1/users/{user_id}/gpu-usage/reset",
            f"/api/v1/users/{user_id}/gpu-usage/reset",
            headers=admin_headers,
            label="POST /api/v1/users/{user_id}/gpu-usage/reset (admin positive test)",
        )
    else:
        runner.skip("admin quota positive test", "pass --admin-token to include it")
        runner.skip("admin GPU usage reset positive test", "pass --admin-token to include it")

    images = [
        png_bytes(255, 0, 0, f"{suffix}_0"),
        png_bytes(0, 255, 0, f"{suffix}_1"),
        png_bytes(0, 0, 255, f"{suffix}_2"),
    ]
    uploaded_images: List[Dict[str, Any]] = []
    for index, image in enumerate(images):
        uploaded_images.append(
            upload_bytes(
                runner,
                headers,
                filename=f"smoke_{suffix}_{index}.png",
                mime_type="image/png",
                content=image,
                check_progress=index == 0,
            )
        )
    image_ids = [str(item["file_id"]) for item in uploaded_images]

    avatar_response = runner.request(
        "PUT",
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": image_ids[0]},
        label="PUT /api/v1/users/update_avatar",
    )
    avatar_profile = response_json(runner, avatar_response, "avatar update JSON")
    runner.require(
        set(avatar_profile) == {
            "avatar_file_id",
            "avatar_thumbnail_file_id",
            "created_at",
        },
        "avatar update returns only avatar fields and created_at",
    )
    runner.require(
        avatar_profile.get("avatar_file_id") == image_ids[0],
        "profile returns avatar_file_id after update",
    )

    duplicate_init = runner.request(
        "POST",
        "/api/v1/upload/init",
        headers=headers,
        json={
            "filename": f"smoke_{suffix}_duplicate.png",
            "file_size": len(images[0]),
            "mime_type": "image/png",
            "file_hash": sha256_bytes(images[0]),
        },
        label="POST /api/v1/upload/init (duplicate reuse)",
    )
    duplicate_data = response_json(runner, duplicate_init, "duplicate init JSON")
    runner.require(bool(duplicate_data.get("already_uploaded")), "duplicate upload returns already_uploaded=true")
    runner.require(duplicate_data.get("file_id") == image_ids[0], "duplicate upload reuses existing file_id")

    cancel_content = json.dumps({"cancel": suffix}).encode("utf-8")
    cancel_init = runner.request(
        "POST",
        "/api/v1/upload/init",
        headers=headers,
        json={
            "filename": f"cancel_{suffix}.json",
            "file_size": len(cancel_content),
            "mime_type": "application/json",
            "file_hash": sha256_bytes(cancel_content),
        },
        label="POST /api/v1/upload/init (cancel session)",
    )
    cancel_upload_id = str(response_json(runner, cancel_init, "cancel upload init JSON")["upload_id"])
    runner.request(
        "POST",
        "/api/v1/upload/{upload_id}/cancel",
        f"/api/v1/upload/{cancel_upload_id}/cancel",
        headers=headers,
    )

    disposable_content = json.dumps({"delete": suffix}).encode("utf-8")
    disposable = upload_bytes(
        runner,
        headers,
        filename=f"delete_{suffix}.json",
        mime_type="application/json",
        content=disposable_content,
    )
    disposable_id = str(disposable["file_id"])

    runner.request(
        "GET",
        "/api/v1/files",
        headers=headers,
        params={"file_type": "image", "include_derivatives": "true", "limit": 200},
    )
    runner.request("GET", "/api/v1/files/{file_id}", f"/api/v1/files/{image_ids[0]}", headers=headers)
    runner.request(
        "POST",
        "/api/v1/files/{file_id}/archive",
        f"/api/v1/files/{disposable_id}/archive",
        headers=headers,
    )
    runner.request(
        "POST",
        "/api/v1/files/{file_id}/media-processing/retry",
        f"/api/v1/files/{image_ids[0]}/media-processing/retry",
        headers=headers,
    )
    download_and_verify(runner, headers, image_ids[0], images[0])

    gaussian_algorithms = runner.request("GET", "/api/v1/reconstruction/algorithms", headers=headers)
    gaussian_names = {
        item["name"] for item in response_json(runner, gaussian_algorithms, "Gaussian algorithms JSON")["algorithms"]
    }
    runner.require(
        gaussian_names == {"anysplat", "dash_gaussian", "vggt_omega"},
        "Gaussian algorithm list is separated",
    )
    mesh_algorithms = runner.request("GET", "/api/v1/reconstruction/mesh/algorithms", headers=headers)
    mesh_names = {
        item["name"] for item in response_json(runner, mesh_algorithms, "Mesh algorithms JSON")["algorithms"]
    }
    runner.require(
        mesh_names == {"dash_gaussian_mesh", "hunyuan3d"},
        "Mesh algorithm list is separated",
    )
    discover_response = runner.request(
        "GET",
        "/api/v1/reconstruction/discover",
        headers=headers,
        params={"page": 1, "page_size": 10},
    )
    discover_data = response_json(runner, discover_response, "discover JSON")
    runner.require(discover_data.get("page_size") == 10, "discover page_size is capped at 10")
    runner.request(
        "GET",
        "/api/v1/reconstruction/discover",
        headers=headers,
        params={"page": 1, "page_size": 11},
        expected=(422,),
        label="GET /api/v1/reconstruction/discover (page_size too large)",
    )

    anysplat_task = create_task(
        runner,
        headers,
        title=f"smoke anysplat {suffix}",
        algorithm="anysplat",
    )
    anysplat_task_id = str(anysplat_task["task_id"])
    runner.require(
        anysplat_task.get("params") == {"frame_nums": 4, "crop_quantile": 0.8},
        "AnySplat default params are normalized",
    )
    runner.require(anysplat_task.get("current_stage") == "task_created", "new task starts at task_created")
    runner.require("mesh_algorithm" not in anysplat_task, "create task response omits mesh_algorithm")
    runner.require("mesh_params" not in anysplat_task, "create task response omits mesh_params")
    runner.require("task_type" not in anysplat_task, "created task response omits removed task_type")
    runner.require("parent_id" not in anysplat_task, "created task response omits removed parent_id")
    runner.request("GET", "/api/v1/reconstruction/tasks", headers=headers)
    runner.request(
        "GET",
        "/api/v1/reconstruction/tasks/{task_id}",
        f"/api/v1/reconstruction/tasks/{anysplat_task_id}",
        headers=headers,
    )
    runner.request(
        "PATCH",
        "/api/v1/reconstruction/tasks/{task_id}/visibility",
        f"/api/v1/reconstruction/tasks/{anysplat_task_id}/visibility",
        expected=(409,),
        headers=headers,
        json={"visibility": "public"},
        label="PATCH /api/v1/reconstruction/tasks/{task_id}/visibility (pending task denied)",
    )
    runner.request(
        "POST",
        "/api/v1/reconstruction/mesh/start/{task_id}",
        f"/api/v1/reconstruction/mesh/start/{anysplat_task_id}",
        expected=(409,),
        headers=headers,
        json={
            "algorithm": "dash_gaussian_mesh",
            "input_file_ids": [image_ids[0]],
            "params": {},
        },
        label="POST /api/v1/reconstruction/mesh/start/{task_id} (Gaussian result required)",
    )
    runner.request(
        "POST",
        "/api/v1/reconstruction/start/{task_id}",
        f"/api/v1/reconstruction/start/{anysplat_task_id}",
        headers=headers,
        json={"input_type": "image", "input_file_ids": image_ids},
    )
    inputs_response = runner.request(
        "GET",
        "/api/v1/reconstruction/tasks/{task_id}/inputs",
        f"/api/v1/reconstruction/tasks/{anysplat_task_id}/inputs",
        headers=headers,
    )
    inputs_data = response_json(runner, inputs_response, "task inputs JSON")
    runner.require(
        inputs_data.get("input_file_ids") == image_ids,
        "task inputs returns uploaded image file_ids",
    )
    runner.request(
        "GET",
        "/api/v1/reconstruction/status/{task_id}",
        f"/api/v1/reconstruction/status/{anysplat_task_id}",
        headers=headers,
    )
    runner.request(
        "GET",
        "/api/v1/reconstruction/logs/{task_id}",
        f"/api/v1/reconstruction/logs/{anysplat_task_id}",
        headers=headers,
    )
    runner.request(
        "POST",
        "/api/v1/reconstruction/cancel/{task_id}",
        f"/api/v1/reconstruction/cancel/{anysplat_task_id}",
        headers=headers,
    )
    runner.request(
        "GET",
        "/api/v1/reconstruction/diagnostics/{task_id}",
        f"/api/v1/reconstruction/diagnostics/{anysplat_task_id}",
        expected=(403,),
        headers=headers,
        label="GET /api/v1/reconstruction/diagnostics/{task_id} (ordinary user denied)",
    )
    if args.admin_token:
        diagnostics = runner.request(
            "GET",
            "/api/v1/reconstruction/diagnostics/{task_id}",
            f"/api/v1/reconstruction/diagnostics/{anysplat_task_id}",
            headers=bearer_headers(args.admin_token),
            label="GET /api/v1/reconstruction/diagnostics/{task_id} (admin positive test)",
        )
        diagnostics_data = response_json(runner, diagnostics, "diagnostics JSON")
        runner.require(isinstance(diagnostics_data.get("checks"), list), "diagnostics returns checks")
    else:
        runner.skip("admin diagnostics positive test", "pass --admin-token to include it")

    runner.request(
        "POST",
        "/api/v1/reconstruction/tasks",
        headers=headers,
        expected=(422,),
        json={"title": "invalid standalone Hunyuan", "algorithm": "hunyuan3d", "params": {}},
        label="POST /api/v1/reconstruction/tasks (Mesh algorithm denied)",
    )
    runner.request(
        "POST",
        "/api/v1/reconstruction/mesh/start/{task_id}",
        f"/api/v1/reconstruction/mesh/start/{anysplat_task_id}",
        headers=headers,
        expected=(409,),
        json={"algorithm": "hunyuan3d", "input_file_ids": [image_ids[0]], "params": {}},
        label="POST /api/v1/reconstruction/mesh/start/{task_id} (Hunyuan requires Gaussian result)",
    )

    delete_task = create_task(
        runner,
        headers,
        title=f"smoke delete {suffix}",
        algorithm="anysplat",
    )
    runner.request(
        "DELETE",
        "/api/v1/reconstruction/tasks/{task_id}",
        f"/api/v1/reconstruction/tasks/{delete_task['task_id']}",
        headers=headers,
    )
    runner.request("DELETE", "/api/v1/files/{file_id}", f"/api/v1/files/{disposable_id}", headers=headers)
    runner.request(
        "GET",
        "/api/v1/files/{file_id}",
        f"/api/v1/files/{disposable_id}",
        expected=(404,),
        headers=headers,
        label="GET /api/v1/files/{file_id} (deleted file hidden)",
    )
    if not args.keep_resources:
        for task_id in (anysplat_task_id, hunyuan_task_id, str(geo_child_data["task_id"])):
            runner.request(
                "DELETE",
                "/api/v1/reconstruction/tasks/{task_id}",
                f"/api/v1/reconstruction/tasks/{task_id}",
                headers=headers,
                label=f"DELETE /api/v1/reconstruction/tasks/{{task_id}} (cleanup {task_id})",
            )
        for image_id in image_ids:
            runner.request(
                "DELETE",
                "/api/v1/files/{file_id}",
                f"/api/v1/files/{image_id}",
                headers=headers,
                label=f"DELETE /api/v1/files/{{file_id}} (cleanup {image_id})",
            )
    runner.request("GET", "/api/v1/users/me/usage", headers=headers, label="GET /api/v1/users/me/usage (after uploads)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test every 3DGS backend API endpoint.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Backend root URL, without /api/v1. Default: %(default)s",
    )
    parser.add_argument(
        "--password",
        default="ApiSmoke_123456",
        help="Password for the temporary smoke-test user.",
    )
    parser.add_argument(
        "--admin-token",
        default="",
        help="Optional administrator access token for positive admin endpoint tests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds. Default: %(default)s",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        help="Optional path for a JSON test report.",
    )
    parser.add_argument(
        "--keep-resources",
        action="store_true",
        help="Keep uploaded images and cancelled tasks for manual inspection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(name, None)
    runner: Optional[SmokeRunner] = None
    try:
        with httpx.Client(timeout=args.timeout) as client:
            runner = SmokeRunner(client, args.base_url)
            run_smoke(runner, args)
    except SmokeFailure as exc:
        print(f"\nSmoke test stopped: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"\nUnexpected smoke-test error: {exc}", file=sys.stderr)
        if runner:
            runner._record("FAIL", "unexpected script error", str(exc))
    finally:
        if runner:
            runner.summary(args.report_file)
    return 1 if not runner or runner.has_failures() else 0


if __name__ == "__main__":
    raise SystemExit(main())

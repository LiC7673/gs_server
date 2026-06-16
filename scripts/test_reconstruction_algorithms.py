"""
End-to-end API smoke test for reconstruction algorithms.

Default flow:
1. Register/login.
2. Upload images and/or a video through the chunked upload API.
3. Create a reconstruction task.
4. Start the selected algorithm.
5. Poll status until completed/failed/cancelled.
6. Download the result through the Files chunked download API.

Example:
  python scripts/test_reconstruction_algorithms.py \
    --base-url http://127.0.0.1:8000/api/v1 \
    --image-dir /data1/lzh/lzy/AnySplat/examples/vrnerf/riverview \
    --anysplat-video /data1/lzh/lzy/test/e3.mp4 \
    --vggt-video /data1/lzh/lzy/test/input.mp4 \
    --dash-input /data1/lzh/lzy/test/e3.mp4 \
    --algorithms anysplat,vggt_omega,dash_gaussian \
    --run-mesh \
    --mesh-algorithm dash_gaussian_mesh \
    --mesh-params '{"voxel_size":0.02}'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx


IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
}
MODEL_MIME_TYPES = {
    ".ply": "model/ply",
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def image_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    raise ValueError(f"Unsupported image type: {path}")


def file_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return image_mime_type(path)
    if ext in VIDEO_MIME_TYPES:
        return VIDEO_MIME_TYPES[ext]
    if ext in MODEL_MIME_TYPES:
        return MODEL_MIME_TYPES[ext]
    raise ValueError(f"Unsupported upload type: {path}")


def find_images(image_dir: Path, max_images: int, min_images: int = 3) -> List[Path]:
    images = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    ]
    images = sorted(images)[:max_images]
    if len(images) < min_images:
        raise RuntimeError(f"Need at least {min_images} images in {image_dir}, got {len(images)}")
    return images


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected: Iterable[int] = (200,),
    **kwargs: Any,
) -> Dict[str, Any]:
    response = client.request(method, url, **kwargs)
    if response.status_code not in set(expected):
        raise RuntimeError(
            f"{method} {url} failed: HTTP {response.status_code}\n{response.text}"
        )
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"{method} {url} did not return JSON: {response.text}") from exc


def register_and_login(
    client: httpx.Client,
    base_url: str,
    username: str,
    email: str,
    password: str,
    no_auth: bool,
) -> Dict[str, str]:
    if no_auth:
        print("[auth] skipped by --no-auth")
        return {}

    register_response = client.post(
        f"{base_url}/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    if register_response.status_code in (200, 201):
        print(f"[auth] registered user={username}")
    elif register_response.status_code in (400, 401, 409):
        print(f"[auth] register skipped: HTTP {register_response.status_code}")
    else:
        raise RuntimeError(
            f"register failed: HTTP {register_response.status_code}\n"
            f"{register_response.text}"
        )

    data = request_json(
        client,
        "POST",
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
    )
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"login response has no access_token: {data}")
    print(f"[auth] logged in user={username}")
    return {"Authorization": f"Bearer {token}"}


def find_existing_file(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    file_hash: str,
    file_size: int,
    category: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "file_hash": file_hash,
        "file_size": file_size,
        "limit": 200,
    }
    if category:
        params["category"] = category
    response = client.get(f"{base_url}/files", headers=headers, params=params)
    if response.status_code != 200:
        return None
    files = response.json().get("files", [])
    return files[0] if files else None


def upload_file(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    path: Path,
    upload_chunk_size: Optional[int],
) -> Dict[str, Any]:
    file_size = path.stat().st_size
    file_hash = sha256_file(path)
    mime_type = file_mime_type(path)
    if mime_type.startswith("image/"):
        category = "multi_view_image"
    elif mime_type.startswith("video/"):
        category = "original_video"
    elif mime_type == "model/ply":
        category = "ply_model"
    else:
        category = "other"

    init_body: Dict[str, Any] = {
        "filename": path.name,
        "file_size": file_size,
        "mime_type": mime_type,
        "file_hash": file_hash,
    }
    if upload_chunk_size:
        init_body["chunk_size"] = upload_chunk_size

    init_response = client.post(
        f"{base_url}/upload/init",
        headers=headers,
        json=init_body,
    )
    if init_response.status_code == 409:
        existing = find_existing_file(
            client,
            base_url,
            headers,
            file_hash=file_hash,
            file_size=file_size,
            category=category,
        )
        if not existing:
            raise RuntimeError(f"duplicate upload but existing file not found: {path}")
        print(f"[upload] reuse existing {path.name} -> {existing['id']}")
        return {
            "file_id": existing["id"],
            "image_id": existing["id"] if category == "multi_view_image" else None,
            "file_hash": existing.get("file_hash") or file_hash,
            "storage_key": existing.get("storage_key") or existing["id"],
            "verified": True,
        }
    if init_response.status_code != 200:
        raise RuntimeError(
            f"upload init failed for {path}: HTTP {init_response.status_code}\n"
            f"{init_response.text}"
        )

    init_data = init_response.json()
    if init_data.get("already_uploaded"):
        print(f"[upload] already uploaded {path.name} -> {init_data.get('file_id')}")
        return {
            "file_id": init_data.get("file_id"),
            "image_id": init_data.get("image_id"),
            "file_hash": init_data.get("file_hash") or file_hash,
            "storage_key": init_data.get("storage_key") or init_data.get("file_id"),
            "verified": True,
        }

    upload_id = init_data["upload_id"]
    chunk_size = int(init_data["chunk_size"])
    total_chunks = int(init_data["total_chunks"])
    print(
        f"[upload] {path.name} size={file_size} chunks={total_chunks} "
        f"mime={mime_type}"
    )

    parts: List[Dict[str, Any]] = []
    with path.open("rb") as handle:
        for chunk_index in range(total_chunks):
            chunk = handle.read(chunk_size)
            response = client.put(
                f"{base_url}/upload/{upload_id}/chunk",
                headers={**headers, "Content-Type": "application/octet-stream"},
                params={"chunk_index": chunk_index},
                content=chunk,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"upload chunk failed for {path} #{chunk_index}: "
                    f"HTTP {response.status_code}\n{response.text}"
                )
            chunk_data = response.json()
            etag = chunk_data.get("etag") or md5_bytes(chunk)
            parts.append({"chunk_index": chunk_index, "etag": etag})
            print(f"  chunk {chunk_index + 1}/{total_chunks} etag={etag}")

    merge_data = request_json(
        client,
        "POST",
        f"{base_url}/upload/{upload_id}/merge",
        headers=headers,
        json={
            "expected_hash": file_hash,
            "expected_size": file_size,
            "parts": parts,
        },
    )
    print(f"[upload] merged {path.name} -> file_id={merge_data.get('file_id')}")
    return merge_data


def create_reconstruction_task(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    algorithm: str,
    params: Dict[str, Any],
) -> str:
    data = request_json(
        client,
        "POST",
        f"{base_url}/reconstruction/tasks",
        headers=headers,
        json={
            "title": f"api smoke {algorithm}",
            "algorithm": algorithm,
            "params": params,
        },
    )
    task_id = data["task_id"]
    print(f"[task] algorithm={algorithm} task_id={task_id}")
    return task_id


def start_reconstruction(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    task_id: str,
    body: Dict[str, Any],
    endpoint: str = "start",
) -> Dict[str, Any]:
    data = request_json(
        client,
        "POST",
        f"{base_url}/reconstruction/{endpoint}/{task_id}",
        headers=headers,
        json=body,
    )
    print(
        f"[start] task_id={task_id} status={data.get('status')} "
        f"input_type={data.get('input_type')} input_files={data.get('input_file_count')}"
    )
    return data


def poll_reconstruction(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    task_id: str,
    poll_interval: float,
    timeout_seconds: int,
) -> Dict[str, Any]:
    started = time.monotonic()
    last_status = ""
    while True:
        data = request_json(
            client,
            "GET",
            f"{base_url}/reconstruction/status/{task_id}",
            headers=headers,
        )
        status = data.get("status", "unknown")
        stage = data.get("current_stage", "")
        progress = data.get("progress", 0)
        elapsed = int(time.monotonic() - started)
        line = f"[poll] {elapsed:4d}s status={status} progress={progress} stage={stage}"
        if line != last_status:
            print(line)
            last_status = line

        if status in {"completed", "partial_completed", "failed", "cancelled"}:
            return data
        if elapsed > timeout_seconds:
            raise TimeoutError(f"poll timeout after {timeout_seconds}s for {task_id}")
        time.sleep(poll_interval)


def print_failure_debug(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    task_id: str,
) -> None:
    for name, url in {
        "logs": f"{base_url}/reconstruction/logs/{task_id}",
        "diagnostics": f"{base_url}/reconstruction/diagnostics/{task_id}",
    }.items():
        response = client.get(url, headers=headers)
        print(f"[{name}] HTTP {response.status_code}")
        try:
            print(json.dumps(response.json(), ensure_ascii=False, indent=2)[:6000])
        except Exception:
            print(response.text[:6000])


def download_result(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    file_id: str,
    output_dir: Path,
    prefix: str,
    download_chunk_size: Optional[int],
) -> Path:
    body: Dict[str, Any] = {}
    if download_chunk_size:
        body["chunk_size"] = download_chunk_size
    init_data = request_json(
        client,
        "POST",
        f"{base_url}/files/{file_id}/download/init",
        headers=headers,
        json=body or None,
    )
    download_id = init_data["download_id"]
    total_chunks = int(init_data["total_chunks"])
    file_size = int(init_data["file_size"])
    file_hash = init_data.get("file_hash") or ""
    filename = init_data.get("filename") or f"{prefix}.bin"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{prefix}_{filename}"

    print(
        f"[download] file_id={file_id} size={file_size} "
        f"chunks={total_chunks} -> {output_path}"
    )
    parts: List[Dict[str, Any]] = []
    with output_path.open("wb") as target:
        for chunk_index in range(total_chunks):
            response = client.get(
                f"{base_url}/files/{file_id}/download/chunk",
                headers=headers,
                params={"download_id": download_id, "chunk_index": chunk_index},
            )
            if response.status_code != 206:
                raise RuntimeError(
                    f"download chunk failed #{chunk_index}: "
                    f"HTTP {response.status_code}\n{response.text}"
                )
            data = response.content
            target.write(data)
            etag = (
                response.headers.get("X-Chunk-Etag")
                or response.headers.get("ETag", "").strip('"')
                or md5_bytes(data)
            )
            parts.append({"chunk_index": chunk_index, "etag": etag})
            print(f"  chunk {chunk_index + 1}/{total_chunks} etag={etag}")

    complete_data = request_json(
        client,
        "POST",
        f"{base_url}/files/downloads/{download_id}/complete",
        headers=headers,
        json={
            "expected_hash": file_hash if len(file_hash) == 64 else "",
            "expected_size": file_size,
            "parts": parts,
        },
    )
    print(f"[download] complete verified={complete_data.get('verified')}")
    return output_path


def run_algorithm(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    algorithm: str,
    image_ids: List[str],
    anysplat_video_file_id: Optional[str],
    video_file_id: Optional[str],
    dash_file_ids: List[str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    task_id = create_reconstruction_task(
        client=client,
        base_url=base_url,
        headers=headers,
        algorithm=algorithm,
        params=args.params,
    )

    endpoint = "start"
    if algorithm == "dash_gaussian" and dash_file_ids:
        start_body = {
            "input_type": "video" if len(dash_file_ids) == 1 else "image",
            "input_file_ids": dash_file_ids,
        }
    elif algorithm == "anysplat" and anysplat_video_file_id:
        start_body = {"input_type": "video", "input_file_ids": [anysplat_video_file_id]}
    elif algorithm == "vggt_omega" and video_file_id:
        start_body = {"input_type": "video", "input_file_ids": [video_file_id]}
    else:
        start_body = {"input_type": "image", "input_file_ids": image_ids}

    start_reconstruction(client, base_url, headers, task_id, start_body, endpoint=endpoint)
    final_status = poll_reconstruction(
        client,
        base_url,
        headers,
        task_id,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
    )

    if (
        args.run_mesh
        and algorithm in {"anysplat", "dash_gaussian", "vggt_omega"}
        and final_status.get("status") == "completed"
        and final_status.get("current_stage") == "gaussian_completed"
    ):
        ply_file_id = final_status.get("ply_id")
        if not ply_file_id:
            raise RuntimeError(f"Gaussian stage returned no ply_id for {task_id}")
        mesh_input_ids = (
            [ply_file_id]
            if args.mesh_algorithm == "dash_gaussian_mesh"
            else list(start_body["input_file_ids"])
        )
        start_reconstruction(
            client,
            base_url,
            headers,
            task_id,
            {
                "algorithm": args.mesh_algorithm,
                "input_file_ids": mesh_input_ids,
                "params": args.mesh_params,
            },
            endpoint="mesh/start",
        )
        final_status = poll_reconstruction(
            client,
            base_url,
            headers,
            task_id,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
        )

    if final_status.get("status") != "completed":
        print(
            f"[result] {algorithm} failed status={final_status.get('status')} "
            f"error={final_status.get('error')}"
        )
        print_failure_debug(client, base_url, headers, task_id)
        return final_status

    result_id = final_status.get("result_id") or final_status.get("ply_id")
    print(
        f"[result] {algorithm} completed task_id={task_id} "
        f"result_id={result_id} category={final_status.get('result_category')}"
    )
    if not args.skip_download:
        result_files = final_status.get("result_files") or []
        if not result_files:
            result_files = [{"file_id": result_id}] if result_id else []
        downloaded = set()
        for index, result_file in enumerate(result_files):
            file_id = result_file.get("file_id")
            if not file_id or file_id in downloaded:
                continue
            downloaded.add(file_id)
            output_path = download_result(
                client,
                base_url,
                headers,
                file_id=file_id,
                output_dir=args.output_dir,
                prefix=f"{algorithm}_{task_id}_{index}",
                download_chunk_size=args.download_chunk_size,
            )
            print(f"[result] saved {output_path}")
    return final_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test reconstruction algorithm APIs.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--algorithms",
        default="anysplat,vggt_omega",
        help="Comma-separated Gaussian algorithms: anysplat,vggt_omega,dash_gaussian",
    )
    parser.add_argument(
        "--image-dir",
        default="/data1/lzh/lzy/AnySplat/examples/vrnerf/riverview",
        help="Directory containing at least 3 jpg/png images.",
    )
    parser.add_argument(
        "--anysplat-video",
        default="",
        help="Optional video path for anysplat. If omitted, anysplat uses images.",
    )
    parser.add_argument(
        "--vggt-video",
        default="",
        help="Optional video path for vggt_omega. If omitted, vggt_omega uses images.",
    )
    parser.add_argument(
        "--dash-input",
        default="",
        help="Optional video file or image directory for dash_gaussian. If omitted, dash_gaussian uses --image-dir.",
    )
    parser.add_argument("--max-images", type=int, default=10)
    parser.add_argument("--username", default=f"apitest_{int(time.time())}")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="Test123456_")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--upload-chunk-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--download-chunk-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("algorithm_api_results"))
    parser.add_argument(
        "--params",
        type=json.loads,
        default={},
        help='JSON object passed to /reconstruction/tasks, for example \'{"frame_nums":4,"crop_quantile":0.8}\'.',
    )
    parser.add_argument(
        "--mesh-params",
        type=json.loads,
        default={},
        help='JSON object passed to the manual /reconstruction/mesh/start stage.',
    )
    parser.add_argument(
        "--run-mesh",
        action="store_true",
        help="After a Gaussian stage completes, manually start --mesh-algorithm on the same task.",
    )
    parser.add_argument(
        "--mesh-algorithm",
        choices=("dash_gaussian_mesh", "hunyuan3d"),
        default="dash_gaussian_mesh",
        help="Mesh algorithm used by --run-mesh.",
    )
    return parser.parse_args()


def main() -> int:
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    algorithms = [item.strip() for item in args.algorithms.split(",") if item.strip()]
    unsupported = sorted(set(algorithms) - {"anysplat", "dash_gaussian", "vggt_omega"})
    if unsupported:
        raise RuntimeError(
            "Only Gaussian algorithms belong in --algorithms; use --run-mesh "
            f"and --mesh-algorithm for Mesh stages. Unsupported: {', '.join(unsupported)}"
        )
    email = args.email or f"{args.username}@example.com"

    need_images = any(algorithm not in {
        "anysplat",
        "vggt_omega",
        "dash_gaussian",
    } for algorithm in algorithms)
    if "anysplat" in algorithms and not args.anysplat_video:
        need_images = True
    if "vggt_omega" in algorithms and not args.vggt_video:
        need_images = True
    if "dash_gaussian" in algorithms and not args.dash_input:
        need_images = True

    image_paths: List[Path] = []
    if need_images:
        image_dir = Path(args.image_dir)
        if not image_dir.is_dir():
            raise RuntimeError(f"image dir does not exist: {image_dir}")
        image_paths = find_images(image_dir, args.max_images)
        print(f"[input] images={len(image_paths)} dir={image_dir}")

    video_path: Optional[Path] = None
    if args.vggt_video:
        video_path = Path(args.vggt_video)
        if not video_path.is_file():
            raise RuntimeError(f"video does not exist: {video_path}")
        print(f"[input] vggt video={video_path}")

    anysplat_video_path: Optional[Path] = None
    if args.anysplat_video:
        anysplat_video_path = Path(args.anysplat_video)
        if not anysplat_video_path.is_file():
            raise RuntimeError(f"anysplat video does not exist: {anysplat_video_path}")
        print(f"[input] anysplat video={anysplat_video_path}")

    dash_paths: List[Path] = []
    if args.dash_input:
        dash_input = Path(args.dash_input)
        if dash_input.is_dir():
            dash_paths = find_images(dash_input, args.max_images, min_images=3)
        elif dash_input.is_file():
            mime_type = file_mime_type(dash_input)
            if not mime_type.startswith("video/"):
                raise RuntimeError("dash input file must be a video; use --image-dir for image folders")
            dash_paths = [dash_input]
        else:
            raise RuntimeError(f"dash input does not exist: {dash_input}")
        print(f"[input] dash files={len(dash_paths)} source={dash_input}")

    with httpx.Client(timeout=None) as client:
        headers = register_and_login(
            client,
            base_url,
            username=args.username,
            email=email,
            password=args.password,
            no_auth=args.no_auth,
        )

        algorithms_data = request_json(
            client,
            "GET",
            f"{base_url}/reconstruction/algorithms",
            headers=headers,
        )
        print("[algorithms]")
        print(json.dumps(algorithms_data, ensure_ascii=False, indent=2))
        mesh_algorithms_data = request_json(
            client,
            "GET",
            f"{base_url}/reconstruction/mesh/algorithms",
            headers=headers,
        )
        print("[mesh algorithms]")
        print(json.dumps(mesh_algorithms_data, ensure_ascii=False, indent=2))

        image_ids: List[str] = []
        for image_path in image_paths:
            upload_data = upload_file(
                client,
                base_url,
                headers,
                image_path,
                args.upload_chunk_size,
            )
            image_id = upload_data.get("image_id") or upload_data.get("file_id")
            if not image_id:
                raise RuntimeError(f"upload returned no image_id/file_id for {image_path}")
            image_ids.append(image_id)

        video_file_id: Optional[str] = None
        if video_path:
            upload_data = upload_file(
                client,
                base_url,
                headers,
                video_path,
                args.upload_chunk_size,
            )
            video_file_id = upload_data.get("file_id")
            if not video_file_id:
                raise RuntimeError(f"upload returned no file_id for {video_path}")

        anysplat_video_file_id: Optional[str] = None
        if anysplat_video_path:
            upload_data = upload_file(
                client,
                base_url,
                headers,
                anysplat_video_path,
                args.upload_chunk_size,
            )
            anysplat_video_file_id = upload_data.get("file_id")
            if not anysplat_video_file_id:
                raise RuntimeError(f"upload returned no file_id for {anysplat_video_path}")

        dash_file_ids: List[str] = []
        for dash_path in dash_paths:
            upload_data = upload_file(
                client,
                base_url,
                headers,
                dash_path,
                args.upload_chunk_size,
            )
            file_id = upload_data.get("file_id") or upload_data.get("image_id")
            if not file_id:
                raise RuntimeError(f"upload returned no file_id for {dash_path}")
            dash_file_ids.append(file_id)

        results: Dict[str, Dict[str, Any]] = {}
        for algorithm in algorithms:
            print("=" * 80)
            print(f"[run] algorithm={algorithm}")
            results[algorithm] = run_algorithm(
                client,
                base_url,
                headers,
                algorithm,
                image_ids=image_ids,
                anysplat_video_file_id=anysplat_video_file_id,
                video_file_id=video_file_id,
                dash_file_ids=dash_file_ids,
                args=args,
            )

    print("=" * 80)
    print("[summary]")
    print(json.dumps(results, ensure_ascii=False, indent=2)[:12000])
    failed = [
        algorithm
        for algorithm, data in results.items()
        if data.get("status") != "completed"
    ]
    if failed:
        print(f"[summary] failed algorithms: {', '.join(failed)}")
        return 1
    print("[summary] all selected algorithms completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        raise SystemExit(130)
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        raise SystemExit(1)

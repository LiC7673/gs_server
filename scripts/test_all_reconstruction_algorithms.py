"""
Run a real end-to-end API test for all configured reconstruction algorithms.

Default flow:
1. Register/login a test user.
2. Upload images from /data1/lzh/dhh/test_data1213 once.
3. Run Gaussian algorithms: anysplat, dash_gaussian, vggt_omega.
4. Pick the first successful task with a PLY result.
5. Run Mesh algorithms on the same task: dash_gaussian_mesh, hunyuan3d.
6. Optionally download result files through the unified chunked download API.

Example:
  python scripts/test_all_reconstruction_algorithms.py \
    --base-url http://127.0.0.1:8888/api/v1 \
    --image-dir /data1/lzh/dhh/test_data1213 \
    --skip-download
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx

from test_reconstruction_algorithms import (
    download_result,
    find_images,
    print_failure_debug,
    register_and_login,
    request_json,
    start_reconstruction,
    poll_reconstruction,
    upload_file,
)


DEFAULT_GAUSSIAN_ALGORITHMS = ["anysplat", "dash_gaussian", "vggt_omega"]
DEFAULT_MESH_ALGORITHMS = ["dash_gaussian_mesh", "hunyuan3d"]
DEFAULT_GAUSSIAN_PARAMS = {
    "anysplat": {"frame_nums": 4, "crop_quantile": 0.8},
    "dash_gaussian": {"iterations": 30000},
    "vggt_omega": {},
}
DEFAULT_MESH_PARAMS = {
    "dash_gaussian_mesh": {},
    "hunyuan3d": {},
}


def parse_json_map(value: str) -> Dict[str, Dict[str, Any]]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    result: Dict[str, Dict[str, Any]] = {}
    for key, item in parsed.items():
        if not isinstance(item, dict):
            raise argparse.ArgumentTypeError(f"{key} value must be a JSON object")
        result[str(key)] = item
    return result


def comma_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def status_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": data.get("task_id"),
        "status": data.get("status"),
        "current_stage": data.get("current_stage"),
        "progress": data.get("progress"),
        "queue_reason": data.get("queue_reason"),
        "error_code": data.get("error_code"),
        "error_status_code": data.get("error_status_code"),
        "error": data.get("error"),
        "ply_id": data.get("ply_id"),
        "result_id": data.get("result_id"),
        "result_files": data.get("result_files") or [],
        "gpu_seconds_cost": data.get("gpu_seconds_cost"),
        "gpu_quota_exceeded": data.get("gpu_quota_exceeded"),
    }


def result_file_ids(data: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for item in data.get("result_files") or []:
        file_id = item.get("file_id")
        if file_id and file_id not in ids:
            ids.append(file_id)
    fallback = data.get("result_id") or data.get("ply_id")
    if fallback and fallback not in ids:
        ids.append(fallback)
    return ids


def maybe_download_results(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    stage_name: str,
    status_data: Dict[str, Any],
    downloaded: Set[str],
    args: argparse.Namespace,
) -> None:
    if args.skip_download:
        return
    for index, file_id in enumerate(result_file_ids(status_data)):
        if file_id in downloaded:
            continue
        downloaded.add(file_id)
        output_path = download_result(
            client,
            base_url,
            headers,
            file_id=file_id,
            output_dir=args.output_dir,
            prefix=f"{stage_name}_{index}",
            download_chunk_size=args.download_chunk_size,
        )
        print(f"[downloaded] {stage_name} -> {output_path}")


def create_task(
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
            "title": f"real test {algorithm} {int(time.time())}",
            "algorithm": algorithm,
            "params": params,
        },
    )
    task_id = data["task_id"]
    print(f"[task] {algorithm} -> {task_id}")
    return task_id


def run_gaussian_algorithm(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    algorithm: str,
    image_ids: List[str],
    params: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    task_id = create_task(client, base_url, headers, algorithm, params)
    start_reconstruction(
        client,
        base_url,
        headers,
        task_id,
        {"input_type": "image", "input_file_ids": image_ids},
    )
    final_status = poll_reconstruction(
        client,
        base_url,
        headers,
        task_id,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
    )
    if final_status.get("status") != "completed" or final_status.get("current_stage") != "gaussian_completed":
        print(f"[failed] Gaussian {algorithm}")
        print_failure_debug(client, base_url, headers, task_id)
    return final_status


def run_mesh_algorithm(
    client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    task_id: str,
    algorithm: str,
    ply_id: str,
    original_image_ids: List[str],
    params: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    input_file_ids = [ply_id] if algorithm == "dash_gaussian_mesh" else original_image_ids
    start_reconstruction(
        client,
        base_url,
        headers,
        task_id,
        {
            "algorithm": algorithm,
            "input_file_ids": input_file_ids,
            "params": params,
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
    if final_status.get("status") != "completed" or final_status.get("current_stage") != "mesh_completed":
        print(f"[failed] Mesh {algorithm}")
        print_failure_debug(client, base_url, headers, task_id)
    return final_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all real reconstruction algorithms via API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8888/api/v1")
    parser.add_argument("--image-dir", default="/data1/lzh/dhh/test_data1213")
    parser.add_argument("--max-images", type=int, default=10)
    parser.add_argument("--username", default=f"algo_test_{int(time.time())}")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="Test123456_")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument(
        "--gaussian-algorithms",
        default=",".join(DEFAULT_GAUSSIAN_ALGORITHMS),
        help="Comma-separated Gaussian algorithms.",
    )
    parser.add_argument(
        "--mesh-algorithms",
        default=",".join(DEFAULT_MESH_ALGORITHMS),
        help="Comma-separated Mesh algorithms.",
    )
    parser.add_argument(
        "--gaussian-params",
        type=parse_json_map,
        default={},
        help='Per-algorithm params, for example \'{"anysplat":{"frame_nums":4},"dash_gaussian":{"iterations":30000}}\'.',
    )
    parser.add_argument(
        "--mesh-params",
        type=parse_json_map,
        default={},
        help='Per-mesh params, for example \'{"dash_gaussian_mesh":{"voxel_size":0.02}}\'.',
    )
    parser.add_argument("--upload-chunk-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--download-chunk-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("all_algorithm_results"))
    parser.add_argument("--report-file", type=Path, default=Path("all_algorithm_report.json"))
    return parser.parse_args()


def main() -> int:
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    image_dir = Path(args.image_dir)
    if not image_dir.is_dir():
        raise RuntimeError(f"image dir does not exist: {image_dir}")

    gaussian_algorithms = comma_list(args.gaussian_algorithms)
    mesh_algorithms = comma_list(args.mesh_algorithms)
    unknown_gaussian = sorted(set(gaussian_algorithms) - set(DEFAULT_GAUSSIAN_ALGORITHMS))
    unknown_mesh = sorted(set(mesh_algorithms) - set(DEFAULT_MESH_ALGORITHMS))
    if unknown_gaussian:
        raise RuntimeError(f"unsupported Gaussian algorithms: {', '.join(unknown_gaussian)}")
    if unknown_mesh:
        raise RuntimeError(f"unsupported Mesh algorithms: {', '.join(unknown_mesh)}")

    image_paths = find_images(image_dir, args.max_images, min_images=3)
    print(f"[input] image_dir={image_dir} images={len(image_paths)}")
    email = args.email or f"{args.username}@example.com"
    report: Dict[str, Any] = {
        "base_url": base_url,
        "image_dir": str(image_dir),
        "gaussian": {},
        "mesh": {},
        "selected_mesh_base_task_id": None,
        "selected_ply_id": None,
    }
    downloaded: Set[str] = set()

    with httpx.Client(timeout=None) as client:
        headers = register_and_login(
            client,
            base_url,
            username=args.username,
            email=email,
            password=args.password,
            no_auth=args.no_auth,
        )

        print("[available gaussian algorithms]")
        print(json.dumps(request_json(client, "GET", f"{base_url}/reconstruction/algorithms", headers=headers), ensure_ascii=False, indent=2))
        print("[available mesh algorithms]")
        print(json.dumps(request_json(client, "GET", f"{base_url}/reconstruction/mesh/algorithms", headers=headers), ensure_ascii=False, indent=2))

        image_ids: List[str] = []
        for path in image_paths:
            data = upload_file(client, base_url, headers, path, args.upload_chunk_size)
            file_id = data.get("image_id") or data.get("file_id")
            if not file_id:
                raise RuntimeError(f"upload returned no file_id for {path}")
            image_ids.append(file_id)
        print(f"[upload] uploaded/reused image ids={image_ids}")

        mesh_base_status: Optional[Dict[str, Any]] = None
        for algorithm in gaussian_algorithms:
            print("=" * 80)
            print(f"[run gaussian] {algorithm}")
            params = {**DEFAULT_GAUSSIAN_PARAMS[algorithm], **args.gaussian_params.get(algorithm, {})}
            try:
                status_data = run_gaussian_algorithm(
                    client,
                    base_url,
                    headers,
                    algorithm,
                    image_ids,
                    params,
                    args,
                )
                report["gaussian"][algorithm] = status_summary(status_data)
                maybe_download_results(client, base_url, headers, algorithm, status_data, downloaded, args)
                if (
                    mesh_base_status is None
                    and status_data.get("status") == "completed"
                    and status_data.get("current_stage") == "gaussian_completed"
                    and status_data.get("ply_id")
                ):
                    mesh_base_status = status_data
            except Exception as exc:
                print(f"[exception] Gaussian {algorithm}: {exc}")
                report["gaussian"][algorithm] = {"status": "exception", "error": str(exc)}

        if mesh_algorithms:
            if not mesh_base_status:
                print("[mesh] skipped: no successful Gaussian task with ply_id")
            else:
                task_id = str(mesh_base_status["task_id"])
                ply_id = str(mesh_base_status["ply_id"])
                report["selected_mesh_base_task_id"] = task_id
                report["selected_ply_id"] = ply_id
                for algorithm in mesh_algorithms:
                    print("=" * 80)
                    print(f"[run mesh] {algorithm} on task={task_id}")
                    params = {**DEFAULT_MESH_PARAMS[algorithm], **args.mesh_params.get(algorithm, {})}
                    try:
                        status_data = run_mesh_algorithm(
                            client,
                            base_url,
                            headers,
                            task_id,
                            algorithm,
                            ply_id,
                            image_ids,
                            params,
                            args,
                        )
                        report["mesh"][algorithm] = status_summary(status_data)
                        maybe_download_results(client, base_url, headers, algorithm, status_data, downloaded, args)
                    except Exception as exc:
                        print(f"[exception] Mesh {algorithm}: {exc}")
                        report["mesh"][algorithm] = {"status": "exception", "error": str(exc)}

    args.report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"[report] {args.report_file}")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])

    failed = []
    for group in ("gaussian", "mesh"):
        for algorithm, data in report[group].items():
            expected_stage = "gaussian_completed" if group == "gaussian" else "mesh_completed"
            if data.get("status") != "completed" or data.get("current_stage") != expected_stage:
                failed.append(f"{group}:{algorithm}")
    if failed:
        print(f"[summary] failed: {', '.join(failed)}")
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

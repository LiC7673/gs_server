import os
from pathlib import Path

import pytest

from app.api.v1.reconstruction import (
    _algorithm_specs,
    _build_command,
    _find_output_file,
    _normalize_anysplat_params,
)
from app.utils.hash import compute_chunk_hash, compute_hash


async def register(client, username):
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "testpass123",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def upload(client, headers, name, content, mime_type="image/png"):
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", headers=headers, json={
        "filename": name,
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": mime_type,
        "file_hash": file_hash,
    })
    upload_id = init.json()["upload_id"]
    await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=content,
    )
    merged = await client.post(f"/api/v1/upload/{upload_id}/merge", headers=headers, json={
        "expected_hash": file_hash,
        "expected_size": len(content),
        "parts": [{"chunk_index": 0, "etag": compute_chunk_hash(content)}],
    })
    return merged.json()["file_id"]


async def create_task(client, headers, params=None):
    return await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "anysplat test",
        "algorithm": "anysplat",
        "params": params or {},
    })


@pytest.mark.asyncio
async def test_anysplat_task_params_receive_defaults_and_validate_types(client):
    headers = await register(client, "anysplatparams")
    created = await create_task(client, headers)
    assert created.status_code == 200
    assert created.json()["params"] == {
        "frame_nums": 4,
        "crop_quantile": 0.8,
    }

    custom = await create_task(client, headers, {"frame_nums": 7, "crop_quantile": 0.35})
    assert custom.status_code == 200
    assert custom.json()["params"] == {
        "frame_nums": 7,
        "crop_quantile": 0.35,
    }

    wrong_frame_nums = await create_task(client, headers, {"frame_nums": 4.0})
    assert wrong_frame_nums.status_code == 422
    wrong_crop_quantile = await create_task(client, headers, {"crop_quantile": "0.8"})
    assert wrong_crop_quantile.status_code == 422
    unknown = await create_task(client, headers, {"unknown": "value"})
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_anysplat_legacy_algorithm_selector_is_kept_but_not_added_to_command(client):
    headers = await register(client, "anysplatselector")
    created = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "legacy anysplat selector",
        "params": {"algorithm": "anysplat"},
    })
    assert created.status_code == 200
    assert created.json()["algorithm"] == "anysplat"
    assert created.json()["params"] == {
        "frame_nums": 4,
        "crop_quantile": 0.8,
        "algorithm": "anysplat",
    }

    spec = _algorithm_specs()["anysplat"]
    command = _build_command(
        spec,
        "recon_test",
        Path("/tmp/images"),
        Path("/tmp/input.mp4"),
        Path("/tmp/output"),
        created.json()["params"],
    )
    assert "algorithm" not in command
    assert command == [
        "/data1/lzh/anaconda3/envs/anysplat/bin/python",
        "/data1/lzh/lzy/AnySplat/export_scene_gaussians.py",
        "/tmp/input.mp4",
        "--frame_nums",
        "4",
        "--output_folder",
        "/tmp/output",
        "--crop_quantile",
        "0.8",
    ]


@pytest.mark.asyncio
async def test_anysplat_start_accepts_video_and_three_images_but_rejects_invalid_groups(client):
    headers = await register(client, "anysplatinputs")
    image_ids = [
        await upload(client, headers, f"image-{index}.png", f"image-{index}".encode())
        for index in range(3)
    ]
    video_id = await upload(client, headers, "e3.mp4", b"video-e3", "video/mp4")

    video_task = (await create_task(client, headers)).json()["task_id"]
    video_started = await client.post(
        f"/api/v1/reconstruction/start/{video_task}",
        headers=headers,
        json={"input_type": "video", "input_file_ids": [video_id]},
    )
    assert video_started.status_code == 200
    assert video_started.json()["input_type"] == "video"
    assert video_started.json()["input_file_count"] == 1

    mismatch_task = (await create_task(client, headers)).json()["task_id"]
    mismatch = await client.post(
        f"/api/v1/reconstruction/start/{mismatch_task}",
        headers=headers,
        json={"input_type": "image", "input_file_ids": [video_id]},
    )
    assert mismatch.status_code == 400

    conflict_task = (await create_task(client, headers)).json()["task_id"]
    conflict = await client.post(
        f"/api/v1/reconstruction/start/{conflict_task}",
        headers=headers,
        json={"input_type": "video", "type": "image", "input_file_ids": [video_id]},
    )
    assert conflict.status_code == 422

    image_task = (await create_task(client, headers)).json()["task_id"]
    images_started = await client.post(
        f"/api/v1/reconstruction/start/{image_task}",
        headers=headers,
        json={"input_type": "image", "input_file_ids": image_ids},
    )
    assert images_started.status_code == 200
    assert images_started.json()["input_type"] == "image_folder"
    assert images_started.json()["input_file_count"] == 3

    too_few_task = (await create_task(client, headers)).json()["task_id"]
    too_few = await client.post(
        f"/api/v1/reconstruction/start/{too_few_task}",
        headers=headers,
        json={"input_file_ids": image_ids[:2]},
    )
    assert too_few.status_code == 400

    mixed_task = (await create_task(client, headers)).json()["task_id"]
    mixed = await client.post(
        f"/api/v1/reconstruction/start/{mixed_task}",
        headers=headers,
        json={"input_file_ids": [image_ids[0], video_id]},
    )
    assert mixed.status_code == 400


def test_anysplat_worker_defaults_and_latest_ply_selection(tmp_path):
    assert _normalize_anysplat_params({}) == {
        "frame_nums": 4,
        "crop_quantile": 0.8,
    }
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    older = output_dir / "older.ply"
    newer = output_dir / "newer.ply"
    empty = output_dir / "empty.ply"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    empty.write_bytes(b"")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    os.utime(empty, (3, 3))
    assert _find_output_file(output_dir, "**/*.ply") == newer

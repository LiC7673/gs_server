from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.v1.reconstruction import (
    _algorithm_specs,
    _build_command,
    _normalize_dash_gaussian_params,
    _register_dash_gaussian_results,
    _rewrite_dash_gaussian_cfg_args,
    _select_dash_gaussian_restore_links_for_ply,
)
from app.models.file import FileCategory, FileDerivativeRecord, FileDerivativeVariant, FileRecord
from app.models.task import TaskFileRole, TaskStatus
from app.services.task_service import TaskService
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
    response = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "dash gaussian test",
        "algorithm": "dash_gaussian",
        "params": params or {},
    })
    return response


@pytest.mark.asyncio
async def test_dash_gaussian_replaces_segment_then_splat_in_algorithm_list(client):
    headers = await register(client, "dashalgorithms")
    response = await client.get(
        "/api/v1/reconstruction/render/algorithm",
        headers={**headers, "X-App-Locale": "en-US"},
    )
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["algorithms"]}
    assert "dash_gaussian" in names
    assert "segment_then_splat" not in names
    dash = next(item for item in response.json()["algorithms"] if item["name"] == "dash_gaussian")
    assert dash["name"] == "dash_gaussian"
    assert dash["display_name"] == "DashGaussian"
    assert dash["available"] is True
    assert dash["params"] == [
        {
            "param_name": "iterations",
            "description": (
                "Number of Gaussian training iterations; more iterations usually improve "
                "stability and quality but take longer and occupy the GPU for more time."
            ),
            "display_name": "Training iterations",
            "default_value": 30000,
        }
    ]

    zh_response = await client.get(
        "/api/v1/reconstruction/render/algorithm",
        headers={**headers, "X-App-Locale": "zh-CN"},
    )
    zh_dash = next(item for item in zh_response.json()["algorithms"] if item["name"] == "dash_gaussian")
    assert zh_dash["params"][0]["display_name"] == "训练轮数"


@pytest.mark.asyncio
async def test_dash_gaussian_task_params_receive_defaults_and_validate(client):
    headers = await register(client, "dashparams")
    created = await create_task(client, headers)
    assert created.status_code == 200
    assert created.json()["params"] == {"iterations": 30000}

    custom = await create_task(client, headers, {"iterations": 1234})
    assert custom.status_code == 200
    assert custom.json()["params"] == {"iterations": 1234}

    selector = await create_task(client, headers, {"algorithm": "dash_gaussian"})
    assert selector.status_code == 200
    assert selector.json()["params"] == {"iterations": 30000, "algorithm": "dash_gaussian"}

    assert (await create_task(client, headers, {"iterations": 0})).status_code == 422
    assert (await create_task(client, headers, {"iterations": -1})).status_code == 422
    assert (await create_task(client, headers, {"iterations": 30000.0})).status_code == 422
    assert (await create_task(client, headers, {"unknown": "value"})).status_code == 422
    assert (await create_task(client, headers, {"algorithm": "segment_then_splat"})).status_code == 422


@pytest.mark.asyncio
async def test_dash_gaussian_start_accepts_video_and_three_images_but_rejects_invalid_groups(client, db_session):
    headers = await register(client, "dashinputs")
    image_ids = [
        await upload(client, headers, f"dash-{index}.png", f"dash-image-{index}".encode())
        for index in range(3)
    ]
    video_id = await upload(client, headers, "dash.mp4", b"dash-video", "video/mp4")

    video_task = (await create_task(client, headers)).json()["task_id"]
    video_started = await client.post(
        f"/api/v1/reconstruction/start/{video_task}",
        headers=headers,
        json={"input_file_ids": [video_id]},
    )
    assert video_started.status_code == 200
    assert video_started.json()["input_type"] == "video"

    image_task = (await create_task(client, headers)).json()["task_id"]
    images_started = await client.post(
        f"/api/v1/reconstruction/start/{image_task}",
        headers=headers,
        json={"input_file_ids": image_ids},
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

    source_id = await upload(client, headers, "source.png", b"dash-source")
    thumbnail_id = await upload(client, headers, "thumbnail.jpg", b"dash-thumbnail", "image/jpeg")
    records = list((await db_session.execute(
        select(FileRecord).where(FileRecord.public_id.in_([source_id, thumbnail_id]))
    )).scalars())
    source = next(record for record in records if record.public_id == source_id)
    thumbnail = next(record for record in records if record.public_id == thumbnail_id)
    thumbnail.category = FileCategory.PREVIEW_IMAGE
    db_session.add(FileDerivativeRecord(
        source_file=source,
        derivative_file=thumbnail,
        variant=FileDerivativeVariant.THUMBNAIL,
    ))
    await db_session.commit()

    thumbnail_task = (await create_task(client, headers)).json()["task_id"]
    thumbnail_rejected = await client.post(
        f"/api/v1/reconstruction/start/{thumbnail_task}",
        headers=headers,
        json={"input_file_ids": [thumbnail_id]},
    )
    assert thumbnail_rejected.status_code == 400


def test_dash_gaussian_command_uses_server_conda_and_iterations(tmp_path):
    assert _normalize_dash_gaussian_params({}) == {"iterations": 30000}
    spec = _algorithm_specs()["dash_gaussian"]
    input_path = tmp_path / "images"
    output_dir = tmp_path / "output"
    command = _build_command(
        spec,
        "recon_test",
        input_path,
        input_path,
        output_dir,
        {"iterations": 1234},
    )
    assert spec.algorithm_path == "/data1/lzh/lzy/DashGaussian"
    assert command == [
        "/data1/lzh/anaconda3/bin/conda",
        "run",
        "-n",
        "DashGaussian",
        "python",
        "train_dash.py",
        "--input_path",
        str(input_path),
        "-m",
        str(output_dir),
        "--iterations",
        "1234",
        "--disable_viewer",
    ]


def test_dash_gaussian_cfg_args_paths_are_rewritten(tmp_path):
    cfg_path = tmp_path / "cfg_args"
    cfg_path.write_text(
        "Namespace(model_path='/old/model', source_path='/old/source', iterations=30000)\n",
        encoding="utf-8",
    )
    _rewrite_dash_gaussian_cfg_args(
        cfg_path,
        model_path="${MODEL_ROOT}",
        source_path="${SOURCE_PATH}",
    )
    rewritten = cfg_path.read_text(encoding="utf-8")
    assert "/old/model" not in rewritten
    assert "/old/source" not in rewritten
    assert "${MODEL_ROOT}" in rewritten
    assert "${SOURCE_PATH}" in rewritten


def test_dash_gaussian_restore_links_follow_selected_ply_generation():
    class Link:
        def __init__(self, file):
            self.file = file
            self.role = TaskFileRole.RESULT

    def file(public_id, filename, category, metainfo):
        return FileRecord(
            public_id=public_id,
            filename=filename,
            original_name=filename,
            category=category,
            mime_type="model/ply" if filename.endswith(".ply") else "text/plain",
            metainfo=metainfo,
            is_deleted=False,
        )

    gen_a = "dash_gaussian_a"
    gen_b = "dash_gaussian_b"
    links = [
        Link(file("file_ply_a", "point_cloud.ply", FileCategory.PLY_MODEL, {
            "generated_by": "dash_gaussian",
            "generation_id": gen_a,
            "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
            "primary_result": True,
        })),
        Link(file("file_cfg_a", "cfg_args", FileCategory.OTHER, {
            "generated_by": "dash_gaussian",
            "generation_id": gen_a,
            "relative_path": "cfg_args",
        })),
        Link(file("file_ply_b", "point_cloud.ply", FileCategory.PLY_MODEL, {
            "generated_by": "dash_gaussian",
            "generation_id": gen_b,
            "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
            "primary_result": True,
        })),
        Link(file("file_cfg_b", "cfg_args", FileCategory.OTHER, {
            "generated_by": "dash_gaussian",
            "generation_id": gen_b,
            "relative_path": "cfg_args",
        })),
    ]

    selected, error = _select_dash_gaussian_restore_links_for_ply(links, "file_ply_b")
    assert error is None
    assert {link.file.public_id for link in selected} == {"file_ply_b", "file_cfg_b"}

    _, invalid_error = _select_dash_gaussian_restore_links_for_ply(
        [
            Link(file("file_other", "point_cloud.ply", FileCategory.PLY_MODEL, {
                "generated_by": "anysplat",
                "relative_path": "point_cloud.ply",
                "primary_result": True,
            }))
        ],
        "file_other",
    )
    assert "not generated by DashGaussian" in invalid_error


@pytest.mark.asyncio
async def test_dash_gaussian_registers_precise_iteration_result(client, db_session, tmp_path):
    headers = await register(client, "dashresult")
    created = await create_task(client, headers, {"iterations": 1234})
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    output_dir = tmp_path / "output"
    result_path = output_dir / "point_cloud" / "iteration_1234" / "point_cloud.ply"
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(b"ply-result")
    (output_dir / "cfg_args").write_text(
        "Namespace(model_path='/private/model', source_path='/private/source')\n",
        encoding="utf-8",
    )

    error = await _register_dash_gaussian_results(db_session, task, output_dir)
    assert error is None
    task.status = TaskStatus.COMPLETED
    task.current_stage = "completed"
    await db_session.commit()

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.status_code == 200
    data = status.json()
    assert data["result_id"] == data["ply_id"]
    by_filename = {item["filename"]: item for item in data["result_files"]}
    assert by_filename["point_cloud.ply"]["category"] == FileCategory.PLY_MODEL.value
    assert by_filename["cfg_args"]["category"] == FileCategory.OTHER.value
    detail = await client.get(f"/api/v1/reconstruction/tasks/{created.json()['task_id']}", headers=headers)
    assert detail.status_code == 200
    results_by_filename = {item["filename"]: item for item in detail.json()["results"]}
    assert results_by_filename["point_cloud.ply"] == {
        "file_id": by_filename["point_cloud.ply"]["file_id"],
        "filename": "point_cloud.ply",
        "file_type": "model",
        "category": "render_model",
        "mime_type": "model/ply",
        "size_bytes": len(b"ply-result"),
    }
    assert results_by_filename["cfg_args"]["category"] == "render_model"
    assert results_by_filename["cfg_args"]["size_bytes"] > 0
    records = list((await db_session.execute(
        select(FileRecord).where(FileRecord.public_id.in_(
            [item["file_id"] for item in data["result_files"]]
        ))
    )).scalars())
    cfg_record = next(record for record in records if record.filename == "cfg_args")
    ply_record = next(record for record in records if record.category == FileCategory.PLY_MODEL)
    assert cfg_record.mime_type == "text/plain"
    assert cfg_record.metainfo["relative_path"] == "cfg_args"
    assert cfg_record.metainfo["generation_id"].startswith("dash_gaussian_")
    assert cfg_record.metainfo["generation_id"] == ply_record.metainfo["generation_id"]


@pytest.mark.asyncio
async def test_dash_gaussian_result_collection_reports_missing_iteration(client, db_session, tmp_path):
    headers = await register(client, "dashmissing")
    created = await create_task(client, headers, {"iterations": 1234})
    task = await TaskService.get_by_public_id(db_session, created.json()["task_id"])
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    error = await _register_dash_gaussian_results(db_session, task, output_dir)
    assert "point_cloud/iteration_1234/point_cloud.ply" in error.replace("\\", "/")

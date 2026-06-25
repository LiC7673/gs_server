# 3DGS 后端接口文档

逐接口请求参数、必填性、响应字段和重复功能审计见 [`api-reference.md`](./api-reference.md)。

## 1. 部署方式

正式环境使用 PostgreSQL、Redis、MinIO、Celery、Celery Beat、Flower 和独立 GPU worker。
业务文件不再使用本地目录存储。

本版本包含开发期重置迁移，启动前会清空旧测试数据：

```bash
docker compose down -v
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| API 文档 | `http://127.0.0.1:8000/docs` |
| 健康检查 | `http://127.0.0.1:8000/health` |
| MinIO 控制台 | `http://127.0.0.1:9001` |
| Flower 队列监控 | `http://127.0.0.1:5555` |

`migrate` 容器会先执行 `alembic upgrade head`。`minio-init` 容器会创建 `3dgs-files`
bucket。GPU worker 需要服务器安装 NVIDIA Container Toolkit。

算法宿主机路径可在启动前调整：

```bash
export ANYSPLAT_HOST_PATH=/data1/lzh/lzy/AnySplat
export DASH_GAUSSIAN_HOST_PATH=/data1/lzh/lzy/DashGaussian
export VGGT_OMEGA_HOST_PATH=/data1/lzh/lzy/vggt-omega
export HUNYUAN3D_HOST_PATH=/data1/lzh/lzy/hunyuan3d-2.1
export ANACONDA_HOST_PATH=/data1/lzh/anaconda3
```

## 2. 鉴权与权限

文件、上传和重建接口均需要：

```http
Authorization: Bearer <access_token>
```

| 资源 | 所有者 | 其他已登录用户 | 管理员 |
| --- | --- | --- | --- |
| 私有任务 | 读写 | 不可见 | 读写 |
| 已完成的公开任务 | 读写 | 只读 | 读写 |
| 输入图片、视频、日志 | 读写 | 不可见 | 读写 |
| 公开任务的结果和预览 | 读写 | 读取和下载 | 读写 |
| 环境诊断 | 不可见 | 不可见 | 读取 |

公开任务仍保留发布者。只有已完成任务可以改成 `public`。发现页展示所有用户发布且拥有成功
PLY 的公开任务；后续高斯或 Mesh 重跑、执行或失败时仍展示最近一次成功结果。

## 3. ID 与文件去重

- 文件 ID 是字符串，例如 `file_1d21...`。
- 任务 ID 是字符串，例如 `recon_8f42...`。
- 文件响应中的 `storage_key` 暂时保留，值与 `file_id` 相同，仅用于兼容旧前端。
- MinIO object key 和容器路径属于内部信息，普通接口不会返回。
- MinIO 去重范围是单个用户，唯一条件为 `(user_id, SHA-256, file_size)`。
- 不同用户上传完全相同的文件时，仍会生成互相隔离的对象。

## 4. 接口列表

### 4.1 账户

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册并返回 Bearer Token |
| POST | `/api/v1/auth/login` | 登录并返回 Bearer Token |
| GET | `/api/v1/auth/me` | 获取当前鉴权用户 |
| GET | `/api/v1/users/me` | 获取个人资料 |
| PUT | `/api/v1/users/me` | 更新个人资料 |
| PUT | `/api/v1/users/update_avatar` | 单独设置或清空用户头像 |
| GET | `/api/v1/users/me/usage` | 获取存储、任务和 GPU 用量 |
| PUT | `/api/v1/users/{user_id}/quota` | 管理员更新用户配额 |
| POST | `/api/v1/users/{user_id}/gpu-usage/reset` | 管理员重置用户当天 GPU 用量 |

头像复用上传和下载能力，不新增二进制上传接口：

1. 使用 `/api/v1/upload/*` 上传图片，合并后得到原图 `file_id`。
2. 调用 `PUT /api/v1/users/update_avatar`，Body 传 `{"avatar_file_id":"file_xxx"}`。
3. `GET /api/v1/users/me` 返回 `avatar_file_id` 和 `avatar_thumbnail_file_id`。
4. 头像缩略图仍用 `/api/v1/files/{avatar_thumbnail_file_id}/download/*` 下载；缩略图未生成时该字段为 `null`。

`GET/PUT /api/v1/users/me` 仅返回用户 ID、用户名、邮箱、昵称、管理员标识、头像 ID 和注册时间。
`PUT /api/v1/users/update_avatar` 仅返回头像原图 ID、头像缩略图 ID 和注册时间。存储、任务及 GPU 用量统一由 `/api/v1/users/me/usage` 返回。

配额语义：

- `task_quota` 表示活跃任务上限，只统计 `pending`、`queued`、`processing`。
- 已完成、失败、取消、`partial_completed` 和 `manual_review` 任务可长期保留，不占用活跃任务额度。
- `gpu_concurrency_quota` 表示同一用户可同时占用 GPU 的任务数；普通用户默认 `1`。
- `gpu_quota` 表示北京时间自然日内可使用的 GPU 秒数；跨天后自动重置。
- 存储配额限制主动上传和自动缩略图；算法结果允许超额保存，但超额后用户不能继续上传新文件。

### 4.2 上传与文件

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| POST | `/api/v1/upload/init` | 初始化断点续传 |
| PUT | `/api/v1/upload/{upload_id}/chunk?chunk_index=0` | 上传一个二进制分片 |
| GET | `/api/v1/upload/{upload_id}/progress` | 查询全部分片状态 |
| POST | `/api/v1/upload/{upload_id}/merge` | 校验并合并分片 |
| POST | `/api/v1/upload/{upload_id}/cancel` | 取消上传 |
| GET | `/api/v1/files` | 获取当前用户文件列表 |
| GET | `/api/v1/files/{file_id}` | 获取可访问文件详情 |
| DELETE | `/api/v1/files/{file_id}` | 删除文件并进入异步回收 |
| POST | `/api/v1/files/{file_id}/archive` | 归档本人文件 |
| POST | `/api/v1/files/{file_id}/media-processing/retry` | 重新生成图像缩略图或视频封面 |
| POST | `/api/v1/files/{file_id}/download/init` | 初始化分片下载 |
| GET | `/api/v1/files/{file_id}/download/chunk` | 下载一个分片 |
| GET | `/api/v1/files/downloads/{download_id}/progress` | 查询 Redis 中的下载进度 |
| POST | `/api/v1/files/downloads/{download_id}/complete` | 前端合并后确认完成 |

### 4.3 重建任务

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | `/api/v1/reconstruction/render/algorithm` | 获取渲染/高斯算法列表 |
| GET | `/api/v1/reconstruction/mesh/algorithms` | 获取 Mesh 算法列表，含每个 Mesh 方法的前置依赖 |
| POST | `/api/v1/reconstruction/tasks` | 创建私有任务 |
| GET | `/api/v1/reconstruction/tasks` | 获取当前用户任务列表 |
| GET | `/api/v1/reconstruction/tasks/{task_id}` | 获取可访问任务详情 |
| GET | `/api/v1/reconstruction/tasks/{task_id}/inputs` | 获取任务输入文件 ID 列表 |
| POST | `/api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/init` | 初始化任务 PLY 结果文件替换上传 |
| POST | `/api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/complete` | 校验并替换任务 PLY 结果文件 |
| PATCH | `/api/v1/reconstruction/tasks/{task_id}/visibility` | 修改完成任务的可见性 |
| DELETE | `/api/v1/reconstruction/tasks/{task_id}` | 删除任务关联，保留文件 |
| GET | `/api/v1/reconstruction/discover` | 获取公开作品列表 |
| POST | `/api/v1/reconstruction/start/{task_id}` | 启动或重跑高斯阶段 |
| POST | `/api/v1/reconstruction/mesh/start/{task_id}` | 选择 Mesh 算法并启动或重跑 Mesh 阶段 |
| GET | `/api/v1/reconstruction/status/{task_id}` | 轮询任务状态 |
| POST | `/api/v1/reconstruction/cancel/{task_id}` | 取消排队或执行中的任务 |
| GET | `/api/v1/reconstruction/logs/{task_id}` | 所有者或管理员查看错误日志 |
| GET | `/api/v1/reconstruction/diagnostics/{task_id}` | 管理员检查算法环境 |

旧 `/api/v1/tasks/*` 模块和 reconstruction 下载接口已经删除。

算法列表接口支持请求头 `X-App-Locale: zh-CN` 或 `X-App-Locale: en-US`，用于切换
`params` 中参数显示名和说明文案的中英文。未传或传入其他值时默认 `zh-CN`。

发现页推荐使用 `GET /api/v1/reconstruction/discover?page=1&page_size=10`。`page_size` 最大为 `10`；旧 `skip/limit` 仍短期兼容，但 `limit` 也不能超过 `10`。

## 5. Apifox 重建测试流程

### 5.1 登录

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "demo_user",
  "password": "demo_password"
}
```

后续请求均填写 `Authorization: Bearer <access_token>`。

### 5.2 创建唯一任务

```http
POST /api/v1/reconstruction/tasks

{
  "title": "demo reconstruction",
  "algorithm": "anysplat",
  "params": {
    "frame_nums": 4,
    "crop_quantile": 0.8
  }
}
```

保存返回的唯一 `task_id`。`algorithm/params` 配置高斯阶段。Mesh 参数在前端主动启动 Mesh
时提交；不要创建单独的 Mesh 任务。

初始阶段为：

```json
{"status": "pending", "current_stage": "task_created"}
```

### 5.3 上传任务数据

先计算完整文件的 SHA-256：

```http
POST /api/v1/upload/init

{
  "task_id": "recon_xxx",
  "filename": "image_001.png",
  "file_size": 123456,
  "mime_type": "image/png",
  "file_hash": "<64位 SHA-256>"
}
```

如果返回 `already_uploaded=true`，直接使用响应中的 `image_id`。否则继续上传每个分片：

```http
PUT /api/v1/upload/{upload_id}/chunk?chunk_index=0
Content-Type: application/octet-stream

<原始二进制内容>
```

合并分片：

```http
POST /api/v1/upload/{upload_id}/merge

{
  "expected_hash": "<64位 SHA-256>",
  "expected_size": 123456,
  "parts": [
    {"chunk_index": 0, "etag": "<分片 MD5>"}
  ]
}
```

上传初始化和合并响应都会返回同一个 `task_id`。合并后文件自动作为该任务的输入文件关联，
任务阶段为 `data_uploading`。视频走相同上传接口。

合并成功后，媒体元信息和缩略图由 CPU `media-worker` 异步生成。上传接口会立即返回原始文件 ID：

```json
{
  "file_id": "file_source",
  "media_processing_status": "pending",
  "thumbnail_id": null
}
```

轮询文件详情：

```http
GET /api/v1/files/{file_id}
```

处理完成后响应包含：

```json
{
  "file_type": "video",
  "metainfo": {
    "size_bytes": 123456,
    "width": 1920,
    "height": 1080,
    "fps": 29.97,
    "duration_seconds": 12.34
  },
  "media_processing_status": "completed",
  "thumbnail_id": "file_thumbnail"
}
```

缩略图仍使用 `/api/v1/files/{thumbnail_id}/download/*` 统一下载流程。媒体处理失败时，所有者可以调用：

```http
POST /api/v1/files/{file_id}/media-processing/retry
```

### 5.4 启动高斯阶段

```http
POST /api/v1/reconstruction/start/{task_id}

{}
```

上传时已绑定 `task_id`，所以启动请求可以使用空对象。后端会读取该任务已关联的输入文件。
兼容场景也可以显式传入 `input_file_ids`，但新前端应优先在上传时绑定任务。

`anysplat` 的 `params.frame_nums` 和 `params.crop_quantile` 均可省略，后端默认补为 `4` 和 `0.8`。

高斯状态链：

```text
task_created
→ data_uploading
→ gaussian_queued
→ gaussian_processing
→ gaussian_completed
```

`gaussian_completed` 后任务停止。需要 Mesh 时，前端从状态响应读取 `ply_id`，再主动调用：

前端可先调用 `GET /api/v1/reconstruction/mesh/algorithms` 读取 `dependencies`。其中
`dash_gaussian_mesh.dependencies.required_gaussian_algorithms=["dash_gaussian"]`，表示它只能在
`dash_gaussian` 高斯阶段成功后的同一任务上运行；其它高斯方法产出的 PLY 不能作为 dash mesh 的前置结果。

```http
POST /api/v1/reconstruction/mesh/start/{task_id}

{
  "algorithm": "dash_gaussian_mesh",
  "input_file_ids": ["file_ply"],
  "params": {"radius": 10, "voxel_size": 0.02}
}
```

Mesh 状态链为 `mesh_queued → mesh_processing → mesh_completed`。Mesh 失败时状态为
`partial_completed/mesh_failed`，已有 PLY 仍可下载。

Hunyuan3D 也通过同一个接口启动，但输入必须是该任务的原始图片、图片组或单视频：

```json
{
  "algorithm": "hunyuan3d",
  "input_file_ids": ["file_original_image"],
  "params": {}
}
```

首次高斯失败且没有旧 PLY 时为 `failed/gaussian_failed`；高斯重跑失败但已有旧 PLY 时为
`partial_completed/gaussian_failed`，旧 PLY 和旧 Mesh 继续保留。

### 5.5 轮询并下载结果

```http
GET /api/v1/reconstruction/status/{task_id}
```

持续轮询同一个 `task_id`。高斯完成为 `completed/gaussian_completed`；主动执行 Mesh 后，
成功为 `completed/mesh_completed`。`results` 保留当前 PLY 和各 Mesh 算法的成功结果，
每项包含 `file_id`、`filename`、`file_type`、`category`、`mime_type`、`size_bytes`。
`category` 只返回 `render_model` 和 `mesh_model`：3DGS 高斯阶段返回的 PLY 是
`render_model`，基于 PLY 继续生成的重建/Mesh 结果是 `mesh_model`。旧兼容字段
`result_files` 暂时保留。

```json
{
  "task_id": "recon_xxx",
  "status": "completed",
  "current_stage": "mesh_completed",
  "results": [
    {
      "file_id": "file_ply",
      "filename": "point_cloud.ply",
      "file_type": "model",
      "category": "render_model",
      "mime_type": "model/ply",
      "size_bytes": 123456
    },
    {
      "file_id": "file_mesh",
      "filename": "mesh.obj",
      "file_type": "model",
      "category": "mesh_model",
      "mime_type": "model/obj",
      "size_bytes": 654321
    }
  ]
}
```

Hunyuan3D 会把主 GLB 以及输出目录内的 OBJ、MTL、纹理、JSON 等有效文件分别登记为结果文件。`results`
中的这些 Mesh 阶段产物均返回 `category=mesh_model`。`result_id` 始终优先指向主 GLB，旧前端仍可按原方式下载主模型：

```json
{
  "status": "completed",
  "result_id": "file_glb",
  "results": [
    {
      "file_id": "file_glb",
      "category": "mesh_model",
      "file_type": "model",
      "mime_type": "model/gltf-binary",
      "filename": "hunyuan3d_result.glb",
      "size_bytes": 123456
    },
    {
      "file_id": "file_ply",
      "category": "render_model",
      "file_type": "model",
      "mime_type": "model/ply",
      "filename": "point_cloud.ply",
      "size_bytes": 234567
    },
    {
      "file_id": "file_obj",
      "category": "mesh_model",
      "file_type": "model",
      "mime_type": "model/obj",
      "filename": "mesh.obj",
      "size_bytes": 345678
    }
  ]
}
```

```http
POST /api/v1/files/{result_id}/download/init
GET  /api/v1/files/{result_id}/download/chunk?download_id=<download_id>&chunk_index=0
GET  /api/v1/files/downloads/{download_id}/progress
POST /api/v1/files/downloads/{download_id}/complete
```

前端按照 `chunk_index` 顺序在本地合并文件。

### 5.6 上传修改后的 PLY 并覆盖云端结果

任务重建完成后，用户可以下载 PLY 到本地修改。若用户选择上传云端，前端使用专用替换流程：

1. 初始化替换上传：

```http
POST /api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/init

{
  "filename": "point_cloud_modified.ply",
  "file_size": 123456,
  "chunk_size": 1048576,
  "mime_type": "model/ply",
  "file_hash": "<修改后文件的 SHA-256>"
}
```

响应返回 `upload_id`。其中 `{file_id}` 必须是该任务已关联的 PLY 结果文件 ID。

2. 继续复用现有分片上传接口：

```http
PUT /api/v1/upload/{upload_id}/chunk?chunk_index=0
Content-Type: application/octet-stream

<修改后的 PLY 分片>
```

3. 所有分片上传完成后，提交替换：

```http
POST /api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/complete?upload_id=<upload_id>

{
  "expected_hash": "<修改后文件的 SHA-256>",
  "expected_size": 123456,
  "parts": [
    {"chunk_index": 0, "etag": "<分片 MD5>"}
  ]
}
```

后端会先合并临时分片并校验大小、分片 MD5 和整体 SHA-256，校验成功后再把原结果文件
`file_id` 指向新的对象存储文件。`file_id` 保持不变，前端继续用原 `file_id` 下载即可得到修改后的 PLY。
旧对象会在没有其他文件引用时标记为异步清理。

## 6. 删除规则

- 删除活跃任务引用的输入文件时，系统会先请求取消任务，再解除关联。
- 删除公开任务的结果或预览时，任务会自动切回 `private`。
- 文件先软删除，Celery Beat 定时回收没有业务引用的 MinIO 对象。
- 删除任务只解除关联，用户上传文件和任务生成文件仍保留在文件库。
- 删除源图像或源视频时，其派生缩略图一并软删除；单独删除缩略图不影响源文件。

## 7. 队列恢复

- 重建任务启用 Celery late ack 和 worker 丢失重投递。
- 算法执行期间持续更新数据库 `heartbeat_at`。
- 入队期间使用 `dispatching` 标记防止重复派发。
- Celery Beat 每分钟检查异常任务并重新入队。
- 过期上传分片和孤立 MinIO 对象每小时清理一次。
- 媒体处理任务由独立 CPU `media-worker` 执行。Celery Beat 每分钟恢复长期等待或心跳过期的媒体任务。

## 8. 文件存储边界

- PostgreSQL 保存用户、文件元数据、任务、文件与任务的关联关系。
- MinIO 保存上传分片、用户原始文件、算法结果、预览和日志。
- Redis 保存 GPU 租约、Celery 队列状态和下载会话。
- API 的 `API_SCRATCH_PATH`、worker 的 `RECONSTRUCTION_SCRATCH_PATH` 和 `MEDIA_SCRATCH_PATH` 仅用于临时流式合并、算法输入 staging、媒体处理和输出回传。任务完成后会清理，不是业务文件库。
- 历史版本遗留的 `storage/` 目录不再作为数据源。确认旧测试数据无用后可手动归档或删除。

普通接口只返回稳定字符串 `file_id`。MinIO object key、容器挂载路径、Python 路径、算法目录和入口脚本不会写入任务实体，也不会通过普通接口公开。

## 9. GPU 动态调度

单机 8 卡 Docker 模式使用：

```env
WORKER_NODE_ID=gpu-node-01
# 可选；容器部署默认使用容器 hostname
WORKER_EXECUTOR_ID=gpu-worker-01
GPU_SCHEDULER_MODE=local
GPU_DEVICE_IDS=0,1,2,3,4,5,6,7
RECONSTRUCTION_WORKER_CONCURRENCY=8
```

worker 在真正开始执行前通过 `nvidia-smi` 检查显存占用，并通过 Redis 获取 GPU 租约。没有空闲卡时，任务保持：

```json
{
  "status": "queued",
  "current_stage": "gaussian_queued",
  "worker_node_id": null,
  "executor_id": null,
  "cuda_device": null
}
```

拿到 GPU 后，所有者和管理员查询任务状态时可以看到：

```json
{
  "status": "processing",
  "worker_node_id": "gpu-node-01",
  "executor_id": "container-or-pod-id",
  "cuda_device": "3",
  "execution_attempt": 1
}
```

`worker_node_id` 表示物理机器，`executor_id` 表示容器或 Pod，`cuda_device` 表示本次实际分配的显卡。进程 ID 仅保存在数据库内部用于恢复，不通过接口返回。

算法运行时会续租并更新心跳。任务取消、算法超时或租约丢失时，worker 会终止整个算法进程组；Linux 下算法主进程还会接收父进程死亡信号。心跳过期后，Celery Beat 会释放旧租约并重新入队，达到重试上限后任务改为 `failed` 并保留错误信息供检查。

## 10. K8s GPU Worker

[`deploy/k8s/gpu-worker.yaml`](../deploy/k8s/gpu-worker.yaml) 提供了一 Pod 一 GPU 的 worker 模板。Kubernetes 负责物理 GPU 分配，应用使用：

```env
GPU_SCHEDULER_MODE=k8s
GPU_DEVICE_IDS=0
RECONSTRUCTION_WORKER_CONCURRENCY=1
```

模板通过 Downward API 将 `spec.nodeName` 写入 `WORKER_NODE_ID`，将 Pod UID 写入 `WORKER_EXECUTOR_ID`。扩容 Deployment 后，不同节点和 Pod 会共同消费 `reconstruction` 队列，文件通过 MinIO 共享，不依赖宿主机业务文件目录。

## 11. 媒体文件与缩略图

- 原始图像使用 `file_type=image`、`category=multi_view_image`。
- 原始视频使用 `file_type=video`、`category=original_video`。
- 缩略图使用 `file_type=image`、`category=preview_image`，并通过 `source_file_id` 指回源文件。
- 图片和视频封面统一生成 JPEG 缩略图，保持比例，长边不超过 `512px`，质量为 `80`。
- 视频封面默认取第 1 秒，不足 1 秒时回退到首帧。
- `GET /api/v1/files` 默认隐藏派生文件。需要查看缩略图实体时传 `include_derivatives=true`。
- 自动生成的缩略图按实际 MinIO 对象大小计入用户存储用量。

支持的视频 MIME：

```text
video/mp4
video/quicktime
video/webm
video/x-msvideo
video/x-matroska
video/mpeg
video/x-m4v
video/3gpp
```

## 12. AnySplat

全算法真实联调脚本：

```bash
python scripts/test_all_reconstruction_algorithms.py \
  --base-url http://127.0.0.1:8888/api/v1 \
  --image-dir /data1/lzh/dhh/test_data1213 \
  --skip-download
```

该脚本会先跑 `anysplat`、`dash_gaussian`、`vggt_omega` 三个高斯算法，再选择一个成功生成 PLY 的任务继续跑
`dash_gaussian_mesh` 和 `hunyuan3d`，最后输出 `all_algorithm_report.json`。

AnySplat 使用服务器上的原始 Conda 环境和入口脚本：

```bash
/data1/lzh/anaconda3/envs/anysplat/bin/python \
  /data1/lzh/lzy/AnySplat/export_scene_gaussians.py \
  <视频路径或图像文件夹路径> \
  --frame_nums 4 \
  --output_folder <临时输出目录> \
  --crop_quantile 0.8
```

部署时 worker 会额外挂载原始路径，同时保留旧的 `/algorithms/AnySplat` 和 `/opt/conda` 别名挂载：

```text
/data1/lzh/lzy/AnySplat:/data1/lzh/lzy/AnySplat:ro
/data1/lzh/anaconda3:/data1/lzh/anaconda3:ro
```

视频冒烟测试：

```bash
python scripts/test_reconstruction_algorithms.py \
  --base-url http://127.0.0.1:8000/api/v1 \
  --algorithms anysplat \
  --anysplat-video /path/to/e3.mp4 \
  --params '{"frame_nums":4,"crop_quantile":0.8}'
```

## 13. DashGaussian

DashGaussian 复用现有上传、MinIO、GPU 队列、轮询、取消、日志和统一分片下载能力。服务器端 worker 使用宿主机原始 Conda 路径执行：

```bash
cd /data1/lzh/lzy/DashGaussian
/data1/lzh/anaconda3/bin/conda run -n DashGaussian \
  python train_dash.py \
  --input_path <视频路径或图像文件夹路径> \
  -m <临时输出目录> \
  --iterations 30000 \
  --disable_viewer
```

部署时 worker 挂载：

```text
/data1/lzh/lzy/DashGaussian:/data1/lzh/lzy/DashGaussian:ro
/data1/lzh/anaconda3:/data1/lzh/anaconda3:ro
```

完整调用顺序：

```text
上传原始图片或视频
→ POST /api/v1/reconstruction/tasks，algorithm=dash_gaussian
→ POST /api/v1/reconstruction/start/{task_id}
→ GET  /api/v1/reconstruction/status/{task_id}
→ POST /api/v1/files/{result_id}/download/init
→ GET  /api/v1/files/{result_id}/download/chunk
```

服务器端可以直接运行真实算法冒烟测试。`--dash-input` 可以填写视频文件或图片目录：

```bash
python scripts/test_reconstruction_algorithms.py \
  --base-url http://127.0.0.1:8000/api/v1 \
  --algorithms dash_gaussian \
  --dash-input /data1/lzh/lzy/test/e3.mp4 \
  --params '{"iterations":30000}'
```

worker 会精确读取 `<输出目录>/point_cloud/iteration_<iterations>/point_cloud.ply` 并登记为主 `ply_model`。同时会把 DashGaussian 输出目录中的 `cfg_args` 等有效模型支持文件登记到 `result_files`，并给同一次高斯输出写入相同的 `metainfo.generation_id`，用于后续 Mesh 阶段根据传入的 PLY 精确恢复对应的 `-m <保存高斯的文件夹路径>`。缺少主 PLY 时任务会标记为 `OUTPUT_FILE_NOT_FOUND`。

DashGaussian PLY→mesh 使用同一个 DashGaussian 环境，算法 ID 为 `dash_gaussian_mesh`。worker 会从 MinIO 下载输入 PLY，然后依次执行：

```bash
/data1/lzh/anaconda3/bin/conda run -n DashGaussian \
  python scripts/filter_gaussians_by_radius.py \
  -i <输入PLY> -o <半径过滤PLY> -r <radius，默认4>

/data1/lzh/anaconda3/bin/conda run -n DashGaussian \
  python scripts/filter_gaussians_by_cluster.py \
  -i <半径过滤PLY> -o <聚类过滤PLY> -v 0.05 --keep largest

/data1/lzh/anaconda3/bin/conda run -n DashGaussian \
  python scripts/render_depth_tsdf_mesh.py \
  -m <标准模型根目录> \
  --iteration 30000 \
  --views train \
  --voxel_size 0.02 \
  --sdf_trunc 0.36 \
  --alpha_threshold 0.35 \
  --max_depth 25 \
  --depth_quantile 0.9 \
  --mask_erode 2 \
  --output <临时Mesh输出目录>/dash_gaussian_mesh.obj
```

后端会先读取前端传入的 PLY `file_id`，通过该文件的 `metainfo.generation_id` 找到同一轮 DashGaussian 输出中的 `cfg_args` 和模型支持文件，恢复完整模型目录，重写 `cfg_args` 中的临时路径，再把第二步过滤结果放到 `<模型根目录>/point_cloud/iteration_<iteration>/point_cloud.ply`。第三步的 `--output` 会自动补成 OBJ 文件路径。Mesh 输出目录中的 OBJ、MTL、贴图、JSON 等有效文件都会登记到 `result_files`，前端从 `result_files` 读取对应 `file_id`，再使用 `/api/v1/files/{file_id}/download/*` 下载。

真实高斯 + 手动 Mesh 冒烟测试：

```bash
python scripts/test_reconstruction_algorithms.py \
  --base-url http://127.0.0.1:8000/api/v1 \
  --algorithms dash_gaussian \
  --dash-input /data1/lzh/lzy/test/e3.mp4 \
  --params '{"iterations":30000}' \
  --run-mesh \
  --mesh-params '{"radius":10,"iteration":30000}'
```

## 14. Hunyuan3D-2.1

Hunyuan3D 复用现有上传、MinIO、GPU 队列、轮询、取消、日志和统一分片下载能力。服务器端 worker 使用宿主机原始 Conda 路径执行：

```bash
cd /data1/lzh/lzy/hunyuan3d-2.1
/data1/lzh/anaconda3/bin/conda run -n hunyuan3d \
  python example.py <图片路径、图片目录或视频路径> \
  -o <临时输出目录>/hunyuan3d_result.glb
```

部署时 worker 同时保留 `/opt/conda` 挂载和下面两个只读原路径挂载，避免影响已有算法：

```text
/data1/lzh/lzy/hunyuan3d-2.1:/data1/lzh/lzy/hunyuan3d-2.1:ro
/data1/lzh/anaconda3:/data1/lzh/anaconda3:ro
```

完整调用顺序：

```text
创建并完成一个高斯任务，取得成功 PLY
→ POST /api/v1/reconstruction/mesh/start/{task_id}
→ Body: {"algorithm":"hunyuan3d","input_file_ids":["该任务原始输入 file_id"],"params":{}}
→ GET  /api/v1/reconstruction/status/{task_id}
→ POST /api/v1/files/{result_id}/download/init
→ GET  /api/v1/files/{result_id}/download/chunk
```

服务器端可以直接运行冒烟脚本验证高斯后接 Hunyuan3D Mesh 的完整链路：

```bash
python scripts/test_reconstruction_algorithms.py \
  --base-url http://127.0.0.1:8000/api/v1 \
  --algorithms dash_gaussian \
  --dash-input /data1/lzh/lzy/test/e3.mp4 \
  --run-mesh \
  --mesh-algorithm hunyuan3d
```

worker 要求生成 `<临时输出目录>/hunyuan3d_result.glb`，并将输出目录中的 GLB、OBJ、PLY、MTL、纹理、JSON、ZIP 等有效文件分别登记到 `result_files`。每个文件都可通过统一下载接口下载；缺少主 GLB 时任务会标记为 `OUTPUT_FILE_NOT_FOUND`。

## 15. 全接口冒烟测试

服务器启动完成后，可以运行轻量级全接口测试。脚本会注册临时用户、执行分片上传下载、文件管理、任务入队、状态轮询、取消和日志检查，并验证普通用户无法访问管理员接口。测试任务入队后会立即取消，不会等待真实 GPU 算法完成。

```bash
cd /data1/lzh/dhh/3dgs-backend
python scripts/test_all_api_endpoints.py \
  --base-url http://127.0.0.1:8000 \
  --report-file api-smoke-report.json
```

如需同时执行管理员接口的正向测试，传入管理员 Token：

```bash
python scripts/test_all_api_endpoints.py \
  --base-url http://127.0.0.1:8000 \
  --admin-token "<admin access token>" \
  --report-file api-smoke-report.json
```

脚本默认清理测试任务和图片。排查问题时可追加 `--keep-resources` 保留现场。真实算法运行仍使用 `scripts/test_reconstruction_algorithms.py`。

## 当前任务模型：唯一 task 手动多阶段

任务不再区分业务子类型，也不再使用父子任务查询。一个 `task_id` 代表一次完整的高斯 + Mesh 工作流。

创建示例：

```json
{
  "title": "single workflow",
  "algorithm": "dash_gaussian",
  "params": {"iterations": 30000}
}
```

上传初始化传入该 `task_id`。全部文件合并后启动高斯：

```http
POST /api/v1/reconstruction/start/{task_id}

{}
```

高斯完成后停在 `completed/gaussian_completed`。需要 Mesh 时调用
`POST /api/v1/reconstruction/mesh/start/{task_id}`，显式提交 Mesh `algorithm` 和 `input_file_ids`。
完成后仍查询同一个 `task_id`。
`results` 会同时包含 PLY、Dash OBJ、Hunyuan GLB 及其附属输出等成功结果，前端按 `category`、`file_type`、`mime_type`
选择文件，再走 `/api/v1/files/{file_id}/download/*` 下载。


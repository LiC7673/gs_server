# 3DGS 后端完整接口参考

本文档根据当前后端代码逐项整理，适用于 Apifox、Flutter 前端和服务器联调。

- API 基础地址：`http://<服务器IP>:8000`
- 业务 API 前缀：`/api/v1`
- 自定义接口数量：`39`
- Swagger：`GET /docs`
- ReDoc：`GET /redoc`
- OpenAPI JSON：`GET /openapi.json`
- 除注册、登录和健康检查外，业务接口默认都需要 Bearer Token。

## 1. 通用约定

### 1.1 Bearer 鉴权

需要鉴权的接口必须发送请求头：

```http
Authorization: Bearer <access_token>
```

注意：

- 请求头名称是 `Authorization`。
- 参数值必须包含 `Bearer ` 前缀和一个空格。
- 不要把 Token 放进 Body 或 Params。
- 当服务器配置 `MOCK_AUTH_ENABLED=true` 时，后端会临时跳过 Token 校验并使用管理员测试账号。正式环境必须关闭。

### 1.2 必填标记

| 标记 | 含义 |
| --- | --- |
| 是 | 请求必须提供 |
| 否 | 可以省略；后端使用默认值或空值 |
| 条件必填 | 取决于当前调用场景 |

### 1.3 通用错误响应

普通业务错误：

```json
{
  "detail": "Error message"
}
```

请求校验失败时返回 HTTP `422`：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "field_name"],
      "msg": "Value error, detailed message",
      "input": "invalid value"
    }
  ]
}
```

常见 HTTP 状态码：

| HTTP 状态码 | 含义 |
| --- | --- |
| `200` | 请求成功 |
| `206` | 下载分片成功 |
| `400` | 请求逻辑错误，例如分片编号错误或输入文件组合不合法 |
| `401` | 未携带 Bearer Token、Token 无效或 Token 已过期 |
| `403` | 当前用户不是管理员 |
| `404` | 资源不存在，或资源不属于当前用户 |
| `409` | 状态冲突，例如 Hash 不匹配、分片不完整、任务状态不允许修改 |
| `410` | 上传会话已过期 |
| `413` | 文件超过服务端允许的最大体积 |
| `422` | Pydantic 参数校验失败 |
| `429` | 用户存储空间、活跃任务数或 GPU 每日配额不足 |
| `503` | 队列、GPU 检查或算法配置不可用 |

### 1.4 Apifox 空值注意事项

可选 Query 参数不使用时，应从 Apifox Params 表格中删除或取消勾选，不要发送空字符串。

错误示例：

```text
GET /api/v1/files?category=&skip=&limit=
```

正确示例：

```text
GET /api/v1/files
```

### 1.5 字符串 ID

| ID 类型 | 示例 |
| --- | --- |
| 文件 ID | `file_faf66c984f79409695a66d14902c81c1` |
| 重建任务 ID | `recon_54b9c4a481f246da820f06edbac4d3df` |
| 上传会话 ID | `31ef7f47-4635-4e66-bff8-8b43c7805055` |
| 下载会话 ID | `6b0a5f16-4d75-41f2-9f74-1c6b826d72f0` |

外部接口中的文件 ID 和重建任务 ID 都是字符串。数据库内部自增整数不会作为文件库接口参数使用。

## 2. 枚举

### 2.1 文件业务分类 `category`

| 值 | 含义 |
| --- | --- |
| `original_video` | 用户上传的原始视频 |
| `multi_view_image` | 用户上传的原始图片 |
| `intermediate_frame` | 中间帧 |
| `ply_model` | PLY 模型 |
| `splat_model` | Splat 模型 |
| `glb_model` | GLB 模型 |
| `preview_image` | 缩略图或预览图 |
| `preview_video` | 预览视频 |
| `log_file` | 日志文件 |
| `other` | ZIP、JSON 或其他文件 |

### 2.2 文件大类 `file_type`

| 值 | 含义 |
| --- | --- |
| `image` | 图片 |
| `video` | 视频 |
| `model` | 模型 |
| `other` | 其他文件 |

### 2.3 媒体处理状态 `media_processing_status`

| 值 | 含义 |
| --- | --- |
| `pending` | 等待生成元信息和缩略图 |
| `processing` | 正在处理 |
| `completed` | 处理完成 |
| `failed` | 处理失败；原文件仍可使用 |
| `skipped` | 非图片或视频，无需处理 |

### 2.4 上传状态 `upload.status`

| 值 | 含义 |
| --- | --- |
| `initiated` | 已初始化，尚未上传分片 |
| `uploading` | 正在上传 |
| `completed` | 已合并完成 |
| `cancelled` | 已取消 |
| `expired` | 上传会话已过期 |

### 2.5 重建任务状态 `task.status`

| 值 | 含义 |
| --- | --- |
| `pending` | 已创建任务，尚未绑定输入 |
| `queued` | 已进入 Celery 队列或正在等待空闲 GPU |
| `processing` | 算法正在执行 |
| `completed` | 算法执行完成 |
| `failed` | 执行失败 |
| `cancelled` | 已取消 |
| `manual_review` | 需要人工检查 |

### 2.6 任务可见性 `visibility`

| 值 | 含义 |
| --- | --- |
| `private` | 仅任务所有者和管理员可见 |
| `public` | 已登录用户可读取已完成任务的结果与预览 |

## 3. 公共响应模型

### 3.1 `UserResponse`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 数据库用户 ID |
| `username` | string | 登录名 |
| `email` | string | 邮箱 |
| `nickname` | string | 昵称 |
| `is_active` | boolean | 账号是否启用 |
| `is_admin` | boolean | 是否管理员 |
| `storage_used` | integer | 已使用存储字节数 |
| `storage_quota` | integer | 存储配额字节数 |
| `task_count` | integer | 兼容字段；建议使用 `/users/me/usage` 获取实时活跃任务数 |
| `task_quota` | integer | 活跃任务上限 |
| `gpu_seconds_used` | integer | 当天已使用 GPU 秒数 |
| `gpu_quota` | integer | 每日 GPU 秒数配额 |
| `gpu_concurrency_quota` | integer | 用户 GPU 并发上限 |
| `avatar_file_id` | string \| null | 当前头像原图文件 ID |
| `avatar_thumbnail_file_id` | string \| null | 当前头像缩略图文件 ID；媒体处理未完成时为 `null` |
| `created_at` | datetime | 创建时间 |

`UserResponse` 仅用于注册、登录、`/auth/me` 和管理员配额接口。

#### 3.1.1 `UserProfileResponse`

用于 `GET/PUT /api/v1/users/me`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 用户 ID |
| `username` | string | 登录名 |
| `email` | string | 邮箱 |
| `nickname` | string | 昵称 |
| `is_admin` | boolean | 是否管理员 |
| `avatar_file_id` | string \| null | 当前头像原图文件 ID |
| `avatar_thumbnail_file_id` | string \| null | 当前头像缩略图文件 ID |
| `created_at` | datetime | 用户注册时间 |

#### 3.1.2 `AvatarResponse`

用于 `PUT /api/v1/users/update_avatar`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `avatar_file_id` | string \| null | 当前头像原图文件 ID |
| `avatar_thumbnail_file_id` | string \| null | 当前头像缩略图文件 ID |
| `created_at` | datetime | 用户注册时间 |

### 3.2 `FileResponse`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 文件 ID，例如 `file_xxx` |
| `user_id` | integer | 文件所有者 |
| `task_id` | string \| null | 当前固定返回 `null`，预留字段 |
| `filename` | string | 文件库名称 |
| `original_name` | string | 原始名称 |
| `category` | enum | 业务分类 |
| `file_type` | enum | 文件大类 |
| `mime_type` | string | MIME 类型 |
| `file_size` | integer | 字节数 |
| `file_hash` | string | SHA-256 |
| `metainfo` | object | 尺寸、帧率和时长等可扩展元信息 |
| `media_processing_status` | enum | 媒体处理状态 |
| `media_processing_error_code` | string \| null | 处理错误码；仅所有者和管理员可见 |
| `media_processing_error` | string \| null | 处理错误；仅所有者和管理员可见 |
| `source_file_id` | string \| null | 派生文件对应的源文件 |
| `derivative_type` | string \| null | 当前可能为 `thumbnail`、`compressed`、`preview_video` |
| `thumbnail_id` | string \| null | 缩略图文件 ID |
| `derivatives` | array | 派生文件列表 |
| `storage_key` | string | 兼容字段，当前值等于 `id`，不是 MinIO object key |
| `is_archived` | boolean | 是否归档 |
| `download_count` | integer | 已下载分片次数 |
| `created_at` | datetime | 创建时间 |

示例：

```json
{
  "id": "file_source",
  "user_id": 1,
  "task_id": null,
  "filename": "input.mp4",
  "original_name": "input.mp4",
  "category": "original_video",
  "file_type": "video",
  "mime_type": "video/mp4",
  "file_size": 10485760,
  "file_hash": "64位SHA-256",
  "metainfo": {
    "size_bytes": 10485760,
    "width": 1920,
    "height": 1080,
    "duration_seconds": 12.34,
    "fps": 29.97
  },
  "media_processing_status": "completed",
  "media_processing_error_code": null,
  "media_processing_error": null,
  "source_file_id": null,
  "derivative_type": null,
  "thumbnail_id": "file_thumbnail",
  "derivatives": [
    {
      "type": "thumbnail",
      "file_id": "file_thumbnail"
    }
  ],
  "storage_key": "file_source",
  "is_archived": false,
  "download_count": 0,
  "created_at": "2026-06-02T10:00:00Z"
}
```

### 3.3 `ReconstructionStatusResponse`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 重建任务 ID |
| `user_id` | integer | 任务所有者 |
| `title` | string | 标题 |
| `algorithm` | string | 当前或最近执行阶段的算法标识；进入 Mesh 阶段后为 `dash_gaussian_mesh` |
| `params` | object | 当前或最近执行阶段的参数 |
| `gaussian_algorithm` | string | 当前任务配置的高斯算法 |
| `gaussian_params` | object | 高斯阶段参数 |
| `mesh_algorithm` | string \| null | 最近一次启动的 Mesh 算法；尚未启动时为 `null` |
| `mesh_params` | object | 最近一次 Mesh 启动参数；创建任务时不返回 |
| `visibility` | enum | `private` 或 `public` |
| `status` | enum | 任务状态 |
| `status_code` | integer | 任务内部状态码，不是当前 HTTP 响应码 |
| `current_stage` | string | 当前工作流阶段，例如 `data_uploading`、`gaussian_processing`、`mesh_processing` |
| `progress` | number | 进度百分比 |
| `queue_reason` | string \| null | 排队原因：`gpu_capacity` 表示全局 GPU 满，`user_gpu_concurrency` 表示用户并发满 |
| `input_kind` | string | 当前执行阶段输入类型；Dash Mesh 为 `ply_model`，Hunyuan3D Mesh 为图片或视频 |
| `input_file_ids` | array[string] | 当前执行阶段显式选择的文件，仅所有者和管理员可见 |
| `result_id` | string \| null | 主结果文件 ID；Hunyuan3D 优先返回 GLB |
| `result_file_id` | string \| null | 兼容字段，当前等于 `result_id` |
| `result_storage_key` | string \| null | 兼容字段，当前等于 `result_id` |
| `ply_id` | string \| null | PLY 结果兼容字段 |
| `results` | array | 新结果文件数组；每项包含 `file_id`、`filename`、`file_type`、`category`、`mime_type`、`size_bytes`。`category` 只返回 `render_model` 或 `mesh_model`；3DGS PLY 是 `render_model`，基于 PLY 的 Mesh/重建结果是 `mesh_model` |
| `result_files` | array | 旧兼容结果文件数组；保留数据库原始分类，例如 `ply_model`、`mesh_model`、`glb_model` |
| `preview_ids` | array[string] | 预览文件 ID |
| `error_code` | string \| null | 错误码；仅所有者和管理员可见 |
| `error_status_code` | integer \| null | 错误状态码；仅所有者和管理员可见 |
| `error` | string \| null | 错误摘要；仅所有者和管理员可见 |
| `worker_node_id` | string \| null | 物理节点；仅所有者和管理员可见 |
| `executor_id` | string \| null | 容器或 Pod；仅所有者和管理员可见 |
| `cuda_device` | string \| null | 动态分配的 GPU；仅所有者和管理员可见 |
| `execution_attempt` | integer | 执行次数；仅所有者和管理员返回真实值 |
| `gpu_seconds_cost` | integer | 该任务累计 GPU 秒数 |
| `gpu_quota_exceeded` | boolean | 是否因每日 GPU 配额耗尽而失败 |
| `cancel_requested` | boolean | 是否请求取消 |
| `created_at` | datetime string | 创建时间 |
| `started_at` | datetime string \| null | 开始时间 |
| `updated_at` | datetime string \| null | 更新时间 |
| `completed_at` | datetime string \| null | 完成时间 |

Hunyuan3D 完成示例：

```json
{
  "task_id": "recon_xxx",
  "user_id": 1,
  "title": "demo",
  "algorithm": "hunyuan3d",
  "params": {},
  "visibility": "private",
  "status": "completed",
  "status_code": 200,
  "current_stage": "mesh_completed",
  "progress": 100,
  "input_kind": "image",
  "input_file_ids": ["file_input"],
  "result_id": "file_glb",
  "result_file_id": "file_glb",
  "result_storage_key": "file_glb",
  "ply_id": "file_ply",
  "results": [
    {
      "file_id": "file_glb",
      "filename": "hunyuan3d_result.glb",
      "file_type": "model",
      "category": "mesh_model",
      "mime_type": "model/gltf-binary",
      "size_bytes": 123456
    },
    {
      "file_id": "file_ply",
      "filename": "point_cloud.ply",
      "file_type": "model",
      "category": "render_model",
      "mime_type": "model/ply",
      "size_bytes": 234567
    },
    {
      "file_id": "file_obj",
      "filename": "mesh.obj",
      "file_type": "model",
      "category": "mesh_model",
      "mime_type": "model/obj",
      "size_bytes": 345678
    }
  ],
  "preview_ids": [],
  "error_code": null,
  "error_status_code": null,
  "error": null,
  "worker_node_id": "gpu-node-01",
  "executor_id": "worker-container",
  "cuda_device": "1",
  "execution_attempt": 1,
  "cancel_requested": false,
  "created_at": "2026-06-02T10:00:00Z",
  "started_at": "2026-06-02T10:00:03Z",
  "updated_at": "2026-06-02T10:10:00Z",
  "completed_at": "2026-06-02T10:10:00Z"
}
```

## 4. 系统接口

### 4.0 FastAPI 自动文档

以下接口由 FastAPI 自动生成，不计入 `38` 个自定义接口：

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/docs` | 不需要 | Swagger UI |
| GET | `/redoc` | 不需要 | ReDoc 文档 |
| GET | `/openapi.json` | 不需要 | OpenAPI JSON，可导入 Apifox |

### 4.1 健康检查

```http
GET /health
```

- 鉴权：不需要
- Path 参数：无
- Query 参数：无
- Body：无

响应：

```json
{
  "status": "ok",
  "app": "3DGS Reconstruction Service"
}
```

## 5. 注册、登录与用户

### 5.1 注册

```http
POST /api/v1/auth/register
Content-Type: application/json
```

- 鉴权：不需要

Body：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `username` | string | 是 | 长度 `3-64`；仅允许字母、数字和下划线 | 登录名，不支持中文 |
| `email` | string | 是 | 长度 `3-128`；必须包含 `@`，域名部分必须包含 `.` | 邮箱 |
| `password` | string | 是 | 长度 `6-128` | 密码 |

请求示例：

```json
{
  "username": "wengzhonghai",
  "email": "wengzhonghai@example.com",
  "password": "Test123456_"
}
```

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `access_token` | string | JWT Token |
| `token_type` | string | 固定为 `bearer` |
| `expires_in` | integer | Token 有效期，单位秒 |
| `user` | `UserResponse` | 当前用户资料 |

### 5.2 登录

```http
POST /api/v1/auth/login
Content-Type: application/json
```

- 鉴权：不需要

Body：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `username` | string | 是 | 长度 `1-128` | 登录名 |
| `password` | string | 是 | 长度 `1-128` | 密码 |

响应：与注册接口相同，返回 Token 和 `UserResponse`。

### 5.3 获取当前鉴权用户

```http
GET /api/v1/auth/me
```

- 鉴权：需要 Bearer Token
- Path 参数：无
- Query 参数：无
- Body：无
- 响应：`UserResponse`，包含账号状态和用量等完整鉴权用户信息。

### 5.4 获取个人资料

```http
GET /api/v1/users/me
```

- 鉴权：需要 Bearer Token
- Path 参数：无
- Query 参数：无
- Body：无
- 响应：精简的 `UserProfileResponse`

### 5.5 更新个人资料

```http
PUT /api/v1/users/me
Content-Type: application/json
```

- 鉴权：需要 Bearer Token

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `nickname` | string \| null | 否 | 昵称，可以使用中文 |
| `email` | string \| null | 否 | 邮箱 |

请求示例：

```json
{
  "nickname": "瓮中海"
}
```

响应：精简的 `UserProfileResponse`。

该接口不接受 `avatar_file_id`；头像必须通过 `/api/v1/users/update_avatar` 单独更新。

### 5.6 更新用户头像

```http
PUT /api/v1/users/update_avatar
Content-Type: application/json
```

- 鉴权：需要 Bearer Token

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `avatar_file_id` | string \| null | 是 | 已上传的原始图片 `file_id`；传 `null` 清空头像 |

设置头像：

```json
{
  "avatar_file_id": "file_avatar"
}
```

清空头像：

```json
{
  "avatar_file_id": null
}
```

响应：`AvatarResponse`，仅返回头像原图 ID、头像缩略图 ID和用户注册时间。

头像设置复用现有上传接口：

1. 先用 `/api/v1/upload/init`、`PUT /api/v1/upload/{upload_id}/chunk`、`POST /api/v1/upload/{upload_id}/merge` 上传图片。
2. 将合并响应中的 `file_id` 作为 `avatar_file_id` 传给 `PUT /api/v1/users/update_avatar`。
3. `GET /api/v1/users/me` 返回 `avatar_file_id` 和 `avatar_thumbnail_file_id`。
4. 缩略图下载仍走 `/api/v1/files/{avatar_thumbnail_file_id}/download/*`；如果媒体 worker 尚未生成缩略图，`avatar_thumbnail_file_id` 为 `null`。

校验规则：

- `avatar_file_id` 必须属于当前用户。
- 只能使用源图片文件，不能使用缩略图等派生文件。
- 视频、模型、JSON 等非图片文件会返回 `400`。

### 5.7 获取用户用量

```http
GET /api/v1/users/me/usage
```

- 鉴权：需要 Bearer Token
- Path 参数：无
- Query 参数：无
- Body：无

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `storage_used` | integer | 已占用 MinIO 唯一对象字节数 |
| `storage_quota` | integer | 存储字节配额 |
| `task_count` | integer | 当前活跃任务数量，仅统计 `pending`、`queued`、`processing` |
| `task_quota` | integer | 活跃任务上限 |
| `total_task_count` | integer | 未删除历史任务总数 |
| `gpu_running_count` | integer | 当前正在计费执行的 GPU 任务数量 |
| `gpu_concurrency_quota` | integer | 用户 GPU 并发上限；普通用户默认 `1` |
| `gpu_seconds_used` | integer | 北京时间当天已使用 GPU 秒数 |
| `gpu_quota` | integer | 每日 GPU 秒数配额 |
| `gpu_quota_exceeded` | boolean | 当天 GPU 配额是否已经耗尽 |
| `gpu_quota_resets_at` | string | 下一次北京时间自然日重置时间 |

说明：

- 已完成、失败、取消、`partial_completed` 和 `manual_review` 任务不占用 `task_quota`。
- `gpu_quota` 在北京时间每天 `00:00` 重置；Beat 未运行时，接口查询和调度前也会懒重置。
- 算法结果允许超过存储配额保存，但超额后主动上传和自动缩略图会被拒绝。

### 5.8 管理员更新用户配额

```http
PUT /api/v1/users/{user_id}/quota
Content-Type: application/json
```

- 鉴权：需要管理员 Bearer Token

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | integer | 是 | 数据库用户 ID |

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `storage_quota` | integer \| null | 否 | 存储字节上限 |
| `task_quota` | integer \| null | 否 | 活跃任务上限 |
| `gpu_quota` | integer \| null | 否 | 每日 GPU 秒数上限 |
| `gpu_concurrency_quota` | integer \| null | 否 | GPU 并发上限；`0` 表示禁止 GPU 执行 |

响应：更新后的 `UserResponse`。

### 5.9 管理员重置用户当天 GPU 用量

```http
POST /api/v1/users/{user_id}/gpu-usage/reset
```

- 鉴权：需要管理员 Bearer Token
- Body：无

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | integer | 用户 ID |
| `gpu_seconds_used` | integer | 重置后为 `0` |
| `gpu_quota` | integer | 每日 GPU 秒数配额 |
| `gpu_quota_resets_at` | string | 下一次重置时间 |

## 6. 分片上传

### 6.1 允许上传的 MIME

```text
video/mp4
video/quicktime
video/mov
video/webm
video/x-msvideo
video/x-matroska
video/mpeg
video/x-m4v
video/3gpp
image/jpeg
image/jpg
image/png
model/ply
application/zip
application/json
other/zip
other/json
```

### 6.2 初始化上传

```http
POST /api/v1/upload/init
Authorization: Bearer <token>
Content-Type: application/json
```

Body：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `task_id` | string \| null | 否 | 必须是自己的待启动任务 | 绑定上传到唯一重建任务；推荐新前端传入 |
| `filename` | string | 是 | - | 文件名 |
| `file_size` | integer | 是 | `> 0` | 完整文件字节数 |
| `chunk_size` | integer \| null | 否 | `> 0` | 每个分片字节数；省略时使用服务端默认值 |
| `mime_type` | string | 是 | 必须在允许列表中 | 文件类型 |
| `file_hash` | string | 是 | 64 位小写或可转小写十六进制 SHA-256 | 完整文件 Hash |

请求示例：

```json
{
  "task_id": "recon_xxx",
  "filename": "input_video.mp4",
  "file_size": 10485760,
  "mime_type": "video/mp4",
  "file_hash": "64位SHA-256"
}
```

首次上传响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string \| null | 绑定的重建任务 ID |
| `upload_id` | string \| null | 上传会话 ID |
| `chunk_size` | integer | 服务端最终采用的分片大小 |
| `total_chunks` | integer | 分片数量 |
| `expires_at` | datetime \| null | 上传会话过期时间 |
| `already_uploaded` | boolean | 是否已经存在相同文件 |
| `file_id` | string \| null | 已存在文件 ID；首次上传时为空 |
| `image_id` | string \| null | 图片兼容字段；图片文件等于 `file_id` |
| `file_hash` | string \| null | 已存在文件 SHA-256 |
| `storage_key` | string \| null | 兼容字段；已存在文件时等于 `file_id` |
| `media_processing_status` | enum \| null | 已存在媒体文件的处理状态 |
| `thumbnail_id` | string \| null | 已存在媒体文件的缩略图 |

首次上传响应示例：

```json
{
  "task_id": "recon_xxx",
  "upload_id": "upload-uuid",
  "chunk_size": 5242880,
  "total_chunks": 2,
  "expires_at": "2026-06-03T10:00:00Z",
  "already_uploaded": false,
  "file_id": null,
  "image_id": null,
  "file_hash": null,
  "storage_key": null,
  "media_processing_status": null,
  "thumbnail_id": null
}
```

同一用户重复上传相同 SHA-256 和大小的文件时，不报错，直接返回：

```json
{
  "upload_id": "completed-upload-uuid",
  "chunk_size": 5242880,
  "total_chunks": 0,
  "expires_at": "2026-06-03T10:00:00Z",
  "already_uploaded": true,
  "file_id": "file_existing",
  "image_id": "file_existing",
  "file_hash": "64位SHA-256",
  "storage_key": "file_existing",
  "media_processing_status": "completed",
  "thumbnail_id": "file_thumbnail"
}
```

> **兼容字段标记：** 图片响应中的 `image_id` 与 `file_id` 相同。新前端统一使用 `file_id` 即可。

### 6.3 上传一个分片

```http
PUT /api/v1/upload/{upload_id}/chunk?chunk_index=0
Authorization: Bearer <token>
Content-Type: application/octet-stream
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `upload_id` | string | 是 | 初始化上传时返回的上传会话 ID |

Query 参数：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `chunk_index` | integer | 是 | `>= 0` | 从 `0` 开始的分片编号 |

Body：

| 类型 | 必填 | 说明 |
| --- | --- | --- |
| binary | 是 | 当前分片原始二进制内容，不使用 multipart |

响应：

```json
{
  "received": true,
  "chunk_index": 0,
  "etag": "32位分片MD5"
}
```

### 6.4 查询上传进度

```http
GET /api/v1/upload/{upload_id}/progress
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `upload_id` | string | 是 | 上传会话 ID |

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string \| null | 上传绑定的重建任务 ID |
| `upload_id` | string | 上传会话 ID |
| `filename` | string | 文件名 |
| `file_size` | integer | 完整文件字节数 |
| `total_chunks` | integer | 总分片数 |
| `received_chunks` | integer | 已上传分片数 |
| `status` | enum | 上传状态 |
| `chunk_statuses` | array[integer] | 每个分片状态 |

`chunk_statuses` 的含义：

| 数字 | 含义 |
| --- | --- |
| `0` | 未上传或需要重新上传 |
| `2` | 已上传 |

响应示例：

```json
{
  "task_id": "recon_xxx",
  "upload_id": "upload-uuid",
  "filename": "input.mp4",
  "file_size": 10485760,
  "total_chunks": 5,
  "received_chunks": 3,
  "status": "uploading",
  "chunk_statuses": [2, 2, 0, 2, 0]
}
```

### 6.5 合并分片

```http
POST /api/v1/upload/{upload_id}/merge
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `upload_id` | string | 是 | 上传会话 ID |

Body：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `expected_hash` | string | 否 | 空字符串、32 位 MD5 或 64 位 SHA-256 | 前端额外校验完整文件 |
| `expected_size` | integer | 否 | `>= 0`；默认 `0` | 前端额外校验完整文件字节数 |
| `parts` | array | 是 | 必须覆盖全部分片 | 分片清单 |
| `parts[].chunk_index` | integer | 是 | `>= 0`，不能重复 | 分片编号 |
| `parts[].etag` | string | 是 | 32 位 MD5 | 上传分片接口返回的 ETag |

请求示例：

```json
{
  "expected_hash": "64位SHA-256",
  "expected_size": 10485760,
  "parts": [
    {
      "chunk_index": 0,
      "etag": "32位分片MD5"
    },
    {
      "chunk_index": 1,
      "etag": "32位分片MD5"
    }
  ]
}
```

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string \| null | 上传绑定的重建任务 ID |
| `file_id` | string | 文件 ID |
| `image_id` | string \| null | 图片兼容字段；图片文件等于 `file_id` |
| `file_hash` | string | SHA-256 |
| `storage_key` | string | 兼容字段；当前等于 `file_id` |
| `verified` | boolean | 是否完成校验 |
| `already_uploaded` | boolean | 合并阶段是否复用了同一用户已有文件 |
| `media_processing_status` | enum | 图片或视频为 `pending`；其他类型通常为 `skipped` |
| `thumbnail_id` | string \| null | 已有缩略图 ID，新媒体通常暂时为空 |

### 6.6 取消上传

```http
POST /api/v1/upload/{upload_id}/cancel
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `upload_id` | string | 是 | 上传会话 ID |

Body：无。

响应：

```json
{
  "cancelled": true,
  "upload_id": "upload-uuid"
}
```

## 7. 文件库与统一分片下载

### 7.1 获取文件列表

```http
GET /api/v1/files
Authorization: Bearer <token>
```

Query 参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `category` | enum | 否 | - | 按业务分类筛选 |
| `file_type` | enum | 否 | - | 按文件大类筛选 |
| `include_derivatives` | boolean | 否 | `false` | 是否包含缩略图等派生文件 |
| `file_hash` | string | 否 | - | 按 Hash 查找；长度 `1-128` |
| `file_size` | integer | 否 | - | 按字节数查找；`>= 0` |
| `skip` | integer | 否 | `0` | 分页偏移；`>= 0` |
| `limit` | integer | 否 | `50` | 返回数量；`1-200` |

响应：

```json
{
  "files": [
    {
      "...": "FileResponse"
    }
  ],
  "total": 1
}
```

### 7.2 获取文件详情

```http
GET /api/v1/files/{file_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 文件 ID |

响应：`FileResponse`。

权限规则：

- 所有者和管理员可以读取。
- 已完成公开任务的结果和预览允许其他已登录用户读取。
- 输入文件、日志和诊断文件不对其他用户公开。

### 7.3 初始化分片下载

```http
POST /api/v1/files/{file_id}/download/init
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 文件 ID |

Body 整体可省略：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `chunk_size` | integer \| null | 否 | `> 0` | 下载分片字节数；省略时使用服务端默认值 |

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `download_id` | string | 下载会话 ID |
| `file_id` | string | 文件 ID |
| `filename` | string | 文件名 |
| `mime_type` | string | MIME 类型 |
| `file_size` | integer | 文件字节数 |
| `file_hash` | string | SHA-256 |
| `chunk_size` | integer | 分片字节数 |
| `total_chunks` | integer | 总分片数 |
| `downloaded_chunks` | integer | 已下载分片数 |
| `downloaded_bytes` | integer | 已下载字节数 |
| `progress` | number | 百分比 |
| `status` | string | 初始为 `initialized` |
| `chunk_statuses` | array[integer] | 每个分片状态，`0` 未下载，`2` 已下载 |
| `created_at` | datetime string | 创建时间 |
| `updated_at` | datetime string | 更新时间 |

### 7.4 下载一个分片

```http
GET /api/v1/files/{file_id}/download/chunk?download_id=<download_id>&chunk_index=0
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 文件 ID |

Query 参数：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `download_id` | string | 是 | - | 初始化下载时返回的下载会话 ID |
| `chunk_index` | integer | 是 | `>= 0` | 从 `0` 开始的分片编号 |

响应：

- HTTP 状态码：`206 Partial Content`
- Body：二进制分片
- 关键响应头：

| 响应头 | 说明 |
| --- | --- |
| `Content-Range` | 当前字节范围 |
| `Content-Disposition` | 文件名 |
| `X-File-Id` | 文件 ID |
| `X-Download-Id` | 下载会话 ID |
| `X-Chunk-Index` | 当前分片编号 |
| `X-Chunk-Etag` | 当前分片 MD5 |
| `ETag` | 当前分片 MD5 |

### 7.5 查询下载进度

```http
GET /api/v1/files/downloads/{download_id}/progress
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `download_id` | string | 是 | 下载会话 ID |

响应包含初始化下载响应中的全部字段，并增加：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `downloaded_ranges` | array[array[integer]] | 已下载字节区间 |
| `completed_at` | datetime string \| null | 完成时间 |

### 7.6 确认下载完成

```http
POST /api/v1/files/downloads/{download_id}/complete
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `download_id` | string | 是 | 下载会话 ID |

Body：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `expected_hash` | string | 否 | 空字符串或 64 位 SHA-256 | 前端合并后额外校验 |
| `expected_size` | integer | 否 | `>= 0`；默认 `0` | 前端合并后额外校验 |
| `parts` | array | 是 | 必须覆盖全部已下载分片 | 下载分片清单 |
| `parts[].chunk_index` | integer | 是 | `>= 0` | 分片编号 |
| `parts[].etag` | string | 是 | 32 位 MD5 | 下载分片响应头中的 `X-Chunk-Etag` |

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `download_id` | string | 下载会话 ID |
| `file_id` | string | 文件 ID |
| `file_hash` | string | SHA-256 |
| `file_size` | integer | 文件字节数 |
| `total_chunks` | integer | 总分片数 |
| `downloaded_chunks` | integer | 已下载分片数 |
| `verified` | boolean | 是否校验成功 |
| `status` | string | 成功时为 `completed` |

### 7.7 删除文件

```http
DELETE /api/v1/files/{file_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 文件 ID |

Body：无。

响应：

```json
{
  "deleted": true,
  "file_id": "file_xxx",
  "status": "pending_cleanup"
}
```

说明：

- 文件会先软删除，再异步清理 MinIO 对象。
- 删除活跃任务输入会自动取消相关任务。
- 删除公开任务结果会将任务自动转回私有。
- 删除源图片或视频会同时软删除派生缩略图。

### 7.8 归档文件

```http
POST /api/v1/files/{file_id}/archive
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 文件 ID |

Body：无。

响应：归档后的 `FileResponse`，其中 `is_archived=true`。

### 7.9 重试媒体处理

```http
POST /api/v1/files/{file_id}/media-processing/retry
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | 原始图片或原始视频文件 ID |

Body：无。

响应：

```json
{
  "file_id": "file_source",
  "media_processing_status": "pending",
  "thumbnail_id": null
}
```

## 8. 重建任务

### 8.1 获取渲染/高斯算法列表

```http
GET /api/v1/reconstruction/render/algorithm
Authorization: Bearer <token>
X-App-Locale: zh-CN
```

- Path 参数：无
- Query 参数：无
- Header：
  - `X-App-Locale`：可选，`zh-CN` 或 `en-US`，用于切换参数显示名和说明文案；默认 `zh-CN`
- Body：无

响应：

```json
{
  "algorithms": [
    {
      "name": "anysplat",
      "display_name": "AnySplat",
      "available": true,
      "params": [
        {
          "param_name": "frame_nums",
          "description": "从视频中取多少帧参与重建；更多帧通常细节更好，但速度更慢、显存和内存占用更高。",
          "display_name": "抽帧数量",
          "default_value": 4
        },
        {
          "param_name": "crop_quantile",
          "description": "控制输入画面的保留范围；数值更大保留更多背景，可能提升完整度，但会增加计算量并可能带入噪声。",
          "display_name": "裁剪范围",
          "default_value": 0.8
        }
      ]
    },
    {
      "name": "dash_gaussian",
      "display_name": "DashGaussian",
      "available": true,
      "params": [
        {
          "param_name": "iterations",
          "description": "高斯模型训练次数；更多轮数通常质量更稳定，但耗时更长、GPU 占用更久。",
          "display_name": "训练轮数",
          "default_value": 30000
        }
      ]
    },
    {
      "name": "vggt_omega",
      "display_name": "VGGT Omega",
      "available": true,
      "params": []
    }
  ],
  "default_algorithm": "anysplat"
}
```

`available` 只表示后端已配置必要路径字符串。真正执行环境仍可通过管理员诊断接口检查。
该接口只返回可创建任务的高斯算法。`dash_gaussian_mesh` 和 `hunyuan3d` 不在此列表中。
旧路径 `GET /api/v1/reconstruction/algorithms` 暂时保留为兼容别名，新客户端应使用
`GET /api/v1/reconstruction/render/algorithm`。

Mesh 算法使用独立列表：

```http
GET /api/v1/reconstruction/mesh/algorithms
Authorization: Bearer <token>
X-App-Locale: en-US
```

```json
{
  "algorithms": [
    {
      "name": "dash_gaussian_mesh",
      "display_name": "DashGaussian PLY to Mesh",
      "available": true,
      "dependencies": {
        "required_stage": "gaussian_completed",
        "required_gaussian_algorithms": ["dash_gaussian"],
        "required_input_type": "ply_model",
        "description": "Requires a completed dash_gaussian render stage and one PLY result from the same task."
      },
      "params": [
        {
          "param_name": "radius",
          "description": "Filters Gaussian points far from the subject; smaller values are cleaner and faster but may remove edge detail, while larger values keep more detail with more noise and memory use.",
          "display_name": "Radius filter",
          "default_value": 4
        },
        {
          "param_name": "voxel_size",
          "description": "Controls mesh resolution; smaller values keep more detail but are slower and use more GPU and system memory, while larger values are faster but coarser.",
          "display_name": "Mesh voxel size",
          "default_value": 0.02
        }
      ]
    },
    {
      "name": "hunyuan3d",
      "display_name": "Hunyuan3D 2.1",
      "available": true,
      "dependencies": {
        "required_stage": "gaussian_completed",
        "required_gaussian_algorithms": [],
        "required_input_type": "original_media",
        "description": "Requires an existing Gaussian result; input must be original image(s) or one video from the same task."
      },
      "params": []
    }
  ],
  "default_algorithm": "dash_gaussian_mesh"
}
```

上方 Mesh 示例为节选；`dash_gaussian_mesh.params` 实际会返回 `radius`、`cluster_voxel_size`、
`keep_largest`、`iteration`、`views`、`voxel_size`、`sdf_trunc`、`alpha_threshold`、
`max_depth`、`depth_quantile`、`mask_erode` 的完整列表。
`dependencies.required_gaussian_algorithms` 为空数组表示不限定具体高斯算法；非空时必须匹配同一任务的高斯阶段算法。

### 8.2 创建任务

```http
POST /api/v1/reconstruction/tasks
Authorization: Bearer <token>
Content-Type: application/json
```

Body：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `title` | string | 否 | `""` | 任务标题 |
| `algorithm` | string \| null | 否 | 默认算法 | 首次执行的高斯阶段算法 |
| `params` | object | 否 | `{}` | 高斯阶段算法参数 |

AnySplat 参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `params.frame_nums` | integer | 否 | `4` | 传给 `export_scene_gaussians.py --frame_nums` |
| `params.crop_quantile` | number | 否 | `0.8` | 传给 `export_scene_gaussians.py --crop_quantile` |
| `params.algorithm` | string | 否 | - | 旧客户端兼容选择器；AnySplat 场景只能为 `anysplat`，不会传给脚本 |

AnySplat 会拒绝其他未知 `params` 字段。`frame_nums` 仅检查整数类型，`crop_quantile` 仅检查数字类型，后端不限制数值范围。

DashGaussian 参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `params.iterations` | integer | 否 | `30000` | 传给 `train_dash.py --iterations`，必须为正整数 |
| `params.algorithm` | string | 否 | - | 兼容选择器；DashGaussian 场景只能为 `dash_gaussian`，不会传给脚本 |

DashGaussian 会拒绝其他未知 `params` 字段。

DashGaussian PLY→mesh 参数在调用 `POST /reconstruction/mesh/start/{task_id}` 时放入
请求体的 `params` 中：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `params.radius` | number | 否 | `4` | 传给 `filter_gaussians_by_radius.py -r`，范围 `4-25`；省略时后端传 `-r 4` |
| `params.cluster_voxel_size` | number | 否 | `0.05` | 传给 `filter_gaussians_by_cluster.py -v` |
| `params.keep_largest` | boolean | 否 | `true` | 为 true 时传 `--keep largest` |
| `params.iteration` | integer | 否 | `30000` | 第三步 mesh 渲染读取的 iteration |
| `params.views` | string | 否 | `train` | 传给 `render_depth_tsdf_mesh.py --views` |
| `params.voxel_size` | number | 否 | `0.02` | 传给 `--voxel_size` |
| `params.sdf_trunc` | number | 否 | `0.36` | 传给 `--sdf_trunc` |
| `params.alpha_threshold` | number | 否 | `0.35` | 传给 `--alpha_threshold` |
| `params.max_depth` | number | 否 | `25` | 传给 `--max_depth` |
| `params.depth_quantile` | number | 否 | `0.9` | 传给 `--depth_quantile` |
| `params.mask_erode` | integer | 否 | `2` | 传给 `--mask_erode`，必须 `>=0` |

`dash_gaussian_mesh` 和 `hunyuan3d` 都是 Mesh 阶段算法，不能用它们创建独立任务，传入时返回 `422`。

创建任务会检查活跃任务额度。活跃任务只包括 `pending`、`queued`、`processing`；
历史完成、失败、取消任务不占用额度。额度不足返回 HTTP `429`：

```json
{
  "detail": {
    "code": "ACTIVE_TASK_QUOTA_EXCEEDED",
    "message": "Active task quota exceeded",
    "task_count": 10,
    "task_quota": 10
  }
}
```

单任务高斯创建示例：

```json
{
  "title": "single reconstruction workflow",
  "algorithm": "dash_gaussian",
  "params": {
    "iterations": 30000
  }
}
```

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 重建任务 ID |
| `status` | enum | 初始为 `pending` |
| `status_code` | integer | 初始为 `100` |
| `algorithm` | string | 算法标识 |
| `params` | object | 保存的业务参数 |
| `visibility` | enum | 初始为 `private` |
| `current_stage` | string | 初始为 `task_created` |
| `created_at` | datetime string | 创建时间 |

### 8.3 获取当前用户任务列表

```http
GET /api/v1/reconstruction/tasks
Authorization: Bearer <token>
```

Query 参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | enum | 否 | - | 按任务状态筛选 |
| `skip` | integer | 否 | `0` | 分页偏移；`>= 0` |
| `limit` | integer | 否 | `50` | 返回数量；`1-200` |

响应：

```json
{
  "tasks": [
    {
      "...": "ReconstructionStatusResponse"
    }
  ],
  "total": 1
}
```

### 8.4 获取发现页公开任务

```http
GET /api/v1/reconstruction/discover
Authorization: Bearer <token>
```

Query 参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | integer | 否 | `1` | 页码；`>= 1` |
| `page_size` | integer | 否 | `10` | 每页数量；`1-10`，后端强制上限 10 |
| `skip` | integer | 否 | - | 兼容旧客户端；分页偏移，`>= 0`，已废弃 |
| `limit` | integer | 否 | - | 兼容旧客户端；返回数量，`1-10`，已废弃 |

如果同时传 `page/page_size` 和 `skip/limit`，以后端会优先使用 `page/page_size`。

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tasks` | array | 公开任务列表 |
| `total` | integer | 当前拥有成功 PLY 的公开任务总数 |
| `page` | integer | 当前页码 |
| `page_size` | integer | 当前每页数量，最大为 10 |
| `total_pages` | integer | 总页数；无数据时为 0 |
| `has_next` | boolean | 是否存在下一页 |
| `has_prev` | boolean | 是否存在上一页 |

说明：

- 返回所有用户发布的 `public` 且拥有成功 PLY 的任务。
- 公开任务在高斯或 Mesh 重跑、排队、执行、失败期间仍展示最近一次成功 PLY。
- 不公开输入文件、错误日志和内部诊断字段。
- 对历史数据中的空阶段、空进度、空输入类型等字段会做安全兜底，避免发现页因脏数据返回 500。

### 8.5 获取任务详情

```http
GET /api/v1/reconstruction/tasks/{task_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

响应：`ReconstructionStatusResponse`。

> **重复功能标记：** 此接口与 `GET /api/v1/reconstruction/status/{task_id}` 当前返回相同模型并使用相同读取权限。建议将本接口用于详情页，将 `/status/{task_id}` 用于轮询。

### 8.5.1 获取任务输入文件 ID

```http
GET /api/v1/reconstruction/tasks/{task_id}/inputs
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

Body：无。

权限：

- 普通用户只能查看自己任务的输入文件 ID。
- 管理员可以查看所有任务的输入文件 ID。
- 公开任务不会向其他普通用户暴露输入文件 ID；发现页只应使用结果和预览文件。

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 重建任务 ID |
| `input_kind` | string | 用户原始上传输入类型：`image`、`image_folder`、`video` 或空字符串 |
| `input_file_ids` | array[string] | 该任务绑定的用户原始上传文件 ID 列表 |
| `input_file_count` | integer | 输入文件数量 |

响应示例：

```json
{
  "task_id": "recon_xxx",
  "input_kind": "image_folder",
  "input_file_ids": ["file_a", "file_b", "file_c"],
  "input_file_count": 3
}
```

### 8.5.2 替换任务 PLY 结果文件

任务重建完成后，用户可下载 PLY 到本地修改，再选择上传云端覆盖原结果。覆盖流程保持原
`file_id` 不变：后端先接收临时分片并校验完整性，校验成功后再把原结果文件指向新的对象存储文件。

#### 初始化替换上传

```http
POST /api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/init
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |
| `file_id` | string | 是 | 该任务已关联的 PLY 结果文件 ID |

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `filename` | string | 是 | 修改后的本地文件名 |
| `file_size` | integer | 是 | 修改后文件大小 |
| `chunk_size` | integer \| null | 否 | 分片大小；省略时使用服务端默认值 |
| `mime_type` | string | 否 | 固定为 `model/ply` |
| `file_hash` | string | 是 | 修改后完整文件的 SHA-256 |

响应：

```json
{
  "task_id": "recon_xxx",
  "file_id": "file_ply",
  "upload_id": "upload_xxx",
  "chunk_size": 1048576,
  "total_chunks": 3,
  "expires_at": "2026-06-24T12:00:00Z"
}
```

#### 上传分片

继续复用现有上传分片接口：

```http
PUT /api/v1/upload/{upload_id}/chunk?chunk_index=0
Authorization: Bearer <token>
Content-Type: application/octet-stream

<PLY 分片二进制>
```

#### 完成替换

```http
POST /api/v1/reconstruction/tasks/{task_id}/results/{file_id}/replace/complete?upload_id={upload_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `expected_hash` | string | 否 | 修改后完整文件的 SHA-256 或 MD5；推荐 SHA-256 |
| `expected_size` | integer | 否 | 修改后文件大小 |
| `parts` | array | 是 | 每个分片的 `chunk_index` 和分片 MD5 `etag` |

响应：

```json
{
  "task_id": "recon_xxx",
  "file_id": "file_ply",
  "filename": "point_cloud.ply",
  "mime_type": "model/ply",
  "file_size": 123456,
  "file_hash": "<修改后文件的 SHA-256>",
  "replaced": true,
  "verified": true
}
```

限制：

- 只能由任务所有者或管理员替换。
- 只能替换该任务已关联的 `model/ply` 结果文件。
- 任务处于 `queued` 或 `processing` 时禁止替换。
- 替换成功后原 `file_id` 不变，任务详情和下载接口会返回修改后的 PLY 内容。

### 8.6 修改任务可见性

```http
PATCH /api/v1/reconstruction/tasks/{task_id}/visibility
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `visibility` | enum | 是 | `private` 或 `public` |

响应：`ReconstructionStatusResponse`。

限制：

- 普通用户只能修改自己的任务。
- 只有 `completed` 任务可以改为 `public`。

### 8.7 删除任务

```http
DELETE /api/v1/reconstruction/tasks/{task_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

Body：无。

响应：

```json
{
  "task_id": "recon_xxx",
  "deleted": true,
  "status": "completed"
}
```

说明：

- 活跃任务会先取消。
- 删除任务只解除任务与文件的关系。
- 已上传输入和算法结果仍保留在文件库中。

### 8.8 通用启动任务

```http
POST /api/v1/reconstruction/start/{task_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 唯一重建任务 ID |

Body：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `input_type` | string \| null | 否 | 输入类型声明，可选 `image`、`image_folder`、`video`、`ply`、`ply_model`；后端仍会按真实文件校验 |
| `input_file_ids` | array[string] | 否 | 输入文件 ID 列表；上传初始化已传 `task_id` 时可省略 |

调用约束：

- 新流程在 `POST /upload/init` 传入 `task_id`，文件合并后自动绑定任务；启动 Body 可以使用 `{}`。
- 兼容流程可以在启动时显式传 `input_file_ids`，但不能为空。
- 所有 ID 必须唯一，并属于当前用户。
- 禁止使用缩略图等派生文件作为算法输入。
- `anysplat`、`dash_gaussian` 和图片模式 `vggt_omega` 至少需要 `3` 张图片。
- `anysplat` 支持单个视频；视频会作为文件路径传给 `export_scene_gaussians.py`。
- `dash_gaussian` 支持单个视频；视频会作为文件路径传给 `train_dash.py --input_path`。
- 此接口始终启动或重跑任务保存的高斯算法，不会自动启动 Mesh。
- 视频模式必须只传 `1` 个视频。
- `hunyuan3d` 支持 `1` 张图片、任意数量图片组成的图集，或 `1` 个视频。
- 图片和视频不能混合提交。

AnySplat 请求示例：

```json
{
  "input_type": "image",
  "input_file_ids": [
    "file_image_1",
    "file_image_2",
    "file_image_3"
  ]
}
```

VGGT Omega 视频请求示例：

```json
{
  "input_type": "video",
  "input_file_ids": ["file_video"]
}
```

Hunyuan3D 请求示例：

```json
{
  "input_type": "image",
  "input_file_ids": ["file_image"]
}
```

AnySplat 视频请求示例：

```json
{
  "input_type": "video",
  "input_file_ids": ["file_video"]
}
```

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 重建任务 ID |
| `status` | enum | 通常为 `queued` |
| `status_code` | integer | 通常为 `102` |
| `algorithm` | string | 算法标识 |
| `current_stage` | string | 启动高斯后为 `gaussian_queued` |
| `input_type` | string | 后端识别出的输入类型，可能为 `image`、`image_folder`、`video`、`ply_model` |
| `input_file_count` | integer | 输入文件总数 |
| `queue_reason` | string \| null | 刚入队通常为 `null`；后续轮询可能为 `gpu_capacity` 或 `user_gpu_concurrency` |

调度时如果每日 GPU 秒数已耗尽，任务不会继续重试，而是进入失败状态：

```json
{
  "error_code": "GPU_DAILY_QUOTA_EXCEEDED",
  "gpu_quota_exceeded": true
}
```

高斯状态链：

```text
task_created
→ data_uploading
→ gaussian_queued
→ gaussian_processing
→ gaussian_completed
```

高斯成功后任务停在 `status=completed/current_stage=gaussian_completed`。高斯阶段允许
使用同一个接口重跑；重跑成功会替换当前 PLY 并解除旧 Mesh 关联，重跑失败时保留旧结果。

### 8.9 手动启动 Mesh

```http
POST /api/v1/reconstruction/mesh/start/{task_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Body：

```json
{
  "algorithm": "dash_gaussian_mesh",
  "input_file_ids": ["file_ply"],
  "params": {
    "radius": 10,
    "voxel_size": 0.02
  }
}
```

- `algorithm` 必填，只允许 `dash_gaussian_mesh` 或 `hunyuan3d`。
- `input_file_ids` 必填且不能为空，所有文件必须已经关联到同一任务。
- 两种 Mesh 算法都要求任务已经存在成功 PLY。
- `dash_gaussian_mesh` 必须传该任务的一个 `ply_model` 结果。
- `hunyuan3d` 必须传该任务原始图片、图片组或单视频，禁止 PLY、缩略图和图片视频混合输入。
- `params` 可选；Dash Mesh 省略时使用默认参数，Hunyuan3D 当前使用空对象。
- Mesh 成功进入 `completed/mesh_completed`。
- Mesh 失败进入 `partial_completed/mesh_failed`，已有 PLY 和最近一次成功 Mesh 保留。
- 同一任务可以分别运行两种 Mesh 算法，并同时保留 Dash OBJ、Hunyuan 主 GLB 及其附属输出文件。
- 旧 `POST /api/v1/reconstruction/hunyuan3d/start/{task_id}` 已删除。

### 8.10 轮询任务状态

```http
GET /api/v1/reconstruction/status/{task_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

响应：`ReconstructionStatusResponse`。

前端建议每 `2-5` 秒轮询一次，直到状态进入：

```text
completed
failed
cancelled
manual_review
```

高斯完成的正常状态是 `completed/gaussian_completed`。前端主动执行 Mesh 后，成功状态是
`completed/mesh_completed`；失败状态是 `partial_completed/mesh_failed`。`results`
保留当前可用的 PLY，以及各 Mesh 算法最近一次成功的结果，前端按类别选择并使用统一下载接口。
旧兼容字段 `result_files` 仍会同步返回。

> **重复功能标记：** 当前响应与 `GET /api/v1/reconstruction/tasks/{task_id}` 相同。保留该接口是为了明确表达“轮询状态”用途。

### 8.11 取消任务

```http
POST /api/v1/reconstruction/cancel/{task_id}
Authorization: Bearer <token>
```

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

Body：无。

响应：

```json
{
  "task_id": "recon_xxx",
  "status": "cancelled",
  "cancelled": true,
  "message": "Cancellation requested"
}
```

### 8.12 获取任务错误日志

```http
GET /api/v1/reconstruction/logs/{task_id}?tail=4000
Authorization: Bearer <token>
```

- 权限：仅任务所有者或管理员

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

Query 参数：

| 字段 | 类型 | 必填 | 默认值 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `tail` | integer | 否 | `4000` | `0-20000` | 返回 stdout 和 stderr 尾部字符数 |

响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 重建任务 ID |
| `status` | enum | 当前任务状态 |
| `error_code` | string \| null | 错误码 |
| `error_status_code` | integer \| null | 错误状态码 |
| `error` | string \| null | 错误摘要 |
| `stdout_tail` | string | 标准输出尾部 |
| `stderr_tail` | string | 标准错误尾部 |

### 8.13 管理员诊断算法环境

```http
GET /api/v1/reconstruction/diagnostics/{task_id}
Authorization: Bearer <admin-token>
```

- 权限：仅管理员

Path 参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 重建任务 ID |

响应：

```json
{
  "task_id": "recon_xxx",
  "status": "queued",
  "algorithm": "hunyuan3d",
  "error_code": null,
  "error_status_code": null,
  "checks": [
    {
      "name": "python_runtime",
      "ok": true,
      "detail": "configured"
    },
    {
      "name": "algorithm_directory",
      "ok": true,
      "detail": "configured"
    },
    {
      "name": "algorithm_entrypoint",
      "ok": true,
      "detail": "configured"
    },
    {
      "name": "minio_bucket",
      "ok": true,
      "detail": "3dgs-files"
    }
  ]
}
```

实际响应还会包含 `celery_task_id`、`worker_node`、`executor` 和 `cuda_device` 检查项。

## 9. 前端完整调用顺序

### 9.1 创建任务并上传图片或视频

```text
POST /api/v1/reconstruction/tasks
→ 保存唯一 task_id
→ POST /api/v1/upload/init，并在 Body 传 task_id
→ 如果 already_uploaded=true，直接记录 file_id
→ 否则循环 PUT /api/v1/upload/{upload_id}/chunk
→ 可选 GET /api/v1/upload/{upload_id}/progress
→ POST /api/v1/upload/{upload_id}/merge
→ 文件自动绑定到 task_id
```

### 9.2 启动单任务高斯阶段

```text
→ POST /api/v1/reconstruction/start/{task_id}
→ Body 可以为 {}
→ GET /api/v1/reconstruction/status/{task_id}
→ 轮询原 task_id，直到 current_stage=gaussian_completed
→ 从 results 读取 render_model PLY
→ 使用 /api/v1/files/{file_id}/download/* 下载
```

AnySplat 支持至少 `3` 张图片组成的图集或单个视频。创建任务时可以传：

```json
{
  "algorithm": "anysplat",
  "params": {
    "frame_nums": 4,
    "crop_quantile": 0.8
  }
}
```

DashGaussian 支持至少 `3` 张图片组成的图集或单个视频。创建任务时可以传：

```json
{
  "algorithm": "dash_gaussian",
  "params": {
    "iterations": 30000
  }
}
```

DashGaussian 或 AnySplat 完成后会登记 PLY，并停在 `gaussian_completed`。

### 9.3 Mesh 手动阶段

```text
gaussian_completed
→ 前端选择当前任务的 PLY 结果
→ POST /api/v1/reconstruction/mesh/start/{task_id}
→ mesh_queued
→ mesh_processing
→ mesh_completed
```

前端不创建任何 Mesh 独立任务。调用统一 Mesh 启动接口时必须提交 `algorithm` 和
`input_file_ids`。Mesh 参数通过请求体的 `params` 设置。

### 9.4 启动 Hunyuan3D

```text
已有同一 task_id 的成功 PLY
→ POST /api/v1/reconstruction/mesh/start/{task_id}
→ Body: {"algorithm":"hunyuan3d","input_file_ids":["原始输入 file_id"],"params":{}}
→ GET /api/v1/reconstruction/status/{task_id}
→ 读取 results
→ result_id 是 GLB 主结果
→ results 中还有 OBJ、MTL、纹理和其他输出文件各自的 file_id
→ 两个文件都使用 /api/v1/files/{file_id}/download/* 下载
```

### 9.5 下载和断点续传

```text
POST /api/v1/files/{file_id}/download/init
→ 循环 GET /api/v1/files/{file_id}/download/chunk
→ 可选 GET /api/v1/files/downloads/{download_id}/progress
→ 前端按 chunk_index 合并
→ POST /api/v1/files/downloads/{download_id}/complete
```

## 10. 重复功能与兼容字段审计

### 10.1 接口级重复或重叠

| 接口 A | 接口 B | 类型 | 当前情况 | 建议 |
| --- | --- | --- | --- | --- |
| `GET /api/v1/auth/me` | `GET /api/v1/users/me` | 功能相近但不重复 | 前者返回完整鉴权用户信息，后者返回精简资料 | Token 校验用前者，资料页用后者 |
| `GET /api/v1/reconstruction/tasks/{task_id}` | `GET /api/v1/reconstruction/status/{task_id}` | 功能重叠 | 都返回 `ReconstructionStatusResponse` | 保留语义区分；详情页用前者，轮询用后者 |

### 10.2 响应字段级兼容别名

| 响应模型 | 字段 | 与哪个字段重复 | 建议 |
| --- | --- | --- | --- |
| 上传初始化与合并响应 | `image_id` | 图片场景下等于 `file_id` | 新前端统一读取 `file_id` |
| 上传初始化与合并响应 | `storage_key` | 当前等于 `file_id` | 兼容旧前端，不要将其理解为真实 MinIO key |
| 文件响应 | `storage_key` | 当前等于 `id` | 新前端统一读取 `id` |
| 任务状态响应 | `result_file_id` | 当前等于 `result_id` | 新前端统一读取 `result_id` |
| 任务状态响应 | `result_storage_key` | 当前等于 `result_id` | 兼容旧前端 |
| 任务状态响应 | `ply_id` | PLY 任务中指向 PLY 结果 | 仅旧 PLY 前端兼容使用 |

### 10.3 已移除的旧接口

以下接口不应继续出现在新前端或 Apifox 项目中：

```text
/api/v1/tasks/*
/api/v1/reconstruction/download/{task_id}
旧版 Download Result
旧版 Download Ply Result
```

所有文件，包括算法结果、缩略图和 ZIP，都统一使用：

```text
/api/v1/files/{file_id}/download/*
```

## 11. Apifox 建议

1. 新建环境变量 `base_url=http://<服务器IP>:8000`。
2. 注册或登录后，将响应中的 `access_token` 保存为环境变量 `token`。
3. 对需要鉴权的接口统一添加请求头：

```text
Authorization: Bearer {{token}}
```

4. 可选 Params 不使用时取消勾选，避免发送空字符串。
5. 上传和下载分片接口使用二进制 Body，不使用 `multipart/form-data`。
6. Hunyuan3D 结果读取 `results`；旧前端只读取 `result_id` 时仍可下载 GLB 主模型。

## 12. 当前 reconstruction 任务模型

任务不再使用业务子类型或父子任务查询。一个 `task_id` 表示同一作品的高斯和可选 Mesh 阶段。

推荐创建请求体：

```json
{
  "title": "single workflow",
  "algorithm": "dash_gaussian",
  "params": {"iterations": 30000}
}
```

上传初始化时绑定任务：

```json
{
  "task_id": "recon_xxx",
  "filename": "input.mp4",
  "file_size": 123456,
  "mime_type": "video/mp4",
  "file_hash": "64位SHA-256"
}
```

上传完成后调用 `POST /api/v1/reconstruction/start/{task_id}` 启动高斯，Body 可使用 `{}`。
高斯完成后按需调用 `POST /api/v1/reconstruction/mesh/start/{task_id}`，提交 Mesh `algorithm` 和 `input_file_ids`
和可选 Mesh `params`。
查询 `GET /api/v1/reconstruction/status/{task_id}` 或 `GET /api/v1/reconstruction/tasks/{task_id}` 时，前端应读取 `results`：

```json
[
  {
    "file_id": "file_ply",
    "category": "render_model",
    "file_type": "model",
    "mime_type": "model/ply",
    "filename": "point_cloud.ply",
    "size_bytes": 123456
  },
  {
    "file_id": "file_mesh",
    "category": "mesh_model",
    "file_type": "model",
    "mime_type": "model/obj",
    "filename": "dash_gaussian_mesh.obj",
    "size_bytes": 654321
  }
]
```

`dash_gaussian_mesh` 阶段会根据请求里的 PLY `file_id` 读取其 `metainfo.generation_id`，只恢复同一轮 DashGaussian 输出中的 `cfg_args` 和模型支持文件，自动把 `--output` 补成 `dash_gaussian_mesh.obj` 文件路径，并把 Mesh 输出目录中的 OBJ、MTL、贴图、JSON 等有效文件登记到 `results`。失败时任务状态为 `partial_completed`，已有 PLY 等结果仍保留。下载仍统一使用 `/api/v1/files/{file_id}/download/*`。


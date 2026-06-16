# Flutter 接口迁移说明

仓库中的 Flutter 客户端仍在调用已经删除的原型接口。前端后续需要按照下表迁移。

| 旧调用 | 新调用 |
| --- | --- |
| `POST /reconstruction/start`，请求体传 `storage_key` | 先上传输入，再调用 `POST /reconstruction/tasks`，最后调用 `POST /reconstruction/start/{task_id}` |
| `GET /reconstruction/status/{task_id}` | 保留 |
| `GET /reconstruction/download/{task_id}` | 从状态接口读取 `result_id`，再调用 `/files/{result_id}/download/*` |
| 整数文件 ID | 字符串 `file_<uuid>` |
| 服务端文件路径 | 不再读取或展示 |

发现页使用 `GET /api/v1/reconstruction/discover?page=1&page_size=10`。`page_size` 最大为 `10`；公开任务响应只暴露结果和预览 ID，不会暴露输入文件 ID、日志、Celery ID、执行命令或容器路径。

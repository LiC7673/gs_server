import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from uuid import uuid4

from app.core.config import settings
from app.core.redis_client import get_redis_client


class GPUInspectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class GPULease:
    node_id: str
    executor_id: str
    lease_scope: str
    device_id: str
    task_id: str
    user_id: int
    token: str
    user_token: str


@dataclass(frozen=True)
class GPUAcquireResult:
    lease: Optional[GPULease] = None
    queue_reason: Optional[str] = None


class GPUScheduler:
    @staticmethod
    def configured_devices() -> List[str]:
        return [item.strip() for item in settings.gpu_device_ids.split(",") if item.strip()]

    @staticmethod
    def _lease_key(lease_scope: str, device_id: str) -> str:
        return f"gpu-lease:{lease_scope}:{device_id}"

    @staticmethod
    def _user_lease_key(lease_scope: str, user_id: int) -> str:
        return f"gpu-user-leases:{lease_scope}:{user_id}"

    @staticmethod
    def _lease_scope(node_id: Optional[str] = None, executor_id: Optional[str] = None) -> str:
        if settings.gpu_scheduler_mode.lower() == "k8s":
            return executor_id or settings.worker_executor_id
        return node_id or settings.worker_node_id

    @staticmethod
    def _lease_value(task_id: str, token: str, user_id: int, user_token: str) -> str:
        return json.dumps({"task_id": task_id, "token": token, "user_id": user_id, "user_token": user_token})

    @staticmethod
    def _user_lease_member(task_id: str, token: str) -> str:
        return f"{task_id}:{token}"

    @staticmethod
    def _memory_used_mb() -> Dict[str, int]:
        command = [
            "nvidia-smi",
            "--query-gpu=index,memory.used",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            if settings.gpu_require_nvidia_smi:
                raise GPUInspectionError(f"Cannot inspect GPUs with nvidia-smi: {exc}") from exc
            return {device: 0 for device in GPUScheduler.configured_devices()}

        usage: Dict[str, int] = {}
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) != 2:
                continue
            try:
                usage[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return usage

    @staticmethod
    async def _acquire_user_slot(
        client,
        lease_scope: str,
        task_id: str,
        user_id: int,
        concurrency_quota: int,
    ) -> Optional[str]:
        token = uuid4().hex
        acquired = await client.eval(
            """
            local now = tonumber(ARGV[1])
            local ttl = tonumber(ARGV[2])
            local limit = tonumber(ARGV[3])
            local member = ARGV[4]
            redis.call("zremrangebyscore", KEYS[1], "-inf", now)
            if limit <= 0 then
                return 0
            end
            if redis.call("zcard", KEYS[1]) >= limit then
                return 0
            end
            redis.call("zadd", KEYS[1], now + ttl, member)
            redis.call("expire", KEYS[1], ttl * 2)
            return 1
            """,
            1,
            GPUScheduler._user_lease_key(lease_scope, user_id),
            time.time(),
            settings.gpu_lease_ttl_seconds,
            concurrency_quota,
            GPUScheduler._user_lease_member(task_id, token),
        )
        return token if acquired else None

    @staticmethod
    async def _renew_user_slot(client, lease: GPULease) -> bool:
        result = await client.eval(
            """
            local ttl = tonumber(ARGV[2])
            if redis.call("zscore", KEYS[1], ARGV[1]) then
                redis.call("zadd", KEYS[1], tonumber(ARGV[3]) + ttl, ARGV[1])
                redis.call("expire", KEYS[1], ttl * 2)
                return 1
            end
            return 0
            """,
            1,
            GPUScheduler._user_lease_key(lease.lease_scope, lease.user_id),
            GPUScheduler._user_lease_member(lease.task_id, lease.user_token),
            settings.gpu_lease_ttl_seconds,
            time.time(),
        )
        return bool(result)

    @staticmethod
    async def _release_user_slot(client, lease: GPULease) -> bool:
        result = await client.zrem(
            GPUScheduler._user_lease_key(lease.lease_scope, lease.user_id),
            GPUScheduler._user_lease_member(lease.task_id, lease.user_token),
        )
        return bool(result)

    @staticmethod
    async def acquire(task_id: str, user_id: int, concurrency_quota: int) -> GPUAcquireResult:
        client = get_redis_client()
        lease_scope = GPUScheduler._lease_scope()
        user_token: Optional[str] = None
        try:
            user_token = await GPUScheduler._acquire_user_slot(
                client,
                lease_scope,
                task_id,
                user_id,
                int(concurrency_quota or 0),
            )
            if user_token is None:
                return GPUAcquireResult(queue_reason="user_gpu_concurrency")

            try:
                usage = await asyncio.to_thread(GPUScheduler._memory_used_mb)
            except Exception:
                await client.zrem(
                    GPUScheduler._user_lease_key(lease_scope, user_id),
                    GPUScheduler._user_lease_member(task_id, user_token),
                )
                raise
            for device_id in GPUScheduler.configured_devices():
                if settings.gpu_scheduler_mode.lower() == "k8s":
                    used_mb = max(usage.values()) if usage else None
                else:
                    used_mb = usage.get(device_id)
                if used_mb is None or used_mb > settings.gpu_memory_busy_threshold_mb:
                    continue
                token = uuid4().hex
                acquired = await client.set(
                    GPUScheduler._lease_key(lease_scope, device_id),
                    GPUScheduler._lease_value(task_id, token, user_id, user_token),
                    nx=True,
                    ex=settings.gpu_lease_ttl_seconds,
                )
                if acquired:
                    return GPUAcquireResult(
                        lease=GPULease(
                            node_id=settings.worker_node_id,
                            executor_id=settings.worker_executor_id,
                            lease_scope=lease_scope,
                            device_id=device_id,
                            task_id=task_id,
                            user_id=user_id,
                            token=token,
                            user_token=user_token,
                        )
                    )
            await client.zrem(
                GPUScheduler._user_lease_key(lease_scope, user_id),
                GPUScheduler._user_lease_member(task_id, user_token),
            )
            return GPUAcquireResult(queue_reason="gpu_capacity")
        finally:
            await client.aclose()

    @staticmethod
    async def renew(lease: GPULease) -> bool:
        client = get_redis_client()
        try:
            device_result = await client.eval(
                """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("expire", KEYS[1], ARGV[2])
                end
                return 0
                """,
                1,
                GPUScheduler._lease_key(lease.lease_scope, lease.device_id),
                GPUScheduler._lease_value(lease.task_id, lease.token, lease.user_id, lease.user_token),
                settings.gpu_lease_ttl_seconds,
            )
            user_result = await GPUScheduler._renew_user_slot(client, lease)
            return bool(device_result) and user_result
        finally:
            await client.aclose()

    @staticmethod
    async def release(lease: GPULease) -> bool:
        client = get_redis_client()
        try:
            device_result = await client.eval(
                """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                end
                return 0
                """,
                1,
                GPUScheduler._lease_key(lease.lease_scope, lease.device_id),
                GPUScheduler._lease_value(lease.task_id, lease.token, lease.user_id, lease.user_token),
            )
            user_result = await GPUScheduler._release_user_slot(client, lease)
            return bool(device_result) or user_result
        finally:
            await client.aclose()

    @staticmethod
    async def release_stale(
        node_id: Optional[str],
        executor_id: Optional[str],
        device_id: Optional[str],
        task_id: str,
        user_id: Optional[int] = None,
    ) -> bool:
        if not node_id:
            return False
        client = get_redis_client()
        lease_scope = GPUScheduler._lease_scope(node_id, executor_id)
        try:
            device_result = 0
            if device_id:
                device_result = await client.eval(
                    """
                    local value = redis.call("get", KEYS[1])
                    if value and string.find(value, ARGV[1], 1, true) then
                        return redis.call("del", KEYS[1])
                    end
                    return 0
                    """,
                    1,
                    GPUScheduler._lease_key(lease_scope, device_id),
                    f'"task_id": "{task_id}"',
                )
            user_result = 0
            if user_id:
                user_result = await client.eval(
                    """
                    local removed = 0
                    local members = redis.call("zrange", KEYS[1], 0, -1)
                    for _, member in ipairs(members) do
                        if string.find(member, ARGV[1], 1, true) then
                            removed = removed + redis.call("zrem", KEYS[1], member)
                        end
                    end
                    return removed
                    """,
                    1,
                    GPUScheduler._user_lease_key(lease_scope, user_id),
                    task_id,
                )
            return bool(device_result) or bool(user_result)
        finally:
            await client.aclose()

    @staticmethod
    def terminate_local_process(
        node_id: Optional[str],
        executor_id: Optional[str],
        process_id: Optional[int],
    ) -> bool:
        if (
            not process_id
            or node_id != settings.worker_node_id
            or executor_id != settings.worker_executor_id
        ):
            return False
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process_id)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                try:
                    os.killpg(int(process_id), signal.SIGTERM)
                except ProcessLookupError:
                    return False
                except Exception:
                    os.kill(int(process_id), signal.SIGTERM)
            return True
        except (OSError, ValueError):
            return False

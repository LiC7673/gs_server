from celery.signals import worker_ready

from app.core.celery_app import celery_app
from app.tasks.async_runner import run_async


@worker_ready.connect
def recover_reconstruction_tasks_on_worker_start(**kwargs):
    from app.api.v1.reconstruction import recover_stale_reconstruction_tasks

    run_async(recover_stale_reconstruction_tasks())


@celery_app.task(bind=True, name="reconstruction.run", max_retries=None)
def run_reconstruction(self, task_id: str) -> dict:
    from app.api.v1.reconstruction import (
        mark_gpu_inspection_failed,
        mark_waiting_for_gpu,
        prepare_gpu_dispatch,
        run_reconstruction_algorithm,
    )
    from app.core.config import settings
    from app.services.gpu_scheduler import GPUInspectionError, GPUScheduler

    async def execute() -> dict:
        dispatch = await prepare_gpu_dispatch(task_id)
        if dispatch is None:
            return {"task_id": task_id, "status": "ignored"}
        try:
            acquire_result = await GPUScheduler.acquire(
                task_id,
                dispatch["user_id"],
                dispatch["gpu_concurrency_quota"],
            )
        except GPUInspectionError as exc:
            await mark_gpu_inspection_failed(task_id, str(exc))
            return {"task_id": task_id, "status": "failed", "error_code": "GPU_INSPECTION_FAILED"}
        lease = acquire_result.lease
        if lease is None:
            should_retry = await mark_waiting_for_gpu(task_id, acquire_result.queue_reason or "gpu_capacity")
            return {"task_id": task_id, "status": "retry" if should_retry else "ignored"}

        self.update_state(
            state="STARTED",
            meta={
                "task_id": task_id,
                "status": "processing",
                "current_stage": "processing",
                "worker_node_id": lease.node_id,
                "executor_id": lease.executor_id,
                "cuda_device": lease.device_id,
            },
        )
        try:
            await run_reconstruction_algorithm(task_id, lease)
        finally:
            try:
                await GPUScheduler.release(lease)
            except Exception:
                pass
        return {"task_id": task_id, "status": "finished"}

    result = run_async(execute())
    if result["status"] == "retry":
        raise self.retry(countdown=settings.gpu_retry_countdown_seconds, max_retries=None)
    return result

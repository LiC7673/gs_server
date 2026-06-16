from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "3dgs_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=False,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.reconstruction_worker_concurrency,
    task_routes={
        "reconstruction.run": {"queue": settings.reconstruction_queue_name},
        "media.*": {"queue": settings.media_queue_name},
        "cleanup.*": {"queue": "maintenance"},
    },
    task_soft_time_limit=settings.task_timeout_seconds,
    task_time_limit=settings.task_timeout_seconds + 300,
    task_max_retries=settings.task_max_retries,
    beat_schedule={
        "cleanup-expired-uploads": {
            "task": "cleanup.expired_uploads",
            "schedule": 3600.0,
        },
        "recover-stale-reconstruction-tasks": {
            "task": "cleanup.stale_tasks",
            "schedule": 60.0,
        },
        "cleanup-orphan-storage-objects": {
            "task": "cleanup.temp_files",
            "schedule": 3600.0,
        },
        "recover-stale-media-tasks": {
            "task": "cleanup.stale_media",
            "schedule": 60.0,
        },
        "reset-daily-gpu-usage": {
            "task": "cleanup.reset_daily_gpu_usage",
            "schedule": crontab(hour=16, minute=0),
        },
    },
)

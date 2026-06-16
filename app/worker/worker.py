from app.core.celery_app import celery_app
from app.tasks import cleanup_tasks, media_tasks, reconstruction_tasks

task_list = [
    reconstruction_tasks.run_reconstruction,
    cleanup_tasks.cleanup_expired_uploads,
    cleanup_tasks.cleanup_stale_tasks,
    cleanup_tasks.cleanup_temp_files,
    cleanup_tasks.cleanup_stale_media,
    media_tasks.process_media,
]

from app.core.celery_app import celery_app
from app.core.config import settings
from app.tasks.async_runner import run_async


@celery_app.task(bind=True, name="media.process", max_retries=None)
def process_media(self, file_id: str) -> dict:
    from app.services.media_service import MediaProcessingFailure, MediaService

    try:
        thumbnail_id = run_async(MediaService.process(file_id, self.request.id))
        return {"file_id": file_id, "thumbnail_id": thumbnail_id, "status": "completed"}
    except MediaProcessingFailure as exc:
        if self.request.retries < settings.media_task_max_retries:
            raise self.retry(
                exc=exc,
                countdown=settings.media_retry_countdown_seconds,
                max_retries=settings.media_task_max_retries,
            )
        return {"file_id": file_id, "status": "failed", "error_code": exc.code}

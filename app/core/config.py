import socket

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "3DGS Reconstruction Service"
    debug: bool = False
    api_prefix: str = "/api/v1"
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60 * 24
    algorithm: str = "HS256"
    mock_auth_enabled: bool = False
    mock_auth_username: str = "mock_user"
    mock_auth_email: str = "mock_user@example.com"

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/3dgs"
    database_url_sync: str = "postgresql://user:password@localhost:5432/3dgs"
    echo_sql: bool = False

    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    redis_url: str = "redis://localhost:6379/2"

    storage_backend: str = "s3"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin123"
    s3_bucket: str = "3dgs-files"
    s3_region: str = "us-east-1"
    api_scratch_path: str = "/tmp/3dgs-api"
    reconstruction_scratch_path: str = "/tmp/3dgs-reconstruction"
    download_session_ttl_seconds: int = 24 * 60 * 60
    media_queue_name: str = "media"
    media_worker_concurrency: int = 2
    media_scratch_path: str = "/tmp/3dgs-media"
    media_max_image_pixels: int = 100_000_000
    media_thumbnail_max_edge: int = 512
    media_thumbnail_jpeg_quality: int = 80
    media_task_max_retries: int = 3
    media_retry_countdown_seconds: int = 30
    media_stale_processing_seconds: int = 300

    max_file_size: int = 10 * 1024 * 1024 * 1024
    upload_chunk_size: int = 5 * 1024 * 1024
    download_chunk_size: int = 5 * 1024 * 1024
    default_user_storage_quota: int = 50 * 1024 * 1024 * 1024
    default_user_task_quota: int = 10
    default_user_gpu_quota: int = 3600
    default_user_active_task_quota: int = 10
    default_user_gpu_concurrency_quota: int = 1
    default_user_gpu_daily_quota_seconds: int = 3600
    gpu_quota_timezone: str = "Asia/Shanghai"
    task_timeout_seconds: int = 7200
    task_max_retries: int = 3

    reconstruction_poll_interval_seconds: int = 2
    reconstruction_queue_name: str = "reconstruction"
    reconstruction_worker_concurrency: int = 8
    reconstruction_preflight_on_start: bool = True
    reconstruction_stale_processing_seconds: int = 300
    reconstruction_output_ply_glob: str = "**/*.ply"
    default_reconstruction_algorithm: str = "anysplat"
    default_mesh_algorithm: str = "dash_gaussian_mesh"

    worker_node_id: str = socket.gethostname()
    worker_executor_id: str = socket.gethostname()
    gpu_scheduler_mode: str = "local"
    gpu_device_ids: str = "0,1,2,3,4,5,6,7"
    gpu_lease_ttl_seconds: int = 60
    gpu_retry_countdown_seconds: int = 10
    gpu_memory_busy_threshold_mb: int = 512
    gpu_require_nvidia_smi: bool = True

    anysplat_python_path: str = "/data1/lzh/anaconda3/envs/anysplat/bin/python"
    anysplat_path: str = "/data1/lzh/lzy/AnySplat"
    anysplat_entrypoint: str = "/data1/lzh/lzy/AnySplat/export_scene_gaussians.py"
    anysplat_args_template: str = "{input_path} --frame_nums {frame_nums} --output_folder {output_folder} --crop_quantile {crop_quantile}"
    anysplat_timeout_seconds: int = 600

    dash_gaussian_conda_path: str = "/data1/lzh/anaconda3/bin/conda"
    dash_gaussian_path: str = "/data1/lzh/lzy/DashGaussian"
    dash_gaussian_entrypoint: str = "train_dash.py"
    dash_gaussian_args_template: str = "--input_path {input_path} -m {output_folder} --iterations {iterations} --disable_viewer"
    dash_gaussian_command_template: str = "{python_path} run -n DashGaussian python {entrypoint} {args}"
    dash_gaussian_timeout_seconds: int = 7200

    vggt_omega_python_path: str = "conda"
    vggt_omega_path: str = "/data1/lzh/lzy/vggt-omega"
    vggt_omega_entrypoint: str = "example.py"
    vggt_omega_args_template: str = "--input {input_path} --output_dir {output_folder}"
    vggt_omega_command_template: str = "{python_path} run -n anysplat python {entrypoint} {args}"
    vggt_omega_timeout_seconds: int = 1200
    vggt_omega_output_glob: str = "**/*"
    vggt_omega_result_category: str = "other"
    vggt_omega_result_mime_type: str = "application/octet-stream"
    vggt_omega_result_filename: str = "vggt_omega_result"

    hunyuan3d_conda_path: str = "/data1/lzh/anaconda3/bin/conda"
    hunyuan3d_path: str = "/data1/lzh/lzy/hunyuan3d-2.1"
    hunyuan3d_entrypoint: str = "example.py"
    hunyuan3d_args_template: str = "{input_path} -o {output_glb}"
    hunyuan3d_command_template: str = "{python_path} run -n hunyuan3d python {entrypoint} {args}"
    hunyuan3d_timeout_seconds: int = 1800

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()

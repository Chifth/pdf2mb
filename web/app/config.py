import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: str = "*") -> list[str]:
    raw = os.getenv(name, default).strip()
    if not raw or raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8000)
    root_path: str = os.getenv("ROOT_PATH", "")
    log_level: str = os.getenv("LOG_LEVEL", "info")
    compress_workers: int = _env_int("COMPRESS_WORKERS", max(1, min(4, (os.cpu_count() or 2))))
    max_upload_mb: int = _env_int("MAX_UPLOAD_MB", 100)
    default_target_bytes: int = _env_int("DEFAULT_TARGET_BYTES", 1_995_000)
    min_target_bytes: int = _env_int("MIN_TARGET_BYTES", 100_000)
    max_target_bytes: int = _env_int("MAX_TARGET_BYTES", 25_000_000)
    file_ttl_seconds: int = _env_int("FILE_TTL_SECONDS", 3600)
    cleanup_interval_seconds: int = _env_int("CLEANUP_INTERVAL_SECONDS", 300)
    storage_dir: str = os.getenv("STORAGE_DIR", "/tmp/pdf2mb")
    cors_origins: tuple[str, ...] = tuple(_env_list("CORS_ORIGINS", "*"))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()


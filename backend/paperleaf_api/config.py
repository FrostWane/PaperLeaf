"""环境配置。

刻意不在导入时连接任何外部服务，保证 CLI、测试与公开演示可以离线启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("PAPERLEAF_MODE", "demo")
    database_url: str = os.getenv(
        "PAPERLEAF_DATABASE_URL", "postgresql+asyncpg://paperleaf:paperleaf@db:5432/paperleaf"
    )
    session_secret: str = os.getenv("PAPERLEAF_SESSION_SECRET", "local-demo-only-change-me")
    session_cookie: str = "paperleaf_session"
    csrf_cookie: str = "paperleaf_csrf"
    session_ttl_seconds: int = int(os.getenv("PAPERLEAF_SESSION_TTL_SECONDS", "604800"))
    secure_cookies: bool = _bool("PAPERLEAF_SECURE_COOKIES", False)
    bootstrap_admin_email: str = os.getenv(
        "PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL", "admin@paperleaf.local"
    )
    bootstrap_admin_password: str = os.getenv(
        "PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD", "paperleaf-dev-admin"
    )
    storage_backend: str = os.getenv("PAPERLEAF_STORAGE_BACKEND", "local")
    local_storage_path: Path = Path(os.getenv("PAPERLEAF_LOCAL_STORAGE_PATH", "data/uploads"))
    max_pdf_bytes: int = int(os.getenv("PAPERLEAF_MAX_PDF_BYTES", str(50 * 1024 * 1024)))
    max_pdf_pages: int = int(os.getenv("PAPERLEAF_MAX_PDF_PAGES", "500"))
    minio_endpoint: str = os.getenv("PAPERLEAF_MINIO_ENDPOINT", "minio:9000")
    minio_access_key: str = os.getenv("PAPERLEAF_MINIO_ACCESS_KEY", "paperleaf")
    minio_secret_key: str = os.getenv("PAPERLEAF_MINIO_SECRET_KEY", "paperleaf-local")
    minio_bucket: str = os.getenv("PAPERLEAF_MINIO_BUCKET", "paperleaf-pdfs")
    minio_secure: bool = _bool("PAPERLEAF_MINIO_SECURE", False)
    cors_origins: str = os.getenv("PAPERLEAF_CORS_ORIGINS", "http://localhost:3000")
    openai_api_key: str | None = os.getenv("PAPERLEAF_OPENAI_API_KEY")
    openai_base_url: str = os.getenv("PAPERLEAF_OPENAI_BASE_URL", "https://api.openai.com/v1")
    chat_model: str = os.getenv("PAPERLEAF_CHAT_MODEL", "gpt-4.1-mini")
    vision_model: str | None = os.getenv("PAPERLEAF_VISION_MODEL")
    embedding_model: str = os.getenv("PAPERLEAF_EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dimensions: int | None = (
        int(os.environ["PAPERLEAF_EMBEDDING_DIMENSIONS"])
        if os.getenv("PAPERLEAF_EMBEDDING_DIMENSIONS")
        else None
    )
    evidence_min_confidence: float = float(os.getenv("PAPERLEAF_EVIDENCE_MIN_CONFIDENCE", "0.35"))
    evidence_min_vector_score: float = float(
        os.getenv("PAPERLEAF_EVIDENCE_MIN_VECTOR_SCORE", "0.35")
    )
    evidence_min_lexical_coverage: float = float(
        os.getenv("PAPERLEAF_EVIDENCE_MIN_LEXICAL_COVERAGE", "0.18")
    )

    @property
    def is_demo(self) -> bool:
        return self.mode in {"demo", "test"}

    @property
    def allowed_origins(self) -> list[str]:
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        if "*" in origins:
            raise RuntimeError("启用凭据时 CORS 不允许使用通配符")
        return origins

    def validate_production(self) -> None:
        quality_values = (
            self.evidence_min_confidence,
            self.evidence_min_vector_score,
            self.evidence_min_lexical_coverage,
        )
        if any(value < 0 or value > 1 for value in quality_values):
            raise RuntimeError("证据质量阈值必须位于 0 到 1 之间")
        if self.mode != "production":
            return
        weak = {"local-demo-only-change-me", "paperleaf-dev-admin", "paperleaf-local"}
        placeholders = (
            self.session_secret.startswith("replace-with-")
            or self.bootstrap_admin_password.startswith("replace-with-")
            or self.minio_secret_key.startswith("replace-with-")
        )
        if (
            self.session_secret in weak
            or self.bootstrap_admin_password in weak
            or placeholders
            or len(self.session_secret) < 32
            or len(self.bootstrap_admin_password) < 12
        ):
            raise RuntimeError("生产模式必须设置强会话密钥和管理员密码")


settings = Settings()

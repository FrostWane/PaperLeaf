"""环境配置。

刻意不在导入时连接任何外部服务，保证 CLI、测试与公开演示可以离线启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MAX_CONFIGURED_MODEL_TIMEOUT_SECONDS = 120.0
MAX_CONFIGURED_ARTIFACT_TIMEOUT_SECONDS = 240.0


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
    embedding_enabled: bool = _bool("PAPERLEAF_EMBEDDING_ENABLED", True)
    embedding_model: str = os.getenv("PAPERLEAF_EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dimensions: int | None = (
        int(os.environ["PAPERLEAF_EMBEDDING_DIMENSIONS"])
        if os.getenv("PAPERLEAF_EMBEDDING_DIMENSIONS")
        else None
    )
    fallback_openai_api_key: str | None = os.getenv("PAPERLEAF_FALLBACK_OPENAI_API_KEY")
    fallback_openai_base_url: str = os.getenv(
        "PAPERLEAF_FALLBACK_OPENAI_BASE_URL", "https://api.openai.com/v1"
    )
    fallback_chat_model: str = os.getenv(
        "PAPERLEAF_FALLBACK_CHAT_MODEL", "gpt-4.1-mini"
    )
    fallback_vision_model: str | None = os.getenv("PAPERLEAF_FALLBACK_VISION_MODEL")
    fallback_embedding_enabled: bool = _bool("PAPERLEAF_FALLBACK_EMBEDDING_ENABLED", True)
    fallback_embedding_model: str = os.getenv(
        "PAPERLEAF_FALLBACK_EMBEDDING_MODEL", "text-embedding-3-small"
    )
    model_timeout_seconds: float = float(os.getenv("PAPERLEAF_MODEL_TIMEOUT_SECONDS", "30"))
    artifact_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_ARTIFACT_TIMEOUT_SECONDS", "120")
    )
    artifact_retry_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_ARTIFACT_RETRY_TIMEOUT_SECONDS", "90")
    )
    structure_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_STRUCTURE_TIMEOUT_SECONDS", "180")
    )
    structure_retry_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_STRUCTURE_RETRY_TIMEOUT_SECONDS", "120")
    )
    model_attempts_per_provider: int = int(
        os.getenv("PAPERLEAF_MODEL_ATTEMPTS_PER_PROVIDER", "1")
    )
    model_circuit_failure_threshold: int = int(
        os.getenv("PAPERLEAF_MODEL_CIRCUIT_FAILURE_THRESHOLD", "3")
    )
    model_circuit_cooldown_seconds: float = float(
        os.getenv("PAPERLEAF_MODEL_CIRCUIT_COOLDOWN_SECONDS", "30")
    )
    evidence_min_confidence: float = float(os.getenv("PAPERLEAF_EVIDENCE_MIN_CONFIDENCE", "0.35"))
    evidence_min_vector_score: float = float(
        os.getenv("PAPERLEAF_EVIDENCE_MIN_VECTOR_SCORE", "0.35")
    )
    evidence_min_lexical_coverage: float = float(
        os.getenv("PAPERLEAF_EVIDENCE_MIN_LEXICAL_COVERAGE", "0.18")
    )
    answer_min_citation_coverage: float = float(
        os.getenv("PAPERLEAF_ANSWER_MIN_CITATION_COVERAGE", "1.0")
    )
    answer_min_claim_lexical_support: float = float(
        os.getenv("PAPERLEAF_ANSWER_MIN_CLAIM_LEXICAL_SUPPORT", "0.12")
    )
    answer_min_support_confidence: float = float(
        os.getenv("PAPERLEAF_ANSWER_MIN_SUPPORT_CONFIDENCE", "0.6")
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
            self.answer_min_citation_coverage,
            self.answer_min_claim_lexical_support,
            self.answer_min_support_confidence,
        )
        if any(value < 0 or value > 1 for value in quality_values):
            raise RuntimeError("证据质量阈值必须位于 0 到 1 之间")
        if self.model_timeout_seconds <= 0:
            raise RuntimeError("模型超时必须大于 0")
        if self.model_timeout_seconds > MAX_CONFIGURED_MODEL_TIMEOUT_SECONDS:
            raise RuntimeError(
                "模型单次超时不能超过 120 秒，以确保明显短于 Worker 租约"
            )
        artifact_timeouts = (
            self.artifact_timeout_seconds,
            self.artifact_retry_timeout_seconds,
            self.structure_timeout_seconds,
            self.structure_retry_timeout_seconds,
        )
        if any(value <= 0 for value in artifact_timeouts):
            raise RuntimeError("论文产物生成超时必须大于 0")
        if any(value > MAX_CONFIGURED_ARTIFACT_TIMEOUT_SECONDS for value in artifact_timeouts):
            raise RuntimeError("论文产物单次超时不能超过 240 秒")
        if self.artifact_retry_timeout_seconds > self.artifact_timeout_seconds:
            raise RuntimeError("论文产物精简重试超时不能大于首次生成超时")
        if self.structure_retry_timeout_seconds > self.structure_timeout_seconds:
            raise RuntimeError("研究脑图精简重试超时不能大于首次生成超时")
        if not 1 <= self.model_attempts_per_provider <= 3:
            raise RuntimeError("单端点模型尝试次数必须位于 1 到 3 之间")
        if self.model_circuit_failure_threshold < 1:
            raise RuntimeError("模型断路器失败阈值必须至少为 1")
        if self.model_circuit_cooldown_seconds <= 0:
            raise RuntimeError("模型断路器冷却时间必须大于 0")
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

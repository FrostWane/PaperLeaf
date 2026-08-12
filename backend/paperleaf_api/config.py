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
    redis_url: str | None = os.getenv("PAPERLEAF_REDIS_URL") or None
    redis_key_prefix: str = os.getenv("PAPERLEAF_REDIS_KEY_PREFIX", "paperleaf")
    redis_timeout_seconds: float = float(os.getenv("PAPERLEAF_REDIS_TIMEOUT_SECONDS", "0.5"))
    agent_rate_limit_requests: int = int(os.getenv("PAPERLEAF_AGENT_RATE_LIMIT_REQUESTS", "12"))
    agent_rate_limit_window_seconds: int = int(
        os.getenv("PAPERLEAF_AGENT_RATE_LIMIT_WINDOW_SECONDS", "60")
    )
    context_engine_enabled: bool = _bool("PAPERLEAF_CONTEXT_ENGINE_ENABLED", False)
    memory_enabled: bool = _bool("PAPERLEAF_MEMORY_ENABLED", False)
    skills_enabled: bool = _bool("PAPERLEAF_SKILLS_ENABLED", False)
    function_tools_enabled: bool = _bool("PAPERLEAF_FUNCTION_TOOLS_ENABLED", False)
    mcp_enabled: bool = _bool("PAPERLEAF_MCP_ENABLED", False)
    multi_agent_enabled: bool = _bool("PAPERLEAF_MULTI_AGENT_ENABLED", False)
    multi_agent_max_branches: int = int(os.getenv("PAPERLEAF_MULTI_AGENT_MAX_BRANCHES", "3"))
    multi_agent_branch_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_MULTI_AGENT_BRANCH_TIMEOUT_SECONDS", "20")
    )
    multi_agent_total_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_MULTI_AGENT_TOTAL_TIMEOUT_SECONDS", "45")
    )
    multi_agent_token_budget: int = int(os.getenv("PAPERLEAF_MULTI_AGENT_TOKEN_BUDGET", "12000"))
    academic_mcp_url: str = os.getenv(
        "PAPERLEAF_ACADEMIC_MCP_URL", "http://academic-search-mcp:8080/mcp"
    )
    academic_mcp_allowed_hosts: str = os.getenv(
        "PAPERLEAF_ACADEMIC_MCP_ALLOWED_HOSTS", "academic-search-mcp"
    )
    mcp_timeout_seconds: float = float(os.getenv("PAPERLEAF_MCP_TIMEOUT_SECONDS", "15"))
    mcp_cache_ttl_seconds: int = int(os.getenv("PAPERLEAF_MCP_CACHE_TTL_SECONDS", "900"))
    mcp_circuit_failure_threshold: int = int(
        os.getenv("PAPERLEAF_MCP_CIRCUIT_FAILURE_THRESHOLD", "3")
    )
    mcp_circuit_cooldown_seconds: int = int(
        os.getenv("PAPERLEAF_MCP_CIRCUIT_COOLDOWN_SECONDS", "60")
    )
    model_context_tokens: int = int(os.getenv("PAPERLEAF_MODEL_CONTEXT_TOKENS", "32768"))
    context_safety_ratio: float = float(os.getenv("PAPERLEAF_CONTEXT_SAFETY_RATIO", "0.10"))
    context_compact_ratio: float = float(os.getenv("PAPERLEAF_CONTEXT_COMPACT_RATIO", "0.70"))
    context_hard_limit_ratio: float = float(os.getenv("PAPERLEAF_CONTEXT_HARD_LIMIT_RATIO", "0.85"))
    context_keep_recent_turns: int = int(os.getenv("PAPERLEAF_CONTEXT_KEEP_RECENT_TURNS", "6"))
    context_max_memories: int = int(os.getenv("PAPERLEAF_CONTEXT_MAX_MEMORIES", "5"))
    context_max_skills: int = int(os.getenv("PAPERLEAF_CONTEXT_MAX_SKILLS", "1"))
    worker_metrics_port: int = int(os.getenv("PAPERLEAF_WORKER_METRICS_PORT", "9101"))
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
    chunk_target_tokens: int = int(os.getenv("PAPERLEAF_CHUNK_TARGET_TOKENS", "700"))
    chunk_overlap_tokens: int = int(os.getenv("PAPERLEAF_CHUNK_OVERLAP_TOKENS", "100"))
    chunk_semantic_unit_tokens: int = int(os.getenv("PAPERLEAF_CHUNK_SEMANTIC_UNIT_TOKENS", "220"))
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
    embedding_provider: str = os.getenv("PAPERLEAF_EMBEDDING_PROVIDER", "auto")
    embedding_model: str = os.getenv("PAPERLEAF_EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dimensions: int | None = (
        int(os.environ["PAPERLEAF_EMBEDDING_DIMENSIONS"])
        if os.getenv("PAPERLEAF_EMBEDDING_DIMENSIONS")
        else None
    )
    embedding_batch_size: int = int(os.getenv("PAPERLEAF_EMBEDDING_BATCH_SIZE", "8"))
    embedding_timeout_seconds: float = float(os.getenv("PAPERLEAF_EMBEDDING_TIMEOUT_SECONDS", "90"))
    embedding_batch_attempts: int = int(os.getenv("PAPERLEAF_EMBEDDING_BATCH_ATTEMPTS", "2"))
    embedding_index_revision: int = int(os.getenv("PAPERLEAF_EMBEDDING_INDEX_REVISION", "1"))
    fallback_openai_api_key: str | None = os.getenv("PAPERLEAF_FALLBACK_OPENAI_API_KEY")
    fallback_openai_base_url: str = os.getenv(
        "PAPERLEAF_FALLBACK_OPENAI_BASE_URL", "https://api.openai.com/v1"
    )
    # 备用端点可能只提供 Embedding（例如本地 Ollama）。聊天模型必须显式配置，
    # 避免把仅向量端点误当成回答服务并制造一次无意义的失败尝试。
    fallback_chat_model: str = os.getenv("PAPERLEAF_FALLBACK_CHAT_MODEL", "")
    fallback_vision_model: str | None = os.getenv("PAPERLEAF_FALLBACK_VISION_MODEL")
    fallback_embedding_enabled: bool = _bool("PAPERLEAF_FALLBACK_EMBEDDING_ENABLED", True)
    fallback_embedding_model: str = os.getenv(
        "PAPERLEAF_FALLBACK_EMBEDDING_MODEL", "text-embedding-3-small"
    )
    model_timeout_seconds: float = float(os.getenv("PAPERLEAF_MODEL_TIMEOUT_SECONDS", "30"))
    agent_answer_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_AGENT_ANSWER_TIMEOUT_SECONDS", "90")
    )
    agent_answer_retry_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_AGENT_ANSWER_RETRY_TIMEOUT_SECONDS", "60")
    )
    agent_evidence_support_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_AGENT_EVIDENCE_SUPPORT_TIMEOUT_SECONDS", "20")
    )
    translation_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_TRANSLATION_TIMEOUT_SECONDS", "90")
    )
    artifact_timeout_seconds: float = float(os.getenv("PAPERLEAF_ARTIFACT_TIMEOUT_SECONDS", "120"))
    artifact_retry_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_ARTIFACT_RETRY_TIMEOUT_SECONDS", "90")
    )
    structure_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_STRUCTURE_TIMEOUT_SECONDS", "180")
    )
    structure_retry_timeout_seconds: float = float(
        os.getenv("PAPERLEAF_STRUCTURE_RETRY_TIMEOUT_SECONDS", "120")
    )
    model_attempts_per_provider: int = int(os.getenv("PAPERLEAF_MODEL_ATTEMPTS_PER_PROVIDER", "1"))
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
        if (
            self.model_timeout_seconds <= 0
            or self.agent_answer_timeout_seconds <= 0
            or self.agent_answer_retry_timeout_seconds <= 0
            or self.agent_evidence_support_timeout_seconds <= 0
            or self.translation_timeout_seconds <= 0
        ):
            raise RuntimeError("模型超时必须大于 0")
        if (
            max(
                self.model_timeout_seconds,
                self.agent_answer_timeout_seconds,
                self.agent_answer_retry_timeout_seconds,
                self.agent_evidence_support_timeout_seconds,
                self.translation_timeout_seconds,
            )
            > MAX_CONFIGURED_MODEL_TIMEOUT_SECONDS
        ):
            raise RuntimeError("模型单次超时不能超过 120 秒，以确保明显短于 Worker 租约")
        if self.agent_answer_retry_timeout_seconds > self.agent_answer_timeout_seconds:
            raise RuntimeError("回答紧凑重试超时不能大于首次回答超时")
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
        if not 1 <= self.multi_agent_max_branches <= 3:
            raise RuntimeError("并行研究分支数必须位于 1 到 3 之间")
        if not 1 <= self.multi_agent_branch_timeout_seconds <= 120:
            raise RuntimeError("单个研究分支超时必须位于 1 到 120 秒之间")
        if (
            not self.multi_agent_branch_timeout_seconds
            <= self.multi_agent_total_timeout_seconds
            <= 180
        ):
            raise RuntimeError("研究编排总超时必须不小于分支超时且不超过 180 秒")
        if self.multi_agent_token_budget < 1000:
            raise RuntimeError("研究编排 Token 预算必须至少为 1000")
        if self.multi_agent_token_budget > self.multi_agent_max_branches * 16_384:
            raise RuntimeError("研究编排 Token 预算超过分支可分配上限")
        if self.multi_agent_total_timeout_seconds < self.multi_agent_branch_timeout_seconds + 2:
            raise RuntimeError("研究编排总超时必须至少预留 2 秒用于合并")
        if not 1 <= self.embedding_batch_size <= 64:
            raise RuntimeError("向量批次大小必须位于 1 到 64 之间")
        if not 1 <= self.embedding_timeout_seconds <= MAX_CONFIGURED_MODEL_TIMEOUT_SECONDS:
            raise RuntimeError("向量批次超时必须位于 1 到 120 秒之间")
        if not 1 <= self.embedding_batch_attempts <= 3:
            raise RuntimeError("向量批次尝试次数必须位于 1 到 3 之间")
        if self.embedding_provider not in {"auto", "primary", "fallback"}:
            raise RuntimeError("向量 Provider 仅支持 auto、primary 或 fallback")
        if self.redis_timeout_seconds <= 0 or self.redis_timeout_seconds > 5:
            raise RuntimeError("Redis 超时必须位于 0 到 5 秒之间")
        if self.agent_rate_limit_requests < 1:
            raise RuntimeError("Agent 限流次数必须至少为 1")
        if not 1 <= self.agent_rate_limit_window_seconds <= 3600:
            raise RuntimeError("Agent 限流窗口必须位于 1 到 3600 秒之间")
        if not self.redis_key_prefix.strip():
            raise RuntimeError("Redis Key 前缀不能为空")
        if not 1024 <= self.worker_metrics_port <= 65535:
            raise RuntimeError("Worker 指标端口必须位于 1024 到 65535 之间")
        if self.model_context_tokens < 4096:
            raise RuntimeError("模型上下文窗口必须至少为 4096 Token")
        if self.multi_agent_enabled and self.multi_agent_token_budget > self.model_context_tokens:
            raise RuntimeError("研究编排 Token 预算不能超过模型上下文窗口")
        ratios = (
            self.context_safety_ratio,
            self.context_compact_ratio,
            self.context_hard_limit_ratio,
        )
        if any(value <= 0 or value >= 1 for value in ratios):
            raise RuntimeError("上下文预算比例必须位于 0 到 1 之间")
        if self.context_compact_ratio >= self.context_hard_limit_ratio:
            raise RuntimeError("主动压缩阈值必须小于硬上限阈值")
        if not 1 <= self.context_keep_recent_turns <= 20:
            raise RuntimeError("保留最近对话轮数必须位于 1 到 20 之间")
        if not 1 <= self.context_max_memories <= 20:
            raise RuntimeError("单轮记忆数量必须位于 1 到 20 之间")
        if self.context_max_skills != 1:
            raise RuntimeError("当前版本每轮只允许加载一个主 Skill")
        if not 1 <= self.mcp_timeout_seconds <= 60:
            raise RuntimeError("MCP 超时必须位于 1 到 60 秒之间")
        if not 60 <= self.mcp_cache_ttl_seconds <= 86400:
            raise RuntimeError("MCP 缓存时间必须位于 60 到 86400 秒之间")
        if self.mcp_circuit_failure_threshold < 1:
            raise RuntimeError("MCP 熔断阈值必须至少为 1")
        if not 1 <= self.mcp_circuit_cooldown_seconds <= 3600:
            raise RuntimeError("MCP 熔断冷却时间必须位于 1 到 3600 秒之间")
        if self.chunk_target_tokens <= 0:
            raise RuntimeError("Chunk 目标长度必须为正数")
        if not 0 <= self.chunk_overlap_tokens < self.chunk_target_tokens:
            raise RuntimeError("Chunk 重叠长度必须小于目标长度")
        if not 1 <= self.chunk_semantic_unit_tokens <= self.chunk_target_tokens:
            raise RuntimeError("语义单元长度必须位于 1 到 Chunk 目标长度之间")
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

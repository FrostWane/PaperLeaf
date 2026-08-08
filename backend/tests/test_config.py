from dataclasses import replace

import pytest

from paperleaf_api.config import settings


def test_production_rejects_example_placeholders_and_short_secrets() -> None:
    candidates = [
        replace(
            settings,
            mode="production",
            session_secret="replace-with-a-random-secret",
            bootstrap_admin_password="strong-admin-password",
        ),
        replace(
            settings,
            mode="production",
            session_secret="short",
            bootstrap_admin_password="strong-admin-password",
        ),
        replace(
            settings,
            mode="production",
            session_secret="x" * 40,
            bootstrap_admin_password="too-short",
        ),
    ]
    for config in candidates:
        try:
            config.validate_production()
        except RuntimeError:
            pass
        else:
            raise AssertionError("弱生产配置必须被拒绝")


def test_evidence_quality_thresholds_must_be_probabilities() -> None:
    with pytest.raises(RuntimeError, match="证据质量阈值"):
        replace(settings, evidence_min_confidence=1.1).validate_production()


def test_answer_quality_thresholds_must_be_probabilities() -> None:
    with pytest.raises(RuntimeError, match="证据质量阈值"):
        replace(settings, answer_min_citation_coverage=-0.1).validate_production()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_timeout_seconds": 0}, "模型超时"),
        ({"model_timeout_seconds": 121}, "不能超过 120 秒"),
        ({"artifact_timeout_seconds": 0}, "论文产物生成超时"),
        ({"artifact_retry_timeout_seconds": 241}, "论文产物单次超时"),
        (
            {"artifact_timeout_seconds": 40, "artifact_retry_timeout_seconds": 45},
            "精简重试超时不能大于",
        ),
        ({"structure_timeout_seconds": 241}, "论文产物单次超时"),
        (
            {"structure_timeout_seconds": 120, "structure_retry_timeout_seconds": 180},
            "研究脑图精简重试超时不能大于",
        ),
        ({"model_attempts_per_provider": 4}, "尝试次数"),
        ({"model_circuit_failure_threshold": 0}, "失败阈值"),
        ({"model_circuit_cooldown_seconds": 0}, "冷却时间"),
        ({"embedding_batch_size": 0}, "向量批次大小"),
        ({"embedding_batch_size": 65}, "向量批次大小"),
        ({"redis_timeout_seconds": 0}, "Redis 超时"),
        ({"redis_timeout_seconds": 6}, "Redis 超时"),
        ({"agent_rate_limit_requests": 0}, "限流次数"),
        ({"agent_rate_limit_window_seconds": 0}, "限流窗口"),
        ({"agent_rate_limit_window_seconds": 3601}, "限流窗口"),
        ({"redis_key_prefix": "   "}, "Key 前缀"),
    ],
)
def test_model_runtime_policy_rejects_invalid_values(changes: dict, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        replace(settings, **changes).validate_production()

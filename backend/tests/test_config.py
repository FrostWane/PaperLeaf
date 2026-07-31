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

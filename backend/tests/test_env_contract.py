import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_environment_contract_is_complete() -> None:
    verifier = _load_script("verify_env_contract.py")
    assert verifier.validate() == []


def test_nondefault_answer_thresholds_are_read_by_api_and_worker() -> None:
    environment = {
        **os.environ,
        "PAPERLEAF_MODE": "test",
        "PAPERLEAF_ANSWER_MIN_CITATION_COVERAGE": "0.91",
        "PAPERLEAF_ANSWER_MIN_CLAIM_LEXICAL_SUPPORT": "0.23",
        "PAPERLEAF_ANSWER_MIN_SUPPORT_CONFIDENCE": "0.74",
    }
    code = (
        "from paperleaf_api.main import settings as api; "
        "from paperleaf_api.worker import settings as worker; "
        "assert (api.answer_min_citation_coverage, api.answer_min_claim_lexical_support, "
        "api.answer_min_support_confidence) == (0.91, 0.23, 0.74); "
        "assert (worker.answer_min_citation_coverage, worker.answer_min_claim_lexical_support, "
        "worker.answer_min_support_confidence) == (0.91, 0.23, 0.74)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT / "backend",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "0.91" not in completed.stdout
    assert "0.23" not in completed.stdout
    assert "0.74" not in completed.stdout


def test_production_preflight_rejects_defaults_and_accepts_hardened_values() -> None:
    preflight = _load_script("production_preflight.py")
    weak = preflight.load_env(ROOT / ".env.example")
    assert preflight.validate(weak)

    hardened = {
        **weak,
        "PAPERLEAF_MODE": "production",
        "PAPERLEAF_SESSION_SECRET": "s" * 64,
        "PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD": "Admin-Strong-Password-123",
        "POSTGRES_PASSWORD": "Database-Strong-Password-123",
        "MINIO_ROOT_PASSWORD": "Storage-Strong-Password-123",
        "GRAFANA_ADMIN_PASSWORD": "Grafana-Strong-Password-123",
        "PAPERLEAF_SECURE_COOKIES": "true",
        "PAPERLEAF_BIND_ADDRESS": "127.0.0.1",
        "NEXT_PUBLIC_API_BASE_URL": "https://paperleaf.example/api/v1",
        "PAPERLEAF_CORS_ORIGINS": "https://paperleaf.example",
        "PAPERLEAF_SPECIALIST_AGENTS_ENABLED": "false",
    }
    assert preflight.validate(hardened) == []
    assert "PAPERLEAF_CORS_ORIGINS 在生产模式只能包含 HTTPS Origin" in preflight.validate(
        {**hardened, "PAPERLEAF_CORS_ORIGINS": ""}
    )

"""验证公开环境变量、Settings 与 Compose 的传递契约。"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / "backend" / "paperleaf_api" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE_FILE = ROOT / "compose.yaml"

SETTINGS_EXCEPTIONS = {
    # Compose 固定使用 MinIO；本地文件存储只用于不经过 Compose 的开发/测试。
    "PAPERLEAF_LOCAL_STORAGE_PATH": "仅非 Compose 本地开发",
    # 以下 Settings 由 Compose 内部拓扑或基础设施变量派生，不要求用户重复填写。
    "PAPERLEAF_DATABASE_URL": "由 POSTGRES_* 组合",
    "PAPERLEAF_STORAGE_BACKEND": "Compose 固定为 MinIO",
    "PAPERLEAF_MINIO_ENDPOINT": "Compose 固定为 minio:9000",
    "PAPERLEAF_MINIO_ACCESS_KEY": "由 MINIO_ROOT_USER 派生",
    "PAPERLEAF_MINIO_SECRET_KEY": "由 MINIO_ROOT_PASSWORD 派生",
    "PAPERLEAF_MINIO_SECURE": "容器私有网络固定为 HTTP",
}
NON_SETTINGS_PUBLIC = {
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "GRAFANA_ADMIN_USER",
    "GRAFANA_ADMIN_PASSWORD",
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "NEXT_PUBLIC_API_BASE_URL",
    "NEXT_PUBLIC_GRAFANA_URL",
    "PAPERLEAF_BIND_ADDRESS",
    "PAPERLEAF_WEB_PORT",
    "PAPERLEAF_API_PORT",
    "PAPERLEAF_MINIO_PORT",
    "PAPERLEAF_MINIO_CONSOLE_PORT",
    "PAPERLEAF_PROMETHEUS_PORT",
    "PAPERLEAF_GRAFANA_PORT",
    "PAPERLEAF_PROMETHEUS_IMAGE",
    "PAPERLEAF_GRAFANA_BASE_IMAGE",
    "PAPERLEAF_REDIS_MAXMEMORY",
    "PAPERLEAF_ACADEMIC_HTTP_TIMEOUT_SECONDS",
    "PAPERLEAF_GIT_SHA",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
}
SETTINGS_REFERENCED_OUTSIDE_DATACLASS = {
    "PAPERLEAF_GIT_SHA",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
}


def settings_variables() -> set[str]:
    tree = ast.parse(SETTINGS_FILE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "getenv" and node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_bool"
        ):
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value))
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
        ):
            found.add(str(node.slice.value))
    return {
        item
        for item in found
        if item.startswith("PAPERLEAF_") or item.startswith("LANGSMITH_")
    }


def env_example_variables() -> set[str]:
    result: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line.strip())
        if match:
            result.add(match.group(1))
    return result


def compose_contract() -> tuple[set[str], set[str], set[str]]:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    backend_environment = set((document.get("x-backend-environment") or {}).keys())
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
    services = document["services"]
    api_environment = set((services["api"].get("environment") or {}).keys())
    worker_environment = set((services["worker"].get("environment") or {}).keys())
    if api_environment != backend_environment or worker_environment != backend_environment:
        raise AssertionError("API 与 Worker 必须继承完全相同的后端环境变量契约")
    return backend_environment, referenced, api_environment & worker_environment


def validate() -> list[str]:
    settings_vars = settings_variables()
    example_vars = env_example_variables()
    backend_vars, compose_refs, shared_backend_vars = compose_contract()
    errors: list[str] = []

    for name in sorted(settings_vars - example_vars - SETTINGS_EXCEPTIONS.keys()):
        errors.append(f"Settings 变量未写入 .env.example：{name}")
    for name in sorted(settings_vars - backend_vars - SETTINGS_EXCEPTIONS.keys()):
        errors.append(f"Settings 变量未传入 Compose API/Worker：{name}")
    for name in sorted(example_vars - settings_vars - NON_SETTINGS_PUBLIC):
        errors.append(f".env.example 变量没有 Settings 或 Compose 用途声明：{name}")
    for name in sorted(NON_SETTINGS_PUBLIC - compose_refs):
        errors.append(f"Compose/基础设施变量未被 compose.yaml 引用：{name}")
    for name in sorted(backend_vars - settings_vars - SETTINGS_REFERENCED_OUTSIDE_DATACLASS):
        errors.append(f"Compose 后端变量未被 Settings 读取：{name}")

    required_thresholds = {
        "PAPERLEAF_ANSWER_MIN_CITATION_COVERAGE",
        "PAPERLEAF_ANSWER_MIN_CLAIM_LEXICAL_SUPPORT",
        "PAPERLEAF_ANSWER_MIN_SUPPORT_CONFIDENCE",
    }
    missing = required_thresholds - shared_backend_vars
    if missing:
        errors.append(f"回答门禁未同时传入 API/Worker：{', '.join(sorted(missing))}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("环境变量契约通过：.env.example、Settings、Compose API/Worker 一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

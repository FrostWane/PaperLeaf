"""无密钥回显的生产部署前检查。"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path
from urllib.parse import urlparse

PLACEHOLDERS = ("replace-with-", "change-me", "paperleaf-local", "paperleaf-dev")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _strong(values: dict[str, str], name: str, minimum: int) -> bool:
    value = values.get(name, "")
    lowered = value.lower()
    return len(value) >= minimum and not any(item in lowered for item in PLACEHOLDERS)


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if values.get("PAPERLEAF_MODE") != "production":
        errors.append("PAPERLEAF_MODE 必须为 production")
    for name, minimum in (
        ("PAPERLEAF_SESSION_SECRET", 64),
        ("PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD", 12),
        ("POSTGRES_PASSWORD", 16),
        ("MINIO_ROOT_PASSWORD", 16),
        ("GRAFANA_ADMIN_PASSWORD", 16),
    ):
        if not _strong(values, name, minimum):
            errors.append(f"{name} 必须使用非默认强随机值")
    if values.get("PAPERLEAF_SECURE_COOKIES", "").lower() != "true":
        errors.append("PAPERLEAF_SECURE_COOKIES 必须为 true")

    bind = values.get("PAPERLEAF_BIND_ADDRESS", "127.0.0.1")
    try:
        is_loopback = ipaddress.ip_address(bind).is_loopback
    except ValueError:
        is_loopback = bind.lower() == "localhost"
    if not is_loopback:
        errors.append("Compose 管理端口必须绑定回环地址；公网入口应由 HTTPS 反向代理提供")

    for name in ("NEXT_PUBLIC_API_BASE_URL",):
        parsed = urlparse(values.get(name, ""))
        if parsed.scheme != "https":
            errors.append(f"{name} 在生产模式必须使用 HTTPS")
    origins = [
        item.strip()
        for item in values.get("PAPERLEAF_CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    if not origins or any(urlparse(item).scheme != "https" for item in origins):
        errors.append("PAPERLEAF_CORS_ORIGINS 在生产模式只能包含 HTTPS Origin")
    if values.get("PAPERLEAF_SPECIALIST_AGENTS_ENABLED", "false").lower() != "false":
        errors.append("v0.9.0 发布配置必须保持 PAPERLEAF_SPECIALIST_AGENTS_ENABLED=false")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperLeaf 生产配置预检")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if not args.env_file.is_file():
        print(f"ERROR: 环境文件不存在：{args.env_file}")
        return 2
    errors = validate(load_env(args.env_file))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("生产配置预检通过（未输出任何密钥值）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

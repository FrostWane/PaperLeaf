#!/usr/bin/env python3
"""创建、验证并销毁独立的 PaperLeaf Compose release smoke 环境。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run(command: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=capture,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:] if capture else ""
        raise SmokeError(f"命令失败（{completed.returncode}）：{' '.join(command[:4])}\n{detail}")
    return completed.stdout.strip() if capture else ""


def ready_payload(api_url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{api_url}/ready", timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def wait_readiness(api_url: str, *, ready: bool, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        status, payload = ready_payload(api_url)
        last = payload
        is_ready = status == 200 and payload.get("agent_ready") is True
        worker_degraded = (
            payload.get("components", {}).get("worker", {}).get("status") == "degraded"
        )
        if is_ready == ready and (is_ready or worker_degraded):
            return payload
        time.sleep(1)
    raise SmokeError(f"readiness 未达到预期 ready={ready}：{last}")


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def compose_base(project: str, env_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "compose.smoke.yaml"),
    ]


def execute(output: Path) -> dict:
    project = f"paperleaf-smoke-{secrets.token_hex(4)}"
    if not project.startswith("paperleaf-smoke-"):
        raise SmokeError("拒绝使用非隔离 Compose project")
    api_port, web_port, minio_port, minio_console = (free_port() for _ in range(4))
    prometheus_port, grafana_port = free_port(), free_port()
    admin_email = f"smoke-{secrets.token_hex(4)}@paperleaf.invalid"
    admin_password = secrets.token_urlsafe(28)
    git_sha = run(["git", "rev-parse", "HEAD"], capture=True)
    started_at = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="paperleaf-release-smoke-") as temp_dir:
        temporary = Path(temp_dir)
        env_file = temporary / "smoke.env"
        state_file = temporary / "state.json"
        env_file.write_text(
            "\n".join(
                [
                    "PAPERLEAF_MODE=smoke",
                    f"PAPERLEAF_GIT_SHA={git_sha}",
                    "PAPERLEAF_BIND_ADDRESS=127.0.0.1",
                    f"PAPERLEAF_API_PORT={api_port}",
                    f"PAPERLEAF_WEB_PORT={web_port}",
                    f"PAPERLEAF_MINIO_PORT={minio_port}",
                    f"PAPERLEAF_MINIO_CONSOLE_PORT={minio_console}",
                    f"PAPERLEAF_PROMETHEUS_PORT={prometheus_port}",
                    f"PAPERLEAF_GRAFANA_PORT={grafana_port}",
                    "POSTGRES_DB=paperleaf_smoke",
                    "POSTGRES_USER=paperleaf_smoke",
                    f"POSTGRES_PASSWORD={secrets.token_urlsafe(28)}",
                    "MINIO_ROOT_USER=paperleafsmoke",
                    f"MINIO_ROOT_PASSWORD={secrets.token_urlsafe(28)}",
                    f"PAPERLEAF_SESSION_SECRET={secrets.token_urlsafe(64)}",
                    "PAPERLEAF_SECURE_COOKIES=false",
                    f"PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL={admin_email}",
                    f"PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD={admin_password}",
                    f"GRAFANA_ADMIN_PASSWORD={secrets.token_urlsafe(28)}",
                    f"PAPERLEAF_REDIS_KEY_PREFIX={project}",
                    f"PAPERLEAF_CORS_ORIGINS=http://127.0.0.1:{web_port}",
                    f"NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:{api_port}/api/v1",
                    "PAPERLEAF_SPECIALIST_AGENTS_ENABLED=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        compose = compose_base(project, env_file)
        cleaned = False
        try:
            run(
                [
                    *compose,
                    "up",
                    "-d",
                    "--build",
                    "--wait",
                    "--wait-timeout",
                    "360",
                    "api",
                    "worker",
                    "web",
                ]
            )
            smoke_env = {
                **os.environ,
                "PAPERLEAF_SMOKE_API_URL": f"http://127.0.0.1:{api_port}",
                "PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL": admin_email,
                "PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
                "PAPERLEAF_SMOKE_TIMEOUT_SECONDS": "180",
            }
            run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "full_stack_smoke.py"),
                    "--state-output",
                    str(state_file),
                ],
                env=smoke_env,
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))

            database_raw = run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-m",
                    "paperleaf_api.smoke_database_audit",
                    "--paper-id",
                    state["paper_id"],
                    "--user-id",
                    state["user_id"],
                    "--run-id",
                    state["run_id"],
                ],
                capture=True,
            )
            database = json.loads(database_raw.splitlines()[-1])

            browser_env = {
                **os.environ,
                "PAPERLEAF_SMOKE_WEB_URL": f"http://127.0.0.1:{web_port}",
                "PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL": admin_email,
                "PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
                "PAPERLEAF_SMOKE_USER_EMAIL": state["user_email"],
                "PAPERLEAF_SMOKE_USER_PASSWORD": state["user_password"],
                "PAPERLEAF_SMOKE_PAPER_ID": state["paper_id"],
                "PAPERLEAF_SMOKE_CITATION_PAGE": str(state["citation_page"]),
            }
            playwright = shutil.which("playwright")
            if not playwright:
                candidate = ROOT / "node_modules" / ".bin" / (
                    "playwright.CMD" if os.name == "nt" else "playwright"
                )
                playwright = str(candidate) if candidate.is_file() else None
            if not playwright:
                raise SmokeError("未安装 Playwright CLI；请先执行 pnpm install")
            run(
                [
                    playwright,
                    "test",
                    "--config=playwright.full-stack.config.ts",
                ],
                env=browser_env,
            )

            run([*compose, "stop", "worker"])
            degraded = wait_readiness(f"http://127.0.0.1:{api_port}", ready=False, timeout=45)
            run([*compose, "start", "worker"])
            recovered = wait_readiness(f"http://127.0.0.1:{api_port}", ready=True, timeout=60)
            images_text = run([*compose, "images", "--format", "json"], capture=True)
            parsed_images = json.loads(images_text) if images_text.strip() else []
            image_records = parsed_images if isinstance(parsed_images, list) else [parsed_images]

            result = {
                "schema_version": 1,
                "status": "passed",
                "evidence_level": "isolated_compose_deterministic_openai_stub",
                "not_a_model_quality_evaluation": True,
                "git_sha": git_sha,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "compose_project_hash": sha12(project),
                "isolated_volumes": True,
                "random_secrets": True,
                "specialist_v3_enabled": False,
                "api": {
                    "paper_id_hash": sha12(state["paper_id"]),
                    "run_id_hash": sha12(state["run_id"]),
                    "run_status": state["run_status"],
                    "answer_nonempty": state["answer_nonempty"],
                    "citation_count": state["citation_count"],
                    "range_ok": state["range_ok"],
                    "admin_observability_ok": state["admin_observability_ok"],
                    "ordinary_user_flow_ok": state["ordinary_user_flow_ok"],
                },
                "sse": {
                    "initial_sequences": state["initial_event_sequences"],
                    "resumed_sequences": state["resumed_event_sequences"],
                    "last_event_id_no_duplicates": all(
                        item > state["initial_event_sequences"][-1]
                        for item in state["resumed_event_sequences"]
                    ),
                },
                "database": database,
                "frontend": {"physical_page_jump": "passed", "browser": "chromium"},
                "worker_readiness": {
                    "after_stop": degraded.get("components", {}).get("worker"),
                    "after_restart": recovered.get("components", {}).get("worker"),
                },
                "images": [
                    {
                        "service": item.get("Service"),
                        "repository": item.get("Repository"),
                        "tag": item.get("Tag"),
                        "id": item.get("ID"),
                    }
                    for item in image_records
                ],
            }
        finally:
            try:
                run([*compose, "down", "-v", "--remove-orphans", "--timeout", "15"])
                cleaned = True
            finally:
                if env_file.exists():
                    env_file.unlink()
        if not cleaned:
            raise SmokeError("隔离 Compose 环境未能清理")
        result["cleanup"] = "completed"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperLeaf 隔离 full-stack smoke")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "release-evidence" / "v0.9.0" / "full-stack-smoke.json",
    )
    args = parser.parse_args()
    try:
        result = execute(args.output.resolve())
    except (SmokeError, OSError, json.JSONDecodeError) as exc:
        print(f"隔离 full-stack smoke 失败：{exc}", file=sys.stderr)
        return 1
    print(f"隔离 full-stack smoke 通过：{args.output}（{result['git_sha'][:12]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""在两个隔离 Compose project 间演练 PostgreSQL + MinIO 备份恢复。"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from backup_restore import create_backup, file_sha256, restore_backup, verify_backup
from run_isolated_full_stack_smoke import (
    ROOT,
    compose_base,
    free_port,
    run,
    sha12,
    wait_readiness,
)
from smoke_compose import PaperLeafClient, SmokeConfig


def execute(output: Path) -> dict:
    nonce = secrets.token_hex(4)
    source_project = f"paperleaf-backup-source-{nonce}"
    restore_project = f"paperleaf-backup-restore-{nonce}"
    api_port, web_port, minio_port, minio_console = (free_port() for _ in range(4))
    prometheus_port, grafana_port = free_port(), free_port()
    admin_email = f"backup-{nonce}@paperleaf.invalid"
    admin_password = secrets.token_urlsafe(28)
    git_sha = run(["git", "rev-parse", "HEAD"], capture=True)
    started_at = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="paperleaf-backup-drill-") as temp_dir:
        temporary = Path(temp_dir)
        env_file = temporary / "drill.env"
        state_file = temporary / "state.json"
        backup_dir = temporary / "backup"
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
                    "POSTGRES_DB=paperleaf_backup_drill",
                    "POSTGRES_USER=paperleaf_backup_drill",
                    f"POSTGRES_PASSWORD={secrets.token_urlsafe(28)}",
                    "MINIO_ROOT_USER=paperleafbackup",
                    f"MINIO_ROOT_PASSWORD={secrets.token_urlsafe(28)}",
                    f"PAPERLEAF_SESSION_SECRET={secrets.token_urlsafe(64)}",
                    "PAPERLEAF_SECURE_COOKIES=false",
                    f"PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL={admin_email}",
                    f"PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD={admin_password}",
                    f"GRAFANA_ADMIN_PASSWORD={secrets.token_urlsafe(28)}",
                    f"PAPERLEAF_REDIS_KEY_PREFIX={source_project}",
                    f"PAPERLEAF_CORS_ORIGINS=http://127.0.0.1:{web_port}",
                    f"NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:{api_port}/api/v1",
                    "PAPERLEAF_SPECIALIST_AGENTS_ENABLED=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        source_compose = compose_base(source_project, env_file)
        restore_compose = compose_base(restore_project, env_file)
        source_cleaned = restore_cleaned = False
        try:
            run(
                [
                    *source_compose,
                    "up",
                    "-d",
                    "--build",
                    "--wait",
                    "--wait-timeout",
                    "360",
                    "api",
                    "worker",
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
            backup_manifest = create_backup(source_project, env_file, backup_dir)
            verified_manifest = verify_backup(backup_dir)
            for service in ("api", "worker", "migrate", "model-stub"):
                run(
                    [
                        "docker",
                        "tag",
                        f"{source_project}-{service}:latest",
                        f"{restore_project}-{service}:latest",
                    ]
                )
            run([*source_compose, "down", "-v", "--remove-orphans", "--timeout", "15"])
            source_cleaned = True

            restore_started = time.perf_counter()
            restore_record = restore_backup(restore_project, env_file, backup_dir)
            run(
                [
                    *restore_compose,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "360",
                    "api",
                    "worker",
                ]
            )
            ready = wait_readiness(f"http://127.0.0.1:{api_port}", ready=True, timeout=90)
            rto_seconds = round(time.perf_counter() - restore_started, 3)

            # 恢复校验必须使用全新的浏览器会话。源环境的客户端最后以管理员身份
            # 查看过观测面板；复用 CookieJar 会把“管理员默认不能读取用户 PDF”
            # 误判为对象存储恢复失败。
            client = PaperLeafClient(
                SmokeConfig(
                    api_url=f"http://127.0.0.1:{api_port}",
                    admin_email=admin_email,
                    admin_password=admin_password,
                    timeout_seconds=90,
                    poll_seconds=1,
                    http_timeout_seconds=60,
                )
            )
            client.json(
                "POST",
                "/api/v1/auth/login",
                payload={"email": state["user_email"], "password": state["user_password"]},
                sensitive=True,
            )
            status, payload, headers = client.request(
                "GET",
                f"/api/v1/papers/{state['paper_id']}/file",
                headers={"Range": "bytes=0-7"},
                expected={206},
            )
            content_range = next(
                (value for key, value in headers.items() if key.casefold() == "content-range"),
                "",
            )
            range_ok = status == 206 and payload.startswith(b"%PDF-") and content_range.startswith(
                "bytes 0-7/"
            )
            database_raw = run(
                [
                    *restore_compose,
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
            database_ok = bool(
                database.get("ownership_ok")
                and database.get("paper_exists")
                and int(database.get("page_count", 0) or 0) > 0
                and int(database.get("chunk_count", 0) or 0) > 0
                and int(database.get("citation_count", 0) or 0) > 0
                and database.get("citation_count") == database.get("valid_citation_count")
                and database.get("run_status") == "completed"
            )
            if not range_ok or not database_ok:
                raise RuntimeError("恢复后的 PostgreSQL/MinIO 交叉校验失败")

            result = {
                "schema_version": 1,
                "status": "passed",
                "evidence_level": "isolated_compose_deterministic_stub",
                "git_sha": git_sha,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source_project_hash": sha12(source_project),
                "restore_project_hash": sha12(restore_project),
                "backup": {
                    "strategy": backup_manifest["strategy"],
                    "duration_seconds": backup_manifest["duration_seconds"],
                    "manifest_sha256": file_sha256(backup_dir / "manifest.json"),
                    "file_count": len(verified_manifest["files"]),
                },
                "rpo": {
                    "seconds": 0,
                    "scope": "计划停写窗口内已确认写入",
                    "requires_quiesce": True,
                },
                "rto": {
                    "seconds": rto_seconds,
                    "endpoint": "/ready",
                    "agent_ready": ready.get("agent_ready") is True,
                    "restore_data_seconds": restore_record["restore_data_seconds"],
                },
                "verification": {
                    "database": database,
                    "minio_pdf_range": range_ok,
                    "run_status": state["run_status"],
                    "citation_count": state["citation_count"],
                },
                "specialist_v3_enabled": False,
                "limitations": [
                    "采用计划停写的一致备份，不是在线连续备份或 PITR。",
                    "RTO 来自本机单次隔离演练，不是生产 SLA。",
                    "模型为确定性 stub，本演练不评价回答质量。",
                ],
            }
        finally:
            if not source_cleaned:
                run([*source_compose, "down", "-v", "--remove-orphans", "--timeout", "15"])
                source_cleaned = True
            run([*restore_compose, "down", "-v", "--remove-orphans", "--timeout", "15"])
            restore_cleaned = True
        if not source_cleaned or not restore_cleaned:
            raise RuntimeError("隔离备份恢复环境未清理")
        result["cleanup"] = "completed"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result


def main() -> int:
    output = ROOT / "docs" / "release-evidence" / "v0.9.0" / "backup-restore.json"
    try:
        result = execute(output)
    except Exception as exc:
        print(f"备份恢复演练失败：{exc}", file=sys.stderr)
        return 1
    print(f"备份恢复演练通过：{output}（RTO {result['rto']['seconds']} 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

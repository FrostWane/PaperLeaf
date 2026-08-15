#!/usr/bin/env python3
"""PostgreSQL + MinIO 一致备份与隔离恢复工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackupError(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def compose_command(project: str, env_file: Path, *arguments: str) -> list[str]:
    if not project or any(item in project for item in ("/", "\\", "..")):
        raise BackupError("Compose project 名称不安全")
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
        *arguments,
    ]


def run(command: list[str], *, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "")[-1600:] if capture else ""
        raise BackupError(f"Docker 备份命令失败（{completed.returncode}）：{detail}")
    return completed.stdout.strip() if capture else ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def verify_backup(source: Path) -> dict:
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("备份缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    actual = snapshot_files(source)
    if expected != actual:
        raise BackupError("备份文件 SHA-256 与 manifest 不一致")
    if "postgres.dump" not in actual or not any(name.startswith("minio/") for name in actual):
        raise BackupError("备份必须同时包含 PostgreSQL dump 与 MinIO 数据")
    return manifest


def create_backup(project: str, env_file: Path, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("备份目标必须为空目录")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "minio").mkdir()
    env = load_env(env_file)
    database = env.get("POSTGRES_DB", "paperleaf")
    user = env.get("POSTGRES_USER", "paperleaf")
    def compose(*args: str) -> list[str]:
        return compose_command(project, env_file, *args)
    started = time.perf_counter()
    quiesced_at = datetime.now(timezone.utc)
    services_stopped = False
    try:
        run(compose("stop", "api", "worker"))
        run(compose("stop", "minio"))
        services_stopped = True
        run(
            compose(
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                user,
                "-d",
                database,
                "-Fc",
                "-f",
                "/tmp/paperleaf-release.dump",
            )
        )
        run(
            compose(
                "cp",
                "postgres:/tmp/paperleaf-release.dump",
                str(destination / "postgres.dump"),
            )
        )
        run(compose("cp", "minio:/data/.", str(destination / "minio")))
        run(compose("exec", "-T", "postgres", "rm", "-f", "/tmp/paperleaf-release.dump"))
        files = snapshot_files(destination)
        manifest = {
            "schema_version": 1,
            "status": "completed",
            "strategy": "quiesced_postgresql_custom_dump_plus_minio_volume_snapshot",
            "quiesced_at": quiesced_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "rpo_contract": "0 acknowledged writes during the planned quiesce window",
            "files": files,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        verify_backup(destination)
        return manifest
    finally:
        if services_stopped:
            run(compose("start", "minio"))
            run(compose("start", "api", "worker"))


def restore_backup(project: str, env_file: Path, source: Path) -> dict:
    source = source.resolve()
    verify_backup(source)
    env = load_env(env_file)
    database = env.get("POSTGRES_DB", "paperleaf")
    user = env.get("POSTGRES_USER", "paperleaf")
    def compose(*args: str) -> list[str]:
        return compose_command(project, env_file, *args)
    started = time.perf_counter()
    run(compose("up", "-d", "--wait", "--wait-timeout", "240", "postgres", "redis"))
    run(compose("create", "minio"))
    run(compose("cp", f"{source / 'minio'}/.", "minio:/data"))
    run(compose("start", "minio"))
    run(compose("cp", str(source / "postgres.dump"), "postgres:/tmp/paperleaf-release.dump"))
    run(
        compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            user,
            "-d",
            database,
            "--clean",
            "--if-exists",
            "/tmp/paperleaf-release.dump",
        )
    )
    run(compose("exec", "-T", "postgres", "rm", "-f", "/tmp/paperleaf-release.dump"))
    return {
        "status": "restored",
        "restored_at": datetime.now(timezone.utc).isoformat(),
        "restore_data_seconds": round(time.perf_counter() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperLeaf 一致备份/恢复")
    parser.add_argument("action", choices=["backup", "restore", "verify"])
    parser.add_argument("--project")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = verify_backup(args.path)
        else:
            if not args.project or not args.env_file:
                raise BackupError("backup/restore 必须提供 --project 与 --env-file")
            result = (
                create_backup(args.project, args.env_file, args.path)
                if args.action == "backup"
                else restore_backup(args.project, args.env_file, args.path)
            )
    except (BackupError, OSError, json.JSONDecodeError) as exc:
        print(f"备份恢复失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

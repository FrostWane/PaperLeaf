import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    path = ROOT / "scripts" / "backup_restore.py"
    spec = importlib.util.spec_from_file_location("backup_restore", path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_backup_manifest_detects_tampering(tmp_path) -> None:
    backup = module()
    (tmp_path / "minio").mkdir()
    (tmp_path / "postgres.dump").write_bytes(b"postgres")
    (tmp_path / "minio" / "object.bin").write_bytes(b"minio")
    manifest = {"files": backup.snapshot_files(tmp_path)}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert backup.verify_backup(tmp_path)["files"] == manifest["files"]
    (tmp_path / "minio" / "object.bin").write_bytes(b"changed")
    with pytest.raises(backup.BackupError, match="SHA-256"):
        backup.verify_backup(tmp_path)


def test_compose_project_rejects_path_traversal(tmp_path) -> None:
    backup = module()
    with pytest.raises(backup.BackupError, match="不安全"):
        backup.compose_command("../production", tmp_path / ".env", "ps")


def test_smoke_database_audit_contract_matches_restore_drill() -> None:
    source = (ROOT / "scripts" / "run_backup_restore_drill.py").read_text(encoding="utf-8")
    assert 'database["checks"]' not in source
    assert source.index("client = PaperLeafClient(") > source.index(
        "restore_record = restore_backup("
    )
    for field in (
        "ownership_ok",
        "paper_exists",
        "page_count",
        "chunk_count",
        "citation_count",
        "valid_citation_count",
        "run_status",
    ):
        assert f'database.get("{field}")' in source or f'database.get("{field}",' in source

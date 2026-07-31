import asyncio
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import JobStatus, UserRole
from paperleaf_api.repository import MemoryRepository
from paperleaf_api.storage import LocalObjectStorage


def _login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    token = client.cookies.get("paperleaf_csrf")
    assert token
    return token


def test_auth_paper_contract_and_cross_user_isolation(tmp_path, valid_pdf_bytes: bytes) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))

    with TestClient(app) as admin_client:
        assert admin_client.get("/health").json()["status"] == "ok"
        csrf = _login(admin_client, "admin@example.com", "admin-password-123")
        created = admin_client.post(
            "/api/v1/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "reader@example.com",
                "temporary_password": "reader-password-123",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        upload = admin_client.post(
            "/api/v1/papers",
            headers={"X-CSRF-Token": csrf},
            data={"title": "测试论文"},
            files={"file": ("paper.pdf", valid_pdf_bytes, "application/pdf")},
        )
        assert upload.status_code == 201
        paper_id = upload.json()["id"]

    with TestClient(app) as reader_client:
        csrf = _login(reader_client, "reader@example.com", "reader-password-123")
        blocked = reader_client.get("/api/v1/papers")
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"
        assert reader_client.get("/api/v1/auth/me").status_code == 200
        changed = reader_client.post(
            "/api/v1/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "reader-password-123",
                "new_password": "reader-new-password-456",
            },
        )
        assert changed.status_code == 200
        assert reader_client.get(f"/api/v1/papers/{paper_id}").status_code == 404
        assert reader_client.get("/api/v1/papers").json() == []
        isolated_bulk = reader_client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": reader_client.cookies.get("paperleaf_csrf")},
            json={"paper_ids": [paper_id], "action": "archive"},
        )
        assert isolated_bulk.status_code == 404


def test_collection_tag_and_admin_job_contract(tmp_path, valid_pdf_bytes: bytes) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))

    with TestClient(app) as client:
        csrf = _login(client, "admin@example.com", "admin-password-123")
        paper = client.post(
            "/api/v1/papers",
            headers={"X-CSRF-Token": csrf},
            files={"file": ("paper.pdf", valid_pdf_bytes, "application/pdf")},
        ).json()
        collection = client.post(
            "/api/v1/collections",
            headers={"X-CSRF-Token": csrf},
            json={"name": "RAG", "description": "检索增强生成"},
        )
        tag = client.post(
            "/api/v1/tags",
            headers={"X-CSRF-Token": csrf},
            json={"name": "待读", "color": "#AFC3CE"},
        )
        assert collection.status_code == 201
        assert tag.status_code == 201
        collection_id, tag_id = collection.json()["id"], tag.json()["id"]
        assert (
            client.post(
                f"/api/v1/collections/{collection_id}/papers/{paper['id']}",
                headers={"X-CSRF-Token": csrf},
            ).json()["assigned"]
            is True
        )
        assert (
            client.post(
                f"/api/v1/tags/{tag_id}/papers/{paper['id']}",
                headers={"X-CSRF-Token": csrf},
            ).json()["assigned"]
            is True
        )
        collections = client.get("/api/v1/collections").json()
        tags = client.get("/api/v1/tags").json()
        assert collections[0]["paper_ids"] == [paper["id"]]
        assert tags[0]["paper_ids"] == [paper["id"]]

        opened = client.post(
            f"/api/v1/papers/{paper['id']}/opened",
            headers={"X-CSRF-Token": csrf},
        )
        assert opened.status_code == 200
        assert opened.json()["last_opened_at"] is not None

        archived = client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": csrf},
            json={"paper_ids": [paper["id"], paper["id"]], "action": "archive"},
        )
        assert archived.status_code == 200
        assert archived.json() == {
            "action": "archive",
            "affected": 1,
            "paper_ids": [paper["id"]],
        }
        assert client.get(f"/api/v1/papers/{paper['id']}").json()["archived_at"] is not None

        organized = client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": csrf},
            json={
                "paper_ids": [paper["id"]],
                "action": "remove_tag",
                "target_id": tag_id,
            },
        )
        assert organized.status_code == 200
        assert client.get("/api/v1/tags").json()[0]["paper_ids"] == []

        job = next(iter(repository.jobs.values()))
        job.status = JobStatus.failed
        job.error_code = "PDF_PARSE_FAILED"
        jobs = client.get("/api/v1/admin/jobs")
        assert jobs.status_code == 200
        assert "error_message" not in jobs.json()[0]
        assert "text" not in jobs.json()[0]
        retried = client.post(f"/api/v1/admin/jobs/{job.id}/retry", headers={"X-CSRF-Token": csrf})
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"


def test_agent_thread_is_user_run_scoped_and_resume_survives_app_rebuild(tmp_path) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)

    async def seed_users():
        owner = await repository.create_user(
            "owner@example.com",
            "owner-password-123",
            UserRole.user,
            must_change_password=False,
        )
        other = await repository.create_user(
            "other@example.com",
            "other-password-123",
            UserRole.user,
            must_change_password=False,
        )
        return owner, other

    owner, other = asyncio.run(seed_users())
    app_before_restart = create_app(
        config, repository=repository, storage=LocalObjectStorage(tmp_path)
    )

    with TestClient(app_before_restart) as owner_client:
        owner_csrf = _login(owner_client, "owner@example.com", "owner-password-123")
        response = owner_client.post(
            "/api/v1/chat/sessions/default/messages",
            headers={"X-CSRF-Token": owner_csrf},
            json={"content": "什么是 RAG？", "scope": "library", "web_enabled": False},
        )
        assert response.status_code == 200
        assert "event: tool_finished" in response.text
        assert '"evidence_quality"' in response.text
        assert '"paper_title"' in response.text
        owner_run = next(
            record for record in repository.agent_runs.values() if record.user_id == owner.id
        )
        assert owner_run.result_summary["evidence_quality"]["grade"] == "sufficient"

    with TestClient(app_before_restart) as other_client:
        other_csrf = _login(other_client, "other@example.com", "other-password-123")
        response = other_client.post(
            "/api/v1/chat/sessions/default/messages",
            headers={"X-CSRF-Token": other_csrf},
            json={"content": "什么是 RAG？", "scope": "library", "web_enabled": False},
        )
        assert response.status_code == 200
        other_run = next(
            record for record in repository.agent_runs.values() if record.user_id == other.id
        )
        assert other_client.get(f"/api/v1/agent/runs/{owner_run.id}").status_code == 404

    assert owner_run.thread_id == f"{owner.id}:default:{owner_run.id}"
    assert other_run.thread_id == f"{other.id}:default:{other_run.id}"
    assert owner_run.thread_id != other_run.thread_id

    interrupted_id = str(uuid.uuid4())
    action_id = str(uuid.uuid4())

    async def seed_interrupted_run():
        await repository.create_agent_run(
            interrupted_id,
            owner.id,
            "default",
            f"{owner.id}:default:{interrupted_id}",
        )
        await repository.update_owned_agent_run(
            interrupted_id,
            owner.id,
            status="interrupted",
            pending_action={"action_id": action_id, "type": "confirm_arxiv_import"},
            result_summary={"answer": "", "citations": []},
        )

    asyncio.run(seed_interrupted_run())

    # 用同一持久仓库创建全新 App，模拟 API 进程重启后恢复业务所有权。
    app_after_restart = create_app(
        config, repository=repository, storage=LocalObjectStorage(tmp_path)
    )
    with TestClient(app_after_restart) as owner_client:
        owner_csrf = _login(owner_client, "owner@example.com", "owner-password-123")
        resumed = owner_client.post(
            f"/api/v1/agent/runs/{interrupted_id}/resume",
            headers={"X-CSRF-Token": owner_csrf},
            json={"action_id": action_id, "decision": "reject"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"

    with TestClient(app_after_restart) as other_client:
        _login(other_client, "other@example.com", "other-password-123")
        assert other_client.get(f"/api/v1/agent/runs/{interrupted_id}").status_code == 404

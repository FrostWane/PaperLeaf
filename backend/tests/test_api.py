import asyncio
import threading
import uuid
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from paperleaf_api.arxiv_service import ArxivPaper
from paperleaf_api.config import settings
from paperleaf_api.main import AppServices, create_app
from paperleaf_api.models import JobStatus, PaperStatus, UserRole
from paperleaf_api.repository import (
    DiscoveryBatchRecord,
    DiscoveryItemRecord,
    MemoryRepository,
    PaperRecord,
)
from paperleaf_api.storage import LocalObjectStorage


def _login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    token = client.cookies.get("paperleaf_csrf")
    assert token
    return token


def test_app_services_rebuild_keeps_quality_policy_and_checkpointer(tmp_path, monkeypatch) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        evidence_min_confidence=0.61,
        evidence_min_vector_score=0.47,
        evidence_min_lexical_coverage=0.29,
        answer_min_citation_coverage=0.92,
        answer_min_claim_lexical_support=0.21,
        answer_min_support_confidence=0.73,
    )
    captured: list[dict] = []

    def capture_graph(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr("paperleaf_api.main.build_agent_graph", capture_graph)
    services = AppServices(
        config,
        repository=MemoryRepository(config.session_secret),
        storage=LocalObjectStorage(tmp_path),
    )
    checkpointer = object()
    services.build_agent_graph(checkpointer)

    rebuilt = captured[-1]
    assert rebuilt["checkpointer"] is checkpointer
    assert rebuilt["quality_policy"].min_confidence == 0.61
    assert rebuilt["quality_policy"].min_vector_score == 0.47
    assert rebuilt["quality_policy"].min_lexical_coverage == 0.29
    assert rebuilt["answer_quality_policy"].min_citation_coverage == 0.92
    assert rebuilt["answer_quality_policy"].min_claim_lexical_support == 0.21
    assert rebuilt["answer_quality_policy"].min_model_support_confidence == 0.73
    assert callable(rebuilt["answerer"])
    assert callable(rebuilt["support_grader"])


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
        metrics = admin_client.get("/metrics/")
        assert metrics.status_code == 200
        assert "python_info" in metrics.text
        csrf = _login(admin_client, "admin@example.com", "admin-password-123")
        model_health = admin_client.get("/api/v1/admin/model-health")
        assert model_health.status_code == 200
        assert model_health.json()["configured"] is False
        assert model_health.json()["providers"] == []
        observability = admin_client.get("/api/v1/admin/observability?window=24h")
        assert observability.status_code == 200
        assert observability.json()["totals"]["runs"] == 0
        assert observability.json()["privacy"] == {
            "content_collected": False,
            "identifiers_collected": False,
        }
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
        assert changed.json()["must_change_password"] is False
        assert reader_client.get("/api/v1/admin/observability").status_code == 403
        assert reader_client.get(f"/api/v1/papers/{paper_id}").status_code == 404
        assert reader_client.get("/api/v1/papers").json() == []
        isolated_bulk = reader_client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": reader_client.cookies.get("paperleaf_csrf")},
            json={"paper_ids": [paper_id], "action": "archive"},
        )
        assert isolated_bulk.status_code == 404


def test_discovery_recommendations_use_owned_library_and_exclude_existing(
    tmp_path, monkeypatch
) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    captured: dict[str, object] = {"calls": 0}

    async def related_search(phrases, limit, *, start=0):
        captured["calls"] = int(captured["calls"]) + 1
        captured.update(phrases=phrases, limit=limit, start=start)
        return [
            ArxivPaper(
                arxiv_id="2401.00001v2",
                title="Existing paper",
                authors=["Author"],
                abstract="duplicate",
                published="2024-01-01T00:00:00Z",
                pdf_url="https://arxiv.org/pdf/2401.00001v2.pdf",
            ),
            ArxivPaper(
                arxiv_id="2601.00002",
                title="Neural drug target affinity estimation",
                authors=["Author"],
                abstract="Protein ligand binding affinity prediction.",
                published="2026-01-01T00:00:00Z",
                pdf_url="https://arxiv.org/pdf/2601.00002.pdf",
            ),
        ]

    monkeypatch.setattr("paperleaf_api.main.search_related_arxiv", related_search)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))

    with TestClient(app) as client:
        csrf = _login(client, "admin@example.com", "admin-password-123")
        admin = asyncio.run(repository.find_user_by_email("admin@example.com"))
        assert admin
        repository.papers["library-paper"] = PaperRecord(
            id="library-paper",
            owner_id=admin.id,
            title="DeepDTA drug target binding affinity",
            authors=["Researcher"],
            year=2018,
            abstract="Protein ligand interaction and binding affinity prediction.",
            doi=None,
            arxiv_id="2401.00001",
            filename="paper.pdf",
            storage_key="test/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="discovery-paper",
            page_count=9,
            status=PaperStatus.ready,
        )

        disabled = client.get("/api/v1/discover/recommendations?limit=6")
        assert disabled.status_code == 403
        assert disabled.json()["detail"]["code"] == "ARXIV_SEARCH_DISABLED"
        admin.preferences = {"arxiv_search_enabled": True}

        response = client.get("/api/v1/discover/recommendations?limit=6")
        restored = client.get("/api/v1/discover/recommendations?limit=6")
        item_id = response.json()["items"][0]["item_id"]
        opened = client.post(
            f"/api/v1/discover/recommendations/items/{item_id}/feedback",
            headers={"X-CSRF-Token": csrf},
            json={"action": "opened"},
        )
        interested = client.post(
            f"/api/v1/discover/recommendations/items/{item_id}/feedback",
            headers={"X-CSRF-Token": csrf},
            json={"action": "interested"},
        )
        metrics = client.get("/api/v1/admin/discovery-metrics?window=30d")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["basis_paper_count"] == 1
    assert payload["seed_paper_title"] == "DeepDTA drug target binding affinity"
    assert payload["strategy"] == "keyword"
    assert payload["restored"] is False
    assert payload["batch_id"]
    assert [item["arxiv_id"] for item in payload["items"]] == ["2601.00002"]
    assert payload["items"][0]["matched_paper_title"] == (
        "DeepDTA drug target binding affinity"
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert restored.status_code == 200
    assert restored.json()["batch_id"] == payload["batch_id"]
    assert restored.json()["restored"] is True
    assert captured["calls"] == 1
    assert opened.json()["opened"] is True
    assert interested.json()["feedback"] == "interested"
    assert metrics.status_code == 200
    assert metrics.json()["impressions"] == 1
    assert metrics.json()["click_through_rate"] == 1
    assert metrics.json()["interest_hit_rate"] == 1
    assert captured["limit"] == 20
    assert captured["start"] == 0


def test_collection_and_admin_job_contract(tmp_path, valid_pdf_bytes: bytes) -> None:
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
        assert collection.status_code == 201
        collection_id = collection.json()["id"]
        assert (
            client.post(
                f"/api/v1/collections/{collection_id}/papers/{paper['id']}",
                headers={"X-CSRF-Token": csrf},
            ).json()["assigned"]
            is True
        )
        collections = client.get("/api/v1/collections").json()
        assert collections[0]["paper_ids"] == [paper["id"]]
        assert collections[0]["recursive_paper_count"] == 1
        assert collections[0]["children"] == []
        assert client.get("/api/v1/tags").status_code == 404

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

        invalid_tag_action = client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": csrf},
            json={
                "paper_ids": [paper["id"]],
                "action": "remove_tag",
                "target_id": "removed-tag",
            },
        )
        assert invalid_tag_action.status_code == 422

        job = next(iter(repository.jobs.values()))
        job.status = JobStatus.failed
        job.error_code = "PDF_PARSE_FAILED"
        job.error_message = "PDF 文件已损坏，无法解析"
        jobs = client.get("/api/v1/admin/jobs")
        assert jobs.status_code == 200
        assert jobs.json()[0]["error_message"] == "PDF 文件已损坏，无法解析"
        assert "text" not in jobs.json()[0]
        retried = client.post(f"/api/v1/admin/jobs/{job.id}/retry", headers={"X-CSRF-Token": csrf})
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"


def test_bulk_reindex_deduplicates_ids_and_does_not_create_parallel_jobs(
    tmp_path, valid_pdf_bytes: bytes
) -> None:
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
        repository.papers[paper["id"]].status = PaperStatus.ready
        for job in repository.jobs.values():
            job.status = JobStatus.completed

        response = client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": csrf},
            json={"paper_ids": [paper["id"], paper["id"]], "action": "reindex"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "action": "reindex",
            "affected": 1,
            "paper_ids": [paper["id"]],
        }
        parse_jobs = [
            job
            for job in repository.jobs.values()
            if job.paper_id == paper["id"] and job.type == "parse_pdf"
        ]
        assert len(parse_jobs) == 2
        assert repository.papers[paper["id"]].status == PaperStatus.queued

        duplicate = client.post(
            "/api/v1/papers/bulk",
            headers={"X-CSRF-Token": csrf},
            json={"paper_ids": [paper["id"]], "action": "reindex"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "所选文献正在处理或当前状态不能重新识别并索引"
        assert len(
            [job for job in repository.jobs.values() if job.paper_id == paper["id"]]
        ) == 2


def test_arxiv_import_persists_exact_metadata_publication(
    tmp_path, valid_pdf_bytes: bytes, monkeypatch
) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)

    async def fake_pdf(_arxiv_id: str, _max_bytes: int) -> bytes:
        return valid_pdf_bytes

    async def fake_metadata(_arxiv_id: str):
        return SimpleNamespace(
            title="Exact arXiv paper",
            authors=["Alice", "Bob"],
            abstract="摘要",
            published="2025-02-03T00:00:00Z",
            journal_ref="Journal of Reliable Systems 12 (2025) 1-9",
        )

    monkeypatch.setattr("paperleaf_api.main.fetch_arxiv_pdf", fake_pdf)
    monkeypatch.setattr("paperleaf_api.main.get_arxiv_paper", fake_metadata)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))
    with TestClient(app) as client:
        csrf = _login(client, "admin@example.com", "admin-password-123")
        admin = asyncio.run(repository.find_user_by_email("admin@example.com"))
        assert admin
        batch_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        asyncio.run(
            repository.create_discovery_batch(
                DiscoveryBatchRecord(
                    id=batch_id,
                    user_id=admin.id,
                    batch_number=0,
                    basis_paper_count=1,
                    seed_paper_title="Seed",
                    profile_terms=["paper"],
                    strategy="keyword",
                ),
                [
                    DiscoveryItemRecord(
                        id=item_id,
                        batch_id=batch_id,
                        user_id=admin.id,
                        arxiv_id="2401.01234",
                        title="Exact arXiv paper",
                        authors=["Alice", "Bob"],
                        abstract="摘要",
                        published="2025-02-03T00:00:00Z",
                        pdf_url="https://arxiv.org/pdf/2401.01234.pdf",
                        journal_ref=None,
                        matched_paper_title="Seed",
                        matched_terms=["paper"],
                        match_type="topic",
                        score=0.5,
                        rank=1,
                    )
                ],
            )
        )
        imported = client.post(
            "/api/v1/discover/arxiv/import",
            headers={"X-CSRF-Token": csrf},
            json={"arxiv_id": "2401.01234", "recommendation_item_id": item_id},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["title"] == "Exact arXiv paper"
        assert imported.json()["authors"] == ["Alice", "Bob"]
        assert imported.json()["year"] == 2025
        assert imported.json()["abstract"] == "摘要"
        assert imported.json()["publication"] == "Journal of Reliable Systems 12 (2025) 1-9"
        assert repository.discovery_items[item_id].imported_at is not None


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

    class ControlledGraph:
        async def ainvoke(self, initial, _config):
            return {
                **(initial if isinstance(initial, dict) else {}),
                "status": "completed",
                "answer": "当前证据不足，无法回答。",
                "retrieved_evidence": [],
                "citations": [],
                "evidence_quality": {"grade": "insufficient"},
            }

    app_before_restart.state.services.agent_graph = ControlledGraph()

    with TestClient(app_before_restart) as owner_client:
        owner_csrf = _login(owner_client, "owner@example.com", "owner-password-123")
        owner_session_response = owner_client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": owner_csrf},
            json={"title": "RAG 问答", "type": "library"},
        )
        assert owner_session_response.status_code == 201
        owner_session_id = owner_session_response.json()["id"]
        response = owner_client.post(
            f"/api/v1/chat/sessions/{owner_session_id}/messages",
            headers={
                "X-CSRF-Token": owner_csrf,
                "Idempotency-Key": "owner-message-1",
            },
            json={"content": "什么是 RAG？", "web_enabled": False},
        )
        assert response.status_code == 202
        owner_run_id = response.json()["run_id"]
        for _ in range(100):
            owner_run = repository.agent_runs[owner_run_id]
            if owner_run.status in {"completed", "failed"}:
                break
            threading.Event().wait(0.01)
        assert owner_run.status == "completed"
        assert owner_run.duration_ms is not None
        assert owner_run.duration_ms >= 0
        public_run = owner_client.get(f"/api/v1/agent/runs/{owner_run.id}").json()
        assert public_run["status"] == "completed"
        assert public_run["duration_ms"] == owner_run.duration_ms
        events = owner_client.get(f"/api/v1/agent/runs/{owner_run.id}/events")
        assert events.status_code == 200
        assert "event: node_started" in events.text
        assert "event: message_delta" in events.text
        assert "event: run_finished" in events.text

    with TestClient(app_before_restart) as other_client:
        other_csrf = _login(other_client, "other@example.com", "other-password-123")
        other_session_response = other_client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": other_csrf},
            json={"title": "RAG 问答", "type": "library"},
        )
        assert other_session_response.status_code == 201
        other_session_id = other_session_response.json()["id"]
        response = other_client.post(
            f"/api/v1/chat/sessions/{other_session_id}/messages",
            headers={
                "X-CSRF-Token": other_csrf,
                "Idempotency-Key": "other-message-1",
            },
            json={"content": "什么是 RAG？", "web_enabled": False},
        )
        assert response.status_code == 202
        other_run = repository.agent_runs[response.json()["run_id"]]
        assert other_client.get(f"/api/v1/agent/runs/{owner_run.id}").status_code == 404

    assert owner_run.thread_id == f"{owner.id}:{owner_session_id}:{owner_run.id}"
    assert other_run.thread_id == f"{other.id}:{other_session_id}:{other_run.id}"
    assert owner_run.thread_id != other_run.thread_id

    action_id = str(uuid.uuid4())

    async def seed_interrupted_run():
        chat_session = await repository.create_chat_session(
            owner.id,
            "等待确认",
            "library",
            None,
            None,
        )
        submission = await repository.submit_chat_message(
            chat_session.id,
            owner.id,
            "搜索并确认导入",
            "interrupt-message-1",
            "interrupt-hash-1",
            {"type": "library", "paper_ids": [], "web_enabled": True},
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        await repository.start_agent_run(submission.run.id, token)
        await repository.finish_agent_run(
            submission.run.id,
            status="interrupted",
            pending_action={"action_id": action_id, "type": "confirm_arxiv_import"},
            result_summary={"answer": "", "citations": []},
            claim_token=token,
        )
        return submission.run.id

    interrupted_id = asyncio.run(seed_interrupted_run())

    # 用同一持久仓库创建全新 App，模拟 API 进程重启后恢复业务所有权。
    app_after_restart = create_app(
        config, repository=repository, storage=LocalObjectStorage(tmp_path)
    )
    app_after_restart.state.services.agent_graph = ControlledGraph()
    with TestClient(app_after_restart) as owner_client:
        owner_csrf = _login(owner_client, "owner@example.com", "owner-password-123")
        resumed = owner_client.post(
            f"/api/v1/agent/runs/{interrupted_id}/resume",
            headers={"X-CSRF-Token": owner_csrf},
            json={"action_id": action_id, "decision": "reject"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["pending_action"] is None

    with TestClient(app_after_restart) as other_client:
        _login(other_client, "other@example.com", "other-password-123")
        assert other_client.get(f"/api/v1/agent/runs/{interrupted_id}").status_code == 404


def test_agent_cancel_is_idempotent_and_stops_active_graph(tmp_path) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))

    class SlowGraph:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.cancelled = threading.Event()

        async def ainvoke(self, _initial, _config):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    slow_graph = SlowGraph()
    app.state.services.agent_graph = slow_graph

    with TestClient(app) as client:
        csrf = _login(client, "admin@example.com", "admin-password-123")
        chat_session = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "取消测试", "type": "library"},
        )
        assert chat_session.status_code == 201
        submitted = client.post(
            f"/api/v1/chat/sessions/{chat_session.json()['id']}/messages",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "cancel-message-1",
            },
            json={"content": "等待取消", "web_enabled": False},
        )
        assert submitted.status_code == 202
        assert slow_graph.started.wait(timeout=5)
        run = repository.agent_runs[submitted.json()["run_id"]]

        cancelled = client.post(
            f"/api/v1/agent/runs/{run.id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert slow_graph.cancelled.wait(timeout=5)

        stream_response = client.get(f"/api/v1/agent/runs/{run.id}/events")
        assert stream_response.status_code == 200
        assert '"status":"cancelled"' in stream_response.text

        repeated = client.post(
            f"/api/v1/agent/runs/{run.id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "cancelled"

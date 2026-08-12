import asyncio
import hashlib
import time
from dataclasses import replace

from fastapi.testclient import TestClient
from test_persistent_agent import ResultGraph

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import PaperStatus
from paperleaf_api.repository import MemoryRepository, PaperRecord
from paperleaf_api.storage import LocalObjectStorage


def test_pdf_selection_accepts_text_layer_variants_and_rejects_other_page(
    tmp_path,
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
    app.state.services.agent_graph = ResultGraph()

    with TestClient(app) as client:
        csrf = _login(client)
        user = client.get("/api/v1/auth/me").json()
        asyncio.run(
            repository.create_paper(
                PaperRecord(
                    id="selection-paper",
                    owner_id=user["id"],
                    title="Selection paper",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="selection.pdf",
                    storage_key=f"{user['id']}/selection.pdf",
                    mime_type="application/pdf",
                    size_bytes=100,
                    sha256="b" * 64,
                    page_count=2,
                    status=PaperStatus.ready,
                )
            )
        )
        repository.paper_pages["selection-paper"] = {
            1: "The fi-\nnal drug-target affinity prediction uses two CNN encoders.",
            2: "This page only describes the evaluation datasets.",
        }
        session = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={
                "title": "选文验证",
                "type": "paper",
                "paper_id": "selection-paper",
            },
        )
        assert session.status_code == 201
        endpoint = f"/api/v1/chat/sessions/{session.json()['id']}/messages"

        invalid_text = "This selection belongs to a different physical page entirely."
        invalid = client.post(
            endpoint,
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "selection-invalid"},
            json={
                "content": "这些讲了什么？",
                "client_context": {
                    "paper_id": "selection-paper",
                    "physical_page": 1,
                    "selected_text": invalid_text,
                    "selected_text_hash": hashlib.sha256(invalid_text.encode("utf-8")).hexdigest(),
                },
            },
        )
        assert invalid.status_code == 422

        selected = "The ﬁnal drug–target affinity prediction uses two CNN encoders."
        accepted = client.post(
            endpoint,
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "selection-valid"},
            json={
                "content": "这些讲了什么？",
                "client_context": {
                    "paper_id": "selection-paper",
                    "physical_page": 1,
                    "selected_text": selected,
                    "selected_text_hash": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                },
            },
        )
        assert accepted.status_code == 202
        run = repository.agent_runs[accepted.json()["run_id"]]
        assert run.scope_snapshot["client_context"]["selected_text"] == (
            "the final drug-target affinity prediction uses two cnn encoders."
        )


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("paperleaf_csrf")
    assert csrf
    return csrf


def test_api_freezes_parallel_compare_for_multi_paper_summary(tmp_path) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
        skills_enabled=True,
        multi_agent_enabled=True,
        multi_agent_token_budget=3072,
    )
    repository = MemoryRepository(config.session_secret)
    app = create_app(config, repository=repository, storage=LocalObjectStorage(tmp_path))
    app.state.services.agent_graph = ResultGraph()

    with TestClient(app) as client:
        csrf = _login(client)
        user = client.get("/api/v1/auth/me").json()
        for index in range(1, 4):
            asyncio.run(
                repository.create_paper(
                    PaperRecord(
                        id=f"compare-paper-{index}",
                        owner_id=user["id"],
                        title=f"Compare paper {index}",
                        authors=[],
                        year=2026,
                        abstract=None,
                        doi=None,
                        arxiv_id=None,
                        filename=f"compare-{index}.pdf",
                        storage_key=f"{user['id']}/compare-{index}.pdf",
                        mime_type="application/pdf",
                        size_bytes=100,
                        sha256=str(index) * 64,
                        page_count=1,
                        status=PaperStatus.ready,
                    )
                )
            )
        session = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "多篇总结", "type": "library"},
        )
        assert session.status_code == 201
        accepted = client.post(
            f"/api/v1/chat/sessions/{session.json()['id']}/messages",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "multi-paper-summary-v2",
            },
            json={"content": "总结这三篇论文的方法和实验"},
        )
        assert accepted.status_code == 202
        run = repository.agent_runs[accepted.json()["run_id"]]
        assert run.orchestration_version == "compare_map_reduce_v2"
        assert run.scope_snapshot["orchestration_version"] == "compare_map_reduce_v2"
        assert run.scope_snapshot["harness"]["multi_agent_enabled"] is True


def test_persistent_chat_api_returns_202_and_replays_sse(tmp_path) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    app = create_app(
        config,
        repository=repository,
        storage=LocalObjectStorage(tmp_path),
    )
    app.state.services.agent_graph = ResultGraph()

    with TestClient(app) as client:
        csrf = _login(client)
        user = client.get("/api/v1/auth/me").json()
        asyncio.run(
            repository.create_paper(
                PaperRecord(
                    id="p1",
                    owner_id=user["id"],
                    title="论文一",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="p1.pdf",
                    storage_key=f"{user['id']}/p1.pdf",
                    mime_type="application/pdf",
                    size_bytes=100,
                    sha256="a" * 64,
                    page_count=5,
                    status=PaperStatus.ready,
                )
            )
        )

        assert (
            client.post(
                "/api/v1/chat/sessions",
                headers={"X-CSRF-Token": csrf},
                json={"title": "   ", "type": "library"},
            ).status_code
            == 422
        )
        session_response = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "证据问答", "type": "library"},
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]

        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        assert (
            client.post(
                endpoint,
                headers={"X-CSRF-Token": csrf},
                json={"content": "比较方法和实验"},
            ).status_code
            == 422
        )
        headers = {
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "browser-message-1",
        }
        accepted = client.post(
            endpoint,
            headers=headers,
            json={"content": "比较方法和实验"},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        replay = client.post(
            endpoint,
            headers=headers,
            json={"content": "比较方法和实验"},
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == run_id
        assert replay.json()["replayed"] is True

        deadline = time.monotonic() + 3
        run = client.get(f"/api/v1/agent/runs/{run_id}")
        while run.json()["status"] not in {"completed", "failed"}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
            run = client.get(f"/api/v1/agent/runs/{run_id}")
        assert run.json()["status"] == "completed"

        messages = client.get(endpoint).json()
        assert [item["sequence"] for item in messages] == [1, 2]
        assert messages[1]["status"] == "completed"
        assert messages[1]["content"].startswith("方法使用页级混合检索")

        all_events = client.get(f"/api/v1/agent/runs/{run_id}/events")
        assert all_events.status_code == 200
        assert "event: message_delta" in all_events.text
        assert "event: run_finished" in all_events.text
        persisted = asyncio.run(repository.list_owned_agent_run_events(run_id, user["id"]))
        assert persisted is not None
        cursor = persisted[-2].sequence
        resumed = client.get(
            f"/api/v1/agent/runs/{run_id}/events",
            headers={"Last-Event-ID": str(cursor)},
        )
        ids = [
            int(line.removeprefix("id: "))
            for line in resumed.text.splitlines()
            if line.startswith("id: ")
        ]
        assert ids
        assert min(ids) > cursor
        assert "event: run_finished" in resumed.text

        interrupted_session = asyncio.run(
            repository.create_chat_session(user["id"], "导入确认", "library", None, None)
        )
        interrupted_submission = asyncio.run(
            repository.submit_chat_message(
                interrupted_session.id,
                user["id"],
                "搜索论文",
                "interrupt-message-1",
                "interrupt-hash-1",
                {"type": "library", "paper_ids": ["p1"], "web_enabled": True},
            )
        )
        assert interrupted_submission is not None
        interrupted_claim = asyncio.run(
            repository.claim_agent_run_job(interrupted_submission.run.id)
        )
        assert interrupted_claim is not None
        asyncio.run(repository.start_agent_run(interrupted_submission.run.id, interrupted_claim))
        asyncio.run(
            repository.finish_agent_run(
                interrupted_submission.run.id,
                status="interrupted",
                result_summary={"answer": "", "citations": []},
                pending_action={
                    "action_id": "approve-arxiv-1",
                    "type": "confirm_arxiv_import",
                    "candidates": [
                        {
                            "arxiv_id": "2401.00001",
                            "title": "候选论文",
                            "internal_trace": "绝不能公开",
                        }
                    ],
                    "risk_message": "确认后才会导入",
                    "allowed_decisions": ["approve", "reject"],
                    "hidden_reasoning": "绝不能公开",
                },
                claim_token=interrupted_claim,
            )
        )
        interrupted_read = client.get(f"/api/v1/agent/runs/{interrupted_submission.run.id}")
        assert interrupted_read.status_code == 200
        public_action = interrupted_read.json()["pending_action"]
        assert public_action["action_id"] == "approve-arxiv-1"
        assert "hidden_reasoning" not in public_action
        assert "internal_trace" not in public_action["candidates"][0]
        resumed_run = client.post(
            f"/api/v1/agent/runs/{interrupted_submission.run.id}/resume",
            headers={"X-CSRF-Token": csrf},
            json={"action_id": "approve-arxiv-1", "decision": "approve"},
        )
        assert resumed_run.status_code == 200
        assert resumed_run.json()["pending_action"] is None
        persisted_resume = asyncio.run(
            repository.get_owned_agent_run(interrupted_submission.run.id, user["id"])
        )
        assert persisted_resume is not None
        assert persisted_resume.scope_snapshot["resumed_action"]["action_id"] == ("approve-arxiv-1")

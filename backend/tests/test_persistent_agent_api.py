import asyncio
import time
from dataclasses import replace

from fastapi.testclient import TestClient
from test_persistent_agent import ResultGraph

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import PaperStatus
from paperleaf_api.repository import MemoryRepository, PaperRecord
from paperleaf_api.storage import LocalObjectStorage


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("paperleaf_csrf")
    assert csrf
    return csrf


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

        assert client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "   ", "type": "library"},
        ).status_code == 422
        session_response = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "证据问答", "type": "library"},
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]

        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        assert client.post(
            endpoint,
            headers={"X-CSRF-Token": csrf},
            json={"content": "比较方法和实验"},
        ).status_code == 422
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
        persisted = asyncio.run(
            repository.list_owned_agent_run_events(run_id, user["id"])
        )
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
            repository.create_chat_session(
                user["id"], "导入确认", "library", None, None
            )
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
        asyncio.run(
            repository.start_agent_run(
                interrupted_submission.run.id, interrupted_claim
            )
        )
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
        interrupted_read = client.get(
            f"/api/v1/agent/runs/{interrupted_submission.run.id}"
        )
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
        assert persisted_resume.scope_snapshot["resumed_action"]["action_id"] == (
            "approve-arxiv-1"
        )

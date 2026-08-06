"""层级集合、递归论文范围与出版物字段测试。"""

import asyncio
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from paperleaf_api.config import settings
from paperleaf_api.db import Base
from paperleaf_api.main import create_app
from paperleaf_api.models import Collection, PaperStatus, UserRole
from paperleaf_api.repository import MemoryRepository, PaperRecord
from paperleaf_api.storage import LocalObjectStorage


def _paper(owner_id: str, suffix: str, status: PaperStatus = PaperStatus.ready) -> PaperRecord:
    return PaperRecord(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        title=f"论文 {suffix}",
        authors=[],
        year=None,
        abstract=None,
        doi=None,
        arxiv_id=None,
        filename=f"{suffix}.pdf",
        storage_key=f"{owner_id}/{suffix}.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        sha256=(suffix * 64)[:64],
        page_count=1,
        status=status,
    )


def test_collection_validation_depth_cycle_owner_and_sibling_name() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        alice = await repository.create_user(
            "alice@example.com", "alice-password-123", UserRole.user
        )
        bob = await repository.create_user(
            "bob@example.com", "bob-password-12345", UserRole.user
        )
        root = await repository.create_collection(alice.id, "根集合", None)
        child = await repository.create_collection(
            alice.id, "方法", None, parent_id=root.id
        )

        with pytest.raises(ValueError, match="同级集合名称已存在"):
            await repository.create_collection(alice.id, "方法", None, parent_id=root.id)
        # 不同父节点允许同名。
        other_root = await repository.create_collection(alice.id, "其他", None)
        await repository.create_collection(
            alice.id, "方法", None, parent_id=other_root.id
        )
        with pytest.raises(ValueError, match="父集合不存在"):
            await repository.create_collection(alice.id, "越权", None, parent_id="missing")
        bob_parent = await repository.create_collection(bob.id, "Bob", None)
        with pytest.raises(ValueError, match="父集合不存在"):
            await repository.create_collection(
                alice.id, "跨用户", None, parent_id=bob_parent.id
            )
        with pytest.raises(ValueError, match="自身或其子集合"):
            await repository.update_collection(root.id, alice.id, parent_id=child.id)

        level = child
        level_four = child
        for number in range(3, 6):
            level = await repository.create_collection(
                alice.id,
                f"第 {number} 层",
                None,
                parent_id=level.id,
            )
            if number == 4:
                level_four = level
        with pytest.raises(ValueError, match="最多支持 5 层"):
            await repository.create_collection(
                alice.id, "第 6 层", None, parent_id=level.id
            )
        with pytest.raises(ValueError, match="最多支持 5 层"):
            await repository.update_collection(
                other_root.id,
                alice.id,
                parent_id=level_four.id,
            )

    asyncio.run(scenario())


def test_recursive_resolution_and_delete_promotes_children_only() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        owner = await repository.create_user(
            "owner@example.com", "owner-password-123", UserRole.user
        )
        root = await repository.create_collection(owner.id, "根", None)
        child = await repository.create_collection(owner.id, "子", None, parent_id=root.id)
        grandchild = await repository.create_collection(
            owner.id, "孙", None, parent_id=child.id
        )
        first = _paper(owner.id, "a")
        second = _paper(owner.id, "b", PaperStatus.partial)
        await repository.create_paper(first)
        await repository.create_paper(second)
        await repository.set_paper_collection(root.id, first.id, owner.id, True)
        await repository.set_paper_collection(child.id, first.id, owner.id, True)
        await repository.set_paper_collection(grandchild.id, second.id, owner.id, True)

        assert set(await repository.resolve_collection_paper_ids(root.id, owner.id) or []) == {
            first.id,
            second.id,
        }
        assert await repository.resolve_collection_paper_ids(
            root.id, owner.id, ready_only=True
        ) == [first.id]

        assert await repository.delete_collection(root.id, owner.id)
        assert repository.collections[child.id].parent_id is None
        assert repository.collections[grandchild.id].parent_id == child.id
        assert (first.id, root.id) not in repository.paper_collections
        assert (first.id, child.id) in repository.paper_collections
        assert set(await repository.resolve_collection_paper_ids(child.id, owner.id) or []) == {
            first.id,
            second.id,
        }

        container = await repository.create_collection(owner.id, "冲突容器", None)
        target = await repository.create_collection(
            owner.id, "待删除", None, parent_id=container.id
        )
        await repository.create_collection(owner.id, "重名", None, parent_id=container.id)
        nested = await repository.create_collection(
            owner.id, "重名", None, parent_id=target.id
        )
        with pytest.raises(ValueError, match="子集合提升后会与同级集合重名"):
            await repository.delete_collection(target.id, owner.id)
        assert repository.collections[nested.id].parent_id == target.id

    asyncio.run(scenario())


def test_collection_tree_api_filters_and_collection_chat_scope(
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
    captured: list[dict] = []

    class CapturingGraph:
        async def ainvoke(self, initial: dict, _config: dict) -> dict:
            captured.append(initial)
            return {**initial, "status": "completed", "answer": "", "citations": []}

    app.state.services.agent_graph = CapturingGraph()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
        csrf = client.cookies.get("paperleaf_csrf")
        assert login.status_code == 200 and csrf

        root = client.post(
            "/api/v1/collections",
            headers={"X-CSRF-Token": csrf},
            json={"name": "DTA"},
        ).json()
        child = client.post(
            "/api/v1/collections",
            headers={"X-CSRF-Token": csrf},
            json={"name": "模型", "parent_id": root["id"]},
        ).json()
        grandchild = client.post(
            "/api/v1/collections",
            headers={"X-CSRF-Token": csrf},
            json={"name": "实验", "parent_id": child["id"]},
        ).json()

        paper_ids: list[str] = []
        for index in range(4):
            response = client.post(
                "/api/v1/papers",
                headers={"X-CSRF-Token": csrf},
                data={"title": f"测试论文 {index}"},
                files={
                    "file": (
                        f"paper-{index}.pdf",
                        valid_pdf_bytes + bytes([index]),
                        "application/pdf",
                    )
                },
            )
            assert response.status_code == 201, response.text
            paper_ids.append(response.json()["id"])
        repository.papers[paper_ids[0]].status = PaperStatus.ready
        repository.papers[paper_ids[1]].status = PaperStatus.ready
        repository.papers[paper_ids[2]].status = PaperStatus.partial
        async def assign_papers() -> None:
            await asyncio.gather(
                repository.set_paper_collection(
                    child["id"], paper_ids[0], login.json()["id"], True
                ),
                repository.set_paper_collection(
                    grandchild["id"], paper_ids[1], login.json()["id"], True
                ),
                repository.set_paper_collection(
                    child["id"], paper_ids[2], login.json()["id"], True
                ),
            )

        asyncio.run(assign_papers())

        tree = client.get("/api/v1/collections").json()
        assert len(tree) == 1
        assert tree[0]["id"] == root["id"]
        assert tree[0]["recursive_paper_count"] == 3
        assert tree[0]["children"][0]["id"] == child["id"]
        assert tree[0]["children"][0]["children"][0]["id"] == grandchild["id"]

        filtered = client.get(f"/api/v1/papers?collection_id={root['id']}")
        assert {item["id"] for item in filtered.json()} == set(paper_ids[:3])
        unfiled = client.get("/api/v1/papers?unfiled=true")
        assert [item["id"] for item in unfiled.json()] == [paper_ids[3]]
        assert client.get(
            f"/api/v1/papers?collection_id={root['id']}&unfiled=true"
        ).status_code == 422

        publication = client.patch(
            f"/api/v1/papers/{paper_ids[0]}",
            headers={"X-CSRF-Token": csrf},
            json={"publication": "Bioinformatics"},
        )
        assert publication.status_code == 200
        assert publication.json()["publication"] == "Bioinformatics"

        chat = client.post(
            "/api/v1/chat/sessions/collection-test/messages",
            headers={"X-CSRF-Token": csrf},
            json={
                "content": "比较这些论文",
                "scope": "collection",
                "selected_collection_id": root["id"],
                "selected_paper_ids": [paper_ids[3]],
            },
        )
        assert chat.status_code == 200
        assert set(captured[0]["selected_paper_ids"]) == set(paper_ids[:2])

    async def create_other_user():
        return await repository.create_user(
            "other@example.com",
            "other-password-123",
            UserRole.user,
            must_change_password=False,
        )

    asyncio.run(create_other_user())
    with TestClient(app) as other_client:
        other_login = other_client.post(
            "/api/v1/auth/login",
            json={"email": "other@example.com", "password": "other-password-123"},
        )
        other_csrf = other_client.cookies.get("paperleaf_csrf")
        assert other_login.status_code == 200 and other_csrf
        assert other_client.get(f"/api/v1/papers?collection_id={root['id']}").json() == []
        other_chat = other_client.post(
            "/api/v1/chat/sessions/cross-user/messages",
            headers={"X-CSRF-Token": other_csrf},
            json={
                "content": "不应泄漏",
                "scope": "collection",
                "selected_collection_id": root["id"],
            },
        )
        assert other_chat.status_code == 404


def test_collection_model_has_hierarchy_constraint_and_tags_are_removed() -> None:
    assert "tags" not in Base.metadata.tables
    assert "paper_tags" not in Base.metadata.tables
    assert "publication" in Base.metadata.tables["papers"].c
    assert Collection.__table__.c.parent_id.foreign_keys
    constraint = next(
        item
        for item in Collection.__table__.constraints
        if item.name == "uq_collection_owner_parent_name"
    )
    assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True

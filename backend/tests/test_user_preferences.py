"""用户资料、偏好、退出登录与管理员保护的 API 测试。"""

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import UserRole
from paperleaf_api.repository import (
    LastAdminProtectionError,
    MemoryRepository,
    UserRecord,
)
from paperleaf_api.storage import LocalObjectStorage


def _login(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("paperleaf_csrf")
    assert csrf
    return csrf, response.json()


def _app(tmp_path):
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    repository = MemoryRepository(config.session_secret)
    return create_app(
        config,
        repository=repository,
        storage=LocalObjectStorage(tmp_path),
    )


def test_preferences_are_persisted_and_returned_by_auth_me(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        csrf, logged_in = _login(client)
        assert logged_in["display_name"] is None
        assert logged_in["preferences"] == {
            "font_scale": "standard",
            "pdf_zoom": 100,
            "left_panel_open": True,
            "assistant_panel_open": True,
            "translation_language": "zh-CN",
            "arxiv_search_enabled": False,
        }

        preferences = client.get("/api/v1/users/me/preferences")
        assert preferences.status_code == 200
        assert preferences.json()["display_name"] is None

        missing_csrf = client.patch(
            "/api/v1/users/me/preferences",
            json={"display_name": "林研究员"},
        )
        assert missing_csrf.status_code == 403

        updated = client.patch(
            "/api/v1/users/me/preferences",
            headers={"X-CSRF-Token": csrf},
            json={
                "display_name": " 林研究员 ",
                "font_scale": "large",
                "pdf_zoom": 130,
                "left_panel_open": False,
                "assistant_panel_open": False,
                "translation_language": "zh-CN",
                "arxiv_search_enabled": True,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {
            "display_name": "林研究员",
            "font_scale": "large",
            "pdf_zoom": 130,
            "left_panel_open": False,
            "assistant_panel_open": False,
            "translation_language": "zh-CN",
            "arxiv_search_enabled": True,
        }

        partial = client.patch(
            "/api/v1/users/me/preferences",
            headers={"X-CSRF-Token": csrf},
            json={"pdf_zoom": 140},
        )
        assert partial.status_code == 200
        assert partial.json()["font_scale"] == "large"
        assert partial.json()["pdf_zoom"] == 140

        current = client.get("/api/v1/auth/me").json()
        assert current["display_name"] == "林研究员"
        assert current["preferences"]["pdf_zoom"] == 140
        assert current["preferences"]["assistant_panel_open"] is False


def test_preferences_validate_zoom_and_unknown_fields(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        csrf, _ = _login(client)
        invalid_zoom = client.patch(
            "/api/v1/users/me/preferences",
            headers={"X-CSRF-Token": csrf},
            json={"pdf_zoom": 210},
        )
        assert invalid_zoom.status_code == 422
        typo = client.patch(
            "/api/v1/users/me/preferences",
            headers={"X-CSRF-Token": csrf},
            json={"pdf_zoon": 120},
        )
        assert typo.status_code == 422


def test_first_login_user_can_open_settings_before_changing_password(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        csrf, _ = _login(client)
        created = client.post(
            "/api/v1/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "new-reader@example.com",
                "temporary_password": "temporary-password-123",
                "role": "user",
            },
        )
        assert created.status_code == 201
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "email": "new-reader@example.com",
                "password": "temporary-password-123",
            },
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["must_change_password"] is True
        first_login_csrf = client.cookies.get("paperleaf_csrf")
        assert first_login_csrf

        assert client.get("/api/v1/users/me/preferences").status_code == 200
        saved = client.patch(
            "/api/v1/users/me/preferences",
            headers={"X-CSRF-Token": first_login_csrf},
            json={"display_name": "新用户"},
        )
        assert saved.status_code == 200
        assert saved.json()["display_name"] == "新用户"

        blocked = client.get("/api/v1/papers")
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["message"] == "请先修改临时密码"


def test_last_admin_reason_and_logout_session_revocation(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        csrf, admin = _login(client)

        last_admin = client.patch(
            f"/api/v1/admin/users/{admin['id']}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert last_admin.status_code == 409
        assert last_admin.json()["detail"] == "不能停用或降级最后一名管理员"

        second_admin = client.post(
            "/api/v1/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "second-admin@example.com",
                "temporary_password": "second-admin-password-123",
                "role": "admin",
            },
        )
        assert second_admin.status_code == 201
        current_admin = client.patch(
            f"/api/v1/admin/users/{admin['id']}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert current_admin.status_code == 409
        assert current_admin.json()["detail"] == "不能停用当前管理员"

        second_admin = client.post(
            "/api/v1/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "backup-admin@example.com",
                "temporary_password": "backup-admin-password-123",
                "role": "admin",
            },
        )
        assert second_admin.status_code == 201
        current_admin = client.patch(
            f"/api/v1/admin/users/{admin['id']}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert current_admin.status_code == 409
        assert current_admin.json()["detail"] == "不能停用当前管理员"

        logged_out = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logged_out.status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401


def test_memory_repository_concurrent_admin_removal_keeps_one_admin() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        first = await repository.create_user(
            "first-admin@example.com",
            "first-admin-password-123",
            UserRole.admin,
            must_change_password=False,
        )
        second = await repository.create_user(
            "second-admin@example.com",
            "second-admin-password-123",
            UserRole.admin,
            must_change_password=False,
        )

        results = await asyncio.gather(
            repository.update_managed_user(first.id, second.id, role=UserRole.user),
            repository.update_managed_user(second.id, first.id, active=False),
            return_exceptions=True,
        )

        assert sum(isinstance(item, UserRecord) for item in results) == 1
        assert sum(isinstance(item, LastAdminProtectionError) for item in results) == 1
        assert await repository.count_active_admins() == 1

    asyncio.run(scenario())

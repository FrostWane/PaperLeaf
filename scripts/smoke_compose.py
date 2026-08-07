#!/usr/bin/env python3
"""PaperLeaf Docker Compose 安全集成冒烟。

凭证只从环境变量读取，不会写入参数、临时文件或日志。脚本不会启动、停止或重启容器。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar


class SmokeFailure(RuntimeError):
    pass


def build_minimal_pdf(marker: str) -> bytes:
    """生成带正确 xref 的单页 PDF，避免依赖测试夹具或第三方库。"""
    safe_marker = "".join(character for character in marker if character.isalnum())[:32]
    content = f"BT /F1 14 Tf 72 720 Td (PaperLeaf smoke {safe_marker}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def multipart_pdf(pdf: bytes, title: str) -> tuple[bytes, str]:
    boundary = "paperleaf-" + secrets.token_hex(16)
    delimiter = boundary.encode("ascii")
    body = bytearray()
    body.extend(b"--" + delimiter + b"\r\n")
    body.extend(b'Content-Disposition: form-data; name="title"\r\n\r\n')
    body.extend(title.encode("utf-8") + b"\r\n")
    body.extend(b"--" + delimiter + b"\r\n")
    body.extend(
        b'Content-Disposition: form-data; name="file"; filename="smoke.pdf"\r\n'
    )
    body.extend(b"Content-Type: application/pdf\r\n\r\n")
    body.extend(pdf + b"\r\n")
    body.extend(b"--" + delimiter + b"--\r\n")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


@dataclass(frozen=True)
class SmokeConfig:
    api_url: str
    admin_email: str
    admin_password: str
    timeout_seconds: float
    poll_seconds: float
    http_timeout_seconds: float

    @classmethod
    def from_environment(cls) -> SmokeConfig:
        email = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL")
        password = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD")
        if not email or not password:
            raise SmokeFailure(
                "必须通过环境变量设置 PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL 和 "
                "PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD"
            )
        return cls(
            api_url=os.getenv("PAPERLEAF_SMOKE_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            admin_email=email,
            admin_password=password,
            timeout_seconds=float(os.getenv("PAPERLEAF_SMOKE_TIMEOUT_SECONDS", "240")),
            poll_seconds=float(os.getenv("PAPERLEAF_SMOKE_POLL_SECONDS", "2")),
            http_timeout_seconds=float(
                os.getenv("PAPERLEAF_SMOKE_HTTP_TIMEOUT_SECONDS", "60")
            ),
        )


class PaperLeafClient:
    def __init__(self, config: SmokeConfig) -> None:
        self.config = config
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int] | None = None,
        sensitive: bool = False,
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            self.config.api_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with self.opener.open(
                request, timeout=self.config.http_timeout_seconds
            ) as response:
                status_code = response.status
                content = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            content = exc.read()
            response_headers = dict(exc.headers.items())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SmokeFailure(f"{method} {path} 无法连接服务") from exc
        expected_codes = expected if expected is not None else {200}
        if status_code not in expected_codes:
            detail = "响应内容已隐藏" if sensitive else content[:300].decode("utf-8", "replace")
            raise SmokeFailure(f"{method} {path} 返回 {status_code}：{detail}")
        return status_code, content, response_headers

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        csrf: bool = False,
        expected: set[int] | None = None,
        sensitive: bool = False,
    ) -> tuple[int, object, dict[str, str]]:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = self.csrf_token()
        status_code, content, response_headers = self.request(
            method,
            path,
            body=body,
            headers=headers,
            expected=expected,
            sensitive=sensitive,
        )
        if not content:
            return status_code, None, response_headers
        try:
            return status_code, json.loads(content), response_headers
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"{method} {path} 没有返回合法 JSON") from exc

    def csrf_token(self) -> str:
        token = next(
            (cookie.value for cookie in self.cookies if cookie.name == "paperleaf_csrf"),
            None,
        )
        if not token:
            raise SmokeFailure("登录响应没有设置 CSRF Cookie")
        return token


def wait_until(client: PaperLeafClient, description: str, check) -> object:
    deadline = time.monotonic() + client.config.timeout_seconds
    last_value = None
    while time.monotonic() < deadline:
        complete, value = check()
        last_value = value
        if complete:
            return value
        time.sleep(client.config.poll_seconds)
    raise SmokeFailure(f"等待{description}超时，最后状态：{last_value!r}")


def run_smoke(config: SmokeConfig) -> None:
    client = PaperLeafClient(config)
    paper_id = None
    collection_id = None
    child_collection_id = None
    deleted = False

    print("[1/8] 等待 API ready")
    wait_until(
        client,
        "API ready",
        lambda: (
            (lambda result: (result[0] == 200 and result[1].get("status") == "ready", result[1]))(
                client.json("GET", "/ready")
            )
        ),
    )

    print("[2/8] 登录并校验 Cookie/CSRF")
    _, login_result, _ = client.json(
        "POST",
        "/api/v1/auth/login",
        payload={"email": config.admin_email, "password": config.admin_password},
        sensitive=True,
    )
    if not isinstance(login_result, dict) or login_result.get("role") != "admin":
        raise SmokeFailure("登录账号不是管理员")
    client.csrf_token()

    try:
        print("[3/8] 上传临时有效 PDF")
        marker = secrets.token_hex(8)
        pdf = build_minimal_pdf(marker)
        multipart, content_type = multipart_pdf(pdf, f"Compose smoke {marker}")
        _, upload_content, _ = client.request(
            "POST",
            "/api/v1/papers",
            body=multipart,
            headers={"Content-Type": content_type, "X-CSRF-Token": client.csrf_token()},
            expected={201},
        )
        upload = json.loads(upload_content)
        paper_id = upload.get("id")
        if not paper_id:
            raise SmokeFailure("上传响应缺少 paper id")

        print("[4/8] 轮询解析完成")

        def paper_ready():
            _, value, _ = client.json("GET", f"/api/v1/papers/{paper_id}")
            current = value.get("status") if isinstance(value, dict) else None
            if current == "failed":
                raise SmokeFailure("PDF 解析作业失败")
            return current in {"ready", "partial"}, current

        final_status = wait_until(client, "PDF ready/partial", paper_ready)
        if final_status != "ready":
            raise SmokeFailure("文本型冒烟 PDF 应解析为 ready，而不是 partial")

        print("[5/8] 验证组织归属、最近阅读与批量归档")
        _, opened, _ = client.json(
            "POST", f"/api/v1/papers/{paper_id}/opened", csrf=True
        )
        if not isinstance(opened, dict) or not opened.get("last_opened_at"):
            raise SmokeFailure("最近阅读时间没有持久化")

        _, collection, _ = client.json(
            "POST",
            "/api/v1/collections",
            payload={"name": f"Smoke collection {marker}"},
            csrf=True,
            expected={201},
        )
        _, child_collection, _ = client.json(
            "POST",
            "/api/v1/collections",
            payload={"name": f"Smoke child {marker}", "parent_id": collection.get("id")},
            csrf=True,
            expected={201},
        )
        if not isinstance(collection, dict) or not isinstance(child_collection, dict):
            raise SmokeFailure("父子集合创建响应不合法")
        collection_id = collection.get("id")
        child_collection_id = child_collection.get("id")
        if not collection_id or not child_collection_id:
            raise SmokeFailure("父子集合响应缺少 id")

        _, bulk_result, _ = client.json(
            "POST",
            "/api/v1/papers/bulk",
            payload={
                "paper_ids": [paper_id],
                "action": "add_collection",
                "target_id": child_collection_id,
            },
            csrf=True,
        )
        if not isinstance(bulk_result, dict) or bulk_result.get("affected") != 1:
            raise SmokeFailure("批量加入子集合没有更新目标文献")

        _, collections, _ = client.json("GET", "/api/v1/collections")
        root_collection = (
            next(
                (item for item in collections if item.get("id") == collection_id),
                None,
            )
            if isinstance(collections, list)
            else None
        )
        child_members = (
            root_collection.get("children", [{}])[0].get("paper_ids")
            if isinstance(root_collection, dict) and root_collection.get("children")
            else None
        )
        if child_members != [paper_id] or root_collection.get("recursive_paper_count") != 1:
            raise SmokeFailure("父集合递归数量或子集合真实归属不正确")

        _, scoped_papers, _ = client.json("GET", f"/api/v1/papers?collection_id={collection_id}")
        if not isinstance(scoped_papers, list) or [
            item.get("id") for item in scoped_papers
        ] != [paper_id]:
            raise SmokeFailure("父集合没有递归返回子集合论文")

        client.json(
            "POST",
            "/api/v1/papers/bulk",
            payload={"paper_ids": [paper_id], "action": "archive"},
            csrf=True,
        )
        _, archived, _ = client.json("GET", f"/api/v1/papers/{paper_id}")
        if not isinstance(archived, dict) or not archived.get("archived_at"):
            raise SmokeFailure("批量归档状态没有持久化")
        client.json(
            "POST",
            "/api/v1/papers/bulk",
            payload={"paper_ids": [paper_id], "action": "unarchive"},
            csrf=True,
        )

        print("[6/8] 验证 Range PDF 下载")
        status_code, partial, headers = client.request(
            "GET",
            f"/api/v1/papers/{paper_id}/file",
            headers={"Range": "bytes=0-7"},
            expected={206},
        )
        if status_code != 206 or not partial.startswith(b"%PDF-"):
            raise SmokeFailure("Range 下载内容不是预期 PDF")
        content_range = next(
            (value for key, value in headers.items() if key.casefold() == "content-range"), ""
        )
        if not content_range.startswith("bytes 0-7/"):
            raise SmokeFailure("Range 响应缺少正确 Content-Range")

        print("[7/8] 验证管理员只读端点")
        for path in ("/api/v1/admin/users", "/api/v1/admin/jobs"):
            _, value, _ = client.json("GET", path)
            if not isinstance(value, list):
                raise SmokeFailure(f"{path} 未返回列表")

        print("[8/8] 删除并等待幂等清理")
        client.json(
            "DELETE",
            f"/api/v1/papers/{paper_id}",
            csrf=True,
            expected={202},
        )

        def paper_removed():
            status_value, _, _ = client.request(
                "GET", f"/api/v1/papers/{paper_id}", expected={200, 404}
            )
            return status_value == 404, status_value

        wait_until(client, "论文及原件清理", paper_removed)
        deleted = True
    finally:
        if paper_id and not deleted:
            try:
                client.json(
                    "DELETE",
                    f"/api/v1/papers/{paper_id}",
                    csrf=True,
                    expected={202, 404},
                )
            except Exception:
                print("清理提示：临时论文未能自动删除，请按 paper id 从管理员作业中检查。")
        # 先删除子集合，再删除父集合；否则父集合删除语义会把子集合提升为顶层，
        # 让一次安全冒烟在真实文献库里残留空集合。
        for resource, resource_id in (
            ("collections", child_collection_id),
            ("collections", collection_id),
        ):
            if not resource_id:
                continue
            try:
                client.json(
                    "DELETE",
                    f"/api/v1/{resource}/{resource_id}",
                    csrf=True,
                    expected={200, 404},
                )
            except Exception:
                print(f"清理提示：临时 {resource} 未能自动删除。")

    print("PaperLeaf Compose 冒烟通过")


def static_check() -> None:
    pdf = build_minimal_pdf("staticcheck")
    if not pdf.startswith(b"%PDF-1.4") or b"xref\n" not in pdf or not pdf.endswith(b"%%EOF\n"):
        raise SmokeFailure("PDF 生成器静态检查失败")
    body, content_type = multipart_pdf(pdf, "Static smoke")
    if pdf not in body or "boundary=" not in content_type:
        raise SmokeFailure("multipart 生成器静态检查失败")
    print("冒烟脚本静态检查通过")


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperLeaf Docker Compose 集成冒烟")
    parser.add_argument(
        "--static-check", action="store_true", help="仅检查脚本与临时 PDF，不访问服务"
    )
    args = parser.parse_args()
    try:
        if args.static_check:
            static_check()
        else:
            run_smoke(SmokeConfig.from_environment())
        return 0
    except SmokeFailure as exc:
        print(f"冒烟失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""受控学术 MCP Gateway。

只允许管理员预配置的服务与白名单工具；用户和模型都不能提供 Server URL。外部工具
描述、Schema 和结果均经过限长与结构校验，且不能作为 PaperLeaf 页级引用证据。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from prometheus_client import Counter, Gauge, Histogram

from .config import Settings
from .repository import McpServerConfigRecord, McpToolSnapshotRecord, Repository
from .runtime_store import RuntimeStore

ACADEMIC_SERVER_ID = "academic"
ALLOWED_REMOTE_TOOLS = {
    "search_openalex",
    "search_semantic_scholar",
    "get_academic_metadata",
}
NORMALIZED_PREFIX = "mcp__academic__"
MCP_SCHEMA_REVISION = 1

MCP_CALLS = Counter(
    "paperleaf_mcp_tool_calls_total",
    "PaperLeaf MCP 工具调用次数",
    ("server", "tool", "status", "error"),
)
MCP_LATENCY = Histogram(
    "paperleaf_mcp_tool_duration_seconds",
    "PaperLeaf MCP 工具调用耗时",
    ("server", "tool", "status"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60),
)
MCP_CACHE = Counter(
    "paperleaf_mcp_cache_total",
    "PaperLeaf MCP 搜索缓存结果",
    ("server", "result"),
)
MCP_HEALTH = Gauge(
    "paperleaf_mcp_server_healthy",
    "PaperLeaf MCP Server 健康状态",
    ("server",),
)


class McpGatewayError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class McpToolDefinition:
    normalized_name: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


def _error_code(error: Exception) -> str:
    if isinstance(error, McpGatewayError):
        return error.code
    if isinstance(  # noqa: UP038
        error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)
    ):
        return "MCP_TIMEOUT"
    if isinstance(error, httpx.HTTPError):
        return "MCP_TRANSPORT_ERROR"
    return "MCP_CALL_FAILED"


def _safe_endpoint(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpGatewayError("MCP_ENDPOINT_INVALID", "MCP 地址格式不合法")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise McpGatewayError("MCP_ENDPOINT_INVALID", "MCP 地址不能包含凭据或查询参数")
    host = parsed.hostname.casefold().rstrip(".")
    normalized_hosts = {item.casefold().rstrip(".") for item in allowed_hosts if item.strip()}
    if host not in normalized_hosts:
        raise McpGatewayError("MCP_HOST_NOT_ALLOWED", "MCP 主机不在服务端白名单")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local):
        raise McpGatewayError("MCP_PRIVATE_IP_REJECTED", "MCP 不允许直接连接私有或环回 IP")
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise McpGatewayError("MCP_METADATA_HOST_REJECTED", "MCP 不允许访问云元数据服务")
    if parsed.path.rstrip("/") != "/mcp":
        raise McpGatewayError("MCP_PATH_INVALID", "MCP 服务路径必须为 /mcp")
    return url


def _annotations(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        raw = value.model_dump(by_alias=True, exclude_none=True)
        return raw if isinstance(raw, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _validate_tool(remote: Any) -> McpToolDefinition:
    name = str(getattr(remote, "name", ""))
    if name not in ALLOWED_REMOTE_TOOLS:
        raise McpGatewayError("MCP_TOOL_NOT_ALLOWED", "MCP 返回了未授权工具")
    schema = getattr(remote, "inputSchema", None) or getattr(remote, "input_schema", None)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise McpGatewayError("MCP_SCHEMA_INVALID", "MCP 工具 Schema 不合法")
    if len(json.dumps(schema, ensure_ascii=False)) > 20_000:
        raise McpGatewayError("MCP_SCHEMA_TOO_LARGE", "MCP 工具 Schema 超出限制")
    annotations = _annotations(getattr(remote, "annotations", None))
    read_only = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    destructive = annotations.get("destructiveHint", annotations.get("destructive_hint"))
    if read_only is not True or destructive is True:
        raise McpGatewayError("MCP_TOOL_NOT_READ_ONLY", "MCP 工具未声明为安全只读")
    return McpToolDefinition(
        normalized_name=f"{NORMALIZED_PREFIX}{name}",
        remote_name=name,
        description=" ".join(str(getattr(remote, "description", "") or "").split())[:1000],
        input_schema=schema,
        annotations=annotations,
    )


def _structured_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise McpGatewayError("MCP_RESULT_INVALID", "MCP 工具没有返回结构化结果")


def _public_url(value: Any) -> str | None:
    """只保留可展示的公网 HTTP(S) 链接，拒绝脚本协议和本地目标。"""

    raw = str(value or "").strip()[:1200]
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "metadata.google.internal"}:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    ):
        return None
    return raw


def _sanitize_result(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise McpGatewayError("MCP_RESULT_TOO_LARGE", "MCP 工具结果超出限制")
    source = " ".join(str(payload.get("source", "学术数据源")).split())[:100]
    sanitized: dict[str, Any] = {
        "source": source,
        "available": payload.get("available") is not False,
    }
    for key in ("query", "error_code"):
        if payload.get(key) is not None:
            sanitized[key] = " ".join(str(payload[key]).split())[:500]
    raw_results = payload.get("results")
    if raw_results is None and isinstance(payload.get("result"), dict):
        raw_results = [payload["result"]]
    results: list[dict[str, Any]] = []
    for raw in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(raw, dict):
            continue
        item = {
            "external_id": str(raw.get("external_id", ""))[:300],
            "title": " ".join(str(raw.get("title", "")).split())[:1000],
            "authors": [
                " ".join(str(author).split())[:200]
                for author in (raw.get("authors") if isinstance(raw.get("authors"), list) else [])
            ][:20],
            "year": raw.get("year") if isinstance(raw.get("year"), int) else None,
            "publication": " ".join(str(raw.get("publication") or "").split())[:500]
            or None,
            "doi": str(raw.get("doi") or "")[:300] or None,
            "arxiv_id": str(raw.get("arxiv_id") or "")[:100] or None,
            "url": _public_url(raw.get("url")),
            "open_access_pdf_url": _public_url(raw.get("open_access_pdf_url")),
            "abstract": " ".join(str(raw.get("abstract") or "").split())[:4000],
            "citation_count": max(0, int(raw.get("citation_count") or 0)),
            "source": source,
        }
        if item["title"]:
            results.append(item)
    sanitized["results"] = results[:10]
    return sanitized


class McpGateway:
    def __init__(
        self,
        repository: Repository,
        runtime_store: RuntimeStore,
        config: Settings,
    ) -> None:
        self.repository = repository
        self.runtime_store = runtime_store
        self.config = config
        self._http_client: httpx.AsyncClient | None = None
        self._client_lock: asyncio.Lock | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._client_lock is None or self._client_loop is not loop:
            self._client_lock = asyncio.Lock()
            self._client_loop = loop
        return self._client_lock

    async def _client(self) -> httpx.AsyncClient:
        async with self._lock():
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.config.mcp_timeout_seconds),
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
            return self._http_client

    async def close(self) -> None:
        async with self._lock():
            if self._http_client is not None and not self._http_client.is_closed:
                await self._http_client.aclose()
            self._http_client = None

    def _bootstrap_record(self) -> McpServerConfigRecord:
        return McpServerConfigRecord(
            id=ACADEMIC_SERVER_ID,
            display_name="学术搜索",
            endpoint_url=self.config.academic_mcp_url,
            enabled=self.config.mcp_enabled,
            allowed_hosts=[
                value.strip()
                for value in self.config.academic_mcp_allowed_hosts.split(",")
                if value.strip()
            ],
        )

    async def ensure_config(self) -> Any:
        return await self.repository.ensure_mcp_server_config(self._bootstrap_record())

    async def list_servers(self) -> list[Any]:
        await self.ensure_config()
        return await self.repository.list_mcp_server_configs()

    async def _server(self, *, require_enabled: bool = True) -> Any:
        server = await self.ensure_config()
        if require_enabled and (not self.config.mcp_enabled or not server.enabled):
            raise McpGatewayError("MCP_DISABLED", "学术 MCP 当前未启用")
        now = datetime.now(timezone.utc)
        circuit_until = getattr(server, "circuit_open_until", None)
        if require_enabled and circuit_until and circuit_until > now:
            raise McpGatewayError("MCP_CIRCUIT_OPEN", "学术 MCP 暂时熔断")
        _safe_endpoint(server.endpoint_url, set(server.allowed_hosts or []))
        return server

    async def _with_session(self, operation: Any, *, require_enabled: bool = True) -> Any:
        server = await self._server(require_enabled=require_enabled)
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        client = await self._client()
        async with streamable_http_client(
            server.endpoint_url, http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(
                    session.initialize(), timeout=self.config.mcp_timeout_seconds
                )
                return await asyncio.wait_for(
                    operation(session), timeout=self.config.mcp_timeout_seconds
                )

    async def discover(self, *, require_enabled: bool = True) -> list[McpToolDefinition]:
        async def operation(session: Any) -> Any:
            return await session.list_tools()

        response = await self._with_session(operation, require_enabled=require_enabled)
        definitions = [_validate_tool(item) for item in response.tools]
        if {item.remote_name for item in definitions} != ALLOWED_REMOTE_TOOLS:
            raise McpGatewayError("MCP_TOOL_SET_INVALID", "MCP 工具集合与服务端策略不一致")
        if len({item.normalized_name for item in definitions}) != len(definitions):
            raise McpGatewayError("MCP_TOOL_NAME_CONFLICT", "MCP 工具名称冲突")
        return definitions

    async def refresh(self) -> list[Any]:
        try:
            definitions = await self.discover(require_enabled=False)
        except Exception as error:
            await self._record_failure(error)
            raise
        records = [
            McpToolSnapshotRecord(
                id=str(uuid.uuid4()),
                server_id=ACADEMIC_SERVER_ID,
                normalized_name=item.normalized_name,
                remote_name=item.remote_name,
                description=item.description,
                input_schema=item.input_schema,
                annotations=item.annotations,
            )
            for item in definitions
        ]
        stored = await self.repository.replace_mcp_tool_snapshots(
            ACADEMIC_SERVER_ID, records
        )
        await self._record_success()
        return stored

    async def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        definitions = await self.refresh()
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "tool_count": len(definitions),
        }

    async def call(self, normalized_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not normalized_name.startswith(NORMALIZED_PREFIX):
            raise McpGatewayError("MCP_TOOL_NOT_ALLOWED", "MCP 工具名称不合法")
        remote_name = normalized_name.removeprefix(NORMALIZED_PREFIX)
        if remote_name not in ALLOWED_REMOTE_TOOLS:
            raise McpGatewayError("MCP_TOOL_NOT_ALLOWED", "MCP 工具不在白名单")
        # 启用状态、熔断和 Endpoint 白名单必须先于缓存读取。停用的服务不能借旧缓存
        # 继续返回结果，恢复后也使用新的配置命名空间。
        server = await self._server(require_enabled=True)
        cache_key = json.dumps(
            {
                "tool": remote_name,
                "arguments": arguments,
                "config_revision": int(getattr(server, "cache_revision", 1) or 1),
                "schema_revision": MCP_SCHEMA_REVISION,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        cached = await self.runtime_store.get_cached_json("mcp-academic", cache_key)
        if cached is not None:
            MCP_CACHE.labels(ACADEMIC_SERVER_ID, "hit").inc()
            return {**cached, "cached": True}
        MCP_CACHE.labels(ACADEMIC_SERVER_ID, "miss").inc()
        started = time.perf_counter()

        async def operation(session: Any) -> Any:
            return await session.call_tool(remote_name, arguments=arguments)

        try:
            raw = await self._with_session(operation)
            if getattr(raw, "isError", False) or getattr(raw, "is_error", False):
                raise McpGatewayError("MCP_REMOTE_ERROR", "MCP 工具返回错误")
            result = _sanitize_result(_structured_result(raw))
            await self.runtime_store.set_cached_json(
                "mcp-academic",
                cache_key,
                result,
                ttl_seconds=self.config.mcp_cache_ttl_seconds,
            )
            await self._record_success()
            MCP_CALLS.labels(ACADEMIC_SERVER_ID, remote_name, "succeeded", "none").inc()
            MCP_LATENCY.labels(ACADEMIC_SERVER_ID, remote_name, "succeeded").observe(
                time.perf_counter() - started
            )
            return {**result, "cached": False}
        except Exception as error:
            code = _error_code(error)
            await self._record_failure(error)
            MCP_CALLS.labels(ACADEMIC_SERVER_ID, remote_name, "failed", code).inc()
            MCP_LATENCY.labels(ACADEMIC_SERVER_ID, remote_name, "failed").observe(
                time.perf_counter() - started
            )
            raise McpGatewayError(code, "学术 MCP 调用失败") from error

    async def set_enabled(self, enabled: bool) -> Any:
        await self.ensure_config()
        return await self.repository.update_mcp_server_config(
            ACADEMIC_SERVER_ID,
            enabled=enabled,
            health_status="unknown" if enabled else "disabled",
            consecutive_failures=0,
            circuit_open_until=None,
            last_error_code=None,
        )

    async def _record_success(self) -> None:
        MCP_HEALTH.labels(ACADEMIC_SERVER_ID).set(1)
        await self.repository.update_mcp_server_config(
            ACADEMIC_SERVER_ID,
            health_status="healthy",
            consecutive_failures=0,
            circuit_open_until=None,
            last_checked_at=datetime.now(timezone.utc),
            last_error_code=None,
        )

    async def _record_failure(self, error: Exception) -> None:
        server = await self.ensure_config()
        failures = int(getattr(server, "consecutive_failures", 0)) + 1
        circuit_open_until = None
        status = "unhealthy"
        if failures >= self.config.mcp_circuit_failure_threshold:
            status = "circuit_open"
            circuit_open_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.config.mcp_circuit_cooldown_seconds
            )
        MCP_HEALTH.labels(ACADEMIC_SERVER_ID).set(0)
        await self.repository.update_mcp_server_config(
            ACADEMIC_SERVER_ID,
            health_status=status,
            consecutive_failures=failures,
            circuit_open_until=circuit_open_until,
            last_checked_at=datetime.now(timezone.utc),
            last_error_code=_error_code(error),
        )

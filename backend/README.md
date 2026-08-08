# PaperLeaf 后端

该目录包含 PaperLeaf 的 FastAPI 服务、页级 RAG 核心、受控 Agent 图以及后台任务基础设施。

## 本地启动

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn paperleaf_api.main:app --reload
```

默认使用 `demo` 模式，不依赖 PostgreSQL、MinIO 或模型服务。演示管理员为
`admin@paperleaf.local`，密码从 `PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD` 读取；未设置时仅限本地开发，
使用 `paperleaf-dev-admin`。

生产部署必须设置：

- `PAPERLEAF_MODE=production`
- `PAPERLEAF_DATABASE_URL=postgresql+asyncpg://...`
- `PAPERLEAF_SESSION_SECRET=...`
- `PAPERLEAF_REDIS_URL=redis://...`（多 API 实例共享限流；单进程可不配置）
- `PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD=...`
- MinIO 与 OpenAI-compatible 服务相关变量

生产模式会拒绝 `replace-with-*` 占位值、少于 32 字符的会话密钥以及少于 12 字符的
管理员密码，避免直接复制示例配置后误启动。

## 测试

```bash
pytest
python -m compileall paperleaf_api tests
```

RAG 检索与引用校验是独立实现；LangGraph 只负责状态、条件路由、中断和恢复。

## 前端接入契约

浏览器统一通过同源代理访问 `/api/v1`。开发环境如直接访问 `http://localhost:8000`，需将
前端来源加入 `PAPERLEAF_CORS_ORIGINS`，请求必须启用 `credentials: "include"`。

### 登录与 CSRF

`POST /api/v1/auth/login`：

```json
{"email":"admin@paperleaf.local","password":"..."}
```

成功后返回当前用户，同时设置两个 Cookie：

- `paperleaf_session`：HttpOnly 会话，前端不能读取。
- `paperleaf_csrf`：CSRF Token。除登录外，所有 `POST/PATCH/DELETE` 请求都必须把它原样放入
  `X-CSRF-Token` 请求头。

`GET /api/v1/auth/me` 用于恢复登录；`POST /api/v1/auth/logout` 和
`POST /api/v1/auth/change-password` 使用相同的 Cookie + CSRF 约定。

管理员创建的用户带有 `must_change_password=true`。这类会话只能访问 `me`、
`change-password` 和 `logout`；其他业务接口返回 `403`，错误码为
`PASSWORD_CHANGE_REQUIRED`。

### 上传 PDF

`POST /api/v1/papers` 使用 `multipart/form-data`：

- `file`：必填 PDF。
- `title`：可选标题。
- `doi`：可选 DOI。

成功返回 `PaperRead`，初始状态为 `queued`。文件阅读接口
`GET /api/v1/papers/{paper_id}/file` 支持标准单段 `Range: bytes=start-end`，返回 `206` 和
`Content-Range`；暂不支持 multipart ranges。

### Agent 消息与 SSE

`POST /api/v1/chat/sessions/{session_id}/messages` 请求体：

```json
{
  "content": "这篇论文的核心贡献是什么？",
  "web_enabled": false
}
```

请求必须携带 `Idempotency-Key`。API 会在一个事务内保存用户消息、助手占位消息、Agent Run、
服务端解析的论文范围快照和唯一后台作业，然后返回 `202`：

```json
{
  "session_id": "...",
  "message_id": "...",
  "run_id": "...",
  "status": "pending",
  "replayed": false
}
```

重复键且请求内容一致时返回原结果；同一键对应不同内容时返回 `409`。真正的 LangGraph 运行由
Worker 领取 `agent_run` 作业执行，不依赖原 HTTP 请求或浏览器连接继续存活。

提交入口按用户执行固定窗口限流，默认每 60 秒 12 次。Redis 通过 Lua 原子保存计数与幂等
判定；相同 `Idempotency-Key` 不重复计数。超过限制返回 `429`、`AGENT_RATE_LIMITED` 和
`Retry-After`。Redis 不可用时退回当前 API 进程内限流，PostgreSQL 中的消息和 Run 不受影响。

前端随后连接 `GET /api/v1/agent/runs/{run_id}/events`。事件先写入 PostgreSQL，再以
`text/event-stream` 返回；断线重连可用 `Last-Event-ID` 补发遗漏事件。每帧保持以下格式：

```text
event: message_delta
data: {"event":"message_delta","run_id":"...","data":{"delta":"..."}}
```

公开事件名固定为：`run_started`、`node_started`、`tool_started`、`tool_finished`、
`message_delta`、`citation`、`interrupt`、`error`、`run_finished`。`interrupt` 的
`data.pending_action` 包含 `action_id`、候选文献和允许决定；前端通过
`POST /api/v1/agent/runs/{run_id}/resume` 提交：

```json
{"action_id":"...","decision":"approve"}
```

前端只展示工具活动摘要，不展示或推断隐藏推理过程。回答不是原始模型 token 直出：Worker
先在内存中缓冲完整事实段落，校验其 Chunk 引用属于当次召回证据并通过支持检查，再把段落
原子写入消息与事件表；未通过的段落不会成为用户可见内容。

### 总结与研究结构图

- `POST /api/v1/papers/{paper_id}/summary` 要求模型返回固定五节 JSON；每个事实都携带一个或多个合法 `chunk_id + physical_page`，服务端再生成稳定 Markdown。
- `POST /api/v1/papers/{paper_id}/structure-graph` 要求 5～12 个语义节点，节点类型限制为研究问题、背景、方法、数据、实验、结果和局限。每个节点至少一个合法引用，边不得引用未知节点、形成自环/循环或留下孤立节点。
- 未配置模型、超时、引用失败和格式错误使用不同中文原因。总结可退回带引用的原文摘录；结构图失败时不再生成顺序 Chunk 伪图。
- 总结和结构图写入 `paper_artifacts`，以全部物理页文本的来源修订标识缓存。重新解析论文时旧产物标记为 `stale`，不会静默复用。

`tool_finished.data.evidence_quality` 给出页级证据数量、检索通道、检索置信度、
`retrieval_grade` 与可选的 `answer_support_grade`。这些字段是服务端质量门禁的公开摘要，
不包含模型思维链。即使检索结果非空，只要相关度不足或证据没有直接支持所问事实，图仍会进入拒答节点。

服务端不会直接使用前端 `session_id` 作为 LangGraph Checkpoint 键。内部 `thread_id` 同时绑定
`user_id + session_id + run_id`；Agent Run 的所有者、内部键、状态、待确认动作、回答摘要和
引用持久化在业务数据库中。API 重启后的查询、恢复和取消都会先按当前用户查找 Run，其他
用户统一得到 `404`，且内部 `thread_id` 不通过公开 API 返回。

### 层级集合、出版物与作业

- `/api/v1/collections` 提供用户隔离的层级集合 CRUD；同级名称唯一、最多五层，父集合不能跨用户或形成循环。
- 集合列表返回 `parent_id`、直接 `paper_ids`、去重后的 `recursive_paper_count` 与嵌套 `children`。
- `GET /api/v1/papers?collection_id=...` 由服务端递归解析后代集合；`unfiled=true` 返回未加入任何集合的论文。
- `POST/DELETE /api/v1/collections/{id}/papers/{paper_id}` 管理集合归属。
- `POST /api/v1/papers/bulk` 在最多 100 篇当前用户文献上执行归档、恢复、集合动作或 `reindex`。重新索引复用私有原始 PDF，防止同一论文产生重复记录，并拒绝为已有活跃解析作业重复排队。
- 文献 `publication` 优先来自 PDF 本地元数据或首页；仍缺失且有 DOI 时，Worker 只向 Crossref 发送 DOI 并缓存成功或失败结果。
- `POST /api/v1/papers/{paper_id}/opened` 记录最近阅读时间；它不影响 PDF 或索引内容。
- 管理员可通过 `GET /api/v1/admin/jobs` 查看作业状态，并通过
  `POST /api/v1/admin/jobs/{id}/retry` 重试失败作业。返回值不包含论文正文、Chunk 或聊天内容。

删除论文会创建幂等 `delete_paper` 作业。Worker 先删除私有原件，再级联清理页面、Chunk、
集合关系及其他关联作业；任一步失败后都可以安全重试。

RAG 离线评测命令和 JSONL 协议见 `evaluation/README.md`。仓库不附带虚构数据或成绩。

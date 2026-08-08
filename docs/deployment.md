# PaperLeaf 部署指南

## 推荐方式：Docker Compose

### 准备

- Docker Engine 24+、Docker Compose v2
- 4 GB 以上可用内存
- 供生产访问的域名与 TLS 终止代理
- 数据库与对象存储的备份位置

```bash
git clone https://github.com/FrostWane/PaperLeaf.git
cd PaperLeaf
cp .env.example .env
```

生成强随机值并填写 `.env`。不要直接使用示例密码。`PAPERLEAF_SESSION_SECRET` 建议使用至少 64 个随机字符。

### 启动与检查

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 migrate redis minio-init api worker prometheus grafana web
```

正常情况下，`migrate` 和 `minio-init` 以退出码 0 完成，`postgres`、`redis`、`minio`、`api` 和 `web` 变为健康状态，`worker`、`prometheus` 与 `grafana` 持续运行。

可使用仓库内的安全冒烟脚本验证临时 PDF 上传、解析、Range 下载和删除。脚本只从环境变量读取管理员凭证，不打印密码，并在完成后清理临时论文：

```bash
python scripts/smoke_compose.py
```

### 服务与端口

| 服务 | 默认端口 | 是否建议直接暴露公网 |
|---|---:|---|
| Web | 3000 | 通过 HTTPS 反向代理暴露 |
| API | 8000 | 建议由同域 `/api` 反向代理 |
| MinIO API | 9000 | 否 |
| MinIO Console | 9001 | 否；仅运维网络 |
| PostgreSQL | 不映射 | 否 |
| Redis | 不映射 | 否 |
| Prometheus | 9090 | 仅运维网络 |
| Grafana | 3001 | 仅运维网络；必须修改默认密码 |

Compose 默认把公开端口绑定到 `127.0.0.1`。若明确需要从局域网访问，可以设置 `PAPERLEAF_BIND_ADDRESS=0.0.0.0`，同时配置防火墙与 HTTPS；不要因此暴露 MinIO 管理端口。

API/Worker 通过 Compose 内部网络访问 Redis；需要检查时使用
`docker compose exec -T redis redis-cli ping`。公网部署不得直接暴露 6379；跨主机时应改用
受控私网中的 Redis，并配置认证和 TLS。

本地默认同时允许 `http://localhost:3000` 和 `http://127.0.0.1:3000`，避免两种访问地址混用时出现 `Failed to fetch`。生产部署应让 Web 与 API 同域，或将 `PAPERLEAF_CORS_ORIGINS` 设置为明确的 HTTPS 来源。带凭据请求禁止使用 `*`。启用 HTTPS 后设置：

```dotenv
PAPERLEAF_SECURE_COOKIES=true
PAPERLEAF_CORS_ORIGINS=https://papers.example.com
NEXT_PUBLIC_API_BASE_URL=https://papers.example.com/api/v1
```

`NEXT_PUBLIC_API_BASE_URL` 会在 Web 镜像构建时写入客户端资源，必须包含 `/api/v1` 前缀；修改后需要重新构建 Web：

```bash
docker compose build web
docker compose up -d web
```

完整自托管镜像固定使用 `NEXT_PUBLIC_DATA_MODE=real`。公开演示构建才使用 `demo`，两者不能共用带有真实用户数据的运行时配置。

## 环境变量

### 身份与会话

| 变量 | 必需 | 说明 |
|---|---|---|
| `PAPERLEAF_MODE` | 是 | 生产环境为 `production` |
| `PAPERLEAF_SESSION_SECRET` | 是 | 会话签名密钥 |
| `PAPERLEAF_SECURE_COOKIES` | 是 | HTTPS 下设为 `true` |
| `PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL` | 首次启动 | 初始管理员邮箱 |
| `PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD` | 首次启动 | 初始管理员密码 |
| `PAPERLEAF_CORS_ORIGINS` | 跨域时 | 逗号分隔的显式来源 |

### 数据与文件

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PAPERLEAF_MAX_PDF_BYTES` | `52428800` | 单个 PDF 最大字节数 |
| `PAPERLEAF_MAX_PDF_PAGES` | `500` | 单个 PDF 最大物理页数 |
| `PAPERLEAF_MINIO_BUCKET` | `paperleaf-pdfs` | 私有 Bucket |
| `POSTGRES_DB` | `paperleaf` | 数据库名 |
| `POSTGRES_USER` | `paperleaf` | 数据库用户 |

### Redis 与 Agent 限流

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PAPERLEAF_REDIS_URL` | `redis://redis:6379/0` | 短期运行态连接；留空时使用单进程内存实现 |
| `PAPERLEAF_REDIS_KEY_PREFIX` | `paperleaf` | 多环境共用 Redis 时必须使用不同前缀 |
| `PAPERLEAF_REDIS_TIMEOUT_SECONDS` | `0.5` | 单次 Redis 操作超时，范围 `(0, 5]` 秒 |
| `PAPERLEAF_REDIS_MAXMEMORY` | `256mb` | Compose Redis 内存上限，管理员页会显示当前使用量 |
| `PAPERLEAF_AGENT_RATE_LIMIT_REQUESTS` | `12` | 每个窗口允许的 Agent 提交次数 |
| `PAPERLEAF_AGENT_RATE_LIMIT_WINDOW_SECONDS` | `60` | 固定窗口秒数，范围 `1~3600` |

Redis 数据允许丢失，默认关闭 RDB/AOF。重启 Redis 会清空短期限流窗口，但不会丢失用户、
消息、任务或 Agent Run。`GET /ready` 会返回运行态存储的 `available/degraded` 状态；Redis
不可用时 API 继续工作并退化为当前进程内限流。

### 学术 MCP

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PAPERLEAF_MCP_ENABLED` | `false` | 是否向 Agent 暴露白名单学术 MCP 工具 |
| `PAPERLEAF_ACADEMIC_MCP_URL` | `http://academic-search-mcp:8080/mcp` | Compose 私网内固定服务地址 |
| `PAPERLEAF_ACADEMIC_MCP_ALLOWED_HOSTS` | `academic-search-mcp` | 服务端主机白名单，普通用户不能修改 |
| `PAPERLEAF_MCP_TIMEOUT_SECONDS` | `15` | 单次发现或调用超时，范围 1～60 秒 |
| `PAPERLEAF_MCP_CACHE_TTL_SECONDS` | `900` | Redis 公开搜索结果缓存时间 |
| `PAPERLEAF_MCP_CIRCUIT_FAILURE_THRESHOLD` | `3` | 连续失败后熔断阈值 |
| `PAPERLEAF_MCP_CIRCUIT_COOLDOWN_SECONDS` | `60` | 熔断冷却时间 |
| `PAPERLEAF_ACADEMIC_HTTP_TIMEOUT_SECONDS` | `12` | MCP 服务访问官方学术 API 的请求超时 |
| `OPENALEX_API_KEY` | — | OpenAlex 搜索所需 Key，只注入 MCP 容器 |
| `SEMANTIC_SCHOLAR_API_KEY` | — | 可选；未配置时受公共 API 限流影响 |

默认关闭 MCP，不影响本地文献问答、arXiv 搜索或 Crossref 元数据补全。启用后执行
`docker compose up -d --build academic-search-mcp api worker`，再以管理员进入“管理 →
Agent Harness”检测连接并刷新工具清单。外部学术结果只作为公开元数据展示；需要全文问答时，
用户仍须确认导入 PDF 并等待页级索引完成。

### RAG 可观测性

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PAPERLEAF_WORKER_METRICS_PORT` | `9101` | Worker 私网 Prometheus 指标端口 |
| `PAPERLEAF_PROMETHEUS_IMAGE` | GHCR 官方镜像 | 固定 Prometheus 镜像；网络受限时可替换为可信私有镜像 |
| `PAPERLEAF_GRAFANA_BASE_IMAGE` | AWS Public ECR Debian | 构建 Grafana 配置镜像的可信基础镜像 |
| `PAPERLEAF_PROMETHEUS_PORT` | `9090` | Prometheus 本机端口 |
| `PAPERLEAF_GRAFANA_PORT` | `3001` | Grafana 本机端口 |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana 管理账号 |
| `GRAFANA_ADMIN_PASSWORD` | — | 必须替换的 Grafana 密码 |
| `NEXT_PUBLIC_GRAFANA_URL` | 本机面板地址 | 管理员页面的 Grafana 入口 |

Prometheus 默认保留 15 天时序数据。Compose 会把仓库内的采集规则和面板配置烘焙进配置镜像，只把时序数据与 Grafana 数据库写入命名卷，从而避免 Windows 绑定挂载差异。Grafana 配置镜像从 Grafana 官方发布站下载固定版本的 Linux 二进制，并在构建时用官方 SHA-256 校验；它不依赖当前网络不可用的 Docker Hub。Grafana 会自动装载 `PaperLeaf RAG` 面板。公网部署不要直接公开 Prometheus；Grafana 应放在受控运维网络或统一身份代理后。管理员业务聚合来自 PostgreSQL，详细口径和隐私边界见 [RAG 可观测性](observability.md)。

数据库和 MinIO 密码不应重复。Compose 会把 `POSTGRES_PASSWORD` 插入数据库连接 URL，因此应使用 URL-safe 随机字符。若使用外部托管服务，修改 Compose 环境变量或部署平台中的对应连接信息。

### 模型

| 变量 | 说明 |
|---|---|
| `PAPERLEAF_OPENAI_API_KEY` | OpenAI-compatible Key |
| `PAPERLEAF_OPENAI_BASE_URL` | 接口根地址 |
| `PAPERLEAF_CHAT_MODEL` | 问答、总结与全文翻译模型 |
| `PAPERLEAF_EMBEDDING_MODEL` | 嵌入模型 |
| `PAPERLEAF_EMBEDDING_DIMENSIONS` | 嵌入向量维度 |
| `PAPERLEAF_EMBEDDING_BATCH_SIZE` | Worker 单次提交给向量服务的 Chunk 数，默认 8，范围 1～64 |
| `PAPERLEAF_VISION_MODEL` | 可选 OCR 视觉模型 |
| `PAPERLEAF_FALLBACK_OPENAI_API_KEY` | 可选备用 OpenAI-compatible Key |
| `PAPERLEAF_FALLBACK_OPENAI_BASE_URL` | 备用服务根地址 |
| `PAPERLEAF_FALLBACK_CHAT_MODEL` | 备用问答、支持检查与总结模型 |
| `PAPERLEAF_FALLBACK_EMBEDDING_MODEL` | 备用嵌入模型，维度必须与主模型一致 |
| `PAPERLEAF_FALLBACK_VISION_MODEL` | 可选备用 OCR 视觉模型 |
| `PAPERLEAF_MODEL_TIMEOUT_SECONDS` | 每次模型调用的超时秒数 |
| `PAPERLEAF_TRANSLATION_TIMEOUT_SECONDS` | 单页全文翻译超时，默认 90 秒 |
| `PAPERLEAF_ARTIFACT_TIMEOUT_SECONDS` | 后台全文概括首次生成超时，默认 120 秒 |
| `PAPERLEAF_ARTIFACT_RETRY_TIMEOUT_SECONDS` | 后台全文概括精简证据重试超时，默认 90 秒且不得大于首次超时 |
| `PAPERLEAF_STRUCTURE_TIMEOUT_SECONDS` | 研究脑图首次生成超时，默认 180 秒 |
| `PAPERLEAF_STRUCTURE_RETRY_TIMEOUT_SECONDS` | 研究脑图精简证据重试超时，默认 120 秒且不得大于首次超时 |
| `PAPERLEAF_MODEL_ATTEMPTS_PER_PROVIDER` | 每个服务的最大尝试次数，范围 1~3 |
| `PAPERLEAF_MODEL_CIRCUIT_FAILURE_THRESHOLD` | 连续失败熔断阈值 |
| `PAPERLEAF_MODEL_CIRCUIT_COOLDOWN_SECONDS` | 熔断冷却秒数 |

不配置 Key 时文献 CRUD、PDF 阅读、全文检索、引用校验和提取式产物仍可工作；全文翻译、向量、模型生成与视觉 OCR 会明确降级。主服务达到连续失败阈值后，相应用途会在冷却期内快速失败并切换备用服务；回答、证据核验、总结、翻译、嵌入与视觉 OCR 分别维护熔断状态，单项故障不会直接关闭全部模型能力。修改嵌入模型或维度后必须重新索引；主、备用嵌入模型也必须输出相同维度。

### DeepSeek 聊天与独立向量服务

DeepSeek 聊天接口不应被当作 Embeddings 接口使用。继续用 DeepSeek 生成回答时，把主服务的
`PAPERLEAF_EMBEDDING_ENABLED` 设为 `false`，再用 `PAPERLEAF_FALLBACK_*` 接入另一个
支持 OpenAI-compatible Embeddings 的服务。模型路由会继续优先用 DeepSeek 回答，并从第二
服务获取文档和查询向量。若第二服务只提供向量，应把 `PAPERLEAF_FALLBACK_CHAT_MODEL`
明确设为空；只有希望它承担聊天降级时才填写真实可用的备用聊天模型。

```dotenv
PAPERLEAF_OPENAI_BASE_URL=https://api.deepseek.com
PAPERLEAF_OPENAI_API_KEY=your-deepseek-key
PAPERLEAF_CHAT_MODEL=your-deepseek-chat-model
PAPERLEAF_EMBEDDING_ENABLED=false

PAPERLEAF_FALLBACK_OPENAI_BASE_URL=https://your-compatible-service.example/v1
PAPERLEAF_FALLBACK_OPENAI_API_KEY=your-embedding-service-key
PAPERLEAF_FALLBACK_CHAT_MODEL=
PAPERLEAF_FALLBACK_EMBEDDING_ENABLED=true
PAPERLEAF_FALLBACK_EMBEDDING_MODEL=your-embedding-model
PAPERLEAF_EMBEDDING_DIMENSIONS=your-model-output-dimensions
PAPERLEAF_EMBEDDING_BATCH_SIZE=8
```

如果服务或模型不接受 `dimensions` 参数，保持 `PAPERLEAF_EMBEDDING_DIMENSIONS` 为空；否则
必须填模型的真实输出维度。不要在同一批 Chunk 中混用不同模型或不同维度。修改配置后执行：

```bash
docker compose up -d --build api worker
```

新上传论文会自动生成向量。既有论文仍保留原 Chunk，但向量为空，因此可在文献库勾选多篇论文
执行“重新识别与索引”，也可逐篇进入“文献设置”重新处理。该操作会复用原始 PDF，重新解析、
使用当前 `structure_aware_v2` 策略切分并替换该论文的旧
Page/Chunk/Embedding。完成后可在“管理 → AI 能力状态”确认向量检索可用，并在 RAG 可观测性
面板确认新问答出现向量或混合召回通道。单次批量操作最多选择 100 篇；正在处理或删除中的论文会被安全跳过。

Windows/macOS 的 Docker Desktop 若使用宿主机 Ollama，应把备用服务根地址设为
`http://host.docker.internal:11434/v1`，Key 可使用非空占位值 `ollama`。本机浏览器或
PowerShell 直接探测 Ollama 时仍使用 `http://localhost:11434`。不要把只监听本机的
Ollama 端口暴露到公网。

问答提交只由 API 持久化，不在 Web 请求内运行模型。生产环境必须持续运行 Worker 才能处理
`agent_run` 作业；反向代理应关闭 SSE 响应缓冲并允许 `Last-Event-ID` 请求头。SSE 断线只影响
实时观察，重新连接会从 PostgreSQL 补发遗漏事件，不会重复提交模型请求。部署多个 Worker 时
必须共享同一 PostgreSQL，并保留作业租约与随机领取令牌，不能绕过仓储层直接写 Agent Run。

全文翻译会把已解析的单页文本发送给聊天模型，并把译文逐页保存在 PostgreSQL。它不会生成新的 PDF。生产部署应按最大 500 页文献估算数据库容量与模型费用，并确保 Worker 持续运行；API 重启或浏览器离开不会中止已创建的翻译作业。

论文总结与结构图会由 Worker 把当前论文的代表性页级证据发送给总结模型，并把验证后的结构化产物保存在 PostgreSQL。Web 请求只创建后台任务，不需要为反向代理开放数分钟的同步响应时间。修改或重新解析页文本后旧产物会标记过期；部署者应把 `paper_artifacts` 和 `jobs` 纳入数据库备份和保留策略。首次生成失败只保存中文原因和空产物，刷新失败则保留上一次成功结果。

## 反向代理

反向代理至少需要：

- 自动 HTTPS 与 HTTP 到 HTTPS 跳转
- `/api` 转发至 API，支持 SSE 且关闭响应缓冲
- PDF Range 请求透传 `Range` 与 `Content-Range`
- 上传大小不低于 `PAPERLEAF_MAX_PDF_BYTES`
- WebSocket 不是当前 SSE 流的必需条件

不要经反向代理公开 PostgreSQL、Redis、MinIO API 或 MinIO Console。

## 备份与恢复

一次一致性备份应同时覆盖 PostgreSQL 与 MinIO。写入频繁的实例应在维护窗口暂停 API/Worker，或使用能够保证时间点一致性的快照机制。

数据库逻辑备份示例：

```bash
docker compose exec -T postgres pg_dump -U paperleaf -Fc paperleaf > paperleaf-db.dump
```

MinIO 建议使用 `mc mirror` 复制到独立存储或对持久卷做快照。恢复前先验证备份可读，并在隔离环境完成一次演练。只恢复数据库或只恢复对象存储可能产生悬空记录。

## 升级

```bash
git pull --ff-only
docker compose build
docker compose run --rm migrate
docker compose up -d
docker compose ps
```

升级前阅读[更新记录](changelog.md)并备份。未发布版的层级集合迁移会永久删除旧标签及论文标签归属；如需保留历史信息，必须在迁移前自行导出。迁移失败时不要强行启动新 API，应保留日志并恢复到已验证的应用与数据版本。

## 可选：Railway

Railway 不是默认参考部署。若选择 Railway，需要分别创建 Web、API、Worker、PostgreSQL/pgvector 和 S3-compatible 对象存储：

1. Web 使用根目录 `Dockerfile`，API 与 Worker 使用 `backend/Dockerfile`。
2. API 启动命令为 `uvicorn paperleaf_api.main:app --host 0.0.0.0 --port $PORT`。
3. Worker 启动命令为 `python -m paperleaf_api.worker`，不分配公网域名。
4. 迁移作为发布前命令执行 `alembic upgrade head`，同一版本只执行一次。
5. PostgreSQL 必须启用 pgvector 扩展；平台提供的默认镜像不满足时使用 pgvector 镜像或外部数据库。
6. 不要把 PDF 保存在临时容器文件系统；连接外部 S3-compatible 存储或持久卷。
7. 将 Web 公网域名写入 CORS，并启用安全 Cookie 与 HTTPS。

平台计费、休眠和持久卷规则会变化，请在创建资源前核对当前政策。Compose 仍是项目唯一完整参考拓扑。

## 故障排查

```bash
docker compose ps
docker compose logs --tail=200 migrate postgres redis minio-init minio api worker prometheus grafana web
docker compose config --quiet
```

- `migrate` 失败：检查数据库连接、凭据和迁移日志。
- `minio-init` 失败：检查 MinIO 健康状态、账号密码和 Bucket 名。
- `/ready` 中 Redis 为 `degraded`：检查 `redis-cli ping`、连接 URL 和容器网络；API 会暂时使用单进程限流，多副本配额在恢复前不保证全局一致。
- Web 无法请求 API：检查构建时 API URL、CORS、HTTPS 混合内容和代理 SSE 配置。
- PDF 可读但无法检索：检查 Worker、解析状态、嵌入配置与向量维度。
- 扫描 PDF 检索为空：配置视觉模型或确认状态是否为 `ocr_unavailable`。
- 管理页显示模型“需检查”：查看对应服务和用途；等待冷却后系统会执行一次半开探测，也可先修复 Key、模型名或网络配置。

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
docker compose logs --tail=100 migrate minio-init api worker web
```

正常情况下，`migrate` 和 `minio-init` 以退出码 0 完成，`postgres`、`minio`、`api` 和 `web` 变为健康状态，`worker` 持续运行。

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

Compose 默认把公开端口绑定到 `127.0.0.1`。若明确需要从局域网访问，可以设置 `PAPERLEAF_BIND_ADDRESS=0.0.0.0`，同时配置防火墙与 HTTPS；不要因此暴露 MinIO 管理端口。

生产部署应让 Web 与 API 同域，或将 `PAPERLEAF_CORS_ORIGINS` 设置为明确的 HTTPS 来源。带凭据请求禁止使用 `*`。启用 HTTPS 后设置：

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

数据库和 MinIO 密码不应重复。Compose 会把 `POSTGRES_PASSWORD` 插入数据库连接 URL，因此应使用 URL-safe 随机字符。若使用外部托管服务，修改 Compose 环境变量或部署平台中的对应连接信息。

### 模型

| 变量 | 说明 |
|---|---|
| `PAPERLEAF_OPENAI_API_KEY` | OpenAI-compatible Key |
| `PAPERLEAF_OPENAI_BASE_URL` | 接口根地址 |
| `PAPERLEAF_CHAT_MODEL` | 问答与总结模型 |
| `PAPERLEAF_EMBEDDING_MODEL` | 嵌入模型 |
| `PAPERLEAF_EMBEDDING_DIMENSIONS` | 嵌入向量维度 |
| `PAPERLEAF_VISION_MODEL` | 可选 OCR 视觉模型 |

不配置 Key 时文献 CRUD、PDF 阅读、全文检索、引用校验和提取式产物仍可工作；向量、模型生成与视觉 OCR 会降级。修改嵌入模型或维度后必须重新索引，不能混用不同维度的向量。

## 反向代理

反向代理至少需要：

- 自动 HTTPS 与 HTTP 到 HTTPS 跳转
- `/api` 转发至 API，支持 SSE 且关闭响应缓冲
- PDF Range 请求透传 `Range` 与 `Content-Range`
- 上传大小不低于 `PAPERLEAF_MAX_PDF_BYTES`
- WebSocket 不是当前 SSE 流的必需条件

不要经反向代理公开 PostgreSQL、MinIO API 或 MinIO Console。

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

升级前阅读[更新记录](changelog.md)并备份。迁移失败时不要强行启动新 API，应保留日志并恢复到已验证的应用与数据版本。

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
docker compose logs --tail=200 migrate postgres minio-init minio api worker web
docker compose config --quiet
```

- `migrate` 失败：检查数据库连接、凭据和迁移日志。
- `minio-init` 失败：检查 MinIO 健康状态、账号密码和 Bucket 名。
- Web 无法请求 API：检查构建时 API URL、CORS、HTTPS 混合内容和代理 SSE 配置。
- PDF 可读但无法检索：检查 Worker、解析状态、嵌入配置与向量维度。
- 扫描 PDF 检索为空：配置视觉模型或确认状态是否为 `ocr_unavailable`。

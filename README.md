# PaperLeaf

PaperLeaf 是一个面向科研阅读的开源个人文献库。它把 PDF 保存、页级检索、带原文页码的问答、论文总结和结构图放在同一个工作台中，并通过受控 Agent 协调文库检索与 arXiv 搜索。

> 当前版本处于早期开发阶段。建议先在本机或受控网络中部署，并在升级前备份数据库与对象存储。

[在线体验固定数据 Demo](https://paperleaf-demo.chenlin1318.chatgpt.site/demo) · [查看部署指南](docs/deployment.md)

![PaperLeaf 三栏论文阅读与证据问答工作台](docs/images/paperleaf-workspace.png)

## 功能

- 上传、保存、查看、修改和删除 PDF 文献
- 集合与标签 CRUD、真实归属筛选、最近阅读、待整理和批量归档
- 文献列表、元数据编辑、处理状态和用户范围内的全文检索
- 按物理页解析与切块，回答附带可跳转的页码证据
- 向量检索、关键词检索与 RRF 融合，不依赖一键式 RAG Chain
- 混合候选按物理页去重并合并通道信号；弱匹配时拒答
- 回答生成后逐条核验主张引用与证据支持；缺引用、伪引用或支持不足时不返回结论
- 使用 LangGraph 编排有界条件路由、人工确认、持久恢复与引用校验
- Agent 通过 SSE 展示可公开的节点进度与耗时，运行中的任务可被真正取消
- 主/备用 OpenAI-compatible 服务统一经过超时、受控重试和按用途隔离的熔断器
- 搜索 arXiv，并在用户确认后导入开放 PDF
- 后端 API 生成论文总结和带证据的结构图
- 管理员创建、停用用户并查看脱敏的模型运行状态；默认不读取用户文献内容
- 未配置模型时仍可使用文献管理和 PDF 阅读功能

当前 `0.8.x` 的[公开 Demo](https://paperleaf-demo.chenlin1318.chatgpt.site/demo)使用固定文献和确定性 AI 产物，便于在不上传文件、不配置模型的情况下检查工作流。`/demo` 会显式绑定固定数据源，并可继续进入带集合、标签和批量整理能力的演示文献库；跨文献提问会展示与真实 SSE 契约一致的 Agent 运行轨迹和回答核验状态。Docker Compose 构建固定使用 `real` 数据模式并连接 FastAPI。

## 快速开始

### 环境要求

- Docker Engine 24+ 与 Docker Compose v2
- 至少 4 GB 可用内存；处理大 PDF 或运行本地模型时需要更多资源

### 启动

```bash
git clone https://github.com/FrostWane/PaperLeaf.git
cd PaperLeaf
cp .env.example .env
```

Windows PowerShell 可使用：

```powershell
Copy-Item .env.example .env
```

打开 `.env`，至少替换以下值：

- `POSTGRES_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `PAPERLEAF_SESSION_SECRET`
- `PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL`
- `PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD`

然后启动：

```bash
docker compose up -d --build
docker compose ps
```

访问：

- Web：<http://localhost:3000>
- API 健康检查：<http://localhost:8000/health>
- MinIO 控制台：<http://localhost:9001>

首次启动会自动执行数据库迁移、创建私有 PDF Bucket，并根据环境变量创建管理员。查看日志：

```bash
docker compose logs -f api worker web
```

## 配置 AI

PaperLeaf 使用 OpenAI-compatible 接口，既可以连接云端模型，也可以连接提供兼容接口的本地服务。核心变量如下：

| 变量 | 说明 |
|---|---|
| `PAPERLEAF_OPENAI_API_KEY` | 服务端 API Key；不要以 `NEXT_PUBLIC_` 开头 |
| `PAPERLEAF_OPENAI_BASE_URL` | 兼容接口根地址 |
| `PAPERLEAF_CHAT_MODEL` | 问答与总结模型 |
| `PAPERLEAF_EMBEDDING_MODEL` | 向量模型 |
| `PAPERLEAF_EMBEDDING_DIMENSIONS` | 向量维度，必须与模型输出一致 |
| `PAPERLEAF_VISION_MODEL` | 可选；低文本页 OCR 使用的视觉模型 |
| `PAPERLEAF_FALLBACK_OPENAI_API_KEY` | 可选备用服务 Key；不配置则只使用主服务 |
| `PAPERLEAF_FALLBACK_OPENAI_BASE_URL` | 备用 OpenAI-compatible 根地址 |
| `PAPERLEAF_FALLBACK_CHAT_MODEL` | 备用问答与总结模型 |
| `PAPERLEAF_FALLBACK_EMBEDDING_MODEL` | 备用向量模型；输出维度必须与主模型一致 |
| `PAPERLEAF_MODEL_TIMEOUT_SECONDS` | 单次模型调用超时 |
| `PAPERLEAF_MODEL_ATTEMPTS_PER_PROVIDER` | 每个服务最多尝试次数，范围 1~3 |
| `PAPERLEAF_MODEL_CIRCUIT_FAILURE_THRESHOLD` | 连续失败多少次后打开熔断器 |
| `PAPERLEAF_MODEL_CIRCUIT_COOLDOWN_SECONDS` | 熔断后的冷却时间 |

修改嵌入模型或维度后，需要对既有文献重新建立索引。未配置 API Key 时，生产环境不会把文献发送给任何模型：系统保留全文检索、引用校验和提取式产物，但不会生成向量、调用模型回答或执行视觉 OCR。

完整环境变量和生产部署注意事项参见[部署指南](docs/deployment.md)。

## 基本使用

1. 管理员登录后在“管理”页面创建普通用户。
2. 在“文献库”上传 PDF，等待状态变为“索引就绪”。
3. 创建集合和标签，勾选多篇文献进行批量归类、标记或归档；“最近阅读”和“待整理”数量会随真实状态更新。
4. 打开文献，在阅读器右侧提问；点击回答后的引用可跳转到对应物理页。
5. 在论文助手中切换“概览”或“结构”，生成带证据页的总结与 Mermaid strict mode 结构图。
6. 在左侧“文献设置”中修改元数据；解析失败或部分可用时可重新处理，删除操作需要二次确认。
7. 在“全库问答”中选择单篇、多篇或全库范围。
8. 在“发现”中搜索 arXiv；PaperLeaf 只会在确认后创建导入任务。

扫描版 PDF 需要配置视觉模型才能补充 OCR。未配置 OCR 时，原始 PDF 仍可保存和阅读，但检索覆盖可能不完整。

## 数据与隐私

- PDF 原件保存在私有 MinIO Bucket 中，不公开对象地址。
- 数据库保存文献元数据、页文本、检索块、向量、后台任务和 LangGraph Checkpoint。
- 模型 Key 只由 API/Worker 读取，不下发浏览器。
- 管理员负责账号和任务管理，默认没有读取用户 PDF 与提问内容的产品入口。
- 启用外部模型会把完成当前请求所需的文本证据或 OCR 页面图像发送给相应提供方，请结合其数据政策自行判断。
- PaperLeaf 不用于绕过付费墙，只导入用户上传或允许来源中的开放文件。

部署者应配置 HTTPS、强密码、备份、最小化网络暴露和符合所在地区要求的数据保留策略。详细边界参见[安全说明](docs/security.md)。

## 当前限制

- V1 只内置 arXiv 搜索，不支持任意网页抓取。
- 暂不支持 Zotero 同步、RIS/BibTeX、团队协作批注和原生移动端。
- OCR、问答和向量质量取决于所配置模型。
- 引用校验可以降低伪造引用风险，但不能替代阅读原文和科研判断。
- 大型 PDF、复杂公式、双栏排版和扫描质量会影响解析效果。

## 本地开发与测试

前端：

```bash
corepack enable
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm storybook:build
pnpm exec playwright install chromium
pnpm test:e2e
```

后端：

```bash
cd backend
python -m venv .venv
python -m pip install -e ".[dev]"
python -m compileall paperleaf_api tests
ruff check paperleaf_api tests
pytest
```

仓库另带自建冻结集与基于 QASPER 人工问题的“公开校准集 + 私有答案 holdout”，以及无需
模型密钥即可复现的哈希向量、BM25、RRF、页去重和拒答基线；可选依赖还可运行本地 ONNX
dense 与 Cross-Encoder 诊断。PDF 不随仓库重新分发。数据来源、许可、预注册锁、真实
分子/分母、未泛化的负向结果与指标边界见
[RAG 离线评测说明](backend/evaluation/README.md)。

容器配置：

```bash
docker compose --env-file .env.example config --quiet
# 启动服务并在 .env 中配置管理员后，可执行临时数据闭环
python scripts/smoke_compose.py
```

测试策略、场景与质量门禁参见[测试指南](docs/testing.md)。

## 架构

```mermaid
flowchart LR
    U["浏览器"] -->|"REST / SSE"| W["Next.js Web"]
    W --> A["FastAPI"]
    A --> P[("PostgreSQL + pgvector")]
    A --> M[("MinIO 私有 PDF")]
    A --> G["LangGraph Agent"]
    Q["Python Worker"] --> P
    Q --> M
    Q --> L["OpenAI-compatible 模型"]
    G --> R["自研页级 RAG"]
    R --> P
```

进一步了解数据流、权限边界和 Agent 设计，请阅读[架构说明](docs/architecture.md)。

## 文档

- [架构说明](docs/architecture.md)
- [部署指南](docs/deployment.md)
- [测试指南](docs/testing.md)
- [RAG 离线评测](backend/evaluation/README.md)
- [安全说明](docs/security.md)
- [贡献指南](docs/contributing.md)
- [更新记录](docs/changelog.md)

## 参与贡献

欢迎提交可复现的问题、文档修正、测试和功能改进。提交代码前请先阅读[贡献指南](docs/contributing.md)，并确保前后端测试通过。

## 许可证

PaperLeaf 基于 [Apache License 2.0](LICENSE) 发布。依赖项和字体遵循各自许可证；仓库内自托管的 Geist 字体保留其 [SIL Open Font License 1.1](public/fonts/LICENSE-Geist.txt)。

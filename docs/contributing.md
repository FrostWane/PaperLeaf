# PaperLeaf 贡献指南

感谢参与 PaperLeaf。我们欢迎问题复现、测试、文档、无障碍改进和功能实现。

## 开始之前

1. 搜索现有 Issue 与 Pull Request，避免重复工作。
2. 大功能、数据模型变化、安全策略和新外部来源应先提出设计讨论。
3. Issue 中不要上传私人文献、凭据、运行 Token 或敏感日志。

## 开发环境

前端需要 Node.js 22：

```bash
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

后端需要 Python 3.11：

```bash
cd backend
python -m venv .venv
python -m pip install -e ".[dev]"
uvicorn paperleaf_api.main:app --reload
```

完整依赖可以使用 Docker Compose：

```bash
cp .env.example .env
docker compose up -d postgres minio minio-init
```

本地凭据只放在 `.env`，不要提交。

## 设计原则

- 权限、所有权和工具授权在服务端校验。
- LangGraph 负责编排，不接管业务 CRUD 与核心检索。
- RAG 改动必须能用固定数据集独立评测。
- Chunk 不跨物理页，引用保留物理页和证据块。
- 确定性流程优先使用普通服务与后台任务，不强行转换成 Agent 节点。
- 前端保持科研工作台的克制视觉，不加入无功能意义的装饰。

## 代码与测试

提交前运行：

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm storybook:build
pnpm test:e2e

cd backend
python -m compileall paperleaf_api tests
ruff check paperleaf_api tests
pytest
```

涉及容器时额外运行：

```bash
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example build web api worker
```

新增行为应包含成功、失败、权限与边界测试。前端交互同时测试键盘与移动视口。检索改进须提交原始计数和可复现协议，不只提交汇总百分比。

## Pull Request

- 保持一次 Pull Request 只解决一个清晰问题。
- 描述问题、方案、验证命令、兼容性和风险。
- 界面变化附桌面与移动截图，说明键盘和无障碍检查结果。
- 数据库变化附向前迁移，并说明是否可回滚和如何备份。
- API 变化同步更新 OpenAPI 类型和公开文档。
- 不提交生成目录、真实 `.env`、模型输出缓存或私人测试数据。

维护者可能要求缩小范围、增加测试或补充迁移说明。合并不保证立即发布。

## 文档语言

面向用户的文档以中文为主。技术标识、API 字段和标准名称保持原文，并在首次出现时说明含义。

## 许可证

提交贡献即表示你有权提交相关内容，并同意按项目的 Apache License 2.0 发布。第三方代码、字体、图标和数据必须保留兼容许可证与必要归属。

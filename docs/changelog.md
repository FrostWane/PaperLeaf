# 更新记录

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。早期开发阶段的接口仍可能调整，升级前请阅读对应版本说明并备份数据。

## 未发布

### 新增

- Next.js 文献工作台与固定数据演示模式。
- FastAPI 用户、文献、任务、问答和管理员接口。
- PostgreSQL/pgvector、MinIO 与后台 Worker 基础设施。
- 按物理页解析、混合检索、RRF、引用校验与证据不足拒答。
- LangGraph 受控 Agent、SSE 状态、arXiv 搜索与确认导入。
- Docker Compose、自检、持续集成与中文部署文档。

### 安全

- 私有对象存储、用户范围过滤、管理员内容边界与上传校验。
- Agent 工具白名单、步数限制、人工确认和文献提示词注入防护。

## 0.1.0

- 建立 PaperLeaf 开源项目骨架。

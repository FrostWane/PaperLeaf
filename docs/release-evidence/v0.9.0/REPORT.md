# PaperLeaf v0.9.0 发布加固报告

生成日期：2026-08-15  
实现提交：`4c6b775d86915d13d7511f204b52f065ca90b9a9`  
发布定位：portfolio release，非 production GA

## 结论

本机功能、测试、迁移、隔离全栈与备份恢复门禁已经通过；GitHub CI、安全扫描、最终提交上的隔离 Compose 复跑和 GitHub Social Preview 仍待完成。因此当前状态为 **release blocked**，未创建 `v0.9.0` tag 或 Release。

Specialist v3 保持默认关闭。历史冻结 RAG 与多 Agent 指标未被改写；当前 HEAD 只校验证据完整性，不声称重新复现历史结果。

## P0 状态

| 项目 | 状态 | 证据与说明 |
|---|---|---|
| 隔离真实模式 full-stack smoke | 本机通过，最终提交 CI 待跑 | 空卷、随机密钥、管理员创建用户、普通用户改密、上传 PDF、Worker 索引、Agent Run completed、SSE Last-Event-ID、合法页码引用、PDF Range、前端跳页、管理员 RAG trace 均通过。使用确定性模型 stub，不评价回答质量。 |
| `/health`、`/ready` 与 Worker 健康 | 通过 | 停止 Worker 后 readiness 降级为 `worker_heartbeat_stale_or_missing`；恢复后自动变为 `queue_poll_recent`。 |
| v0.9.0 版本与项目文档 | 通过 | Web、Python 包与 FastAPI 版本统一；README、截图、CHANGELOG、License/CI/Release badge 已更新。Tag/Release 尚未创建。 |
| 环境变量契约 | 通过 | `.env.example`、Settings、Compose API/Worker 自动比对；三个 `PAPERLEAF_ANSWER_MIN_*` 非默认值在 API 与 Worker 均有测试。 |
| 正式评测证据包 | 通过完整性校验 | 修复 `evidence_index.json`；保留 `human_review_pending`、`quality_pending`、历史 SHA 与 `64.2% → 61.6%` 负结果。 |

## P1 状态

| 项目 | 状态 | 证据与说明 |
|---|---|---|
| PostgreSQL + MinIO 备份恢复 | 通过 | 计划停写一致快照；隔离恢复后用户归属、1 页、1 Chunk、1 条合法引用及 PDF Range 均通过；本机单次 RTO `23.129s`，停写窗口内确认写入 RPO `0s`。这不是生产 SLA 或 PITR。 |
| 崩溃、租约接管与 fencing | 通过自动化测试 | SIGTERM、Agent/PDF 任务异常、租约重领、旧 claim token 拒写纳入后端测试；历史真实 Worker 强杀证据仍按原 SHA 保存。 |
| Python 依赖锁、SBOM、漏洞扫描 | 部分完成 | `requirements.lock` 已用于本地后端镜像；SBOM、Trivy CRITICAL 与 gitleaks 由 GitHub CI 执行，本机未安装对应扫描器。 |
| 测试与部署文档 | 通过 | 明确区分 Mock、Demo E2E、isolated Compose、真实模型与人工评审。 |
| production preflight | 通过自动化测试 | production 模式拒绝弱密钥、默认口令、不安全 Cookie、HTTP API 地址与非 HTTPS CORS。 |

## 测试结果

- 后端：`596 collected`，`588 passed / 8 skipped`；Ruff 与 compileall 通过。
- 前端：Vitest `122/122`；TypeScript 通过；ESLint `0 error / 2` 条 TanStack Table 已知兼容提示。
- Playwright：`52 passed / 8 skipped`，覆盖 2K、桌面、平板和移动端；axe serious/critical 为 0。
- 生产构建：前端构建、Storybook、Lighthouse 均通过。
- 迁移：真实 PostgreSQL fresh upgrade、downgrade、re-upgrade 与 `alembic check` 通过。
- 容器：API、Worker、migrate、academic-search-mcp 镜像通过；Web 依赖层因本机镜像源最后一个包持续超时未完成。Dockerfile 已增加共享 pnpm BuildKit 缓存，重试已验证 465 个生产依赖全部复用；最终结果以 GitHub CI 为准。

## 管理员与普通用户闭环

隔离 smoke 使用两个独立会话执行以下流程：

```text
管理员登录
→ 读取用户、任务、RAG、Harness
→ 创建普通用户和临时密码
→ 管理员退出
→ 普通用户首次登录并改密
→ 验证普通用户访问管理接口返回 403
→ 上传 PDF、等待索引、发起问答
→ SSE 断线重连、回答与引用、PDF Range
→ 普通用户退出
→ 管理员重新登录
→ 最近 7 天 RAG telemetry 至少包含刚完成的 Run
```

本地真实库的 RAG 管理页问题也已只读核对：24 小时为 `0/0`，7 天为 `716 Run / 706 trace`，30 天为 `752 Run / 708 trace`。根因是默认 24 小时窗口没有新 Run，而非 trace 丢失；界面已改为默认 7 天，并保留时间范围空状态。

## 原始证据与 SHA-256

| 文件 | SHA-256 |
|---|---|
| `docs/release-evidence/v0.9.0/full-stack-smoke.json` | `6a720ed494452b0fe52a2f7e2f60e3431e543ba9629a78a9d4706ebbc978b153` |
| `docs/release-evidence/v0.9.0/backup-restore.json` | `7e2ef7f278924de0a78f5d34c90730b94f162ab4da5a7f17c4f233ff6597488b` |
| `docs/release-evidence/v0.9.0/admin-rag-diagnosis.json` | `577012902fcce0729db76a50797004f6cea16881ed5a768e8e46227130e68e51` |
| `backend/evaluation/results/paperleaf-formal-evaluation-20260814/evidence_index.json` | `bf5d1e1b65bf822eead089c226a97a537945cd3bdd3569e0e9cb4ae979ad4d9d` |

`full-stack-smoke.json` 与 `backup-restore.json` 的 `git_sha` 是运行时仓库中尚未提交的父 SHA；本报告不会把它伪装成最终提交的精确复跑。实现提交包含当时已测工作树，后续 UI 轨迹修复另由 Vitest、Playwright、构建和真实浏览器截图覆盖。最终提交上的隔离 Compose 证据以 GitHub CI artifact 为准。

## 残余边界与发布阻塞

完整边界见 [`docs/known-limitations.md`](../../known-limitations.md)。当前阻塞项：

1. 最终 GitHub CI 尚未运行，gitleaks、SBOM、Trivy 和干净 Linux Web 镜像构建没有最终结论；
2. GitHub Social Preview 尚未上传；
3. 最终提交的 isolated Compose artifact 尚未生成；
4. 真实 DeepSeek/Ollama 是人工发布门禁，不由 stub smoke 代替；
5. 48 例 v1/v2/v3 完整对照与人工盲评仍为 `quality_pending`。

上述 P0 未全部转绿前，不创建 `v0.9.0` Release。

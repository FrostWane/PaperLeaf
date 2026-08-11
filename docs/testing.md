# PaperLeaf 测试指南

## 质量目标

测试按“确定性业务、检索质量、Agent 控制、用户界面、部署安全”分层。任何准确率或性能改进必须由相同数据、相同协议和可定位的提交支持。

## 本地快速检查

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
pnpm lighthouse
```

后端：

```bash
cd backend
python -m pip install -e ".[dev]"
python -m compileall paperleaf_api tests
ruff check paperleaf_api tests
pytest
```

容器：

```bash
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example build web api worker
# 服务启动后，从环境变量读取管理员凭证并执行临时数据闭环
python scripts/smoke_compose.py
```

GitHub Actions 对拉取请求执行上述检查并运行密钥扫描。后端任务会启动独立 pgvector PostgreSQL，依次验证全新 `upgrade head`、回退到 0.2.0 结构、再次升级和 `alembic check`，避免只在内存仓储上通过。

当前仓库的里程碑 4 本地门禁为：Pytest 169 项通过、5 个需要测试数据库的用例明确跳过，Vitest 67 项通过，TypeScript、ESLint 和生产构建通过。另在隔离 PostgreSQL 中定向验证了持久会话的并发幂等与迁移往返。Playwright 首轮为 46 项通过、8 项按视口设计跳过、6 项失败；六个失败归为 Demo 重载持久化、旧字号选择器和滚动区焦点三类，修复后的对应定向用例 8/8 通过，2K 字号用例通过，390/768/384（200% 等效）布局门禁通过。为避免重复消耗，本地未再次运行完整套件，提交后的 GitHub Actions 是第二层全量门禁。CI 的后端任务强制连接名为 `paperleaf_test` 的真实 pgvector PostgreSQL，并在 Pytest 前执行 `alembic upgrade head`。

2026-08-08 管理页后台任务分页定向验证：`tests/admin-view.test.tsx` 7/7 通过，TypeScript 通过，定向 ESLint 0 错误；Docker 生产构建与 Web/API 健康检查均通过。分页用例覆盖每页 15 条、翻页、末页数量和末页按钮禁用。

2026-08-08 Ollama/pgvector 真实闭环：`qwen3-embedding:0.6b` 从 Worker 返回 1024 维；四篇论文共 196 个 Chunk 全部写入 196 个向量。中文问题检索 DeepDTA 的前五条证据均同时命中 `keyword_rewrite` 与 `vector` 通道。兼容、批次、配置、模型路由专项 35/35 通过，Ruff 通过。

2026-08-08 批量重新索引与管理页分区：后端接口与权限定向 12/12 通过，验证重复 ID 去重、活动解析任务不重复入队和中文冲突原因；前端管理页及数据源契约 23/23 通过，TypeScript 与 Ruff 通过。浏览器在 2560×1440 和 768×1024 检查默认概览、内部工作区切换与横向溢出，详细 AI 能力默认折叠。

里程碑 5 后端唯一一次全量为 176 passed / 5 skipped，隔离 PostgreSQL Artifact 缓存与 0008 升降级通过；审查修复后的连通性、五节非空、坏缓存重建和强制刷新定向测试 17/17 通过。前端受影响测试 27 项、桌面/手机 Demo E2E 2/2、类型、ESLint 和生产构建通过；强制刷新新增契约 17 项中 16 项首轮通过，唯一结构图测试因 5 秒预算不足超时，将预算按动态 Mermaid 导入调整为 10 秒后该文件 3/3 通过。没有把超时写成业务正确性失败。

## 当前后端自动化范围

2026-08-11 TaskFrame 与 Run 级 ProviderPolicy 返工：后端收集 436 项，430 项通过、6 项因
可选外部基础设施跳过，Ruff 全绿；确定性 Harness 100/100。最终真实 DeepSeek + OpenAlex /
Semantic Scholar / arXiv + PostgreSQL + Redis 矩阵为 5/6：三类模型 TaskFrame 均走
`model_function_call`，纯数量修改保留年份和来源，纯来源修改保留数量、年份和历史候选；唯一
失败是 Semantic Scholar 429 后没有候选。该 Run 的 ProviderPolicy 明确禁止 OpenAlex/arXiv，
没有发生旧兜底越权，也没有把空表格算成推荐成功。完整证据见
`docs/reports/2026-08-11-task-frame-and-provider-policy.md`。

2026-08-10 论文发现与外部工具可靠性门禁：后端全量 407 passed / 6 skipped；前端
21 个文件、113 项 Vitest 全部通过；TypeScript、Ruff、ESLint（0 错误）和生产构建通过。
新增 38 项确定性多轮矩阵，覆盖 `5 篇`、`五篇`、`five papers`，来源指定、来源排除、
年份和下一轮覆盖。`context-harness-v1` 确定性评测 100/100 且最终输入超限为 0。
真实 DeepSeek + OpenAlex/Ollama/PostgreSQL/Redis/MCP 矩阵为 10/10，
部署工具限次补丁后的复测为 4/4。外部 Provider 限流单独记录为上游降级，不计为网络性能承诺，
也不允许用本地 PDF 或模型知识补造书目候选。

- 生产占位密钥与过短密钥拒绝。
- 登录、首次修改密码门禁、CSRF、停用用户会话撤销与跨用户文献隔离。
- 当前用户昵称与阅读/AI 偏好持久化、部分更新、输入边界，以及首次改密用户仍可打开设置页。
- 最后一名管理员与当前管理员保护返回具体中文原因；退出登录后原会话立即失效。
- 层级集合归属与递归范围、最近阅读、批量整理/归档、跨用户批量操作隔离、管理员任务列表与失败任务重试。
- 集合循环、跨用户父节点、五层上限、同级重名、删除提升子节点，以及标签表和既有标签数据被迁移明确删除。
- 出版物本地识别、Crossref 超时/错误/缓存，以及并发用户编辑不被迟到的外部查询覆盖。
- PDF 文件头、单段与后缀 Range、本地对象幂等删除。
- 论文删除作业只入队一次。
- 全文翻译创建与复用、目标语言白名单、当前页优先、无文本页、逐页失败隔离、取消、失败页重试、重索引失效与再次创建。
- 翻译 Worker 的任务租约、随机领取令牌、过期任务回收、分块心跳、租约过期立即停止模型调用、迟到结果拒绝、错误码脱敏与单翻译唯一 Job。
- PostgreSQL 专项验证并发创建不重复入队、取消或来源变更后旧 Worker 不可写回，以及生命周期重启仍沿用一致的行锁顺序。
- 页内切块参数、RRF、页级去重与通道信号合并、引用页/片段校验与非法引用拒绝。
- Agent 有证据回答、无证据拒答、非空但弱匹配拒答、检索相关但答案不受支持时拒答、伪造引用抑制、arXiv 导入前中断、用户/运行级线程隔离和应用重建后恢复。
- 发现推荐覆盖种子论文轮换、arXiv 批次偏移、库内/历史曝光论文去重、摘要缺失时代表 Chunk 补全、Embedding 语义排序、Embedding 故障时关键词降级、兴趣正负反馈调权、最近批次跨页面恢复、反馈幂等更新、点击/兴趣/导入指标口径、空文献库、联网偏好前后端门禁与 `no-store`。
- 持久会话与消息跨请求保存、跨用户隔离、同一会话单活动 Run，以及 `Idempotency-Key` 在并发提交下只创建一组消息、Run 和作业。
- Context 专项覆盖 100 组冻结样本、物理页/选中文字权限校验、低置信度澄清、50 轮压缩、Token 预算、Tool Call/Result 配对和摘要不得成为 Citation Evidence。
- 长期记忆覆盖只从用户原话提取、相同内容幂等、修改产生版本、停用/删除不再选择、200 条容量和跨用户隔离；Embedding 不可用时仍通过关键词路径工作。
- Skill Registry 覆盖七类科研任务的稳定路由、按需加载、版本留痕、非法字段、重复声明与未知工具的启动失败；Feature Flag 关闭时验证旧 Agent 链路不变。
- Function Calling 覆盖强类型 Schema、身份参数注入、跨用户论文 ID、未知工具、最多四步/并行三工具、参数单次修正、超时重试、调用 ID 复用、Provider 降级、大结果外置，以及 `request_import` 在批准前零写入、拒绝与重复 Resume 幂等。
- 学术 MCP 覆盖固定主机与私网拒绝、只读工具集合、恶意 Schema、脚本/内网结果链接清洗、Redis 缓存、超时与熔断、联网授权、管理员/CSRF 门禁，以及外部元数据不得成为页级 Citation Evidence。
- Harness 管理页覆盖接口失败不伪装零数据、独立时间窗口迟到响应门禁、隐私标记、截断提示、MCP 防重复操作、中文错误与键盘页签。
- Agent Run 由 Worker 作业执行；事件序号、SSE `Last-Event-ID` 补发、心跳、断线后继续运行、重复 Resume、取消、失租 Worker 拒绝写回和删除会话竞态均有自动化覆盖。
- 模型增量先缓冲为完整段落；非法引用、超出范围证据或支持不足的段落不得进入事件表或消息表，合法增量可以精确重构最终消息。
- 总结五节非空、逐事实引用、结构图 5～12 节点、从研究问题可达全部节点、非法端点/循环/越权引用，以及失败原因和一次紧凑重试。
- Artifact 同版本缓存、强制刷新、旧坏缓存重建、重索引 stale 和跨用户隔离。
- Artifact 后台幂等入队、Worker 租约写入、离页轮询恢复、失败不覆盖旧成功结果，以及英文事实/英文摘录不落库不渲染。
- 模型主备切换、超时、熔断、半开恢复、取消传播、脱敏尝试记录，以及生产 Graph 重建时质量策略不丢失。
- SSE 真实节点开始/完成顺序、运行耗时与轨迹持久化；运行中取消会停止 Graph，重复取消保持幂等。
- 冻结评测集生成与配额、论文 ID/页码、页级 Recall/MRR、跨论文引用、检索通道和严格拒答校准。

当前 Pytest 主要使用内存仓储和本地临时对象存储；可选 PostgreSQL 集成用例会拒绝连接名称不含 `test` 的数据库，并通过真实行锁验证最后一名管理员保护和改密响应一致性。本轮本地临时 PostgreSQL 结果为 2 项通过。上述 Compose 冒烟还补充验证了 PostgreSQL/pgvector、MinIO、真实迁移和 Worker 领取任务；故障注入与完整 Testcontainers 编排仍待扩充。后续自动化容器测试必须使用临时数据库与独立 Bucket，不能连接开发者的真实数据。

Redis 专项覆盖固定窗口、幂等键不重复计数、标识哈希、`429 + Retry-After` 契约以及连接故障后的进程内降级。Compose 验证还必须检查 `redis-cli ping`、`/ready` 的 `available` 状态，并在停止 Redis 后验证 API 不因短期运行态故障退出。多 API 实例共享配额将在容量里程碑的负载测试中验证，未完成前不宣称已有跨机器压测结果。

2026-08-07 Redis 基础落地验证：后端完整回归 206 passed / 6 skipped；真实 Redis 7.4.10 返回 `PONG`，正常 `/ready` 为 `available`。停止 Redis 后 `/ready` 为 `degraded`，限流退化为 `memory-fallback` 且 API 保持运行；恢复 Redis 后重新可用。真实 TTL 冒烟确认同一幂等键在窗口内不重复计数，窗口结束后不会被旧判定继续阻塞。本轮没有执行并发容量压测，因此不报告吞吐或延迟提升。

2026-08-07 RAG 可观测性里程碑验证：Python 3.11 临时测试镜像完整回归为 216 passed / 6 skipped；前端完整 Vitest 为 91/91，最终管理员面板的加载隔离、无样本口径、窗口竞态与数据源契约定向测试为 21/21，TypeScript 与 ESLint 为 0 error。Compose 中 Prometheus 的 API、Worker 两个采集目标均为 `up=1`，Grafana 自动装载 `PaperLeaf RAG` 面板并成功查询 Prometheus 数据源；Redis 实测约使用 1.2 MiB，配置上限为 256 MiB。两次虚构 SyntheticDTA 闭环分别耗时 8.684 秒与 4.779 秒，均生成持久 RAG Trace 和 Prometheus 阶段指标。该样本只证明观测链路可用，不代表检索质量、吞吐或稳定延迟已经达标；当前开发库仍含升级前 Run，因此页面会同时展示原始覆盖数和低覆盖警告。

## RAG 评测

`backend/evaluation/datasets/paperleaf-rag-v1` 已冻结 20 篇 arXiv 精确版本和 120 个问题。仓库只分发注释与官方下载链接，不重新分发论文 PDF。问题覆盖：

- 单页事实
- 跨页综合
- 方法与实验结果
- 具有相似术语的干扰文献
- 文库中不可回答的问题
- 文献内提示词注入与伪工具指令

主要指标：

| 指标 | 目标 |
|---|---:|
| Recall@5 | ≥ 85% |
| 关键事实正确率 | ≥ 80% |
| 引用物理页准确率 | ≥ 95% |
| 有依据事实的引用覆盖率 | ≥ 90% |
| 不可回答问题错误作答率 | ≤ 5% |
| 非法引用 | 0 |

每次实验保存数据集版本、基线与候选提交、模型、提示词、随机设置、原始计数、绝对变化、相对变化、失败样本和运行时间。协议或数据不同的结果不得直接比较。

当前无密钥下限的 test 结果为：BM25 Recall@5 71.1%，RRF 73.3%，页去重 75.6%；严格拒答把不可回答错误作答率从 100.0% 降至 0.0%，但引用覆盖率也降至 31.2%。因此当前只达到“非法引用为 0”和“不可回答错误作答率 ≤5%”两项门槛，不能宣称整体 RAG 已达标。完整分子、分母、负向消融和解释边界见[冻结评测报告](../backend/evaluation/results/paperleaf-rag-v1/REPORT.md)。

### 2026-08-07 本地真实模型回归样本

以下数据是同一台本地 Docker Compose 环境、当前 DeepSeek 配置下的单次闭环观测，只用于回归定位，不作为跨环境性能承诺：

| 场景 | 结果 | 总耗时 | 回答长度 | 合法引用 |
|---|---:|---:|---:|---:|
| DeepDTA 结构化概括 | 完成 | 33.88 s | 1217 字符 | 6 |
| DeepDTA 两句核心创新与局限 | 完成 | 15.51 s | 436 字符 | 3 |
| DeepDTA 与 AttentionDTA 跨文献比较 | 完成 | 30.68 s | 1421 字符 | 5，覆盖 2 篇论文 |

优化前，同一概括链路因为 30 秒回答超时后输出原文摘录、以及重复调用证据支持模型，曾出现约 66.81 秒后仍被误拒绝。当前实现把查询改写限制为 6 秒、回答预算独立放宽到 60 秒、移除重复在线模型门禁，并通过短引用别名避免 DeepSeek 截断长 Chunk ID。上述三次回归均未出现英文原文直出或非法引用。

0.4.0 把线上质量规则接入同一评测器后，在已经用于诊断的 v1 test 上得到：页级 RRF Recall@5 仍为 68/90（75.6%）；确定性质量门禁把不可回答错误作答从 10/10 降到 1/10，但引用覆盖率为 22/80（27.5%），仍未达到发布目标。BGE-small、整段 Cross-Encoder、句窗 Cross-Encoder 和窗口 BM25 均未超过页级 RRF，因此未进入默认链路。详见[0.4 诊断报告](../backend/evaluation/results/paperleaf-rag-v1/DIAGNOSTIC-0.4.md)。这些数字不是新盲测结果。

0.6.0 引入 QASPER 外部人工问题：validation 校准集包含 29 篇论文、60 问，test 隐藏集包含 55 篇论文、120 问。隐藏答案与 183 个证据锚点保存在仓库外，`lock.json` 在评分前固定输入、候选、协议和检索实现哈希。唯一一次盲测中，页级 RRF 与自适应向量门控均得到页召回 132/167（79.0%）、证据组完整命中 82/96（85.4%）和 MRR@5 61.4%；校准集上的 MRR 增益没有泛化，所以候选未进入生产默认链路。纯检索协议对不可回答题错误作答 24/24，明确未达产品门槛。详见[首次盲测报告](../backend/evaluation/results/paperleaf-qasper-holdout-v1/REPORT.md)。holdout 已揭盲，后续重跑只能标记为诊断。

## Agent 测试

- 当前自动化验证有证据回答、无证据拒答、伪造引用被服务端抑制、联网检索在导入前中断、真实节点轨迹，以及运行中取消传播。
- 图为有限无环路由，调用处的 `recursion_limit` 为 8；当前没有“最多四步”的独立限流器。
- SSE 解析器与数据源契约验证分片、多个事件、事件序号、断线补发、暂停终止连接、节点开始/完成、耗时、证据质量摘要和错误帧。
- 持久 Agent 的内存仓储与真实 PostgreSQL 用例共同覆盖幂等提交、租约令牌、事件/消息原子增量、取消、人工确认恢复、删除竞态和所有权隔离。
- PostgreSQL Checkpoint 的容器重启、真实模型超时及真实 arXiv 故障仍属于发布前补充场景；未完成前不宣称外部模型调用“恰好一次”。

工具调用成功率 ≥95%、中断恢复成功率 100%、跨用户泄露为 0 均为发布目标；在对应容器测试和故障注入完成前不发布这些指标。

### Harness 专项评测

`context-harness-v1` 提供两种明确分级的入口：

```bash
# 确定性模式：调用生产解析、Skill 路由、记忆筛选和 Token 门禁；进入 CI，不调用模型
cd backend
python -m paperleaf_api.evaluation_harness \
  --cases evaluation/context-harness-v1/cases.jsonl \
  --output evaluation/context-harness-v1/latest-deterministic.json

# 真实模式：必须在 Docker Compose 已运行且本地模型凭证可用时显式执行
docker compose run --rm api python -m paperleaf_api.evaluation_harness_live \
  --runs 100 --concurrency 3 --timeout-seconds 300
```

真实模式通过 HTTP 创建并保留 `[实测]` 会话，等待 Worker 与当前 DeepSeek 配置完成回答，
再从 PostgreSQL 核对 Skill、Function Calling、持久 Tool Call、最终 Token、引用范围和选文
物理页。它还会创建或复用 `[系统验收] Harness 真实闭环` 集合，并可导入三篇公开 arXiv
论文。真实模式不进入 GitHub Actions，避免外部网络、额度或 Provider 波动反复发送 CI 失败邮件。
报告中的 `deterministic_no_external_model`、`real_model_real_infrastructure` 必须原样保留，
不得把前者写成真实模型通过率。完整 100 次运行才适用 `≥99/100` 结构门禁；少量冒烟只报告
实际分子和分母。

## 前端与无障碍

2026-08-09 Harness 可靠性回归：后端收集 330 项，324 项通过、6 项因可选外部基础设施跳过；Ruff 全部通过。确定性 evaluator 冻结 100 例，指代 67/67、澄清 89/89、Skill 61/61、记忆 10/10、工具 10/10、授权 10/10、审批 2/2，最终输入超限为 0；证据级别明确标记为 `deterministic_no_external_model`。前端 Vitest 112/112，Playwright 52 项通过、8 项按视口设计跳过，Storybook、TypeScript、ESLint（0 error，保留 2 个既有 TanStack Table 编译器提示）和生产构建通过。隔离 PostgreSQL 完成全新升级到 `20260809_0017`、`0017 → 0016 → 0017` 回滚再升级；Compose 的 API、Worker、Web 和学术 MCP 镜像构建及健康检查通过。

同日真实本地复测使用 DeepSeek、Ollama、PostgreSQL、pgvector、Redis 与真实 PDF.js 文本层：鼠标拖选 37 字后，Run `8a237abb-196c-436d-b97d-7c448bf58a83` 以 `trace_original` 完成，引用 3 条且均为物理页 1，最终模型输入 5366/21307 Token；`get_page_text` 的精确 ID 与唯一可信标题解析都成功。此前 5 次向量故障注入均完成关键词降级。

2026-08-10 已补齐 OpenAlex 严格联网闭环：用户未指定数据源时，Run `d0ce2efa-35df-48e8-923a-7c3854357de6` 自动选择 `find_related_papers@2` 并持久化一次成功的 OpenAlex Tool Call；最终在 25.274 秒内输出 5/5 篇带年份、出版物、DOI 和来源的候选，当前集合重复为 0，非法 Chunk 引用为 0，最终输入 2883/21307 Token。该 OpenAlex 调用命中 Redis 缓存，12 ms 不能作为外部网络延迟基线；完整报告见 `docs/reports/2026-08-10-openalex-auto-discovery.md`。这里记录的是单机当前配置的证据，不外推为全部 Provider 的语义准确率。

2026-08-10 已补齐多轮联网发现回归：在上一轮“推荐 5 篇尚未入库论文”后追问“有没有更近的论文，如 2026 年的”，真实 Run `8d348fcf-5b44-4b33-9635-8ff54695d92b` 通过 `context_task_inheritance` 继承 `find_related_papers`，OpenAlex 实际收到 `year_from=2026, year_to=2026`，并返回 5/5 篇 2026 年候选。本地 Chunk 引用为 0，结构性验收通过。完整报告见 `docs/reports/2026-08-10-multiturn-discovery-context.md`。

2026-08-10 推荐质量与连续换批再次回归：后端 427 项收集、421 通过、6 项跳过，学术 MCP 5/5，前端 Vitest 113/113；真实 Run `e04edc39-b275-47fd-b765-c85ccaa0e041` 与 `357967f9-3dd3-452c-9717-77f60de19440` 属于同一会话，首轮和 2026 年追问均返回 5 篇，跨批标题重复 0，第二轮 5/5 年份为 2026。Context Snapshot 只保存第一轮实际展示的 5 个标题实体。该结果是 2/2 结构回归，不是人工 Precision@5；完整报告见 `docs/reports/2026-08-10-recommendation-quality-and-batching.md`。

当前 Vitest 覆盖登录表单校验、登录/改密/上传、持久会话 CRUD、202 幂等提交、会话切换竞态、Agent 断线补发与暂停恢复、安全 Markdown、单篇与集合范围绑定、管理员模型状态、文献修改/重试/删除、总结与结构图映射、层级集合、PDF 缩放与布局恢复、翻译确认/取消/失败/partial 终态。Storybook 提供上传弹窗、论文工作台、文献组织工作台和组织管理弹层供人工检查。

2026-08-07 后台概括回归：虚构英文 DTA 论文经真实 DeepSeek 配置完成 `Job → Worker → paper_artifacts`，任务提交 17.2 ms、生成 17.85 s，得到 5 个中文章节和 6 条合法引用；全部事实均含中文，英文降级摘录为 0。完整前端 Vitest 为 83/83，完整后端 Pytest 为 189 passed / 5 skipped（跳过项需要 PostgreSQL 容器）；生产构建成功。

当前 Playwright 自动化场景为：

1. 公开演示提问后从引用跳转到对应论文页。
2. 跨文献提问展示 Agent 运行轨迹与证据质量摘要，并通过 URL 中的物理页参数打开正确 PDF 页。
3. 首页 WCAG 2 A/AA axe 扫描无违规。
4. 论文工作台 axe `serious`/`critical` 为 0。
5. 论文工作台的可调整分隔条保留数值 ARIA，行内引用的可访问名称包含可见引用编号。
6. 生成证据化概览，点击证据跳转到对应物理页，并构建结构图。
7. 编辑文献元数据，验证删除必须经过二次确认。
8. 2560×1440 下验证关键阅读字号、Geist 字体资源、MIME 类型、三档字号实际值与横向溢出；同时检查年份和任务状态保持单行、中文表头使用无衬线字体、管理分区标题层级与论文标题可用宽度。
9. 创建、修改和删除组织项，批量加入与移出集合，并验证真实筛选数量。
10. 批量归档和恢复，四视口检查页面与表格均无非预期横向溢出，并执行文献组织 axe 扫描。
11. 从单篇论文的行级入口查看当前集合，并添加、移除集合；桌面端检查表格集合，紧凑视口回开弹窗核对服务端最终状态。
12. 在 390×844 下验证真实账户入口可达，以及设置页的账户操作和偏好控件保持至少 44px 触摸目标。
13. 验证 PDF 50%～200% 缩放、适合宽度、专注阅读和全文翻译；翻译开启后检查窄阅读列译文几何、页面横向溢出、44px 弹窗操作和 axe serious/critical 为 0。
14. 验证示例问题只写入输入框、会话离开页面后恢复、390/768 与 200% 等效宽度下的历史抽屉、发送按钮、触摸目标和横向溢出。
15. 验证后台只发布已核验事实段落，前端将新增内容逐字呈现；减少动态效果偏好开启时直接显示完整已核验内容。

2026-08-07 引用与脑图体验回归：改动前完整前端 Vitest 为 87/87，完整后端 Pytest 为 192 passed / 5 skipped；最终增量再通过 ChatWorkspace 17/17、Artifact 18/18、TypeScript、Ruff、ESLint（0 error，保留 2 个既有 TanStack Table 编译器提示）和生产构建。真实 Docker Compose 冒烟完成临时 PDF 上传、页级解析、父子集合递归、Range 读取、归档与幂等删除；2560×1440 实际工作台无页面级溢出，16 个跨文献会话正确分页。DeepSeek 真实 Enter 提问返回中文回答和页码引用；DeepDTA 研究脑图后台生成 7 个中文语义节点、5 条去重证据，并从引用 `[2]` 正确跳到 PDF 第 3 页。该记录只陈述本次本机样本，不外推为大规模质量指标。

它们分别在 2560×1440、1440×900 桌面、768×1024 平板和 390×844 Pixel 7 移动 Chromium 项目中运行。字号与字体资源专项仅在 2K 项目执行，移动账户与触摸目标专项仅在移动项目执行；2026-08-06 的里程碑 3 本地完整结果为 46 项通过、6 项按视口设计跳过。Playwright 每次在独立的 `3100` 端口构建并启动固定 demo 数据的生产服务，且不复用开发者正在运行的 `3000` 站点，避免本机 real 模式和 `.env` 污染确定性测试；跨文献提问的提交按钮在 hydration 完成前保持禁用。失败时保留 Trace 和截图；仓库尚未启用基于基准图的像素差异门禁。

以下是后续端到端与人工发布验收清单，并非当前 Playwright 覆盖：

1. 登录与首次修改密码。
2. 上传 PDF，观察解析进度并打开阅读器。
3. 提问，接收流式回答并跳转到正确物理页。
4. 创建父子集合并修改论文归属；集合 CRUD、递归筛选、批量归属、归档、元数据编辑和二次确认删除已进入自动化覆盖。
5. 搜索 arXiv，拒绝一次导入，再确认一次导入。
6. 管理员创建和停用用户，验证内容不可跨用户读取。

发布前应补 Firefox、WebKit、真实 PDF 上传以及生产 API 模式，并结合语义断言、溢出检查和人工截图确认。

无障碍门禁：

- axe `serious` 与 `critical` 为 0。
- 核心路径可全键盘完成，焦点顺序与焦点环可见。
- 200% 缩放不丢失功能，不出现非预期横向滚动。
- 状态同时包含文字或图形，不只依赖淡蓝、淡绿或红色。
- 触摸目标至少 44 × 44 CSS 像素。

性能门禁：Lighthouse 性能 ≥90、无障碍 100、最佳实践 ≥95、SEO ≥90、LCP ≤2.5 秒、CLS ≤0.1。CI 对生产构建的首页和固定数据演示页各运行三轮桌面预设。

0.8.1 本地三轮结果如下；区间保留三次原始结果的最小值与最大值，不把单次最好值包装成稳定成绩：

| 页面 | 性能 | 无障碍 | 最佳实践 | SEO | LCP | CLS |
|---|---:|---:|---:|---:|---:|---:|
| `/` | 98 | 100 | 100 | 100 | 860–890 ms | 0 |
| `/demo` | 95–96 | 100 | 100 | 100 | 1100–1130 ms | 0 |

里程碑 3 在同一台本机、相同 desktop preset 下重新执行六轮，结果如下。首页性能为 98/98/99、LCP 为 851/868/857 ms；Demo 性能为 97/97/96、LCP 为 967/927/1068 ms。相较上表，Demo 的三次 LCP 中位数由 1103 ms 降至 967 ms，减少 136 ms；这只是同机实验室结果，不代表真实用户网络下必然获得同等改善。

| 页面 | 性能 | 无障碍 | 最佳实践 | SEO | LCP | CLS |
|---|---:|---:|---:|---:|---:|---:|
| `/` | 98–99 | 100 | 100 | 100 | 851–868 ms | 0 |
| `/demo` | 96–97 | 100 | 100 | 100 | 927–1068 ms | 0 |

测试环境为 Windows 11 23H2、AMD Ryzen 5 5600H、约 14 GB 内存、Headless Chrome 150、Lighthouse 12.6.1，使用 Lighthouse desktop preset 的模拟节流；本机 benchmark index 为 1870。实验室页面加载无法证明真实用户 INP ≤200 ms，也没有单独拆分字体造成的 CLS，因此这两项仍是线上观测目标。

## 发布前人工冒烟清单

仓库早期版本曾完成一次无外部模型费用的 7/7 核心容器闭环。2026-08-07 本机 Docker Desktop 恢复后，本轮已重新构建 Web/API/Worker，并完成真实 PostgreSQL、MinIO、Worker 闭环；以下清单继续区分已经验证和仍待发布环境验证的项目：

- [x] 历史 7/7 证据：`docker compose up -d --build` 从空卷启动，迁移、Bucket、健康检查、上传、解析、Range、管理员端点和删除通过。
- [x] 0.8.1：Compose 配置解析通过；Windows 生产服务器抽查 18/18 CSS/JS 资源返回 200。
- [x] 2026-08-07：在可用 Docker 引擎上重新执行上传—解析—父子集合—Range—归档—删除闭环；临时数据已清理。
- 新管理员可登录并创建用户。
- 配置真实模型后，问答、引用跳页与总结形成闭环。
- 未配置模型时界面明确降级，不出现无限加载。
- 重启 API/Worker 后未完成任务与 Agent 中断可恢复。
- 备份文件可在隔离环境恢复。

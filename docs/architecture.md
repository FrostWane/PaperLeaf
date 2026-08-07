# PaperLeaf 架构说明

## 设计目标

PaperLeaf 把确定性的文献管理、可评测的检索链路和受控 Agent 分开。业务权限不交给模型判断，检索与引用不隐藏在框架内部，耗时任务不阻塞 Web 请求。

## 组件

```mermaid
flowchart TB
    subgraph Client["客户端"]
        Web["Next.js Web\nPDF 阅读器 / 文献库 / Agent 面板"]
    end
    subgraph Services["应用服务"]
        API["FastAPI\n认证 / RBAC / CRUD / SSE"]
        Worker["Python Worker\n解析 / OCR / Agent / 翻译 / 清理"]
        Graph["LangGraph\n状态 / 路由 / 中断 / 恢复"]
        RAG["页级 RAG\n向量 + 关键词 + RRF"]
        Runtime["模型运行时\n超时 / 主备 / 熔断 / 脱敏轨迹"]
    end
    subgraph Data["数据层"]
        DB[("PostgreSQL + pgvector")]
        Store[("MinIO 私有对象存储")]
        Redis[("Redis\n限流 / 短期运行态")]
    end
    Model["OpenAI-compatible 模型"]
    Arxiv["arXiv API / 开放 PDF"]

    Web -->|"REST / SSE / Range"| API
    API --> DB
    API --> Store
    API --> Redis
    Worker --> Graph
    Graph --> RAG
    Graph --> Arxiv
    Graph --> Runtime
    Runtime --> Model
    RAG --> DB
    Worker --> DB
    Worker --> Store
    Worker --> Runtime
```

### Web

- 提供文献列表、PDF 阅读、逐页双栏翻译、问答、发现、管理与设置页面。
- 使用 REST 处理业务操作，使用 SSE 接收回答增量、引用和可恢复动作。
- 根级账户状态只在内存中复用，应用内导航不会因页面组件重新挂载而闪回未登录状态；退出后立即清空。
- 从 `/auth/me` 获取真实身份和角色；普通用户界面不渲染管理入口，但权限判断仍以 API 为准。
- 昵称、字号、默认 PDF 缩放、阅读器侧栏、翻译语言与 arXiv 搜索偏好写入用户记录；页面临时状态仍只保存在客户端。

### API

- 校验会话、CSRF、资源所有权、管理员权限和输入模型。
- 提供当前用户偏好读取与部分更新接口；停用用户会撤销其现有会话，最后一名管理员和当前管理员由服务端给出明确拒绝原因。
- 生成 PDF 的鉴权 Range 响应，不公开 MinIO Bucket。
- 创建后台任务和 Agent Run，不在请求线程中解析大 PDF。
- 将可公开的节点开始/完成、工具活动、耗时和错误码通过 SSE 返回，不输出隐藏推理。

### Worker

- 使用 PostgreSQL 作业表领取任务；任务通过租约与领取令牌防止过期 Worker 写回，并设计为可重试和幂等。
- 按物理页解析 PDF，低文本页按配置进入 OCR。
- 按当前阅读页优先级翻译已解析文本；每页独立提交，已完成页不会因其他页失败而回滚。
- 建立全文与向量索引，清理删除中的文献及其派生数据。

### PostgreSQL 与 MinIO

- PostgreSQL 保存身份、文献元数据、层级集合、页、Chunk、向量、任务、Agent Run 和 LangGraph Checkpoint。
- 结构化总结和研究结构图通过 `summarize_paper`、`build_structure_graph` 作业交给 Worker；API 只负责幂等入队并返回 `processing`。
- 产物保存为用户私有 `paper_artifacts`；来源修订由全部物理页文本计算，重索引会使旧产物进入 `stale`。刷新生成期间继续保留上一次成功产物，失败不会覆盖它。
- pgvector 负责精确向量检索，PostgreSQL 全文索引负责关键词检索。
- MinIO 保存 PDF 原件；Bucket 初始化为私有。
- 数据库记录对象键，不将预签名链接作为持久数据。

### Redis 运行态边界

- Redis 只保存 Agent 提交的短期限流计数和幂等判定，所有 Key 使用前缀并哈希用户标识。
- 限流计数通过单段 Lua 脚本原子完成读取、递增、过期和判定，避免多个 API 实例超卖配额。
- Redis 不保存用户、会话、论文、消息、任务、Agent Run、Checkpoint、引用或 PDF 内容；这些数据仍以 PostgreSQL/MinIO 为真相源。
- Redis 不可用时 API 降级为当前进程内限流并在 `/ready` 标记 `degraded`，不会让短期缓存故障中断文献管理和已持久化任务。
- 当前 SSE 仍从 PostgreSQL 事件表补发；未来即使使用 Redis 加速唤醒，也不能绕过持久事件序列。

## 文献导入数据流

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as API
    participant S as MinIO
    participant D as PostgreSQL
    participant W as Worker

    U->>A: 上传 PDF
    A->>A: 校验大小、页数、文件名与 PDF 文件头
    A->>S: 保存私有原件
    A->>D: 创建文献与解析任务
    A-->>U: 返回 uploaded/queued
    W->>D: 领取任务
    W->>S: 读取 PDF
    W->>W: 按页解析、按需 OCR、切块
    W->>D: 写入页面、Chunk、全文索引与向量
    W->>D: 标记 ready 或 partial/failed
```

Chunk 不跨物理页，引用始终关联 `paper_id + page_number + chunk_id`。解析失败不会删除原始 PDF，用户仍可阅读并重试。

## 全文翻译数据流

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as API
    participant D as PostgreSQL
    participant W as Worker
    participant M as OpenAI-compatible 模型

    U->>A: 确认目标语言与当前页
    A->>D: 同一事务创建或复用翻译、逐页状态和作业
    A-->>U: 202 + 翻译任务状态
    W->>D: 带租约领取 translate_paper
    loop 当前页优先，其余页按物理页
        W->>D: 读取一页解析文本与来源哈希
        W->>M: 发送受限单页翻译请求
        W->>D: 复验领取令牌、取消、删除与来源哈希
        W->>D: 单独提交该页译文或失败状态
    end
    U->>A: 轮询任务并读取当前页译文
    A-->>U: 双栏展示原始 PDF 与纯文本译文
```

- 顶层任务使用 `queued/running/partial/completed/failed/cancelled`；单页使用 `queued/running/completed/no_text/failed/cancelled`。
- 相同论文、来源版本和目标语言复用缓存；重新索引会立即把旧译文标记为来源已变化，不会静默复用。
- 用户从取消、部分失败或失败状态重新开始时复用唯一翻译与唯一 Job；若来源版本已变化，服务端会先清除旧页、旧领取令牌和旧来源哈希，再按最新 revision 重新排队。
- 取消请求持久化在数据库。Worker 在调用模型前、模型返回后和每页之间复验；取消保留已经成功的页面。
- 页面文本过长时仍在物理页内按段落分段，不跨页拼接；单页最多处理 48,000 个字符和 6 个分块，每块模型输出上限为 4,096 token。公式、引用编号和专有名词只作为翻译内容处理。

## 层级集合与出版物元数据

- 集合通过同表 `parent_id` 形成最多五层的用户私有树；同级名称唯一，API 拒绝跨用户父节点、循环和超深移动。
- 论文与集合保持多对多关系。父集合范围由服务端递归解析并去重，前端不提交后代论文 ID 作为权限依据。
- 删除集合只删除目标集合及其直接归属，直接子集合提升到原父级，论文和后代集合均保留。
- PDF 元数据识别先在本地完成。只有 DOI 已识别而出版物仍为空时，Worker 才向 Crossref 官方接口发送 DOI；查询在事务外执行，写回前重新锁定论文，避免覆盖用户同时修改的 DOI 或出版物。

## RAG 数据流

1. API 固定用户与文献范围，并在查询前过滤资源所有权。
2. 向量召回与关键词召回独立执行，并保留各通道分数与命中来源。
3. RRF 合并候选后按 `paper_id + physical_page` 聚合；同一页的多个 Chunk 只占一个召回位，通道信号合并到页级证据。
4. 确定性质量门禁分别计算检索置信度、词项覆盖和通道一致性；“返回列表非空”不再等于证据充分。
5. 生成模型综合有限证据组织回答，不得照抄英文摘要。证据偏弱时仍回答可支持的部分，并在结尾明确说明边界。
6. 提示词只向模型暴露 `E1`、`E2` 等运行内短别名；回答返回后由服务端还原为真实 Chunk ID，避免长 UUID 被模型截断，同时保证跨论文别名唯一。
7. 服务端按 Markdown 事实段落校验引用是否属于本次召回证据、论文和物理页。漏引或非法引用段落直接丢弃；存在召回证据时，最终消息至少保留一个合法引用段落。
8. 完全没有证据时允许模型给出帮助性说明或明确标注的一般知识，但不能声称读过当前论文；模型超时、鉴权或服务故障会返回明确错误，绝不把原始 Chunk 拼成伪 AI 回答。

`tool_finished` SSE 只向前端发送上述可公开的质量摘要，不发送思维链。该边界使切块、召回、页聚合、检索质量、答案支持、拒答和引用准确率可以分别测试。

## 持久问答与后台 Agent

```mermaid
sequenceDiagram
    participant U as 浏览器
    participant A as FastAPI
    participant D as PostgreSQL
    participant W as Worker
    participant G as LangGraph

    U->>A: POST 消息 + Idempotency-Key
    A->>D: 原子保存消息、Run、范围快照和 Job
    A-->>U: 202 + message_id + run_id
    W->>D: SKIP LOCKED 领取 agent_run + 租约令牌
    W->>G: 使用服务端范围和 Checkpoint 执行
    G-->>W: 模型流与候选段落
    W->>W: 还原短引用并校验 Chunk / 论文 / 页码
    W->>D: 原子追加已核验段落和公开事件
    U->>A: GET events + Last-Event-ID
    A->>D: 回放遗漏事件并等待新事件
    A-->>U: SSE 增量 / 引用 / 中断 / 终态
```

- 会话分别绑定单篇论文、集合或全库。提交时由服务端递归解析可访问且已索引的论文 ID，并把结果写入 Run 范围快照；模型和前端都不能在执行中扩大范围。
- 同一会话只允许一个活动 Run。相同幂等键与相同请求只复用已受理结果，不重复创建消息、Run 或作业；相同键配不同请求会被拒绝。
- Agent 事件按 Run 内递增序号持久化。SSE 是可中断的观察通道，不拥有运行生命周期；页面关闭、路由切换和临时断线不会取消 Worker。
- 模型通过兼容接口内部流式返回，但未经核验的 token 只保留在 Worker 内存。完整事实段落通过引用来源检查后才追加到助手消息与 `message_delta` 事件，前端再用稳定的自适应字符步长逐步呈现。失败、取消和过期租约不能写入未经核验的尾部内容。
- arXiv 候选通过 `interrupt` 进入持久等待状态。批准或拒绝动作携带 `action_id`，相同决定可幂等重放；恢复后清除待确认动作并重新入队同一 Run。
- 取消请求先持久化。Worker 在节点和写入边界检查取消与租约；失去租约的旧 Worker 不能取消或覆盖后来领取者的结果。

## 证据化产物

- 总结模型只能输出固定五节 JSON：研究问题、核心方法、实验设置、主要结果、局限与适用范围。每个事实独立携带引用数组，服务端校验 Chunk 所有权和物理页后生成 Markdown。
- 结构图模型输出 5～12 个受控语义节点和边。提示词只暴露 `E1/E2` 短证据别名，服务端映射回真实 Chunk 后再检查节点类型、引用、未知端点、孤立节点与有向环。首次拓扑不合法会用紧凑上下文重试；第二次若节点、中文摘要和引用都合法而仅拓扑不完整，服务端按语义类型归一为从研究问题出发的无环完整链，再生成经过字符约束的 Mermaid `flowchart TD`。前端仍使用 strict 安全模式渲染。
- 模型超时或 JSON 格式错误会使用更小上下文重试，作业还可由 Worker 按租约恢复。总结事实必须含中文且引用合法；结构图节点摘要也必须含中文。
- 首次生成最终失败时只保存中文失败原因和空产物，不保存或展示英文 Chunk 摘录。刷新生成失败时继续返回上一次成功的中文产物，不用原文摘录或 Chunk 顺序连线伪装模型结果。
- 产物以 `paper_id + type` 唯一保存，记录来源修订、状态、结构化 payload、稳定 Markdown 和降级原因。只有来源修订一致的 ready 产物可直接复用。

## 模型运行时

问答、证据支持检查、论文总结、全文翻译、查询/文献嵌入和视觉 OCR 共用同一类运行策略，但按 `provider + purpose` 隔离故障状态：

1. 先调用主服务；可重试故障在配置的 1~3 次范围内重试。
2. 主服务失败或熔断时尝试备用服务；未配置备用服务时进入相应能力的确定性降级。
3. 每次调用由应用层统一设置超时，SDK 内建重试关闭，避免嵌套重试放大请求量。
4. 连续失败达到阈值后打开熔断器；冷却结束只允许一个半开探测，成功后关闭，失败则重新计时。
5. 用户取消会沿异步任务传播到当前 Graph 和模型调用，取消不会计入服务故障。

运行记录只包含用途、服务别名、模型名、成功/失败/超时/取消状态、尝试序号、耗时和稳定错误码。API Key、Base URL、提示词、证据正文、响应正文与隐藏推理均不进入公开轨迹。

## Agent 边界

当前 LangGraph 只编排以下受限工具：

- `search_library`
- `search_arxiv`

arXiv 下载由独立、鉴权且需要 CSRF 的导入接口完成；论文总结与结构图同样是独立的用户范围 API，不由模型自主调用。

当前图是有限无环路由，并在调用处设置递归上限 8。arXiv 候选在导入前中断并等待确认；实际下载由受控导入接口完成。模型没有 Shell、任意 URL、数据库或文件系统访问权，PDF 中的指令被当作文献内容而不是系统命令。

## 多用户隔离

- 文献、集合和 Agent Run 的公开访问接口都校验当前用户。
- 管理导航的前端隐藏只是体验优化，管理员接口仍逐次校验当前会话角色；退出登录会删除服务端会话并清理认证 Cookie。
- 查询服务在数据访问层加入用户范围，而不是检索后再过滤。
- 管理员接口与用户内容接口分离；管理员默认只管理账号和任务元数据。
- Agent Run 查询按 `run_id + user_id` 校验所有权；Checkpoint 线程 ID 由用户、会话与运行 ID 共同组成，避免跨用户或跨运行复用状态。

## 故障与恢复

- `migrate` 容器成功后 API 和 Worker 才启动。
- PostgreSQL 与 MinIO 使用健康检查和持久卷。
- 作业记录阶段、尝试次数和错误码；重复执行不应生成重复 Chunk 或对象。
- 作业领取记录租约时间和随机令牌。每个模型分块前后都会检查并续租；已经过期的 Worker 不能复活自己的租约，也不能继续调用模型或写回结果。单次模型超时最多 120 秒，显著短于 30 分钟作业租约；翻译页按瞬态/永久错误分类重试并退避。
- 问答会话、消息、Agent Run、公开事件、中断状态、范围快照与 LangGraph Checkpoint 均写入 PostgreSQL，API 或浏览器重建后可按原运行 ID 恢复；其他用户访问同一 ID 仍返回 404。
- Worker 使用作业租约执行 Agent。取消接口持久化取消状态，执行者在安全边界停止 Graph 与模型调用；重复取消保持幂等，完成或失败的运行不能被改写为取消。
- 删除采用 `deleting` 状态和后台清理，避免部分删除对外可见。

## 扩展点

- 模型通过 OpenAI-compatible 适配器替换。
- 检索器与重排器保持独立接口，可用固定评测集比较。
- 新文献来源必须实现来源白名单、重定向复验、文件校验和用户确认。
- 对象存储可替换为兼容 S3 的服务，但必须保持私有访问语义。
- 容量演进、观测指标和消息队列触发条件见[容量与可观测性里程碑](scaling.md)。

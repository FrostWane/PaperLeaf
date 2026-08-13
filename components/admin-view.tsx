"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { Activity, ChevronLeft, ChevronRight, Plus, RefreshCw, UserX, X } from "lucide-react";
import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createAdminUser, getAdminDiscoveryMetrics, getAdminHarnessMetrics, getAdminModelHealth, getAdminRagObservability, listAdminJobs, listAdminMcpServers, listAdminUsers, refreshAdminMcpServer, retryAdminJob, setAdminMcpServerEnabled, setAdminUserActive, testAdminMcpServer } from "@/lib/data-source";
import { users as fixtureUsers } from "@/lib/fixtures";
import type { AdminDiscoveryMetrics, AdminHarnessMetrics, AdminJob, AdminMcpServers, AdminRagObservability, ModelRuntimeHealth, UserRecord } from "@/lib/types";

const helper = createColumnHelper<UserRecord>();
const jobsPageSize = 15;
type AdminTab = "overview" | "rag" | "harness" | "discovery" | "users" | "jobs";
const adminTabs: ReadonlyArray<readonly [AdminTab, string]> = [
  ["overview", "运行概览"],
  ["rag", "RAG 质量"],
  ["harness", "Agent Harness"],
  ["discovery", "推荐效果"],
  ["users", "用户与权限"],
  ["jobs", "后台任务"],
];
const demoJobs: AdminJob[] = [{ id: "job-demo", paperId: "attention", type: "parse_pdf", status: "running", progress: 68, attempts: 1, maxAttempts: 3 }];
const demoHealth: ModelRuntimeHealth = {
  configured: true,
  providers: [{
    provider: "primary",
    purposes: Object.fromEntries(["answer", "evidence_support", "summary", "translation", "embedding"].map((purpose) => [purpose, { configured: true, status: "closed", consecutiveFailures: 0, retryAfterMs: 0 }])),
  }],
  policy: { timeoutSeconds: 30, attemptsPerProvider: 2, failureThreshold: 3, cooldownSeconds: 60 },
};
const emptyHealth: ModelRuntimeHealth = {
  configured: false,
  providers: [],
  policy: { timeoutSeconds: 0, attemptsPerProvider: 0, failureThreshold: 0, cooldownSeconds: 0 },
};
const emptyObservability: AdminRagObservability = {
  windowHours: 24,
  generatedAt: "",
  limitReached: false,
  totals: { runs: 0, terminalRuns: 0, completedRuns: 0, failedRuns: 0, citedAnswers: 0, groundedAnswers: 0, ragIssueRuns: 0, telemetryRuns: 0, telemetryCoverage: 0, completionRate: 0, failureRate: 0, citedAnswerRate: 0, ragIssueRate: 0 },
  funnel: [],
  latency: { overall: { samples: 0 }, stages: [] },
  retrievalChannels: [],
  intents: [],
  failures: [],
  chunkingStrategies: [],
  runtimeStore: { backend: "memory", status: "available" },
  privacy: { contentCollected: false, identifiersCollected: false },
};
const emptyDiscoveryMetrics: AdminDiscoveryMetrics = {
  windowHours: 720,
  generatedAt: "",
  batches: 0,
  impressions: 0,
  opened: 0,
  interested: 0,
  notInterested: 0,
  imported: 0,
  feedbackCount: 0,
  clickThroughRate: 0,
  interestHitRate: 0,
  feedbackRate: 0,
  importRate: 0,
};
const emptyHarnessMetrics: AdminHarnessMetrics = {
  windowHours: 24,
  generatedAt: "",
  limitReached: false,
  context: { runs: 0, compactedRuns: 0, compressionRate: 0, tokensBefore: 0, tokensAfter: 0, contextLimitErrors: 0, contextLimitRate: 0, referenceConfidenceBands: { high: 0, medium: 0, clarify: 0 }, clarificationRate: 0 },
  memory: { total: 0, active: 0, disabled: 0, pinned: 0, usersWithMemory: 0, capacity: 0, supersededVersions: 0, types: {}, sources: {} },
  skills: { runs: 0, distribution: [], routeSources: {}, fallbackRuns: 0 },
  tools: { calls: 0, successful: 0, successRate: 0, retriedCalls: 0, timeouts: 0, permissionDenied: 0, statuses: {}, distribution: [], errorCategories: {} },
  mcp: { calls: 0, successful: 0, successRate: 0, servers: [] },
  embedding: { configured: false, total: 0, ready: 0, readyCurrent: 0, stale: 0, unavailable: 0, failed: 0, fallbackRuns: 0, fallbackReasons: {} },
  parallelCompare: { runs: 0, plannedSubtasks: 0, succeededSubtasks: 0, failedSubtasks: 0, timeoutSubtasks: 0, successRate: 0, partialRuns: 0, partialRate: 0, fallbackRuns: 0, fallbackRate: 0, fallbackReasons: {}, findingCount: 0, dedupCount: 0, conflictCount: 0, paperCoverageCount: 0, branchEvidenceCount: 0, branchClaimCount: 0, estimatedBranchInputTokens: 0, estimatedBranchOutputTokens: 0, providerTokenSamples: 0, branchErrorCategories: {}, versions: {} },
  privacy: { contentCollected: false, identifiersCollected: false },
};
const emptyMcpServers: AdminMcpServers = { featureEnabled: false, servers: [] };

const skillLabels: Record<string, string> = {
  legacy_agent: "兼容链路",
  paper_qa: "论文问答",
  trace_original: "原文定位",
  compare_papers: "跨文献比较",
  find_related_papers: "相关论文发现",
  verify_claim: "主张核验",
  summarize_paper: "论文总结",
  build_research_map: "研究脑图",
};

const toolLabels: Record<string, string> = {
  search_current_paper: "检索当前论文",
  search_library: "检索文献库",
  get_page_text: "读取物理页",
  search_arxiv: "搜索 arXiv",
  find_related_papers: "查找相关论文",
  request_import: "请求导入",
  summarize_paper: "读取论文总结",
  build_structure_graph: "读取研究脑图",
  mcp__academic__search_openalex: "OpenAlex 搜索",
  mcp__academic__search_semantic_scholar: "Semantic Scholar 搜索",
  mcp__academic__get_academic_metadata: "读取学术元数据",
};

const embeddingFallbackLabels: Record<string, string> = {
  embedding_provider_unavailable: "向量服务不可用",
  embedding_contract_mismatch: "索引契约不一致",
  query_dimension_mismatch: "查询向量维度不一致",
  stored_dimension_mismatch: "已存向量维度不一致",
  vector_query_failed: "向量查询失败",
};

const compareFallbackLabels: Record<string, string> = {
  disabled: "功能关闭",
  skill_not_supported: "任务不适用",
  scope_too_small: "范围过小",
  scope_too_large: "范围过大",
  invalid_plan: "计划无效",
  budget_exceeded: "预算超限",
  all_subtasks_failed: "全部子任务失败",
  no_merged_evidence: "无合并证据",
  cancelled: "运行取消",
  timeout: "运行超时",
  lease_lost: "工作租约失效",
  internal_error: "内部错误",
  other: "其他原因",
};

const compareBranchErrorLabels: Record<string, string> = {
  timeout: "超时",
  schema: "输出格式",
  provider: "模型服务",
  invalid_output: "输出无效",
  scope_violation: "范围校验",
};

const mcpStatusLabels: Record<string, string> = {
  healthy: "可用",
  unhealthy: "不可用",
  circuit_open: "已熔断",
  disabled: "已停用",
  unknown: "尚未检测",
};

const mcpErrorLabels: Record<string, string> = {
  MCP_TIMEOUT: "连接超时",
  MCP_TRANSPORT_ERROR: "连接失败",
  MCP_CIRCUIT_OPEN: "连续失败，已暂时熔断",
  MCP_DISABLED: "服务未启用",
  OPENALEX_API_KEY_REQUIRED: "OpenAlex 尚未配置 API Key",
  SEMANTIC_SCHOLAR_RATE_LIMITED: "Semantic Scholar 请求频率受限",
};

const stageLabels: Record<string, string> = {
  intent: "意图识别",
  retrieval: "证据召回",
  evidence_grading: "证据评级",
  generation: "回答生成",
  answer_support: "答案支持检查",
  citation_validation: "引用校验",
};

function percent(value: number): string { return `${(value * 100).toFixed(1)}%`; }
function sampledPercent(value: number, samples: number): string { return samples > 0 ? percent(value) : "—"; }
function duration(value?: number): string { return typeof value === "number" ? value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${value} 毫秒` : "—"; }
function bytes(value?: number): string { return typeof value === "number" ? value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MiB` : `${Math.round(value / 1024)} KiB` : "—"; }
function generatedAt(value: string): string {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" })
    : "尚未生成";
}

const runtimeStoreLabels: Record<string, string> = {
  redis: "Redis",
  memory: "进程内状态存储",
  "memory-fallback": "进程内降级存储",
};

const purposeCopy: Record<string, { label: string; description: string }> = {
  answer: { label: "回答生成", description: "根据检索到的论文证据组织回答" },
  evidence_support: { label: "证据核验", description: "检查回答中的结论是否有原文支持" },
  summary: { label: "论文总结", description: "提炼研究问题、方法、结果与局限" },
  translation: { label: "全文翻译", description: "按物理页翻译已解析的论文文本" },
  embedding: { label: "向量检索", description: "按语义查找与问题相关的论文段落" },
  vision: { label: "视觉 OCR", description: "识别扫描版 PDF 页面中的文字" },
};
const visiblePurposeNames = ["answer", "evidence_support", "summary", "translation", "embedding", "vision"] as const;

const jobTypeLabels: Record<string, string> = {
  parse_pdf: "解析 PDF",
  delete_paper: "删除文献",
  import_arxiv: "导入论文",
  import_paper: "导入论文",
  translate_paper: "翻译全文",
  agent_run: "运行问答",
  summarize_paper: "生成论文概括",
  build_structure_graph: "生成研究脑图",
};

const jobErrorLabels: Record<string, string> = {
  PDF_PARSE_FAILED: "PDF 解析失败",
  PAPER_NOT_FOUND: "关联文献不存在",
  PDF_ENCRYPTED: "PDF 已加密，无法处理",
  PDF_INVALID: "文件不是有效的 PDF",
  MODEL_TIMEOUT: "AI 服务响应超时",
  MODEL_NOT_CONFIGURED: "尚未配置所需的 AI 服务",
};

function jobStatusCopy(job: AdminJob): string {
  if (job.status === "queued") return "等待处理";
  if (job.status === "completed") return "已完成";
  if (job.status === "running") {
    return `处理进度 ${job.progress}% · 第 ${Math.max(job.attempts, 1)} 次执行，最多 ${job.maxAttempts} 次`;
  }
  const reason = job.errorMessage
    ?? (job.errorCode ? jobErrorLabels[job.errorCode] ?? `任务执行失败（错误代码：${job.errorCode}）` : "任务执行失败，请检查服务状态后重试");
  return `处理失败 · 已尝试 ${job.attempts} 次 · 失败原因：${reason}`;
}

function jobProgressLabel(job: AdminJob): string {
  if (job.status === "queued") return "等待处理";
  if (job.status === "completed") return "已完成";
  if (job.status === "failed") return "失败";
  return `${job.progress}%`;
}

export function AdminView() {
  const real = process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const grafanaUrl = process.env.NEXT_PUBLIC_GRAFANA_URL;
  const [users, setUsers] = useState<UserRecord[]>(() => real ? [] : fixtureUsers);
  const [jobs, setJobs] = useState<AdminJob[]>(() => real ? [] : demoJobs);
  const [modelHealth, setModelHealth] = useState<ModelRuntimeHealth>(() => real ? emptyHealth : demoHealth);
  const [usersLoaded, setUsersLoaded] = useState(!real);
  const [jobsLoaded, setJobsLoaded] = useState(!real);
  const [modelHealthLoaded, setModelHealthLoaded] = useState(!real);
  const [observability, setObservability] = useState<AdminRagObservability>(emptyObservability);
  const [discoveryMetrics, setDiscoveryMetrics] = useState<AdminDiscoveryMetrics>(emptyDiscoveryMetrics);
  const [harnessMetrics, setHarnessMetrics] = useState<AdminHarnessMetrics>(emptyHarnessMetrics);
  const [mcpServers, setMcpServers] = useState<AdminMcpServers>(emptyMcpServers);
  const [harnessMetricsLoaded, setHarnessMetricsLoaded] = useState(!real);
  const [mcpServersLoaded, setMcpServersLoaded] = useState(!real);
  const [harnessMetricsError, setHarnessMetricsError] = useState("");
  const [mcpServersError, setMcpServersError] = useState("");
  const [harnessLoading, setHarnessLoading] = useState(false);
  const [ragWindow, setRagWindow] = useState<"24h" | "7d" | "30d">("24h");
  const [discoveryWindow, setDiscoveryWindow] = useState<"24h" | "7d" | "30d">("30d");
  const [harnessWindow, setHarnessWindow] = useState<"24h" | "7d" | "30d">("24h");
  const [observabilityLoading, setObservabilityLoading] = useState(real);
  const [discoveryLoading, setDiscoveryLoading] = useState(real);
  const [observabilityError, setObservabilityError] = useState("");
  const [discoveryError, setDiscoveryError] = useState("");
  const [activeTab, setActiveTab] = useState<AdminTab>("overview");
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [jobsPage, setJobsPage] = useState(1);
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"status" | "error">("status");
  const [showCreate, setShowCreate] = useState(false);
  const [pendingDeactivation, setPendingDeactivation] = useState<UserRecord | null>(null);
  const [email, setEmail] = useState("");
  const [temporaryPassword, setTemporaryPassword] = useState("");
  const refreshSequence = useRef(0);
  const ragRefreshSequence = useRef(0);
  const discoveryRefreshSequence = useRef(0);
  const harnessRefreshSequence = useRef(0);
  const [mcpPending, setMcpPending] = useState<{ serverId: string; action: "test" | "refresh" | "toggle" } | null>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const refreshCore = useCallback(async () => {
    if (!real) return;
    const sequence = ++refreshSequence.current;
    const [usersResult, jobsResult, healthResult] = await Promise.allSettled([
      listAdminUsers(),
      listAdminJobs(),
      getAdminModelHealth(),
    ]);
    if (sequence !== refreshSequence.current) return;
    const errors: string[] = [];
    if (usersResult.status === "fulfilled") { setUsers(usersResult.value); setUsersLoaded(true); }
    else errors.push(usersResult.reason instanceof Error ? usersResult.reason.message : "用户数据读取失败");
    if (jobsResult.status === "fulfilled") { setJobs(jobsResult.value); setJobsLoaded(true); }
    else errors.push(jobsResult.reason instanceof Error ? jobsResult.reason.message : "任务数据读取失败");
    if (healthResult.status === "fulfilled") { setModelHealth(healthResult.value); setModelHealthLoaded(true); }
    else errors.push(healthResult.reason instanceof Error ? healthResult.reason.message : "AI 状态读取失败");
    if (errors.length) { setMessageKind("error"); setMessage(errors.join("；")); }
  }, [real]);

  const refreshRag = useCallback(async () => {
    if (!real) return;
    const sequence = ++ragRefreshSequence.current;
    setObservabilityLoading(true);
    try {
      const value = await getAdminRagObservability(ragWindow);
      if (sequence !== ragRefreshSequence.current) return;
      setObservability(value);
      setObservabilityError("");
    } catch (error) {
      if (sequence !== ragRefreshSequence.current) return;
      setObservabilityError(error instanceof Error ? error.message : "RAG 指标读取失败");
    } finally {
      if (sequence === ragRefreshSequence.current) setObservabilityLoading(false);
    }
  }, [ragWindow, real]);

  const refreshDiscovery = useCallback(async () => {
    if (!real) return;
    const sequence = ++discoveryRefreshSequence.current;
    setDiscoveryLoading(true);
    try {
      const value = await getAdminDiscoveryMetrics(discoveryWindow);
      if (sequence !== discoveryRefreshSequence.current) return;
      setDiscoveryMetrics(value);
      setDiscoveryError("");
    } catch (error) {
      if (sequence !== discoveryRefreshSequence.current) return;
      setDiscoveryError(error instanceof Error ? error.message : "推荐指标读取失败");
    } finally {
      if (sequence === discoveryRefreshSequence.current) setDiscoveryLoading(false);
    }
  }, [discoveryWindow, real]);

  useEffect(() => { void refreshCore(); }, [refreshCore]);
  useEffect(() => { void refreshRag(); }, [refreshRag]);
  useEffect(() => { void refreshDiscovery(); }, [refreshDiscovery]);
  const refreshHarness = useCallback(async () => {
    if (!real) { setHarnessMetricsLoaded(true); setMcpServersLoaded(true); return; }
    const sequence = ++harnessRefreshSequence.current;
    setHarnessLoading(true);
    const [metricsResult, serversResult] = await Promise.allSettled([
      getAdminHarnessMetrics(harnessWindow),
      listAdminMcpServers(),
    ]);
    if (sequence !== harnessRefreshSequence.current) return;
    if (metricsResult.status === "fulfilled") { setHarnessMetrics(metricsResult.value); setHarnessMetricsLoaded(true); setHarnessMetricsError(""); }
    else setHarnessMetricsError(metricsResult.reason instanceof Error ? metricsResult.reason.message : "Harness 指标读取失败");
    if (serversResult.status === "fulfilled") { setMcpServers(serversResult.value); setMcpServersLoaded(true); setMcpServersError(""); }
    else setMcpServersError(serversResult.reason instanceof Error ? serversResult.reason.message : "MCP 状态读取失败");
    setHarnessLoading(false);
  }, [harnessWindow, real]);
  useEffect(() => { if (activeTab === "harness") void refreshHarness(); }, [activeTab, refreshHarness]);
  const runMcpAction = useCallback(async (action: "test" | "refresh" | "toggle", serverId: string, enabled?: boolean) => {
    if (mcpPending) return;
    setMcpPending({ serverId, action });
    try {
      if (action === "test") await testAdminMcpServer(serverId);
      else if (action === "refresh") await refreshAdminMcpServer(serverId);
      else await setAdminMcpServerEnabled(serverId, Boolean(enabled));
      setMessageKind("status");
      setMessage(action === "test" ? "MCP 连接检测通过" : action === "refresh" ? "MCP 工具清单已刷新" : enabled ? "MCP 服务已启用" : "MCP 服务已停用");
      await refreshHarness();
    } catch (error) {
      setMessageKind("error"); setMessage(error instanceof Error ? error.message : "MCP 操作失败");
    } finally {
      setMcpPending(null);
    }
  }, [mcpPending, refreshHarness]);
  useEffect(() => {
    const pageCount = Math.max(1, Math.ceil(jobs.length / jobsPageSize));
    setJobsPage((current) => Math.min(current, pageCount));
  }, [jobs.length]);

  const toggleUser = useCallback(async (user: UserRecord) => {
    const disabling = user.status === "正常";
    if (!real) {
      if (user.role === "管理员" && users.filter((item) => item.role === "管理员" && item.status === "正常").length <= 1) {
        setMessageKind("error"); setMessage("不能停用最后一名管理员");
        return;
      }
      setUsers((items) => items.map((item) => item.id === user.id ? { ...item, status: item.status === "正常" ? "已停用" : "正常" } : item));
      setMessageKind("status"); setMessage(disabling ? `已停用用户 ${user.name}` : `已启用用户 ${user.name}`);
      return;
    }
    try {
      const updated = await setAdminUserActive(user.id, user.status !== "正常");
      setUsers((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (error) { setMessageKind("error"); setMessage(error instanceof Error ? error.message : "用户状态更新失败"); }
  }, [real, users]);

  async function createUser() {
    if (!email.includes("@") || temporaryPassword.length < 12) { setMessageKind("error"); setMessage("请输入有效邮箱和至少 12 位临时密码"); return; }
    if (!real) {
      setUsers((items) => [...items, { id: `u${items.length + 1}`, name: email.split("@")[0], email, role: "用户", status: "正常", papers: 0 }]);
    } else {
      try { const created = await createAdminUser(email, temporaryPassword); setUsers((items) => [...items, created]); }
      catch (error) { setMessageKind("error"); setMessage(error instanceof Error ? error.message : "用户创建失败"); return; }
    }
    setEmail(""); setTemporaryPassword(""); setShowCreate(false); setMessageKind("status"); setMessage("用户已创建，首次登录必须修改临时密码。 ");
  }

  const columns = useMemo(() => [
    helper.accessor("name", { header: "用户", cell: ({ row }) => <div className="admin-user"><span className="avatar small">{row.original.name.slice(0, 1)}</span><span><strong>{row.original.name}</strong><small>{row.original.email}</small></span></div> }),
    helper.accessor("role", { header: "角色" }),
    helper.accessor("papers", { header: "文献数", cell: (info) => <span className="mono">{real ? "—" : info.getValue()}</span> }),
    helper.accessor("status", { header: "状态", cell: (info) => <span className={info.getValue() === "正常" ? "status-pill ready" : "status-pill partial"}><span>{info.getValue() === "正常" ? "✓" : "!"}</span>{info.getValue()}</span> }),
    helper.display({ id: "actions", header: "操作", cell: ({ row }) => <button type="button" className="secondary-button" aria-label={`${row.original.status === "正常" ? "停用用户" : "启用用户"} ${row.original.name}`} onClick={() => row.original.status === "正常" ? setPendingDeactivation(row.original) : void toggleUser(row.original)}>{row.original.status === "正常" ? "停用用户" : "启用用户"}</button> }),
  ], [real, toggleUser]);
  const table = useReactTable({ data: users, columns, getCoreRowModel: getCoreRowModel() });
  const activeUsers = users.filter((user) => user.status === "正常").length;
  const runningJobs = jobs.filter((job) => job.status === "running" || job.status === "queued").length;
  const failedJobs = jobs.filter((job) => job.status === "failed").length;
  const modelCircuitOpen = modelHealth.providers.some((provider) => Object.values(provider.purposes).some((purpose) => purpose.configured && purpose.status === "open"));
  const modelState = !modelHealth.configured ? "降级模式" : modelCircuitOpen ? "需检查" : "正常";
  const runtimeProviders = modelHealth.providers.length > 0
    ? modelHealth.providers
    : [{ provider: "unconfigured", purposes: {} }];
  const telemetrySamples = observability.totals.telemetryRuns;
  const terminalSamples = observability.totals.terminalRuns;
  const confidenceSamples = Object.values(harnessMetrics.context.referenceConfidenceBands).reduce((sum, value) => sum + value, 0);
  const harnessPrivacySafe = !harnessMetrics.privacy.contentCollected && !harnessMetrics.privacy.identifiersCollected;
  const runtimeStoreLabel = runtimeStoreLabels[observability.runtimeStore.backend] ?? "运行状态存储";
  const jobsPageCount = Math.max(1, Math.ceil(jobs.length / jobsPageSize));
  const visibleJobs = jobs.slice((jobsPage - 1) * jobsPageSize, jobsPage * jobsPageSize);

  function moveAdminTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % adminTabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + adminTabs.length) % adminTabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = adminTabs.length - 1;
    else return;
    event.preventDefault();
    setActiveTab(adminTabs[next][0]);
    tabRefs.current[next]?.focus();
  }

  return <div className="admin-layout">
    <nav className="admin-section-tabs" aria-label="管理工作区" role="tablist">
      {adminTabs.map(([id, label], index) => <button type="button" role="tab" id={`admin-tab-${id}`} aria-selected={activeTab === id} aria-controls={`admin-panel-${id}`} tabIndex={activeTab === id ? 0 : -1} ref={(node) => { tabRefs.current[index] = node; }} className={activeTab === id ? "active" : ""} key={id} onKeyDown={(event) => moveAdminTab(event, index)} onClick={() => setActiveTab(id)}>{label}</button>)}
    </nav>
    {message && <p className="admin-message" role={messageKind === "error" ? "alert" : "status"}>{message}</p>}

    {activeTab === "overview" && <div id="admin-panel-overview" role="tabpanel" aria-labelledby="admin-tab-overview" className="admin-tab-panel">
      <div className="metric-row"><article><span>活跃用户</span><strong>{usersLoaded ? activeUsers : "—"}</strong><small>{usersLoaded ? `共 ${users.length} 个账号` : "正在读取"}</small></article><article><span>处理中任务</span><strong>{jobsLoaded ? runningJobs : "—"}</strong><small>等待中与运行中</small></article><article><span>失败任务</span><strong>{jobsLoaded ? failedJobs : "—"}</strong><small>{failedJobs > 0 ? "需要检查或重试" : "当前无需处理"}</small></article><article><span>AI 服务</span><strong>{modelHealthLoaded ? modelState : "读取中"}</strong><small>回答、核验、总结与检索</small></article></div>
      <section className="admin-section admin-priority"><div className="section-bar"><div><span className="eyebrow">优先级</span><h2>现在需要关注什么</h2></div></div>
        <div className="admin-priority-row" data-state={failedJobs > 0 ? "warning" : "ready"}><span><strong>{failedJobs > 0 ? `${failedJobs} 个后台任务失败` : "后台任务运行正常"}</strong><small>{failedJobs > 0 ? "查看失败原因，确认后可从任务页重试。" : `${runningJobs} 个任务正在等待或执行。`}</small></span><button type="button" className="secondary-button" onClick={() => setActiveTab("jobs")}>查看任务</button></div>
        <div className="admin-priority-row" data-state={observability.totals.ragIssueRuns > 0 ? "warning" : "ready"}><span><strong>{telemetrySamples === 0 ? "RAG 质量尚无样本" : observability.totals.ragIssueRuns > 0 ? `${observability.totals.ragIssueRuns} 次问答出现异常或受限` : "RAG 链路没有异常记录"}</strong><small>{telemetrySamples === 0 ? "完成真实问答后开始统计召回、意图、耗时和失败原因。" : `当前统计窗口为 ${ragWindow === "24h" ? "24 小时" : ragWindow === "7d" ? "7 天" : "30 天"}。`}</small></span><button type="button" className="secondary-button" onClick={() => setActiveTab("rag")}>查看质量</button></div>
        <div className="admin-priority-row" data-state={modelState === "正常" ? "ready" : "warning"}><span><strong>{modelState === "正常" ? "AI 能力可用" : `AI 服务${modelState}`}</strong><small>{modelState === "正常" ? "回答、核验和检索服务运行正常。" : "部分能力可能回退到关键词检索或等待服务恢复。"}</small></span><button type="button" className="secondary-button" onClick={() => { setCapabilitiesOpen(true); globalThis.setTimeout(() => document.getElementById("ai-capability-status")?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 0); }}>查看能力</button></div>
      </section>
      <details id="ai-capability-status" className="admin-section model-runtime" open={capabilitiesOpen} onToggle={(event) => setCapabilitiesOpen(event.currentTarget.open)}><summary className="section-bar"><div><span className="eyebrow">AI 服务可用性</span><h2>AI 能力状态</h2></div><span className="capability-summary-state">{modelState} · {capabilitiesOpen ? "收起详情" : "展开详情"}</span></summary>
        <div className="capability-policy">单次调用超时 {modelHealth.policy.timeoutSeconds} 秒 · 每项能力最多尝试 {modelHealth.policy.attemptsPerProvider} 次</div>
        {!modelHealthLoaded ? <div className="runtime-empty"><Activity size={17} /><span><strong>正在读取 AI 服务状态</strong><small>读取完成后显示各项能力的真实可用状态。</small></span></div> : <>{!modelHealth.configured && <div className="runtime-empty"><Activity size={17} /><span><strong>当前未配置外部模型</strong><small>系统继续使用全文检索与确定性回答，不会产生模型调用费用。</small></span></div>}{runtimeProviders.map((provider, providerIndex) => <div className="runtime-provider" key={provider.provider}><span className="runtime-provider-name"><Activity size={15} /><strong>{provider.provider === "unconfigured" ? "尚未配置 AI 服务" : providerIndex === 0 ? "主要 AI 服务" : `备用 AI 服务 ${providerIndex}`}</strong></span><div className="runtime-purposes">{visiblePurposeNames.map((purposeName) => { const copy = purposeCopy[purposeName]; const purpose = provider.purposes[purposeName]; const state = !purpose?.configured ? "暂不可用 · 尚未配置" : purpose.status === "closed" ? "可用" : purpose.status === "half_open" ? "正在检测恢复" : `暂不可用，${Math.ceil(purpose.retryAfterMs / 1000)} 秒后重试`; const statusName = purpose?.configured ? purpose.status : "unconfigured"; return <span key={purposeName} data-status={statusName} title={copy.description}><i />{copy.label}<small>{state} · {copy.description}</small></span>; })}</div></div>)}</>}
      </details>
    </div>}

    {activeTab === "rag" && <div id="admin-panel-rag" role="tabpanel" aria-labelledby="admin-tab-rag" className="admin-tab-panel">
      <div className="metric-row rag-metrics"><article><span>Agent 运行</span><strong>{observability.totals.runs}</strong><small>全部运行记录</small></article><article><span>含引用回答</span><strong>{sampledPercent(observability.totals.citedAnswerRate, telemetrySamples)}</strong><small>{observability.totals.citedAnswers} / {telemetrySamples} 条已采集轨迹</small></article><article><span>RAG 异常/受限率</span><strong>{sampledPercent(observability.totals.ragIssueRate, telemetrySamples)}</strong><small>{observability.totals.ragIssueRuns} / {telemetrySamples} 条已采集轨迹</small></article><article><span>端到端 P95</span><strong>{duration(observability.latency.overall.p95Ms)}</strong><small>{observability.latency.overall.samples} 个终态耗时样本</small></article></div>
      <section className="admin-section rag-observability" aria-busy={observabilityLoading}><div className="section-bar"><div><span className="eyebrow">检索增强链路</span><h2>RAG 运行质量</h2><small>更新于 {generatedAt(observability.generatedAt)}</small></div><div className="rag-window" aria-label="统计时间范围">{(["24h", "7d", "30d"] as const).map((value) => <button type="button" key={value} className={ragWindow === value ? "active" : ""} aria-pressed={ragWindow === value} onClick={() => setRagWindow(value)}>{value === "24h" ? "24 小时" : value === "7d" ? "7 天" : "30 天"}</button>)}</div></div>
      {observabilityError && <p className="rag-error-note" role="alert">RAG 指标读取失败：{observabilityError} <button type="button" onClick={() => void refreshRag()}>重试</button></p>}
      <div className="admin-runtime-strip"><span><i data-status={observability.runtimeStore.status} />{runtimeStoreLabel} {observability.runtimeStore.status === "available" ? "可用" : "降级"}</span>{observability.runtimeStore.usedMemoryBytes !== undefined && <span>{runtimeStoreLabel}内存 {bytes(observability.runtimeStore.usedMemoryBytes)}{observability.runtimeStore.maxMemoryBytes ? ` / ${bytes(observability.runtimeStore.maxMemoryBytes)}` : ""}</span>}{observability.runtimeStore.keyCount !== undefined && <span>短期 Key {observability.runtimeStore.keyCount}</span>}{observability.runtimeStore.connectedClients !== undefined && <span>存储连接 {observability.runtimeStore.connectedClients}</span>}<span>指标覆盖 {telemetrySamples > 0 ? percent(observability.totals.telemetryCoverage) : "—"}</span><span>运行失败 {terminalSamples > 0 ? percent(observability.totals.failureRate) : "—"}</span><span>活跃用户 {activeUsers}/{users.length}</span><span>处理中任务 {runningJobs}</span><span>AI 服务 {modelState}</span>{grafanaUrl && <a href={grafanaUrl} target="_blank" rel="noreferrer">打开 Grafana</a>}</div>
      {telemetrySamples === 0 ? <div className="runtime-empty"><Activity size={17} /><span><strong>{terminalSamples > 0 ? `已有 ${terminalSamples} 次终态运行，但尚无可分析的 RAG 轨迹` : "还没有可分析的 RAG 运行"}</strong><small>{terminalSamples > 0 ? "这些记录来自可观测性升级前；完成新的问答后会开始形成细分指标。" : "完成一次单篇或跨文献问答后，这里会显示召回、证据和生成链路。"}</small></span></div> : <>
        {(observability.totals.telemetryCoverage < 1 || observability.limitReached) && <p className="rag-coverage-note">{observability.totals.telemetryCoverage < 1 ? `当前窗口包含升级前记录，已采集 ${telemetrySamples} / ${terminalSamples} 条终态运行（${percent(observability.totals.telemetryCoverage)}）。${observability.totals.telemetryCoverage < 0.8 ? "覆盖不足 80%，细分指标不适合直接比较。" : ""}` : ""}{observability.limitReached ? " 当前窗口运行数达到查询上限，本页所有指标均基于最近 5000 次运行。" : ""}</p>}
        <div className="rag-dashboard-grid">
          <article className="rag-panel"><h3>证据漏斗 <small>占已采集运行</small></h3>{observability.funnel.length === 0 ? <p className="rag-empty">当前窗口没有证据漏斗数据。</p> : <ol className="rag-funnel">{observability.funnel.map((step) => <li key={step.key}><span><strong>{step.label}</strong><small>{percent(step.rate)}</small></span><div aria-hidden="true"><i style={{ width: `${Math.max(step.rate * 100, step.count > 0 ? 3 : 0)}%` }} /></div><b>{step.count}</b></li>)}</ol>}</article>
          <article className="rag-panel"><h3>阶段耗时</h3>{observability.latency.stages.length === 0 ? <p className="rag-empty">当前窗口没有阶段耗时数据。</p> : <div className="rag-table-wrap"><table className="rag-table" aria-label="RAG 阶段耗时"><thead><tr><th>阶段</th><th>样本</th><th>P50</th><th>P95</th></tr></thead><tbody>{observability.latency.stages.map((item) => <tr key={item.stage}><td>{stageLabels[item.stage] ?? item.stage}</td><td>{item.samples}</td><td>{duration(item.p50Ms)}</td><td>{duration(item.p95Ms)}</td></tr>)}</tbody></table></div>}</article>
          <article className="rag-panel rag-panel-wide"><h3>召回通道</h3>{observability.retrievalChannels.length === 0 ? <p className="rag-empty">当前窗口没有召回通道数据。</p> : <div className="rag-table-wrap"><table className="rag-table" aria-label="RAG 召回通道"><thead><tr><th>通道</th><th>参与运行</th><th>证据充足率</th><th>含引用回答率</th><th>召回 P95</th></tr></thead><tbody>{observability.retrievalChannels.map((item) => <tr key={item.channel}><td>{item.label}</td><td>{item.runs}</td><td>{percent(item.sufficientEvidenceRate)}</td><td>{percent(item.citedAnswerRate)}</td><td>{duration(item.retrievalP95Ms)}</td></tr>)}</tbody></table></div>}</article>
          <article className="rag-panel"><h3>问题意图</h3>{observability.intents.length === 0 ? <p className="rag-empty">当前窗口没有意图分类数据。</p> : <div className="rag-table-wrap"><table className="rag-table" aria-label="RAG 问题意图"><thead><tr><th>意图</th><th>运行</th><th>含引用回答</th><th>P95</th></tr></thead><tbody>{observability.intents.map((item) => <tr key={item.intent}><td>{item.label}</td><td>{item.runs}</td><td>{percent(item.citedAnswerRate)}</td><td>{duration(item.p95Ms)}</td></tr>)}</tbody></table></div>}</article>
          <article className="rag-panel"><h3>失败与受限</h3>{observability.failures.length === 0 ? <p className="rag-empty">当前窗口没有失败或受限记录。</p> : <ul className="rag-failures">{observability.failures.map((item) => <li key={item.category}><span>{item.label}<small>{percent(item.rate)}</small></span><strong>{item.count}</strong></li>)}</ul>}<div className="rag-strategies"><span>索引策略</span>{observability.chunkingStrategies.length === 0 ? <small>暂无数据</small> : observability.chunkingStrategies.map((item) => <small key={item.strategy}>{item.strategy} · {item.runs}</small>)}</div></article>
        </div>
      </>}
      </section>
    </div>}
    {activeTab === "harness" && <div id="admin-panel-harness" role="tabpanel" aria-labelledby="admin-tab-harness" className="admin-tab-panel">
      <div className="metric-row rag-metrics" aria-busy={harnessLoading}>
        <article><span>Token 压缩率</span><strong>{harnessMetricsLoaded && harnessMetrics.context.tokensBefore > 0 ? percent(harnessMetrics.context.compressionRate) : "—"}</strong><small>{harnessMetricsLoaded ? `${harnessMetrics.context.compactedRuns} 次运行触发压缩` : "正在读取"}</small></article>
        <article><span>指代需澄清率</span><strong>{harnessMetricsLoaded && confidenceSamples > 0 ? percent(harnessMetrics.context.clarificationRate) : "—"}</strong><small>{harnessMetricsLoaded ? `${harnessMetrics.context.referenceConfidenceBands.clarify} / ${confidenceSamples} 个置信度样本` : "正在读取"}</small></article>
        <article><span>工具成功率</span><strong>{harnessMetricsLoaded && harnessMetrics.tools.calls > 0 ? percent(harnessMetrics.tools.successRate) : "—"}</strong><small>{harnessMetricsLoaded ? `${harnessMetrics.tools.successful} / ${harnessMetrics.tools.calls} 次调用` : "正在读取"}</small></article>
        <article><span>MCP 成功率</span><strong>{harnessMetricsLoaded && harnessMetrics.mcp.calls > 0 ? percent(harnessMetrics.mcp.successRate) : "—"}</strong><small>{harnessMetricsLoaded ? `${harnessMetrics.mcp.successful} / ${harnessMetrics.mcp.calls} 次外部调用` : "正在读取"}</small></article>
      </div>
      <section className="admin-section rag-observability" aria-busy={harnessLoading}><div className="section-bar"><div><span className="eyebrow">运行观测</span><h2>Agent Harness</h2><small>更新于 {generatedAt(harnessMetrics.generatedAt)}</small></div><div className="rag-window" aria-label="Harness 统计时间范围">{(["24h", "7d", "30d"] as const).map((value) => <button type="button" key={value} className={harnessWindow === value ? "active" : ""} aria-pressed={harnessWindow === value} onClick={() => setHarnessWindow(value)}>{value === "24h" ? "24 小时" : value === "7d" ? "7 天" : "30 天"}</button>)}</div></div>
        {harnessMetricsError && <p className="rag-error-note" role="alert">Harness 指标读取失败：{harnessMetricsError}{harnessMetricsLoaded ? "。当前保留上次成功结果。" : "。"} <button type="button" onClick={() => void refreshHarness()}>重试</button></p>}
        {!harnessPrivacySafe && <p className="rag-error-note" role="alert">服务端报告正在采集内容或身份标识，已将此状态显式标记；请暂停使用并检查观测配置。</p>}
        {harnessMetrics.limitReached && <p className="rag-coverage-note">当前窗口达到查询上限，指标基于最近 5000 次运行或 10000 次工具调用。</p>}
        {!harnessMetricsLoaded ? <div className="runtime-empty" role="status"><Activity size={17} /><span><strong>{harnessMetricsError ? "暂时无法读取 Harness 指标" : "正在读取 Harness 指标"}</strong><small>{harnessMetricsError ? "连接恢复后可重试。" : "正在汇总运行数据。"}</small></span></div> : <div className="rag-dashboard-grid">
          <article className="rag-panel"><h3>上下文预算</h3>{harnessMetrics.context.runs === 0 ? <p className="rag-empty">当前窗口尚无上下文快照。</p> : <dl className="harness-stat-list"><div><dt>对话 Token</dt><dd>{harnessMetrics.context.tokensBefore} → {harnessMetrics.context.tokensAfter}</dd></div><div><dt>构建 P50 / P95</dt><dd>{duration(harnessMetrics.context.buildP50Ms)} / {duration(harnessMetrics.context.buildP95Ms)}</dd></div><div><dt>高 / 中 / 待澄清</dt><dd>{harnessMetrics.context.referenceConfidenceBands.high} / {harnessMetrics.context.referenceConfidenceBands.medium} / {harnessMetrics.context.referenceConfidenceBands.clarify}</dd></div><div><dt>上下文超限</dt><dd>{harnessMetrics.context.contextLimitErrors}</dd></div></dl>}</article>
          <article className="rag-panel"><h3>长期记忆 <small>当前快照</small></h3>{harnessMetrics.memory.total === 0 ? <p className="rag-empty">当前没有长期记忆条目。管理员只能查看聚合数量，不能读取正文。</p> : <><dl className="harness-stat-list"><div><dt>启用 / 总数</dt><dd>{harnessMetrics.memory.active} / {harnessMetrics.memory.total}</dd></div><div><dt>固定 / 停用</dt><dd>{harnessMetrics.memory.pinned} / {harnessMetrics.memory.disabled}</dd></div><div><dt>有记忆用户</dt><dd>{harnessMetrics.memory.usersWithMemory}</dd></div><div><dt>这些用户的容量</dt><dd>{harnessMetrics.memory.capacity > 0 ? `${harnessMetrics.memory.active} / ${harnessMetrics.memory.capacity}` : "—"}</dd></div><div><dt>冲突旧版本</dt><dd>{harnessMetrics.memory.supersededVersions}</dd></div></dl><p className="rag-empty">管理员只能查看数量和容量，不能读取用户记忆正文。</p></>}</article>
          <article className="rag-panel"><h3>Skill 路由</h3>{harnessMetrics.skills.distribution.length === 0 ? <p className="rag-empty">当前窗口还没有 Skill 路由样本。</p> : <div className="rag-table-wrap"><table className="rag-table" aria-label="Skill 路由分布"><thead><tr><th>Skill</th><th>运行</th><th>终态完成率</th></tr></thead><tbody>{harnessMetrics.skills.distribution.map((item) => <tr key={item.skill}><td>{skillLabels[item.skill] ?? item.skill}</td><td>{item.runs}</td><td>{item.terminalRuns > 0 ? percent(item.completionRate) : "—"}</td></tr>)}</tbody></table></div>}</article>
          <article className="rag-panel"><h3>工具调用</h3>{harnessMetrics.tools.distribution.length === 0 ? <p className="rag-empty">当前窗口还没有工具调用。</p> : <div className="rag-table-wrap"><table className="rag-table" aria-label="工具调用分布"><thead><tr><th>工具</th><th>调用</th></tr></thead><tbody>{harnessMetrics.tools.distribution.slice(0, 8).map((item) => <tr key={item.tool}><td>{toolLabels[item.tool] ?? item.tool}</td><td>{item.calls}</td></tr>)}</tbody></table></div>}<dl className="harness-stat-list compact"><div><dt>P50 / P95</dt><dd>{duration(harnessMetrics.tools.p50Ms)} / {duration(harnessMetrics.tools.p95Ms)}</dd></div><div><dt>重试 / 超时 / 权限拒绝</dt><dd>{harnessMetrics.tools.retriedCalls} / {harnessMetrics.tools.timeouts} / {harnessMetrics.tools.permissionDenied}</dd></div></dl></article>
          <article className="rag-panel"><h3>向量索引契约</h3><dl className="harness-stat-list"><div><dt>当前模型</dt><dd>{harnessMetrics.embedding.configured ? `${harnessMetrics.embedding.model} · ${harnessMetrics.embedding.dimensions} 维 · 修订 ${harnessMetrics.embedding.revision}` : "未配置"}</dd></div><div><dt>当前可用 / 全部就绪</dt><dd>{harnessMetrics.embedding.readyCurrent} / {harnessMetrics.embedding.ready}</dd></div><div><dt>过期 / 不可用 / 失败</dt><dd>{harnessMetrics.embedding.stale} / {harnessMetrics.embedding.unavailable} / {harnessMetrics.embedding.failed}</dd></div><div><dt>关键词降级</dt><dd>{harnessMetrics.embedding.fallbackRuns} 次</dd></div></dl>{Object.keys(harnessMetrics.embedding.fallbackReasons).length > 0 && <p className="rag-empty">{Object.entries(harnessMetrics.embedding.fallbackReasons).map(([reason, count]) => `${embeddingFallbackLabels[reason] ?? "其他向量异常"} ${count}`).join(" · ")}</p>}</article>
          <article className="rag-panel">
            <h3>跨文献并行比较</h3>
            {harnessMetrics.parallelCompare.runs === 0 ? <p className="rag-empty">当前窗口还没有并行跨文献比较样本。</p> : <>
              <dl className="harness-stat-list">
                <div><dt>运行 / 子任务</dt><dd>{harnessMetrics.parallelCompare.runs} / {harnessMetrics.parallelCompare.plannedSubtasks}</dd></div>
                <div><dt>成功率</dt><dd>{percent(harnessMetrics.parallelCompare.successRate)}</dd></div>
                <div><dt>部分完成 / 回退</dt><dd>{harnessMetrics.parallelCompare.partialRuns} / {harnessMetrics.parallelCompare.fallbackRuns}</dd></div>
                <div><dt>分支 P50 / P95</dt><dd>{duration(harnessMetrics.parallelCompare.subtaskP50Ms)} / {duration(harnessMetrics.parallelCompare.subtaskP95Ms)}</dd></div>
              </dl>
              <details className="admin-inline-details">
                <summary>查看分支详情</summary>
                <dl className="harness-stat-list compact">
                  <div><dt>合并 / 核验 P95</dt><dd>{duration(harnessMetrics.parallelCompare.mergeP95Ms)} / {duration(harnessMetrics.parallelCompare.validationP95Ms)}</dd></div>
                  <div><dt>证据 / 主张 / 论文</dt><dd>{harnessMetrics.parallelCompare.branchEvidenceCount} / {harnessMetrics.parallelCompare.branchClaimCount} / {harnessMetrics.parallelCompare.paperCoverageCount}</dd></div>
                  <div><dt>去重 / 冲突</dt><dd>{harnessMetrics.parallelCompare.dedupCount} / {harnessMetrics.parallelCompare.conflictCount}</dd></div>
                  <div><dt>估算 Token（入 / 出）</dt><dd>{harnessMetrics.parallelCompare.estimatedBranchInputTokens} / {harnessMetrics.parallelCompare.estimatedBranchOutputTokens}</dd></div>
                </dl>
                {Object.keys(harnessMetrics.parallelCompare.branchErrorCategories).length > 0 && <p className="rag-empty">{Object.entries(harnessMetrics.parallelCompare.branchErrorCategories).map(([reason, count]) => `${compareBranchErrorLabels[reason] ?? "其他异常"} ${count}`).join(" · ")}</p>}
              </details>
              {Object.keys(harnessMetrics.parallelCompare.fallbackReasons).length > 0 && <p className="rag-empty">{Object.entries(harnessMetrics.parallelCompare.fallbackReasons).map(([reason, count]) => `${compareFallbackLabels[reason] ?? compareFallbackLabels.other} ${count}`).join(" · ")}</p>}
            </>}
          </article>
        </div>}
      </section>
      <section className="admin-section"><div className="section-bar"><div><span className="eyebrow">只读学术搜索</span><h2>MCP 服务</h2></div><button type="button" className="secondary-button" disabled={harnessLoading} onClick={() => void refreshHarness()}><RefreshCw size={15} />刷新状态</button></div>
        {mcpServersError && <p className="rag-error-note" role="alert">MCP 状态读取失败：{mcpServersError}{mcpServersLoaded ? "。当前保留上次成功结果。" : "。"} <button type="button" onClick={() => void refreshHarness()}>重试</button></p>}
        {mcpServersLoaded && !mcpServers.featureEnabled && <p className="rag-coverage-note">联网学术搜索尚未启用。</p>}
        {!mcpServersLoaded ? <div className="table-message">{mcpServersError ? "暂时无法读取 MCP 服务状态。" : "正在读取 MCP 服务状态…"}</div> : mcpServers.servers.length === 0 ? <div className="table-message">尚未建立 MCP 服务配置。</div> : mcpServers.servers.map((server) => { const pending = mcpPending?.serverId === server.id; const errorLabel = server.lastErrorCode ? mcpErrorLabels[server.lastErrorCode] ?? "服务检测失败" : ""; return <div className="mcp-server-row" key={server.id}><span><strong>{server.displayName}</strong><small>{mcpStatusLabels[server.healthStatus] ?? server.healthStatus} · {server.toolCount} 个已发现工具{errorLabel ? ` · ${errorLabel}` : ""}</small>{server.lastErrorCode && <details><summary>查看技术错误码</summary><code>{server.lastErrorCode}</code></details>}</span><div><button type="button" className="secondary-button" aria-label={`检测${server.displayName}连接`} disabled={pending} onClick={() => void runMcpAction("test", server.id)}>{pending && mcpPending?.action === "test" ? "检测中…" : "检测连接"}</button><button type="button" className="secondary-button" aria-label={`刷新${server.displayName}工具`} disabled={pending} onClick={() => void runMcpAction("refresh", server.id)}>{pending && mcpPending?.action === "refresh" ? "刷新中…" : "刷新工具"}</button><button type="button" className="secondary-button" aria-label={`${server.enabled ? "停用" : "启用"}${server.displayName}`} disabled={pending || (!mcpServers.featureEnabled && !server.enabled)} onClick={() => void runMcpAction("toggle", server.id, !server.enabled)}>{pending && mcpPending?.action === "toggle" ? "处理中…" : server.enabled ? "停用" : "启用"}</button></div></div>; })}
      </section>
    </div>}
    {activeTab === "discovery" && <div id="admin-panel-discovery" role="tabpanel" aria-labelledby="admin-tab-discovery" className="admin-tab-panel">
      <div className="metric-row"><article><span>推荐曝光</span><strong>{discoveryMetrics.impressions}</strong><small>{discoveryMetrics.batches} 批去重推荐</small></article><article><span>点击率</span><strong>{discoveryMetrics.impressions > 0 ? percent(discoveryMetrics.clickThroughRate) : "—"}</strong><small>{discoveryMetrics.opened} / {discoveryMetrics.impressions} 篇查看来源</small></article><article><span>兴趣命中率</span><strong>{discoveryMetrics.feedbackCount > 0 ? percent(discoveryMetrics.interestHitRate) : "—"}</strong><small>{discoveryMetrics.interested} / {discoveryMetrics.feedbackCount} 条明确反馈</small></article><article><span>导入率</span><strong>{discoveryMetrics.impressions > 0 ? percent(discoveryMetrics.importRate) : "—"}</strong><small>{discoveryMetrics.imported} 篇进入文献库</small></article></div>
      <section className="admin-section discovery-observability" aria-busy={discoveryLoading}><div className="section-bar"><div><span className="eyebrow">内容推荐漏斗</span><h2>论文发现效果</h2><small>只统计推荐条目的曝光、点击、反馈与导入，不采集 PDF 正文。数据生成于 {generatedAt(discoveryMetrics.generatedAt)}。</small></div><div className="rag-window" aria-label="推荐统计时间范围">{(["24h", "7d", "30d"] as const).map((value) => <button type="button" key={value} className={discoveryWindow === value ? "active" : ""} aria-pressed={discoveryWindow === value} onClick={() => setDiscoveryWindow(value)}>{value === "24h" ? "24 小时" : value === "7d" ? "7 天" : "30 天"}</button>)}</div></div>
        {discoveryError && <p className="rag-error-note" role="alert">推荐指标读取失败：{discoveryError} <button type="button" onClick={() => void refreshDiscovery()}>重试</button></p>}
        {discoveryMetrics.impressions === 0 ? <div className="runtime-empty"><Activity size={17} /><span><strong>还没有推荐行为样本</strong><small>用户开启联网发现并看到第一批推荐后，这里会开始形成指标。</small></span></div> : <div className="discovery-metric-grid">
          <article><h3>行为漏斗</h3><ol className="rag-funnel">{[
            ["曝光", discoveryMetrics.impressions, 1],
            ["查看来源", discoveryMetrics.opened, discoveryMetrics.clickThroughRate],
            ["明确反馈", discoveryMetrics.feedbackCount, discoveryMetrics.feedbackRate],
            ["感兴趣", discoveryMetrics.interested, discoveryMetrics.impressions ? discoveryMetrics.interested / discoveryMetrics.impressions : 0],
            ["导入文献库", discoveryMetrics.imported, discoveryMetrics.importRate],
          ].map(([label, count, rate]) => <li key={String(label)}><span><strong>{label}</strong><small>{percent(Number(rate))}</small></span><div aria-hidden="true"><i style={{ width: `${Math.max(Number(rate) * 100, Number(count) > 0 ? 3 : 0)}%` }} /></div><b>{count}</b></li>)}</ol></article>
          <article className="discovery-metric-notes"><h3>指标口径</h3><dl><div><dt>点击率</dt><dd>查看过 arXiv 来源的推荐数 ÷ 曝光数，同一条重复点击只计算一次。</dd></div><div><dt>兴趣命中率</dt><dd>“感兴趣” ÷ 全部明确兴趣反馈；未反馈不当作“不感兴趣”。</dd></div><div><dt>反馈覆盖率</dt><dd>{percent(discoveryMetrics.feedbackRate)}，样本过少时不宜据此判断推荐质量。</dd></div><div><dt>排序学习</dt><dd>感兴趣主题会被轻度加权，不感兴趣主题会被轻度降权，文献库相似度始终是主信号。</dd></div></dl></article>
        </div>}
      </section>
    </div>}
    <Dialog.Root open={Boolean(pendingDeactivation)} onOpenChange={(open) => !open && setPendingDeactivation(null)}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <div className="dialog-head"><div><Dialog.Title>确认停用用户</Dialog.Title><Dialog.Description>停用后，该用户的现有会话将失效，重新启用后才能再次登录。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close></div>
          <div className="confirm-paper"><span>即将停用</span><strong>{pendingDeactivation?.name}</strong><p>{pendingDeactivation?.email}</p></div>
          <div className="dialog-actions"><Dialog.Close asChild><button type="button" className="secondary-button">取消</button></Dialog.Close><button type="button" className="danger-button" onClick={() => { const user = pendingDeactivation; setPendingDeactivation(null); if (user) void toggleUser(user); }}><UserX size={15} />确认停用</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
    {activeTab === "users" && <section id="admin-panel-users" role="tabpanel" aria-labelledby="admin-tab-users" className="admin-section"><div className="section-bar"><div><span className="eyebrow">账号与访问权限</span><h2>用户与权限</h2></div><button className="primary-button" onClick={() => setShowCreate((value) => !value)}><Plus size={15} />创建用户</button></div>
      {showCreate && <div className="admin-create"><label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>临时密码<input type="password" value={temporaryPassword} onChange={(event) => setTemporaryPassword(event.target.value)} /></label><button className="primary-button" onClick={() => void createUser()}>保存用户</button></div>}
      {!usersLoaded && <div className="table-message">正在读取用户数据…</div>}
      <div className="table-scroll"><table className="data-table admin-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>
    </section>}
    {activeTab === "jobs" && <section id="admin-panel-jobs" role="tabpanel" aria-labelledby="admin-tab-jobs" className="admin-section jobs"><div className="section-bar"><div><span className="eyebrow">异步处理队列</span><h2>后台任务</h2><small>上传、导入、删除等耗时操作会在离开页面后继续执行。</small></div><button className="secondary-button" onClick={() => void refreshCore()}><RefreshCw size={15} />刷新</button></div>
      {!jobsLoaded ? <div className="table-message">正在读取后台任务…</div> : jobs.length === 0 && <div className="table-message">当前没有后台任务。</div>}
      {visibleJobs.map((job) => <div className="job-row" key={job.id}><span className="job-icon"><Activity size={16} /></span><span><strong>{jobTypeLabels[job.type] ?? "其他后台任务"}</strong><small>{jobStatusCopy(job)}</small></span><div className="job-progress" role={job.status === "running" ? "progressbar" : undefined} aria-label={job.status === "running" ? `处理进度 ${job.progress}%` : undefined} aria-valuemin={job.status === "running" ? 0 : undefined} aria-valuemax={job.status === "running" ? 100 : undefined} aria-valuenow={job.status === "running" ? job.progress : undefined}><span style={{ width: `${job.status === "completed" ? 100 : job.progress}%` }} /></div><span className="mono">{jobProgressLabel(job)}</span>{job.status === "failed" && <button className="secondary-button" onClick={async () => { try { const updated = await retryAdminJob(job.id); setJobs((items) => items.map((item) => item.id === updated.id ? updated : item)); } catch (error) { setMessageKind("error"); setMessage(error instanceof Error ? error.message : "重试失败"); } }}>重试</button>}</div>)}
      {jobs.length > jobsPageSize && <nav className="admin-job-pagination" aria-label="后台任务分页"><button type="button" className="secondary-button" disabled={jobsPage === 1} onClick={() => setJobsPage((page) => Math.max(1, page - 1))}><ChevronLeft size={16} />上一页</button><span aria-live="polite">第 {jobsPage} / {jobsPageCount} 页 · 共 {jobs.length} 个任务</span><button type="button" className="secondary-button" disabled={jobsPage === jobsPageCount} onClick={() => setJobsPage((page) => Math.min(jobsPageCount, page + 1))}>下一页<ChevronRight size={16} /></button></nav>}
    </section>}
  </div>;
}

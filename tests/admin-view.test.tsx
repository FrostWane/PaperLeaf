import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminView } from "@/components/admin-view";
import { API_BASE_URL } from "@/lib/data-source";
import { server } from "./test-server";

const modelHealth = {
  configured: true,
  providers: [{
    provider: "primary",
    purposes: {
      answer: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      evidence_support: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      summary: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      translation: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      embedding: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      vision: { configured: false, status: "open", consecutive_failures: 3, retry_after_ms: 5000 },
      query_rewrite: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
    },
  }],
  policy: { timeout_seconds: 30, attempts_per_provider: 2, failure_threshold: 3, cooldown_seconds: 60 },
};
const observability = {
  window_hours: 24,
  generated_at: "2026-08-07T12:00:00Z",
  limit_reached: false,
  totals: { runs: 12, terminal_runs: 12, completed_runs: 10, failed_runs: 2, cited_answers: 8, grounded_answers: 7, rag_issue_runs: 3, telemetry_runs: 12, telemetry_coverage: 1, completion_rate: 0.8333, failure_rate: 0.1667, cited_answer_rate: 0.6667, rag_issue_rate: 0.25 },
  funnel: [
    { key: "observed", label: "已采集运行", count: 12, rate: 1 },
    { key: "retrieved", label: "召回证据", count: 11, rate: 0.9167 },
    { key: "sufficient", label: "证据充足", count: 9, rate: 0.75 },
    { key: "cited", label: "引用回答", count: 8, rate: 0.6667 },
  ],
  latency: { overall: { samples: 12, p50_ms: 1200, p95_ms: 4300 }, stages: [{ stage: "retrieval", samples: 12, p50_ms: 90, p95_ms: 280 }] },
  retrieval_channels: [{ channel: "keyword", label: "关键词检索", runs: 10, cited_answer_rate: 0.7, sufficient_evidence_rate: 0.8, retrieval_p95_ms: 230 }],
  intents: [{ intent: "method", label: "方法与实现", runs: 6, cited_answer_rate: 0.8333, sufficient_evidence_rate: 0.8333, p95_ms: 3500 }],
  failures: [{ category: "unverified_answer", label: "回答引用未通过", count: 2, rate: 0.1667 }],
  chunking_strategies: [{ strategy: "structure_aware_v2", runs: 12 }],
  runtime_store: { backend: "redis", status: "available" },
  privacy: { content_collected: false, identifiers_collected: false },
};
const discoveryMetrics = {
  window_hours: 24,
  generated_at: "2026-08-08T12:00:00Z",
  batches: 3,
  impressions: 18,
  opened: 6,
  interested: 4,
  not_interested: 2,
  imported: 2,
  feedback_count: 6,
  click_through_rate: 1 / 3,
  interest_hit_rate: 2 / 3,
  feedback_rate: 1 / 3,
  import_rate: 1 / 9,
};

describe("AdminView 管理信息语义", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    server.use(
      http.get(`${API_BASE_URL}/admin/users`, () => HttpResponse.json([
        { id: "admin-1", email: "only-admin@example.org", role: "admin", active: true },
        { id: "user-1", email: "reader@example.org", role: "user", active: false },
      ])),
      http.get(`${API_BASE_URL}/admin/model-health`, () => HttpResponse.json(modelHealth)),
      http.get(`${API_BASE_URL}/admin/observability`, () => HttpResponse.json(observability)),
      http.get(`${API_BASE_URL}/admin/discovery-metrics`, () => HttpResponse.json(discoveryMetrics)),
      http.get(`${API_BASE_URL}/admin/jobs`, () => HttpResponse.json([
        { id: "queued", type: "agent_run", status: "queued", progress: 0, attempts: 0, max_attempts: 3 },
        { id: "running", type: "parse_pdf", status: "running", progress: 68, attempts: 1, max_attempts: 3 },
        { id: "completed", type: "import_arxiv", status: "completed", progress: 100, attempts: 1, max_attempts: 3 },
        { id: "failed", type: "translate_paper", status: "failed", progress: 42, attempts: 3, max_attempts: 3, error_code: "MODEL_TIMEOUT" },
      ])),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("用明确按钮和中文说明展示 AI 能力与任务状态", async () => {
    render(<AdminView />);

    expect(await screen.findByRole("tab", { name: /运行概览/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "现在需要关注什么" })).toBeInTheDocument();
    const capabilities = screen.getByRole("heading", { name: "AI 能力状态" }).closest("details");
    expect(capabilities).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "查看能力" }));
    expect(capabilities).toHaveAttribute("open");
    expect(screen.queryByRole("heading", { name: "RAG 运行质量" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "后台任务" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /用户与权限/ }));
    expect(await screen.findByRole("button", { name: "停用用户 only-admin" })).toHaveTextContent("停用用户");
    expect(screen.getByRole("button", { name: "启用用户 reader" })).toHaveTextContent("启用用户");

    fireEvent.click(screen.getByRole("tab", { name: /运行概览/ }));
    expect(screen.getByRole("heading", { name: "AI 能力状态" })).toBeInTheDocument();
    expect(screen.getByText("回答生成")).toHaveAttribute("title", "根据检索到的论文证据组织回答");
    expect(screen.getByText("全文翻译")).toHaveAttribute("title", "按物理页翻译已解析的论文文本");
    expect(screen.getByText("视觉 OCR")).toHaveTextContent("视觉 OCR");
    expect(screen.getByText(/暂不可用 · 尚未配置.*识别扫描版/)).toBeInTheDocument();
    expect(screen.queryByText("其他 AI 能力")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /RAG 质量/ }));
    expect(screen.getByRole("heading", { name: "RAG 运行质量" })).toBeInTheDocument();
    expect(screen.getByText("关键词检索")).toBeInTheDocument();
    expect(screen.getByText("回答引用未通过")).toBeInTheDocument();
    expect(screen.getByText("structure_aware_v2 · 12")).toBeInTheDocument();
    expect(screen.getByText("Redis 可用")).toBeInTheDocument();
    expect(screen.getAllByText("含引用回答").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("RAG 异常/受限率")).toBeInTheDocument();
    expect(screen.getAllByText("66.7%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("8 / 12 条已采集轨迹")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /后台任务/ }));
    const jobs = screen.getByRole("heading", { name: "后台任务" }).closest("section");
    expect(jobs).not.toBeNull();
    const jobArea = within(jobs as HTMLElement);
    expect(jobArea.getByText("解析 PDF")).toBeInTheDocument();
    expect(jobArea.getByText("处理进度 68% · 第 1 次执行，最多 3 次")).toBeInTheDocument();
    expect(jobArea.getAllByText("等待处理").length).toBeGreaterThanOrEqual(1);
    expect(jobArea.getByRole("progressbar", { name: "处理进度 68%" })).toHaveAttribute("aria-valuenow", "68");
    expect(jobArea.getByText("导入论文")).toBeInTheDocument();
    const completed = jobArea.getByText("已完成", { selector: "small" }).closest(".job-row");
    expect(completed).not.toBeNull();
    expect(completed).not.toHaveTextContent("100%");
    expect(completed).not.toHaveTextContent("1/3");
    expect(jobArea.getByText(/失败原因：AI 服务响应超时/)).toBeInTheDocument();
  });

  it("停用前确认，并展示后端返回的具体禁止原因", async () => {
    server.use(http.patch(`${API_BASE_URL}/admin/users/admin-1`, () => HttpResponse.json(
      { detail: "不能停用或降级最后一名管理员" },
      { status: 409 },
    )));

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("tab", { name: /用户与权限/ }));
    fireEvent.click(await screen.findByRole("button", { name: "停用用户 only-admin" }));
    expect(screen.getByRole("dialog", { name: "确认停用用户" })).toHaveTextContent("停用后，该用户的现有会话将失效");
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("不能停用或降级最后一名管理员"));
  });

  it("单独展示推荐点击率、兴趣命中率和清晰口径", async () => {
    render(<AdminView />);

    fireEvent.click(await screen.findByRole("tab", { name: "推荐效果" }));
    expect(screen.getByRole("heading", { name: "论文发现效果" })).toBeInTheDocument();
    const summary = screen.getByRole("tabpanel").querySelector(".metric-row");
    expect(summary).not.toBeNull();
    expect(within(summary as HTMLElement).getByText("点击率").parentElement).toHaveTextContent("33.3%");
    expect(within(summary as HTMLElement).getByText("兴趣命中率").parentElement).toHaveTextContent("66.7%");
    expect(screen.getByText("4 / 6 条明确反馈")).toBeInTheDocument();
    expect(screen.getByText(/未反馈不当作“不感兴趣”/)).toBeInTheDocument();
    expect(screen.getByText(/感兴趣主题会被轻度加权/)).toBeInTheDocument();
  });

  it("取消停用确认时不会发送状态修改请求", async () => {
    let updates = 0;
    server.use(http.patch(`${API_BASE_URL}/admin/users/admin-1`, () => {
      updates += 1;
      return HttpResponse.json({ id: "admin-1", email: "only-admin@example.org", role: "admin", active: false });
    }));

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("tab", { name: /用户与权限/ }));
    fireEvent.click(await screen.findByRole("button", { name: "停用用户 only-admin" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "确认停用用户" })).not.toBeInTheDocument();
    await waitFor(() => expect(updates).toBe(0));
  });

  it("真实模式不展示演示数据，单个接口失败不阻断 RAG 指标", async () => {
    server.use(http.get(`${API_BASE_URL}/admin/users`, () => HttpResponse.json(
      { detail: "用户数据暂不可用" },
      { status: 503 },
    )));

    render(<AdminView />);

    expect(screen.queryByText("林研究员")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("tab", { name: /RAG 质量/ }));
    expect((await screen.findAllByText("66.7%")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("alert")).toHaveTextContent("用户数据暂不可用");
    expect(screen.queryByText("林研究员")).not.toBeInTheDocument();
  });

  it("快速切换窗口时只接收最后一次请求", async () => {
    server.use(http.get(`${API_BASE_URL}/admin/observability`, async ({ request }) => {
      const window = new URL(request.url).searchParams.get("window");
      if (window === "7d") await new Promise((resolve) => setTimeout(resolve, 100));
      if (window === "30d") await new Promise((resolve) => setTimeout(resolve, 10));
      const hours = window === "30d" ? 720 : window === "7d" ? 168 : 24;
      return HttpResponse.json({ ...observability, window_hours: hours, generated_at: `2026-08-07T${hours === 720 ? "13" : "12"}:00:00Z` });
    }));

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("tab", { name: /RAG 质量/ }));
    await screen.findAllByText("66.7%");
    fireEvent.click(screen.getByRole("button", { name: "7 天" }));
    fireEvent.click(screen.getByRole("button", { name: "30 天" }));

    await waitFor(() => expect(screen.getByText(/2026\/8\/7 21:00:00/)).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 130));
    expect(screen.getByText(/2026\/8\/7 21:00:00/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "30 天" })).toHaveAttribute("aria-pressed", "true");
  });

  it("没有轨迹样本时显示未知状态而不是零质量", async () => {
    server.use(http.get(`${API_BASE_URL}/admin/observability`, () => HttpResponse.json({
      ...observability,
      totals: { ...observability.totals, runs: 4, terminal_runs: 4, telemetry_runs: 0, telemetry_coverage: 0, cited_answers: 0, grounded_answers: 0, rag_issue_runs: 0, cited_answer_rate: 0, rag_issue_rate: 0 },
      funnel: [],
      latency: { overall: { samples: 0 }, stages: [] },
      retrieval_channels: [],
      intents: [],
      failures: [],
      chunking_strategies: [],
      runtime_store: { backend: "memory", status: "available" },
    })));

    render(<AdminView />);

    fireEvent.click(await screen.findByRole("tab", { name: /RAG 质量/ }));
    expect(await screen.findByText("已有 4 次终态运行，但尚无可分析的 RAG 轨迹")).toBeInTheDocument();
    expect(screen.getByText("进程内状态存储 可用")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("后台任务每页展示 15 条并支持翻页", async () => {
    const manyJobs = Array.from({ length: 16 }, (_, index) => ({
      id: `job-${index + 1}`,
      type: "parse_pdf",
      status: "failed",
      progress: 20,
      attempts: 1,
      max_attempts: 3,
      error_message: `测试失败 ${index + 1}`,
    }));
    server.use(http.get(`${API_BASE_URL}/admin/jobs`, () => HttpResponse.json(manyJobs)));

    const { container } = render(<AdminView />);

    fireEvent.click(await screen.findByRole("tab", { name: /后台任务/ }));
    expect(await screen.findByText(/测试失败 1$/)).toBeInTheDocument();
    expect(container.querySelectorAll(".job-row")).toHaveLength(15);
    expect(screen.queryByText(/测试失败 16$/)).not.toBeInTheDocument();
    expect(screen.getByText("第 1 / 2 页 · 共 16 个任务")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText(/测试失败 16$/)).toBeInTheDocument();
    expect(container.querySelectorAll(".job-row")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });

  it("按需展示 Harness 聚合指标并管理白名单 MCP 服务", async () => {
    let connectionTests = 0;
    server.use(
      http.get(`${API_BASE_URL}/admin/harness/metrics`, () => HttpResponse.json({
        window_hours: 24,
        generated_at: "2026-08-08T12:00:00Z",
        limit_reached: false,
        context: { runs: 20, compacted_runs: 8, compression_rate: 0.42, tokens_before: 100000, tokens_after: 58000, build_p50_ms: 35, build_p95_ms: 96, context_limit_errors: 1, context_limit_rate: 0.05, reference_confidence_average: 0.87, reference_confidence_bands: { high: 16, medium: 3, clarify: 1 }, clarification_rate: 0.05 },
        memory: { total: 12, active: 10, disabled: 2, pinned: 3, users_with_memory: 4, capacity: 800, superseded_versions: 2, types: { preference: 6 }, sources: { explicit: 8 } },
        skills: { runs: 20, fallback_runs: 1, route_sources: { model: 18, fallback: 2 }, distribution: [{ skill: "paper_qa", runs: 12, terminal_runs: 12, completion_rate: 0.9167 }] },
        tools: { calls: 30, successful: 28, success_rate: 0.9333, p50_ms: 88, p95_ms: 610, retried_calls: 2, timeouts: 1, permission_denied: 0, statuses: { completed: 28 }, error_categories: { timeout: 1 }, distribution: [{ tool: "search_library", calls: 20 }] },
        mcp: { calls: 5, successful: 4, success_rate: 0.8, servers: [{ id: "academic", display_name: "学术搜索", enabled: true, health_status: "healthy", consecutive_failures: 0 }] },
        embedding: { configured: true, provider: "ollama", model: "qwen3-embedding:0.6b", dimensions: 1024, revision: 1, total: 5, ready: 4, ready_current: 3, stale: 1, unavailable: 0, failed: 0, fallback_runs: 2, fallback_reasons: { query_dimension_mismatch: 2 } },
        parallel_compare: { runs: 4, planned_subtasks: 10, succeeded_subtasks: 8, failed_subtasks: 2, timeout_subtasks: 1, success_rate: 0.8, partial_runs: 1, partial_rate: 0.25, fallback_runs: 1, fallback_rate: 0.25, fallback_reasons: { all_subtasks_failed: 1 }, subtask_p50_ms: 180, subtask_p95_ms: 880, merge_p50_ms: 45, merge_p95_ms: 120, validation_p95_ms: 740, finding_count: 28, dedup_count: 7, conflict_count: 2, paper_coverage_count: 9, branch_evidence_count: 28, branch_claim_count: 16, estimated_branch_input_tokens: 3200, estimated_branch_output_tokens: 640, provider_token_samples: 0, branch_error_categories: { schema: 1 }, versions: { specialist_subgraph_v3: 4 } },
        privacy: { content_collected: false, identifiers_collected: false },
        debug_memory_text: "绝密记忆不应展示",
      })),
      http.get(`${API_BASE_URL}/admin/mcp/servers`, () => HttpResponse.json({
        feature_enabled: true,
        servers: [{ id: "academic", display_name: "学术搜索", transport: "streamable_http", enabled: true, health_status: "healthy", consecutive_failures: 0, tool_count: 3, tools: [] }],
      })),
      http.post(`${API_BASE_URL}/admin/mcp/servers/academic/test`, async () => {
        connectionTests += 1;
        await new Promise((resolve) => setTimeout(resolve, 40));
        return HttpResponse.json({ ok: true });
      }),
    );

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("tab", { name: "Agent Harness" }));

    expect(await screen.findByRole("heading", { name: "Agent Harness" })).toBeInTheDocument();
    expect(screen.getByText("42.0%")).toBeInTheDocument();
    expect(screen.getByText("10 / 12")).toBeInTheDocument();
    expect(screen.getByText("93.3%")).toBeInTheDocument();
    expect(screen.getByText("学术搜索")).toBeInTheDocument();
    expect(screen.getByText("qwen3-embedding:0.6b · 1024 维 · 修订 1")).toBeInTheDocument();
    expect(screen.getByText("3 / 4")).toBeInTheDocument();
    expect(screen.getByText(/查询向量维度不一致 2/)).toBeInTheDocument();
    const comparePanel = screen.getByRole("heading", { name: "跨文献并行比较" }).closest("article")!;
    expect(within(comparePanel).getByText("80.0%")).toBeInTheDocument();
    expect(within(comparePanel).getByText("28 / 16 / 9")).toBeInTheDocument();
    expect(within(comparePanel).getByText("7 / 2")).toBeInTheDocument();
    expect(within(comparePanel).getByText("3200 / 640")).toBeInTheDocument();
    expect(within(comparePanel).getByText(/全部子任务失败 1/)).toBeInTheDocument();
    expect(screen.getByText(/可用 · 3 个已发现工具/)).toBeInTheDocument();
    expect(screen.queryByText("绝密记忆不应展示")).not.toBeInTheDocument();

    const testConnection = screen.getByRole("button", { name: "检测学术搜索连接" });
    fireEvent.click(testConnection);
    expect(testConnection).toBeDisabled();
    fireEvent.click(testConnection);
    await waitFor(() => expect(connectionTests).toBe(1));
  });

  it("Harness 指标失败时不伪装成零数据，并保留独立 MCP 状态", async () => {
    server.use(
      http.get(`${API_BASE_URL}/admin/harness/metrics`, () => HttpResponse.json(
        { detail: "指标服务暂不可用" },
        { status: 503 },
      )),
      http.get(`${API_BASE_URL}/admin/mcp/servers`, () => HttpResponse.json({
        feature_enabled: true,
        servers: [{ id: "academic", display_name: "学术搜索", transport: "streamable_http", enabled: true, health_status: "healthy", consecutive_failures: 0, tool_count: 3, tools: [] }],
      })),
    );

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("tab", { name: "Agent Harness" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("指标服务暂不可用");
    expect(screen.getByText("暂时无法读取 Harness 指标")).toBeInTheDocument();
    expect(screen.queryByText("0 → 0")).not.toBeInTheDocument();
    expect(screen.getByText(/可用 · 3 个已发现工具/)).toBeInTheDocument();
  });

  it("管理页签支持方向键切换与焦点跟随", async () => {
    render(<AdminView />);
    const overview = await screen.findByRole("tab", { name: "运行概览" });
    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowRight" });
    const rag = screen.getByRole("tab", { name: "RAG 质量" });
    expect(rag).toHaveFocus();
    expect(rag).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "RAG 质量" })).toBeInTheDocument();
  });
});

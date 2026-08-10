import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { API_BASE_URL, changePassword, getAdminDiscoveryMetrics, getAdminModelHealth, getAdminRagObservability, login, realDataSource, setAdminUserActive } from "@/lib/data-source";
import { server } from "./test-server";

describe("真实 API 契约", () => {
  it("登录发送后端需要的 email/password 并携带 cookie 模式", async () => {
    let payload: unknown;
    server.use(http.post(`${API_BASE_URL}/auth/login`, async ({ request }) => { payload = await request.json(); return HttpResponse.json({ id: "u1", email: "a@b.com" }); }));
    await login("a@b.com", "12345678");
    expect(payload).toEqual({ email: "a@b.com", password: "12345678" });
  });

  it("首次改密使用后端字段并携带 CSRF", async () => {
    document.cookie = "paperleaf_csrf=password-token; path=/";
    let payload: unknown;
    let csrf = "";
    server.use(http.post(`${API_BASE_URL}/auth/change-password`, async ({ request }) => {
      payload = await request.json();
      csrf = request.headers.get("X-CSRF-Token") ?? "";
      return HttpResponse.json({ id: "u1", email: "a@b.com", role: "user", active: true, must_change_password: false });
    }));
    const user = await changePassword("temporary-password", "a-new-strong-password");
    expect(payload).toEqual({ current_password: "temporary-password", new_password: "a-new-strong-password" });
    expect(csrf).toBe("password-token");
    expect(user.mustChangePassword).toBe(false);
  });

  it("读取单篇文献时映射真实页数与 arXiv 元数据", async () => {
    server.use(http.get(`${API_BASE_URL}/papers/p1`, () => HttpResponse.json({ id: "p1", title: "论文", authors: ["作者甲"], year: 2025, page_count: 12, status: "ready", abstract: "摘要", arxiv_id: "2501.00001" })));
    await expect(realDataSource.getPaper("p1")).resolves.toMatchObject({ id: "p1", authors: "作者甲", pages: 12, status: "ready", arxivId: "2501.00001" });
  });

  it("发现推荐只在换一批时请求刷新，并映射持久化状态", async () => {
    let requested = "";
    server.use(http.get(`${API_BASE_URL}/discover/recommendations`, ({ request }) => {
      requested = new URL(request.url).search;
      return HttpResponse.json({
        items: [{
          item_id: "recommendation-item-1",
          arxiv_id: "2601.00002",
          title: "Related paper",
          authors: ["作者甲", "作者乙"],
          abstract: "摘要",
          published: "2026-01-02T00:00:00Z",
          matched_paper_title: "DeepDTA",
          matched_terms: ["drug", "target"],
          match_type: "semantic",
        }],
        batch_id: "recommendation-batch-2",
        batch: 2,
        basis_paper_count: 3,
        seed_paper_title: "DeepDTA",
        profile_terms: ["drug", "target"],
        strategy: "semantic_keyword",
        restored: true,
        feedback_applied: true,
        generated_at: "2026-08-08T12:00:00Z",
      });
    }));

    await expect(realDataSource.recommendArxiv({ refresh: true })).resolves.toMatchObject({
      batchId: "recommendation-batch-2",
      batch: 2,
      basisPaperCount: 3,
      seedPaperTitle: "DeepDTA",
      strategy: "semantic_keyword",
      restored: true,
      feedbackApplied: true,
      items: [{ itemId: "recommendation-item-1", id: "2601.00002", authors: "作者甲、作者乙", matchedPaperTitle: "DeepDTA", matchedTerms: ["drug", "target"], matchType: "semantic" }],
    });
    expect(requested).toContain("refresh=true");
    expect(requested).not.toContain("exclude=");
  });

  it("保存推荐反馈并映射管理端漏斗指标", async () => {
    document.cookie = "paperleaf_csrf=feedback-token; path=/";
    let feedbackPayload: unknown;
    server.use(
      http.post(`${API_BASE_URL}/discover/recommendations/items/item-1/feedback`, async ({ request }) => {
        feedbackPayload = await request.json();
        return HttpResponse.json({ item_id: "item-1", feedback: "interested", opened: false, imported: false });
      }),
      http.get(`${API_BASE_URL}/admin/discovery-metrics`, () => HttpResponse.json({
        window_hours: 720, generated_at: "2026-08-08T12:00:00Z", batches: 2,
        impressions: 12, opened: 4, interested: 3, not_interested: 1, imported: 2,
        feedback_count: 4, click_through_rate: 1 / 3, interest_hit_rate: 0.75,
        feedback_rate: 1 / 3, import_rate: 1 / 6,
      })),
    );

    await expect(realDataSource.recordDiscoveryFeedback("item-1", "interested")).resolves.toBe("interested");
    expect(feedbackPayload).toEqual({ action: "interested" });
    await expect(getAdminDiscoveryMetrics("30d")).resolves.toMatchObject({
      impressions: 12, opened: 4, interestHitRate: 0.75, clickThroughRate: 1 / 3,
    });
  });

  it("上传使用 multipart 和 CSRF 请求头", async () => {
    document.cookie = "paperleaf_csrf=test-token; path=/";
    let uploadedType = ""; let csrf = "";
    server.use(http.post(`${API_BASE_URL}/papers`, async ({ request }) => { const data = await request.formData(); uploadedType = (data.get("file") as File).type; csrf = request.headers.get("X-CSRF-Token") ?? ""; return HttpResponse.json({ id: "p1", title: "论文", page_count: 3 }, { status: 201 }); }));
    const paper = await realDataSource.upload(new File(["%PDF-1.7"], "论文.pdf", { type: "application/pdf" }), () => undefined);
    expect(uploadedType).toBe("application/pdf"); expect(csrf).toBe("test-token"); expect(paper.id).toBe("p1");
  });

  it("批量重新索引发送所选文献并返回实际入队数量", async () => {
    document.cookie = "paperleaf_csrf=reindex-token; path=/";
    let payload: unknown;
    server.use(http.post(`${API_BASE_URL}/papers/bulk`, async ({ request }) => {
      payload = await request.json();
      return HttpResponse.json({ action: "reindex", affected: 1, paper_ids: ["p1"] });
    }));

    await expect(realDataSource.bulkPapers({ paperIds: ["p1", "p2"], action: "reindex" })).resolves.toEqual({
      action: "reindex",
      affected: 1,
      paperIds: ["p1"],
    });
    expect(payload).toEqual({ paper_ids: ["p1", "p2"], action: "reindex" });
  });

  it("Chat 使用 content/scope/selected_paper_ids/web_enabled 并聚合 SSE", async () => {
    document.cookie = "paperleaf_csrf=chat-token; path=/";
    let payload: Record<string, unknown> = {};
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, async ({ request }) => { payload = await request.json() as Record<string, unknown>; return new HttpResponse('event: node_started\ndata: {"event":"node_started","run_id":"r1","data":{"node":"retrieve_library","step":2}}\n\nevent: node_finished\ndata: {"event":"node_finished","run_id":"r1","data":{"node":"retrieve_library","step":2,"status":"completed","duration_ms":18}}\n\nevent: tool_finished\ndata: {"event":"tool_finished","run_id":"r1","data":{"tool":"search_library","evidence_quality":{"grade":"sufficient","confidence":0.82,"reason_code":"channel_agreement","summary":"已定位 1 个证据页，关键词与语义检索相互印证","evidence_count":1,"page_count":1,"paper_count":1,"channels":["keyword","vector"]}}}\n\nevent: message_delta\ndata: {"event":"message_delta","run_id":"r1","data":{"delta":"有依"}}\n\nevent: message_delta\ndata: {"event":"message_delta","run_id":"r1","data":{"delta":"据 [chunk:c1]"}}\n\nevent: citation\ndata: {"event":"citation","run_id":"r1","data":{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","excerpt":"原文"}}\n\nevent: tool_finished\ndata: {"event":"tool_finished","run_id":"r1","data":{"tool":"validate_answer","evidence_quality":{"grade":"sufficient","confidence":0.91,"reason_code":"deterministic_claim_support","summary":"回答中的 1 条主张均有可核验引用。","evidence_count":1,"page_count":1,"paper_count":1,"channels":["keyword","vector"],"claim_count":1,"cited_claim_count":1,"supported_claim_count":1,"claim_citation_coverage":1,"claim_support_coverage":1}}}\n\nevent: run_finished\ndata: {"event":"run_finished","run_id":"r1","data":{"status":"completed"}}\n\n', { headers: { "content-type": "text/event-stream" } }); }));
    const progress: string[] = [];
    const answerUpdates: string[] = [];
    const citationUpdates: number[] = [];
    const answer = await realDataSource.ask("为什么？", ["p1"], {
      onActivity: (activity) => progress.push(`${activity.label}:${activity.status}`),
      onAnswerUpdate: (value) => answerUpdates.push(value),
      onCitationsUpdate: (items) => citationUpdates.push(items.length),
    });
    expect(payload).toEqual({ content: "为什么？", scope: "paper", selected_paper_ids: ["p1"], web_enabled: false });
    expect(answer.answer).toBe("有依据"); expect(answer.citations[0]).toMatchObject({ paperId: "p1", page: 2, chunkId: "c1" });
    expect(answer.citations[0].quote).toBe("原文");
    expect(answer.evidenceQuality).toMatchObject({ grade: "sufficient", confidence: 0.91, pageCount: 1, channels: ["keyword", "vector"], claimCount: 1, citedClaimCount: 1, supportedClaimCount: 1, claimCitationCoverage: 1, claimSupportCoverage: 1 });
    expect(progress).toEqual(["检索文献证据:running", "检索文献证据:completed", "检索文献库:completed", "核验回答证据:completed"]);
    expect(answerUpdates).toEqual(["有依", "有依据"]);
    expect(citationUpdates).toEqual([1]);
    expect(answer.activities).toEqual([
      expect.objectContaining({ node: "retrieve_library", status: "completed", durationMs: 18 }),
      expect.objectContaining({ node: "search_library", label: "检索文献库", status: "completed" }),
      expect.objectContaining({ node: "validate_answer", label: "核验回答证据", status: "completed" }),
    ]);
  });

  it("Chat 将真实 Function Tool 名称映射为用户可见活动", async () => {
    document.cookie = "paperleaf_csrf=tool-activity-token; path=/";
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, () => new HttpResponse(
      'event: tool_started\ndata: {"event":"tool_started","run_id":"r-tool","data":{"tool":"mcp__academic__search_openalex","call_index":0}}\n\n'
      + 'event: tool_finished\ndata: {"event":"tool_finished","run_id":"r-tool","data":{"tool":"mcp__academic__search_openalex","call_index":0,"status":"succeeded"}}\n\n'
      + 'event: message_delta\ndata: {"event":"message_delta","run_id":"r-tool","data":{"delta":"找到候选论文"}}\n\n'
      + 'event: run_finished\ndata: {"event":"run_finished","run_id":"r-tool","data":{"status":"completed"}}\n\n',
      { headers: { "content-type": "text/event-stream" } },
    )));
    const progress: string[] = [];

    const answer = await realDataSource.ask("联网推荐五篇论文", [], {
      onActivity: (activity) => progress.push(`${activity.label}:${activity.status}`),
    });

    expect(progress).toEqual(["查询 OpenAlex:running", "查询 OpenAlex:completed"]);
    expect(answer.answer).toBe("找到候选论文");
    expect(answer.activities).toEqual([
      expect.objectContaining({
        key: "tool:0:mcp__academic__search_openalex",
        node: "mcp__academic__search_openalex",
        label: "查询 OpenAlex",
        status: "completed",
      }),
    ]);
  });

  it("管理员模型状态映射运行策略与熔断字段", async () => {
    server.use(http.get(`${API_BASE_URL}/admin/model-health`, () => HttpResponse.json({
      configured: true,
      providers: [{ provider: "primary", purposes: { answer: { configured: true, status: "open", consecutive_failures: 3, retry_after_ms: 4200 } } }],
      policy: { timeout_seconds: 30, attempts_per_provider: 2, failure_threshold: 3, cooldown_seconds: 60 },
    })));
    await expect(getAdminModelHealth()).resolves.toMatchObject({
      configured: true,
      providers: [{ provider: "primary", purposes: { answer: { status: "open", consecutiveFailures: 3, retryAfterMs: 4200 } } }],
      policy: { timeoutSeconds: 30, attemptsPerProvider: 2, failureThreshold: 3, cooldownSeconds: 60 },
    });
  });

  it("管理员 RAG 指标映射召回通道、意图、耗时和失败率", async () => {
    server.use(http.get(`${API_BASE_URL}/admin/observability`, () => HttpResponse.json({
      window_hours: 168,
      generated_at: "2026-08-07T12:00:00Z",
      totals: { runs: 5, terminal_runs: 5, failed_runs: 1, cited_answers: 3, grounded_answers: 2, rag_issue_runs: 1, telemetry_runs: 5, telemetry_coverage: 1, failure_rate: 0.2, cited_answer_rate: 0.6, rag_issue_rate: 0.2 },
      funnel: [{ key: "retrieved", label: "召回证据", count: 4, rate: 0.8 }],
      latency: { overall: { samples: 5, p95_ms: 2400 }, stages: [{ stage: "retrieval", samples: 5, p50_ms: 90, p95_ms: 210 }] },
      retrieval_channels: [{ channel: "vector", label: "向量检索", runs: 4, cited_answer_rate: 0.75, sufficient_evidence_rate: 0.75, retrieval_p95_ms: 180 }],
      intents: [{ intent: "comparison", label: "比较分析", runs: 2, cited_answer_rate: 0.5, sufficient_evidence_rate: 1, p95_ms: 2300 }],
      failures: [{ category: "model_timeout", label: "模型响应超时", count: 1, rate: 0.2 }],
      chunking_strategies: [{ strategy: "structure_aware_v2", runs: 5 }],
      runtime_store: { backend: "redis", status: "available" },
      privacy: { content_collected: false, identifiers_collected: false },
    })));
    await expect(getAdminRagObservability("7d")).resolves.toMatchObject({
      windowHours: 168,
      totals: { runs: 5, failureRate: 0.2, citedAnswerRate: 0.6, groundedAnswers: 2, ragIssueRate: 0.2 },
      latency: { overall: { p95Ms: 2400 }, stages: [{ stage: "retrieval", p95Ms: 210 }] },
      retrievalChannels: [{ channel: "vector", retrievalP95Ms: 180 }],
      intents: [{ intent: "comparison" }],
      failures: [{ category: "model_timeout" }],
      runtimeStore: { backend: "redis", status: "available" },
    });
  });

  it("管理员操作失败时保留服务端返回的具体原因", async () => {
    server.use(http.patch(`${API_BASE_URL}/admin/users/admin-1`, () => HttpResponse.json(
      { detail: "不能停用或降级最后一名管理员" },
      { status: 409 },
    )));
    await expect(setAdminUserActive("admin-1", false)).rejects.toThrow("不能停用或降级最后一名管理员");
  });

  it("修改、重试和删除文献都使用 CSRF 且映射最新状态", async () => {
    document.cookie = "paperleaf_csrf=manage-token; path=/";
    const methods: string[] = [];
    const tokens: string[] = [];
    let updatePayload: unknown;
    server.use(
      http.patch(`${API_BASE_URL}/papers/p1`, async ({ request }) => {
        methods.push(request.method); tokens.push(request.headers.get("X-CSRF-Token") ?? ""); updatePayload = await request.json();
        return HttpResponse.json({ id: "p1", title: "新标题", authors: ["作者甲"], year: 2026, page_count: 8, status: "ready" });
      }),
      http.post(`${API_BASE_URL}/papers/p1/retry`, ({ request }) => {
        methods.push(request.method); tokens.push(request.headers.get("X-CSRF-Token") ?? "");
        return HttpResponse.json({ id: "p1", title: "新标题", authors: ["作者甲"], year: 2026, page_count: 8, status: "queued" });
      }),
      http.delete(`${API_BASE_URL}/papers/p1`, ({ request }) => {
        methods.push(request.method); tokens.push(request.headers.get("X-CSRF-Token") ?? "");
        return HttpResponse.json({ id: "p1", status: "deleting" }, { status: 202 });
      }),
    );
    const updated = await realDataSource.updatePaper("p1", { title: "新标题", authors: ["作者甲"], year: 2026 });
    const retried = await realDataSource.retryPaper("p1");
    await realDataSource.deletePaper("p1");
    expect(updatePayload).toEqual({ title: "新标题", authors: ["作者甲"], year: 2026 });
    expect(updated.status).toBe("ready");
    expect(retried.status).toBe("indexing");
    expect(methods).toEqual(["PATCH", "POST", "DELETE"]);
    expect(tokens).toEqual(["manage-token", "manage-token", "manage-token"]);
  });

  it("总结和结构图保留物理页与 Chunk 映射，并仅在明确刷新时绕过缓存", async () => {
    document.cookie = "paperleaf_csrf=artifact-token; path=/";
    const citation = (chunk: string, page: number) => ({ chunk_id: chunk, physical_page: page });
    const artifactQueries: string[] = [];
    server.use(
      http.post(`${API_BASE_URL}/papers/p1/summary`, ({ request }) => {
        artifactQueries.push(new URL(request.url).search);
        return HttpResponse.json({ paper_id: "p1", artifact_status: "ready", stale: false, mode: "model", sections: [
        { key: "research_question", title: "研究问题", facts: [{ text: "问题事实", citations: [citation("p1:p2:c0", 2)] }] },
        { key: "core_method", title: "核心方法", facts: [{ text: "方法事实", citations: [citation("p1:p4:c0", 4)] }] },
        { key: "experimental_setup", title: "实验设置", facts: [{ text: "实验事实", citations: [citation("p1:p6:c0", 6)] }] },
        { key: "main_results", title: "主要结果", facts: [{ text: "结果事实", citations: [citation("p1:p8:c0", 8)] }] },
        { key: "limitations_scope", title: "局限与适用范围", facts: [{ text: "局限事实", citations: [citation("p1:p9:c0", 9)] }] },
      ], citations: [citation("p1:p2:c0", 2)] });
      }),
      http.post(`${API_BASE_URL}/papers/p1/structure-graph`, ({ request }) => {
        artifactQueries.push(new URL(request.url).search);
        return HttpResponse.json({ paper_id: "p1", artifact_status: "ready", stale: false, mermaid: "flowchart TD\n n1 --> n2\n n2 --> n3\n n3 --> n4\n n4 --> n5", nodes: [
        { id: "n1", type: "研究问题", label: "问题", summary: "问题说明", citations: [citation("p1:p2:c0", 2)] },
        { id: "n2", type: "方法", label: "方法", summary: "方法说明", citations: [citation("p1:p4:c0", 4), citation("p1:p5:c0", 5)] },
        { id: "n3", type: "数据", label: "数据", summary: "数据说明", citations: [citation("p1:p6:c0", 6)] },
        { id: "n4", type: "结果", label: "结果", summary: "结果说明", citations: [citation("p1:p8:c0", 8)] },
        { id: "n5", type: "局限", label: "局限", summary: "局限说明", citations: [citation("p1:p9:c0", 9)] },
      ], edges: [{ source: "n1", target: "n2" }, { source: "n2", target: "n3" }, { source: "n3", target: "n4" }, { source: "n4", target: "n5" }] });
      }),
    );
    const summary = await realDataSource.summarizePaper("p1");
    expect(summary).toMatchObject({ paperId: "p1", status: "ready" });
    expect(summary.sections).toHaveLength(5);
    expect(summary.sections[0]).toMatchObject({ key: "research_problem", facts: [{ citations: [{ chunkId: "p1:p2:c0", physicalPage: 2 }] }] });
    const graph = await realDataSource.buildStructureGraph("p1");
    expect(graph).toMatchObject({ paperId: "p1", status: "ready" });
    expect(graph.nodes).toHaveLength(5);
    expect(graph.nodes[0]).toMatchObject({ label: "问题", type: "research_problem", citations: [{ physicalPage: 2 }] });
    expect(graph.nodes[1]).toMatchObject({ label: "方法", citations: [{ physicalPage: 4 }, { physicalPage: 5 }] });
    await realDataSource.summarizePaper("p1", { refresh: true });
    await realDataSource.buildStructureGraph("p1", { refresh: true });
    expect(artifactQueries).toEqual(["", "", "?refresh=true", "?refresh=true"]);
  });

  it("拒绝旧式乱序 Chunk 图并把模型错误代码转换为中文", async () => {
    server.use(
      http.post(`${API_BASE_URL}/papers/p1/structure-graph`, () => HttpResponse.json({ paper_id: "p1", status: "failed", fallback_reason: "模型输出格式不合法", evidence_excerpt: "可回读证据 [chunk:c1]", mermaid: "", nodes: [], edges: [] })),
      http.post(`${API_BASE_URL}/papers/p1/summary`, () => HttpResponse.json({ detail: { code: "model_timeout" } }, { status: 503 })),
    );
    await expect(realDataSource.buildStructureGraph("p1")).resolves.toMatchObject({ status: "failed", nodes: [], fallbackReason: "模型输出格式不合法", evidenceExcerpt: "可回读证据 [chunk:c1]" });
    await expect(realDataSource.summarizePaper("p1")).rejects.toThrow("论文分析模型响应超时");
  });

  it("层级集合使用真实 API 字段并传递父集合", async () => {
    document.cookie = "paperleaf_csrf=organize-token; path=/";
    const requests: Array<{ method: string; path: string; csrf: string; body?: unknown }> = [];
    server.use(
      http.get(`${API_BASE_URL}/collections`, () => HttpResponse.json([{ id: "c1", name: "核心方法", description: "基础论文", parent_id: null, paper_ids: ["p1"], recursive_paper_count: 2, children: [{ id: "c1-1", name: "检索", parent_id: "c1", paper_ids: ["p2"], recursive_paper_count: 1, children: [] }] }])),
      http.post(`${API_BASE_URL}/collections`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "c2", name: "实验", parent_id: "c1", paper_ids: [], recursive_paper_count: 0, children: [] }, { status: 201 }); }),
      http.patch(`${API_BASE_URL}/collections/c2`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "c2", name: "实验复现", parent_id: null, paper_ids: [], recursive_paper_count: 0, children: [] }); }),
      http.delete(`${API_BASE_URL}/collections/c2`, ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "" }); return HttpResponse.json({ status: "deleted" }); }),
    );
    await expect(realDataSource.listCollections()).resolves.toMatchObject([{ id: "c1", parentId: null, recursivePaperCount: 2, children: [{ id: "c1-1", parentId: "c1" }] }]);
    await realDataSource.createCollection({ name: "实验", parentId: "c1" });
    await realDataSource.updateCollection("c2", { name: "实验复现", parentId: null });
    await realDataSource.deleteCollection("c2");
    expect(requests).toHaveLength(3);
    expect(requests.every((item) => item.csrf === "organize-token")).toBe(true);
    expect(requests[0].body).toEqual({ name: "实验", parent_id: "c1" });
    expect(requests[1].body).toEqual({ name: "实验复现", parent_id: null });
  });

  it("全文翻译创建传递当前页优先级并映射任务与逐页状态", async () => {
    document.cookie = "paperleaf_csrf=translation-token; path=/";
    let createPayload: unknown;
    server.use(
      http.post(`${API_BASE_URL}/papers/p1/translations`, async ({ request }) => {
        createPayload = await request.json();
        expect(request.headers.get("X-CSRF-Token")).toBe("translation-token");
        return HttpResponse.json({ id: "t1", paper_id: "p1", target_language: "zh-CN", status: "partial", completed_pages: 5, failed_pages: 2, total_pages: 12 }, { status: 202 });
      }),
      http.get(`${API_BASE_URL}/papers/p1/translations/t2`, () => HttpResponse.json({ id: "t2", paper_id: "p1", target_language: "zh-CN", status: "completed", completed_pages: 0, total_pages: 12 })),
      http.get(`${API_BASE_URL}/papers/p1/translations/t1/pages/7`, () => HttpResponse.json({ page: 7, status: "no_text", translated_text: "" })),
    );
    await expect(realDataSource.createPaperTranslation("p1", "zh-CN", 7)).resolves.toMatchObject({ id: "t1", status: "partial", progress: 100, completedPages: 5, failedPages: 2, totalPages: 12 });
    expect(createPayload).toEqual({ target_language: "zh-CN", priority_page: 7, refresh: false });
    await realDataSource.createPaperTranslation("p1", "zh-CN", 7, { refresh: true });
    expect(createPayload).toEqual({ target_language: "zh-CN", priority_page: 7, refresh: true });
    await expect(realDataSource.getPaperTranslation("p1", "t2")).resolves.toMatchObject({ status: "completed", progress: 100, completedPages: 0, totalPages: 12 });
    await expect(realDataSource.getPaperTranslationPage("p1", "t1", 7)).resolves.toEqual({ page: 7, status: "no_text", text: "", error: undefined });
  });

  it("文献列表支持服务端集合递归范围并映射出版物", async () => {
    let requested = "";
    server.use(http.get(`${API_BASE_URL}/papers`, ({ request }) => {
      requested = new URL(request.url).searchParams.get("collection_id") ?? "";
      return HttpResponse.json([{ id: "p1", title: "论文", authors: ["作者"], year: 2026, publication: "Bioinformatics", page_count: 8, status: "ready" }]);
    }));
    await expect(realDataSource.listPapers({ collectionId: "dta" })).resolves.toMatchObject([{ publication: "Bioinformatics" }]);
    expect(requested).toBe("dta");
  });

  it("批量整理和最近阅读记录保留所有权相关字段", async () => {
    document.cookie = "paperleaf_csrf=bulk-token; path=/";
    let bulkPayload: unknown;
    server.use(
      http.post(`${API_BASE_URL}/papers/bulk`, async ({ request }) => { bulkPayload = await request.json(); return HttpResponse.json({ action: "add_collection", affected: 2, paper_ids: ["p1", "p2"] }); }),
      http.post(`${API_BASE_URL}/papers/p1/opened`, () => HttpResponse.json({ id: "p1", title: "论文", authors: ["作者"], status: "ready", last_opened_at: "2026-07-29T04:00:00Z" })),
    );
    await realDataSource.bulkPapers({ paperIds: ["p1", "p2"], action: "add_collection", targetId: "c1" });
    expect(bulkPayload).toEqual({ paper_ids: ["p1", "p2"], action: "add_collection", target_id: "c1" });
    await expect(realDataSource.recordPaperOpened("p1")).resolves.toMatchObject({ id: "p1", lastOpenedAt: "2026-07-29T04:00:00Z" });
  });
});

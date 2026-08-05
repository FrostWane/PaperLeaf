import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { API_BASE_URL, changePassword, getAdminModelHealth, login, realDataSource } from "@/lib/data-source";
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

  it("上传使用 multipart 和 CSRF 请求头", async () => {
    document.cookie = "paperleaf_csrf=test-token; path=/";
    let uploadedType = ""; let csrf = "";
    server.use(http.post(`${API_BASE_URL}/papers`, async ({ request }) => { const data = await request.formData(); uploadedType = (data.get("file") as File).type; csrf = request.headers.get("X-CSRF-Token") ?? ""; return HttpResponse.json({ id: "p1", title: "论文", page_count: 3 }, { status: 201 }); }));
    const paper = await realDataSource.upload(new File(["%PDF-1.7"], "论文.pdf", { type: "application/pdf" }), () => undefined);
    expect(uploadedType).toBe("application/pdf"); expect(csrf).toBe("test-token"); expect(paper.id).toBe("p1");
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
    expect(progress).toEqual(["检索文献证据:running", "检索文献证据:completed"]);
    expect(answerUpdates).toEqual(["有依", "有依据"]);
    expect(citationUpdates).toEqual([1]);
    expect(answer.activities).toEqual([expect.objectContaining({ node: "retrieve_library", status: "completed", durationMs: 18 })]);
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

  it("总结和结构图保留物理页与 Chunk 映射", async () => {
    document.cookie = "paperleaf_csrf=artifact-token; path=/";
    server.use(
      http.post(`${API_BASE_URL}/papers/p1/summary`, () => HttpResponse.json({ paper_id: "p1", content: "证据化总结", mode: "extractive", citations: [{ chunk_id: "p1:p2:c0", physical_page: 2 }] })),
      http.post(`${API_BASE_URL}/papers/p1/structure-graph`, () => HttpResponse.json({ paper_id: "p1", mermaid: "flowchart TD\n n1 --> n2", nodes: [{ id: "n1", label: "问题", physical_page: 2, chunk_id: "p1:p2:c0" }], edges: [{ source: "n1", target: "n2" }] })),
    );
    await expect(realDataSource.summarizePaper("p1")).resolves.toMatchObject({ paperId: "p1", mode: "extractive", citations: [{ chunkId: "p1:p2:c0", physicalPage: 2 }] });
    await expect(realDataSource.buildStructureGraph("p1")).resolves.toMatchObject({ paperId: "p1", nodes: [{ label: "问题", physicalPage: 2 }], edges: [{ source: "n1", target: "n2" }] });
  });

  it("集合、标签和文献归属使用真实 API 字段", async () => {
    document.cookie = "paperleaf_csrf=organize-token; path=/";
    const requests: Array<{ method: string; path: string; csrf: string; body?: unknown }> = [];
    server.use(
      http.get(`${API_BASE_URL}/collections`, () => HttpResponse.json([{ id: "c1", name: "核心方法", description: "基础论文", paper_ids: ["p1"] }])),
      http.post(`${API_BASE_URL}/collections`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "c2", name: "实验", paper_ids: [] }, { status: 201 }); }),
      http.patch(`${API_BASE_URL}/collections/c2`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "c2", name: "实验复现", paper_ids: [] }); }),
      http.delete(`${API_BASE_URL}/collections/c2`, ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "" }); return HttpResponse.json({ status: "deleted" }); }),
      http.get(`${API_BASE_URL}/tags`, () => HttpResponse.json([{ id: "t1", name: "RAG", color: "#AFC3CE", paper_ids: ["p1"] }])),
      http.post(`${API_BASE_URL}/tags`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "t2", name: "复现", color: "#B8C9BC", paper_ids: [] }, { status: 201 }); }),
      http.patch(`${API_BASE_URL}/tags/t2`, async ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "", body: await request.json() }); return HttpResponse.json({ id: "t2", name: "已复现", color: "#B8C9BC", paper_ids: [] }); }),
      http.delete(`${API_BASE_URL}/tags/t2`, ({ request }) => { requests.push({ method: request.method, path: new URL(request.url).pathname, csrf: request.headers.get("X-CSRF-Token") ?? "" }); return HttpResponse.json({ status: "deleted" }); }),
    );
    await expect(realDataSource.listCollections()).resolves.toEqual([{ id: "c1", name: "核心方法", description: "基础论文", paperIds: ["p1"] }]);
    await realDataSource.createCollection({ name: "实验" });
    await realDataSource.updateCollection("c2", { name: "实验复现" });
    await realDataSource.deleteCollection("c2");
    await expect(realDataSource.listTags()).resolves.toEqual([{ id: "t1", name: "RAG", color: "#AFC3CE", paperIds: ["p1"] }]);
    await realDataSource.createTag({ name: "复现", color: "#B8C9BC" });
    await realDataSource.updateTag("t2", { name: "已复现", color: "#B8C9BC" });
    await realDataSource.deleteTag("t2");
    expect(requests).toHaveLength(6);
    expect(requests.every((item) => item.csrf === "organize-token")).toBe(true);
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

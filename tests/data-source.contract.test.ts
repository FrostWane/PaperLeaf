import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { API_BASE_URL, changePassword, login, realDataSource } from "@/lib/data-source";
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
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, async ({ request }) => { payload = await request.json() as Record<string, unknown>; return new HttpResponse('event: message_delta\ndata: {"event":"message_delta","run_id":"r1","data":{"delta":"有依据"}}\n\nevent: citation\ndata: {"event":"citation","run_id":"r1","data":{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","quote":"原文"}}\n\nevent: run_finished\ndata: {"event":"run_finished","run_id":"r1","data":{"status":"completed"}}\n\n', { headers: { "content-type": "text/event-stream" } }); }));
    const answer = await realDataSource.ask("为什么？", ["p1"]);
    expect(payload).toEqual({ content: "为什么？", scope: "paper", selected_paper_ids: ["p1"], web_enabled: false });
    expect(answer.answer).toBe("有依据"); expect(answer.citations[0]).toMatchObject({ paperId: "p1", page: 2, chunkId: "c1" });
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
});

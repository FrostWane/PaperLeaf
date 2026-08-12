import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { API_BASE_URL, realDataSource } from "@/lib/data-source";
import { server } from "./test-server";

function sessionPayload(overrides: Record<string, unknown> = {}) {
  return { id: "s1", title: "证据对比", type: "collection", paper_id: null, collection_id: "c1", current_run_id: null, current_run_status: null, created_at: "2026-08-06T10:00:00Z", updated_at: "2026-08-06T10:00:00Z", ...overrides };
}

function runPayload(status: string) {
  return { run_id: "r1", session_id: "s1", status, cancel_requested: false, answer: status === "completed" ? "第一段\n\n第二段" : "第一段", citations: [], created_at: "2026-08-06T10:00:00Z", updated_at: "2026-08-06T10:00:01Z" };
}

describe("持久化对话真实 API", () => {
  it("按会话契约执行 CRUD，提交返回 202 并携带幂等键", async () => {
    document.cookie = "paperleaf_csrf=chat-contract; path=/";
    const seen: Array<{ method: string; path: string; body?: unknown; key?: string }> = [];
    server.use(
      http.get(`${API_BASE_URL}/chat/sessions`, () => HttpResponse.json([sessionPayload()])),
      http.post(`${API_BASE_URL}/chat/sessions`, async ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json(sessionPayload(), { status: 201 });
      }),
      http.patch(`${API_BASE_URL}/chat/sessions/s1`, async ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json(sessionPayload({ title: "新标题" }));
      }),
      http.delete(`${API_BASE_URL}/chat/sessions/s1`, ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname });
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(`${API_BASE_URL}/chat/sessions/s1/messages`, () => HttpResponse.json([{ id: "m1", session_id: "s1", role: "assistant", sequence: 3, status: "failed", content: "已核验部分", citations: [], run_id: "r0", created_at: "2026-08-06T10:00:00Z", updated_at: "2026-08-06T10:00:02Z" }])),
      http.post(`${API_BASE_URL}/chat/sessions/s1/messages`, async ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname, body: await request.json(), key: request.headers.get("Idempotency-Key") ?? "" });
        return HttpResponse.json({ session_id: "s1", message_id: "m2", run_id: "r1", status: "pending", replayed: false }, { status: 202 });
      }),
    );

    await expect(realDataSource.listChatSessions()).resolves.toMatchObject([{ id: "s1", type: "collection", collectionId: "c1" }]);
    await realDataSource.createChatSession({ type: "collection", collectionId: "c1", title: "证据对比" });
    await realDataSource.updateChatSession("s1", "新标题");
    await expect(realDataSource.listChatMessages("s1")).resolves.toMatchObject([{ id: "m1", runId: "r0", sequence: 3, status: "failed", updatedAt: "2026-08-06T10:00:02Z" }]);
    await expect(realDataSource.submitChatMessage("s1", "比较方法", "stable-key-1", { webEnabled: true })).resolves.toMatchObject({ runId: "r1", status: "pending" });
    await realDataSource.deleteChatSession("s1");

    expect(seen[0].body).toEqual({ type: "collection", title: "证据对比", collection_id: "c1" });
    expect(seen[1].body).toEqual({ title: "新标题" });
    expect(seen[2]).toMatchObject({ key: "stable-key-1", body: { content: "比较方法", web_enabled: true } });
    expect(seen[3].method).toBe("DELETE");
  });

  it("SSE 断线后用 Last-Event-ID 补发，并对乱序与重放只累计一次", async () => {
    let streamRequest = 0;
    let runRequest = 0;
    const lastEventIds: string[] = [];
    server.use(
      http.get(`${API_BASE_URL}/agent/runs/r1/events`, ({ request }) => {
        streamRequest += 1;
        lastEventIds.push(request.headers.get("Last-Event-ID") ?? "");
        if (streamRequest === 1) return new HttpResponse(
          'id: 1\nevent: message_delta\ndata: {"sequence":1,"event":"message_delta","data":{"delta":"第一段","citations":[{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","excerpt":"证据"}]}}\n\nid: 3\nevent: citation\ndata: {"sequence":3,"event":"citation","data":{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","excerpt":"证据"}}\n\nid: 2\nevent: message_delta\ndata: {"sequence":2,"event":"message_delta","data":{"delta":"\\n\\n第二段"}}\n\n',
          { headers: { "content-type": "text/event-stream" } },
        );
        return new HttpResponse(
          'id: 2\nevent: message_delta\ndata: {"sequence":2,"event":"message_delta","data":{"delta":"\\n\\n第二段"}}\n\nid: 3\nevent: citation\ndata: {"sequence":3,"event":"citation","data":{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","excerpt":"证据"}}\n\nid: 4\nevent: run_finished\ndata: {"sequence":4,"event":"run_finished","data":{"status":"completed"}}\n\n',
          { headers: { "content-type": "text/event-stream" } },
        );
      }),
      http.get(`${API_BASE_URL}/agent/runs/r1`, () => {
        runRequest += 1;
        return HttpResponse.json(runPayload(runRequest === 1 ? "running" : "completed"));
      }),
    );
    const answers: string[] = [];
    const citationSizes: number[] = [];
    await realDataSource.subscribeAgentRun("r1", {
      onAnswerUpdate: (answer) => answers.push(answer),
      onCitationsUpdate: (citations) => citationSizes.push(citations.length),
    });
    expect(answers).toEqual(["第一段", "第一段\n\n第二段"]);
    expect(citationSizes).toEqual([1]);
    expect(lastEventIds).toEqual(["", "3"]);
  });

  it("interrupted 关闭本次事件流后直接返回，不进入永久重连", async () => {
    let requests = 0;
    server.use(
      http.get(`${API_BASE_URL}/agent/runs/r1/events`, () => {
        requests += 1;
        return new HttpResponse('id: 1\nevent: interrupt\ndata: {"sequence":1,"event":"interrupt","data":{"pending_action":{"action_id":"a1"}}}\n\n', { headers: { "content-type": "text/event-stream" } });
      }),
      http.get(`${API_BASE_URL}/agent/runs/r1`, () => HttpResponse.json({ ...runPayload("interrupted"), pending_action: { action_id: "a1", type: "arxiv_import", risk_message: "需要确认", allowed_decisions: ["approve", "reject"], candidates: [] } })),
    );
    const connections: string[] = [];
    await realDataSource.subscribeAgentRun("r1", { onConnectionState: (state) => connections.push(state) });
    expect(requests).toBe(1);
    expect(connections).toEqual(["connected"]);
  });

  it("读取运行时保留编排版本，供页面恢复比较进度", async () => {
    server.use(
      http.get(`${API_BASE_URL}/agent/runs/r1`, () => HttpResponse.json({
        ...runPayload("running"),
        orchestration_version: "compare_map_reduce_v2",
      })),
    );

    await expect(realDataSource.getAgentRun("r1")).resolves.toMatchObject({
      runId: "r1",
      orchestrationVersion: "compare_map_reduce_v2",
    });
  });

  it("将并行比较子任务映射为隔离活动，并把超时标记为未完成", async () => {
    server.use(
      http.get(`${API_BASE_URL}/agent/runs/r1/events`, () => new HttpResponse(
        [
          'id: 1\nevent: node_started\ndata: {"sequence":1,"event":"node_started","data":{"node":"plan_comparison","subtask_total":2,"objective":"不得展示"}}\n\n',
          'id: 2\nevent: node_started\ndata: {"sequence":2,"event":"node_started","data":{"node":"compare_subtask","subtask_id":"s1","ordinal":1,"total":2,"paper_count":2,"paper_ids":["private"]}}\n\n',
          'id: 3\nevent: node_started\ndata: {"sequence":3,"event":"node_started","data":{"node":"compare_subtask","subtask_id":"s2","ordinal":2,"total":2}}\n\n',
          'id: 4\nevent: node_finished\ndata: {"sequence":4,"event":"node_finished","data":{"node":"compare_subtask","subtask_id":"s1","ordinal":1,"total":2,"status":"completed","finding_count":4,"duration_ms":120}}\n\n',
          'id: 5\nevent: node_finished\ndata: {"sequence":5,"event":"node_finished","data":{"node":"compare_subtask","subtask_id":"s2","ordinal":2,"total":2,"status":"timeout","error_category":"provider","duration_ms":500}}\n\n',
          'id: 6\nevent: node_finished\ndata: {"sequence":6,"event":"node_finished","data":{"node":"merge_comparison","status":"partial","partial_failure":true,"succeeded_subtasks":1,"failed_subtasks":1}}\n\n',
          'id: 7\nevent: run_finished\ndata: {"sequence":7,"event":"run_finished","data":{"status":"completed"}}\n\n',
        ].join(""),
        { headers: { "content-type": "text/event-stream" } },
      )),
      http.get(`${API_BASE_URL}/agent/runs/r1`, () => HttpResponse.json(runPayload("completed"))),
    );
    const activities: Array<{ key: string; status: string; kind?: string; rawStatus?: string; findingCount?: number }> = [];

    await realDataSource.subscribeAgentRun("r1", { onActivity: (activity) => activities.push(activity) });

    expect(activities.filter((item) => item.key === "subtask:s1")).toMatchObject([
      { status: "running", kind: "comparison_subtask" },
      { status: "completed", kind: "comparison_subtask", findingCount: 4 },
    ]);
    expect(activities.filter((item) => item.key === "subtask:s2")).toMatchObject([
      { status: "running", kind: "comparison_subtask" },
      { status: "failed", kind: "comparison_subtask", rawStatus: "timeout" },
    ]);
    expect(activities.at(-1)).toMatchObject({ key: "comparison:merge", status: "completed", kind: "comparison_merge", rawStatus: "partial" });
    expect(JSON.stringify(activities)).not.toContain("不得展示");
    expect(JSON.stringify(activities)).not.toContain("private");
  });
});

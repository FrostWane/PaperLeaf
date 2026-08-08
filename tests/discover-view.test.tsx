import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiscoverView } from "@/components/discover-view";
import { API_BASE_URL } from "@/lib/data-source";
import { server } from "./test-server";

function recommendation(id: string, title: string, batch: number) {
  return {
    items: [{
      item_id: `item-${id}`,
      arxiv_id: id,
      title,
      authors: ["Ada Lovelace"],
      abstract: "A related research paper.",
      published: "2026-01-01T00:00:00Z",
      matched_paper_title: "DeepDTA",
      matched_terms: ["drug", "target"],
      match_type: "semantic",
    }],
    batch_id: `batch-${batch}`,
    batch,
    basis_paper_count: 3,
    seed_paper_title: "DeepDTA",
    profile_terms: ["drug", "target", "affinity"],
    strategy: "semantic_keyword",
    generated_at: "2026-08-08T12:00:00Z",
  };
}

describe("发现论文", () => {
  beforeEach(() => vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real"));
  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("首屏恢复上次推荐，只有换一批才请求刷新", async () => {
    const requests: string[] = [];
    server.use(
      http.get(`${API_BASE_URL}/users/me/preferences`, () => HttpResponse.json({ arxiv_search_enabled: true })),
      http.get(`${API_BASE_URL}/discover/recommendations`, ({ request }) => {
      const url = new URL(request.url);
      requests.push(url.search);
      const refresh = url.searchParams.get("refresh") === "true";
      return HttpResponse.json({ ...(refresh
        ? recommendation("2601.00002", "Second related paper", 1)
        : recommendation("2601.00001", "First related paper", 0)), restored: !refresh });
      }),
    );

    render(<DiscoverView />);

    expect(await screen.findByRole("heading", { name: "First related paper" })).toBeInTheDocument();
    expect(screen.getByText("与《DeepDTA》相关 · drug / target")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "换一批" }));
    expect(await screen.findByRole("heading", { name: "Second related paper" })).toBeInTheDocument();
    expect(requests).toHaveLength(2);
    expect(requests[0]).not.toContain("refresh=true");
    expect(requests[1]).toContain("refresh=true");
  });

  it("保存感兴趣反馈并在重新进入时恢复选中状态", async () => {
    let feedbackCalls = 0;
    let interested = false;
    server.use(
      http.get(`${API_BASE_URL}/users/me/preferences`, () => HttpResponse.json({ arxiv_search_enabled: true })),
      http.get(`${API_BASE_URL}/discover/recommendations`, () => HttpResponse.json({
        ...recommendation("2601.00001", "Persistent paper", 0),
        restored: true,
        items: [{ ...recommendation("2601.00001", "Persistent paper", 0).items[0], feedback: interested ? "interested" : null }],
      })),
      http.post(`${API_BASE_URL}/discover/recommendations/items/item-2601.00001/feedback`, async ({ request }) => {
        feedbackCalls += 1;
        interested = (await request.json() as { action: string }).action === "interested";
        return HttpResponse.json({ item_id: "item-2601.00001", feedback: "interested", opened: false, imported: false });
      }),
    );

    const first = render(<DiscoverView />);
    const interestButton = await screen.findByRole("button", { name: "感兴趣" });
    fireEvent.click(interestButton);
    await waitFor(() => expect(interestButton).toHaveAttribute("aria-pressed", "true"));
    expect(feedbackCalls).toBe(1);
    first.unmount();

    render(<DiscoverView />);
    expect(await screen.findByRole("button", { name: "感兴趣" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("heading", { name: "Persistent paper" })).toBeInTheDocument();
  });

  it("空文献库不展示固定演示论文", async () => {
    server.use(
      http.get(`${API_BASE_URL}/users/me/preferences`, () => HttpResponse.json({ arxiv_search_enabled: true })),
      http.get(`${API_BASE_URL}/discover/recommendations`, () => HttpResponse.json({
      items: [],
      batch: 0,
      basis_paper_count: 0,
      profile_terms: [],
      strategy: "empty_library",
      })),
    );

    render(<DiscoverView />);

    expect(await screen.findByRole("heading", { name: "先添加几篇研究论文" })).toBeInTheDocument();
    expect(screen.queryByText("Retrieval-Augmented Generation for Large Language Models: A Survey")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "换一批" })).toBeDisabled());
  });

  it("未授权联网发现时不发送文献主题", async () => {
    let recommendationRequests = 0;
    server.use(
      http.get(`${API_BASE_URL}/users/me/preferences`, () => HttpResponse.json({ arxiv_search_enabled: false })),
      http.get(`${API_BASE_URL}/discover/recommendations`, () => {
        recommendationRequests += 1;
        return HttpResponse.json(recommendation("2601.00001", "不应出现", 0));
      }),
    );

    render(<DiscoverView />);

    expect(await screen.findByRole("heading", { name: "开启个性化论文推荐" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往设置" })).toHaveAttribute("href", "/settings#agent");
    expect(recommendationRequests).toBe(0);
  });
});

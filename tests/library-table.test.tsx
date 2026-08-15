import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryTable } from "@/components/library-table";
import { API_BASE_URL } from "@/lib/data-source";
import type { Paper } from "@/lib/types";
import { server } from "./test-server";

function paper(index: number): Paper {
  return {
    id: `paper-${index}`,
    title: `分页论文 ${String(index).padStart(2, "0")}`,
    authors: "PaperLeaf",
    venue: "测试期刊",
    publication: "测试期刊",
    year: 2026,
    status: "ready",
    pages: 1,
    abstract: "用于验证文献库分页。",
  };
}

describe("LibraryTable 文献分页", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    server.use(
      http.get(`${API_BASE_URL}/papers`, () => HttpResponse.json(Array.from({ length: 41 }, (_, index) => paper(index + 1)))),
      http.get(`${API_BASE_URL}/collections`, () => HttpResponse.json([])),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("每页展示 20 篇，并在筛选后回到第一页", async () => {
    const { container } = render(<LibraryTable />);

    await screen.findByText("分页论文 01");
    expect(container.querySelectorAll("tbody tr")).toHaveLength(20);
    expect(screen.getByText("第 1 / 3 页 · 共 41 篇")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("分页论文 21")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 3 页 · 共 41 篇")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("搜索标题、作者或出版物"), { target: { value: "分页论文 03" } });
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "文献库分页" })).not.toBeInTheDocument());
    expect(screen.getByText("分页论文 03")).toBeInTheDocument();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
  });
});

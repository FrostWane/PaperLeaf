import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CollectionTree } from "@/components/collection-tree";
import type { PaperCollection } from "@/lib/types";

const collections: PaperCollection[] = [{
  id: "dta",
  name: "DTA",
  parentId: null,
  paperIds: ["p1"],
  recursivePaperCount: 2,
  children: [{ id: "deep", name: "深度模型", parentId: "dta", paperIds: ["p2"], recursivePaperCount: 1, children: [] }],
}];

afterEach(cleanup);

describe("CollectionTree", () => {
  it("固定根可折叠全部集合，并支持方向键展开父集合", () => {
    render(<CollectionTree collections={collections} selectedId="all" onSelect={() => undefined} allCount={2} />);
    expect(screen.getByRole("treeitem", { name: /全部文献.*2/ })).toHaveAttribute("aria-level", "1");
    expect(screen.getByRole("treeitem", { name: /DTA.*2/ })).toHaveAttribute("aria-level", "2");

    fireEvent.click(screen.getByTestId("collection-toggle-all"));
    expect(screen.queryByRole("treeitem", { name: /DTA/ })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("treeitem", { name: /全部文献/ }), { key: "ArrowRight" });
    const parent = screen.getByRole("treeitem", { name: /DTA.*2/ });
    expect(screen.queryByRole("treeitem", { name: /深度模型/ })).not.toBeInTheDocument();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    expect(screen.getByRole("treeitem", { name: /深度模型/ })).toBeVisible();
    fireEvent.keyDown(parent, { key: "ArrowLeft" });
    expect(screen.queryByRole("treeitem", { name: /深度模型/ })).not.toBeInTheDocument();
  });

  it("Enter 和 Space 都会选择当前集合", () => {
    const onSelect = vi.fn();
    render(<CollectionTree collections={collections} selectedId="all" onSelect={onSelect} allCount={2} />);
    const parent = screen.getByRole("treeitem", { name: /DTA.*2/ });
    fireEvent.keyDown(parent, { key: "Enter" });
    fireEvent.keyDown(parent, { key: " " });
    expect(onSelect).toHaveBeenNthCalledWith(1, "dta");
    expect(onSelect).toHaveBeenNthCalledWith(2, "dta");
  });

  it("恢复深层筛选时自动展开祖先集合", async () => {
    render(<CollectionTree collections={collections} selectedId="deep" onSelect={() => undefined} allCount={2} />);
    await waitFor(() => expect(screen.getByRole("treeitem", { name: /深度模型.*1/ })).toHaveAttribute("aria-selected", "true"));
  });
});

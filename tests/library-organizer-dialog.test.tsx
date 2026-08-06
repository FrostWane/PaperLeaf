import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LibraryOrganizerDialog } from "@/components/library-organizer-dialog";
import type { PaperCollection } from "@/lib/types";

const collections: PaperCollection[] = [{
  id: "dta",
  name: "DTA",
  parentId: null,
  paperIds: ["p1"],
  recursivePaperCount: 2,
  children: [{ id: "deep", name: "深度模型", parentId: "dta", paperIds: ["p2"], recursivePaperCount: 1, children: [] }],
}];

describe("LibraryOrganizerDialog", () => {
  it("从父集合创建子集合，并通过父集合选择器移动集合", async () => {
    const onCreateCollection = vi.fn(async () => undefined);
    const onUpdateCollection = vi.fn(async () => undefined);
    render(<LibraryOrganizerDialog open onOpenChange={() => undefined} collections={collections} onCreateCollection={onCreateCollection} onUpdateCollection={onUpdateCollection} onDeleteCollection={async () => undefined} />);

    fireEvent.click(screen.getByRole("treeitem", { name: /DTA.*2/ }));
    fireEvent.click(screen.getByRole("button", { name: "在此新建子集合" }));
    expect(screen.getByLabelText("父集合")).toHaveValue("dta");
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "实验数据" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(onCreateCollection).toHaveBeenCalledWith({ name: "实验数据", description: undefined, parentId: "dta" }));

    fireEvent.keyDown(screen.getByRole("treeitem", { name: /DTA.*2/ }), { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("treeitem", { name: /深度模型/ }));
    expect(screen.getByLabelText("父集合")).toHaveValue("dta");
    fireEvent.change(screen.getByLabelText("父集合"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(onUpdateCollection).toHaveBeenCalledWith("deep", expect.objectContaining({ name: "深度模型", parentId: null })));
  });
});

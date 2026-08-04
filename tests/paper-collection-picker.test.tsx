import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PaperCollectionPicker } from "@/components/paper-collection-picker";
import { PaperCollectionsDialog } from "@/components/paper-collections-dialog";

const collections = [
  { id: "dta", name: "DTA", description: "药物—靶点亲和力", paperIds: ["paper-1"] },
  { id: "dti", name: "DTI", description: "药物—靶点相互作用", paperIds: [] },
];

describe("PaperCollectionPicker", () => {
  it("展示当前归属，并能添加与移除多个集合", () => {
    let selectedIds = ["dta"];
    const { rerender } = render(
      <PaperCollectionPicker collections={collections} selectedIds={selectedIds} onChange={(next) => { selectedIds = next; }} />,
    );

    expect(screen.getByRole("checkbox", { name: /DTA/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /DTI/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole("checkbox", { name: /DTI/ }));
    expect(selectedIds).toEqual(["dta", "dti"]);

    rerender(<PaperCollectionPicker collections={collections} selectedIds={selectedIds} onChange={(next) => { selectedIds = next; }} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /DTA/ }));
    expect(selectedIds).toEqual(["dti"]);
  });

  it("部分保存失败时保持弹窗并同步服务器的真实集合", async () => {
    const paper = { id: "paper-1", title: "Attention", authors: "Vaswani 等", year: 2017, venue: "NeurIPS", pages: 15, status: "ready" as const, tags: [], abstract: "" };
    const onSave = vi.fn().mockResolvedValue({ selectedIds: ["dta"], error: "部分集合保存失败：DTI。已重新读取服务器中的实际结果。" });

    render(<PaperCollectionsDialog paper={paper} collections={collections} open onOpenChange={() => undefined} onSave={onSave} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /DTA/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /DTI/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存集合" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("部分集合保存失败：DTI");
    expect(screen.getByRole("checkbox", { name: /DTA/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /DTI/ })).not.toBeChecked();
    expect(screen.getByRole("dialog", { name: "管理论文集合" })).toBeVisible();
  });
});

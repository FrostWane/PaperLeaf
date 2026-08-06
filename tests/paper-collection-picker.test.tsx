import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PaperCollectionPicker } from "@/components/paper-collection-picker";
import { PaperCollectionsDialog } from "@/components/paper-collections-dialog";

const collections = [
  { id: "dta", name: "DTA", description: "药物—靶点亲和力", parentId: null, paperIds: ["paper-1"], recursivePaperCount: 2, children: [
    { id: "deep-dta", name: "深度模型", description: "神经网络方法", parentId: "dta", paperIds: ["paper-2"], recursivePaperCount: 1, children: [] },
  ] },
  { id: "dti", name: "DTI", description: "药物—靶点相互作用", parentId: null, paperIds: [], recursivePaperCount: 0, children: [] },
];

afterEach(cleanup);

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

  it("支持键盘展开父集合并选择子集合", () => {
    let selectedIds: string[] = [];
    render(<PaperCollectionPicker collections={collections} selectedIds={selectedIds} onChange={(next) => { selectedIds = next; }} />);
    const toggle = screen.getByRole("button", { name: "收起 DTA" });
    fireEvent.click(toggle);
    expect(screen.queryByRole("checkbox", { name: /深度模型/ })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("button", { name: "展开 DTA" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "展开 DTA" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /深度模型/ }));
    expect(selectedIds).toEqual(["deep-dta"]);
  });

  it("五层集合全部可见且最深层仍可多选", () => {
    const level5 = { id: "l5", name: "第五层", parentId: "l4", paperIds: [], recursivePaperCount: 0, children: [] };
    const level4 = { id: "l4", name: "第四层", parentId: "l3", paperIds: [], recursivePaperCount: 0, children: [level5] };
    const level3 = { id: "l3", name: "第三层", parentId: "l2", paperIds: [], recursivePaperCount: 0, children: [level4] };
    const level2 = { id: "l2", name: "第二层", parentId: "l1", paperIds: [], recursivePaperCount: 0, children: [level3] };
    const fiveLevels = [{ id: "l1", name: "第一层", parentId: null, paperIds: [], recursivePaperCount: 0, children: [level2] }];
    let selectedIds: string[] = [];
    render(<PaperCollectionPicker collections={fiveLevels} selectedIds={selectedIds} onChange={(next) => { selectedIds = next; }} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /第五层/ }));
    expect(selectedIds).toEqual(["l5"]);
  });

  it("部分保存失败时保持弹窗并同步服务器的真实集合", async () => {
    const paper = { id: "paper-1", title: "Attention", authors: "Vaswani 等", year: 2017, venue: "arXiv", publication: "NeurIPS", pages: 15, status: "ready" as const, abstract: "" };
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

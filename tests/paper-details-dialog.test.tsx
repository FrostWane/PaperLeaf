import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PaperDetailsDialog } from "@/components/paper-details-dialog";
import { papers } from "@/lib/fixtures";

describe("PaperDetailsDialog", () => {
  afterEach(cleanup);

  it("在关闭期间同步 Worker 回填的最新论文元数据", async () => {
    const initial = {
      ...papers[0],
      id: "paper-metadata",
      title: "document",
      authors: "",
      year: 0,
    };
    const callbacks = {
      onOpenChange: vi.fn(),
      onSave: vi.fn(async () => undefined),
      onDelete: vi.fn(async () => undefined),
      onRetry: vi.fn(async () => undefined),
    };
    const view = render(
      <PaperDetailsDialog paper={initial} open={false} {...callbacks} />,
    );

    const parsed = {
      ...initial,
      title: "解析后的论文标题",
      authors: "Ada Lovelace、Alan Turing",
      year: 2024,
      publication: "Bioinformatics",
    };
    view.rerender(
      <PaperDetailsDialog paper={parsed} open={false} {...callbacks} />,
    );
    view.rerender(
      <PaperDetailsDialog paper={parsed} open {...callbacks} />,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("标题")).toHaveValue("解析后的论文标题");
      expect(screen.getByLabelText("作者")).toHaveValue("Ada Lovelace、Alan Turing");
      expect(screen.getByLabelText("年份")).toHaveValue("2024");
      expect(screen.getByLabelText("出版物")).toHaveValue("Bioinformatics");
    });
  });
});

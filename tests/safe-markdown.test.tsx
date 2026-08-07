import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SafeMarkdown } from "@/components/safe-markdown";
import type { Citation } from "@/lib/types";

const citation: Citation = {
  id: "c1",
  chunkId: "paper:p4:c1",
  paperId: "paper",
  paperTitle: "测试论文",
  page: 4,
  quote: "可核对原文",
  href: "/api/v1/papers/paper/file#page=4",
};

describe("SafeMarkdown", () => {
  afterEach(cleanup);

  it("渲染标题、表格与代码，但移除原始 HTML 和图片", () => {
    const { container } = render(<SafeMarkdown content={'## 结论\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```js\nalert(1)\n```\n\n<img src="https://evil.test/a.png"><script>alert(2)</script>\n\n![跟踪](https://evil.test/pixel.png)'} />);
    expect(screen.getByRole("heading", { name: "结论" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("alert(1)")).toBeInTheDocument();
    expect(container.querySelector("img,script")).toBeNull();
    expect(screen.queryByAltText("跟踪")).not.toBeInTheDocument();
  });

  it("只允许 http/https 外链，危险与相对链接降级为文本", () => {
    render(<SafeMarkdown content={'[安全](https://example.com) [脚本](javascript:alert(1)) [数据](data:text/html,x) [站内](/admin) [邮件](mailto:a@example.com)'} />);
    expect(screen.getByRole("link", { name: "安全" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("link", { name: "安全" })).toHaveAttribute("target", "_blank");
    expect(screen.queryByRole("link", { name: "脚本" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "数据" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "站内" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "邮件" })).not.toBeInTheDocument();
  });

  it("仅把结构化 citations 白名单中的 Chunk 标记转换为页码按钮", () => {
    const onOpen = vi.fn();
    render(<SafeMarkdown content="结论 [chunk:paper:p4:c1]，伪造 [chunk:paper:p99:fake] 和 [内部链接](#paperleaf-citation-0)。" citations={[citation]} onOpenCitation={onOpen} />);
    const button = screen.getByRole("button", { name: "查看《测试论文》PDF 第 4 页" });
    expect(button).toHaveTextContent("[1]");
    fireEvent.click(button);
    expect(onOpen).toHaveBeenCalledWith(citation);
    expect(screen.queryByText(/p99/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "内部链接" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /查看《测试论文》/ })).toHaveLength(1);
  });

  it("不在链接、图片或代码上下文内注入引用按钮，避免嵌套交互元素", () => {
    const { container } = render(<SafeMarkdown content={'[chunk:paper:p4:c1](https://example.com) `\[chunk:paper:p4:c1\]`'} citations={[citation]} />);
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://example.com");
    expect(container.querySelector("a button")).toBeNull();
    expect(screen.queryByRole("button", { name: /查看《测试论文》/ })).not.toBeInTheDocument();
  });
});

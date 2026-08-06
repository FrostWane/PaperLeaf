import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("首页工作台入口", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("真实部署统一进入登录页，不把用户送往演示数据", async () => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    const { default: Home } = await import("@/app/page");
    render(<Home />);

    expect(screen.getByRole("link", { name: "登录工作台" })).toHaveAttribute("href", "/login");
    expect(screen.getByRole("link", { name: /进入 PaperLeaf/ })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("link", { name: "打开演示" })).not.toBeInTheDocument();
  });

  it("演示构建继续进入固定数据 Demo", async () => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "demo");
    const { default: Home } = await import("@/app/page");
    render(<Home />);

    expect(screen.getByRole("link", { name: "打开演示" })).toHaveAttribute("href", "/demo");
    expect(screen.getByRole("link", { name: /体验 PaperLeaf/ })).toHaveAttribute("href", "/demo");
  });
});

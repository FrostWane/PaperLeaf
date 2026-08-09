import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { server } from "./test-server";

// 组件行为测试不应为每个 PaperWorkspace 重复解析 Mermaid 的完整浏览器包。
// Mermaid 自身的接线由 structure-diagram 专项测试覆盖，真实 SVG 由 E2E 覆盖。
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: "<svg></svg>" }),
  },
}));

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

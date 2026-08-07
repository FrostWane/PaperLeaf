import { describe, expect, it } from "vitest";
import { artifactFailureMessage } from "@/lib/artifacts";

describe("AI 产物失败原因", () => {
  it.each([
    ["model_not_configured", "尚未配置论文分析模型"],
    ["model_timeout", "模型响应超时"],
    ["citation_validation_failed", "页码引用未通过证据核验"],
    ["invalid_output", "不符合结构化格式要求"],
  ])("将 %s 映射成明确中文原因", (reason, message) => {
    expect(artifactFailureMessage(reason)).toContain(message);
  });

  it("上游返回英文异常时只展示正常中文说明", () => {
    const message = artifactFailureMessage("Model request timed out after 120 seconds");

    expect(message).toContain("这次概括没有生成成功");
    expect(message).toContain("论文原文和索引都已保留");
    expect(message).not.toMatch(/[A-Za-z]{4,}/);
  });
});

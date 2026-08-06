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
});

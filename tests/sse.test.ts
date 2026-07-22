import { describe, expect, it } from "vitest";
import { parseSseChunk } from "@/lib/sse";

describe("parseSseChunk", () => {
  it("解析事件、id 与 JSON 数据", () => {
    const events = parseSseChunk('id: run-1\nevent: message_delta\ndata: {"delta":"你好"}\n\nevent: run_finished\ndata: {"status":"completed"}\n\n');
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({ id: "run-1", type: "message_delta", data: { delta: "你好" } });
    expect(events[1].type).toBe("run_finished");
  });

  it("保留多行纯文本数据", () => {
    expect(parseSseChunk("event: error\ndata: 第一行\ndata: 第二行\n\n")[0].data).toBe("第一行\n第二行");
  });

  it("解包后端 SSE envelope", () => {
    const [event] = parseSseChunk('event: message_delta\ndata: {"event":"message_delta","run_id":"run-9","data":{"delta":"证据"}}\n\n');
    expect(event).toEqual({ type: "message_delta", id: "run-9", data: { delta: "证据" } });
  });
});

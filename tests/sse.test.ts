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
    const [event] = parseSseChunk('id: 7\nevent: message_delta\ndata: {"id":7,"sequence":7,"event":"message_delta","run_id":"run-9","data":{"delta":"证据"}}\n\n');
    expect(event).toEqual({ type: "message_delta", id: "7", data: { delta: "证据" } });
  });

  it("没有 SSE id 时使用 envelope sequence，不把 run_id 当游标", () => {
    const [event] = parseSseChunk('event: citation\ndata: {"sequence":9,"event":"citation","run_id":"run-9","data":{"chunk_id":"c1"}}\n\n');
    expect(event).toEqual({ type: "citation", id: "9", data: { chunk_id: "c1" } });
  });
});

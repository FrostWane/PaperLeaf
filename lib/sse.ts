import type { AgentEvent, AgentEventType } from "./types";

export function parseSseChunk(input: string): AgentEvent[] {
  return input
    .replace(/\r\n/g, "\n")
    .split("\n\n")
    .map((block) => block.trim())
    .filter(Boolean)
    .flatMap((block) => {
      let type: AgentEventType = "message_delta";
      let id: string | undefined;
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) type = line.slice(6).trim() as AgentEventType;
        else if (line.startsWith("id:")) id = line.slice(3).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (!data.length) return [];
      const raw = data.join("\n");
      try {
        const parsed = JSON.parse(raw) as unknown;
        if (parsed && typeof parsed === "object" && "event" in parsed && "data" in parsed) {
          const envelope = parsed as { event: AgentEventType; run_id?: string; data: unknown };
          return [{ type: envelope.event, id: envelope.run_id ?? id, data: envelope.data }];
        }
        return [{ type, id, data: parsed }];
      }
      catch { return [{ type, id, data: raw }]; }
    });
}

export async function* readAgentStream(response: Response): AsyncGenerator<AgentEvent> {
  if (!response.body) throw new Error("服务器未返回流式内容");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const boundary = buffer.lastIndexOf("\n\n");
    if (boundary >= 0) {
      const ready = buffer.slice(0, boundary + 2);
      buffer = buffer.slice(boundary + 2);
      for (const event of parseSseChunk(ready)) yield event;
    }
    if (done) break;
  }
  for (const event of parseSseChunk(buffer)) yield event;
}

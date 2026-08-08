export type MemoryType = "preference" | "research_interest" | "entity_alias" | "workflow" | "pinned_context";

export interface MemoryItem {
  id: string;
  type: MemoryType;
  value: string;
  confidence: number;
  sourceKind: string;
  sourceExcerpt?: string;
  pinned: boolean;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface MemoryList {
  items: MemoryItem[];
  total: number;
  active: number;
  capacity: number;
}

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const item = document.cookie.split("; ").find((cookie) => cookie.startsWith(`${name}=`));
  return item ? decodeURIComponent(item.slice(name.length + 1)) : "";
}

function mutationHeaders(json = false): Record<string, string> {
  const csrf = readCookie("paperleaf_csrf");
  return { ...(json ? { "content-type": "application/json" } : {}), ...(csrf ? { "X-CSRF-Token": csrf } : {}) };
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // 非 JSON 错误沿用固定中文提示。
  }
  return fallback;
}

function mapMemory(raw: Record<string, unknown>): MemoryItem {
  return {
    id: String(raw.id ?? ""),
    type: String(raw.type ?? "preference") as MemoryType,
    value: String(raw.value ?? ""),
    confidence: Number(raw.confidence ?? 0),
    sourceKind: String(raw.source_kind ?? "manual"),
    sourceExcerpt: raw.source_excerpt ? String(raw.source_excerpt) : undefined,
    pinned: raw.pinned === true,
    enabled: raw.enabled !== false,
    createdAt: String(raw.created_at ?? ""),
    updatedAt: String(raw.updated_at ?? ""),
  };
}

export async function listMemories(): Promise<MemoryList> {
  const response = await fetch(`${API_BASE_URL}/memories`, { credentials: "include" });
  if (!response.ok) throw new Error(await errorMessage(response, "长期记忆读取失败"));
  const body = await response.json() as Record<string, unknown>;
  return {
    items: Array.isArray(body.items) ? body.items.map((item) => mapMemory(item as Record<string, unknown>)) : [],
    total: Number(body.total ?? 0),
    active: Number(body.active ?? 0),
    capacity: Number(body.capacity ?? 200),
  };
}

export async function createMemory(type: MemoryType, value: string, pinned = false): Promise<MemoryItem> {
  const response = await fetch(`${API_BASE_URL}/memories`, {
    method: "POST", credentials: "include", headers: mutationHeaders(true),
    body: JSON.stringify({ type, value, pinned }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "长期记忆保存失败"));
  return mapMemory(await response.json() as Record<string, unknown>);
}

export async function updateMemory(id: string, input: Partial<Pick<MemoryItem, "type" | "value" | "pinned" | "enabled">>): Promise<MemoryItem> {
  const response = await fetch(`${API_BASE_URL}/memories/${id}`, {
    method: "PATCH", credentials: "include", headers: mutationHeaders(true), body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "长期记忆更新失败"));
  return mapMemory(await response.json() as Record<string, unknown>);
}

export async function deleteMemory(id: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/memories/${id}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
  if (!response.ok) throw new Error(await errorMessage(response, "长期记忆删除失败"));
}

export async function clearMemories(): Promise<number> {
  const response = await fetch(`${API_BASE_URL}/memories/clear`, { method: "POST", credentials: "include", headers: mutationHeaders() });
  if (!response.ok) throw new Error(await errorMessage(response, "长期记忆清空失败"));
  const body = await response.json() as { deleted?: number };
  return Number(body.deleted ?? 0);
}

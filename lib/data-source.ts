import { arxivResults, groundedAnswer, papers, paperStructureGraph, paperSummary } from "./fixtures";
import { readAgentStream } from "./sse";
import type { AdminJob, AgentAnswer, ArxivResult, Paper, PaperStructureGraph, PaperSummary, PaperUpdateInput, SessionUser, UserRecord } from "./types";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

export function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const item = document.cookie.split("; ").find((cookie) => cookie.startsWith(`${name}=`));
  return item ? decodeURIComponent(item.slice(name.length + 1)) : "";
}

export function mutationHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const csrf = readCookie("paperleaf_csrf");
  return csrf ? { ...extra, "X-CSRF-Token": csrf } : extra;
}

function mapSessionUser(item: Record<string, unknown>): SessionUser {
  return {
    id: String(item.id),
    email: String(item.email),
    role: item.role === "admin" ? "admin" : "user",
    active: item.active !== false,
    mustChangePassword: item.must_change_password === true,
  };
}

function mapPaper(item: Record<string, unknown>): Paper {
  const rawStatus = String(item.status ?? "queued");
  return {
    id: String(item.id),
    title: String(item.title),
    authors: Array.isArray(item.authors) && item.authors.length ? item.authors.join("、") : "待识别",
    year: Number(item.year ?? new Date().getFullYear()),
    venue: item.arxiv_id ? "arXiv" : "本地文献",
    pages: Number(item.page_count ?? 0),
    status: rawStatus === "ready" ? "ready" : rawStatus === "failed" ? "failed" : rawStatus === "partial" ? "partial" : rawStatus === "deleting" ? "deleting" : "indexing",
    progress: item.progress === undefined || item.progress === null ? undefined : Number(item.progress),
    tags: [],
    abstract: String(item.abstract ?? ""),
    arxivId: item.arxiv_id ? String(item.arxiv_id) : undefined,
    doi: item.doi ? String(item.doi) : undefined,
    filename: item.filename ? String(item.filename) : undefined,
    sizeBytes: item.size_bytes === undefined ? undefined : Number(item.size_bytes),
    createdAt: item.created_at ? String(item.created_at) : undefined,
  };
}

export async function login(email: string, password: string): Promise<SessionUser> {
  const response = await fetch(`${API_BASE_URL}/auth/login`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ email, password }) });
  if (!response.ok) throw new Error(response.status === 401 ? "邮箱或密码错误" : "暂时无法登录");
  return mapSessionUser(await response.json() as Record<string, unknown>);
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<SessionUser> {
  const response = await fetch(`${API_BASE_URL}/auth/change-password`, {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!response.ok) throw new Error(response.status === 400 ? "当前密码不正确" : "密码修改失败");
  return mapSessionUser(await response.json() as Record<string, unknown>);
}

function mapAdminUser(item: Record<string, unknown>): UserRecord {
  const email = String(item.email);
  return {
    id: String(item.id),
    name: email.split("@")[0],
    email,
    role: item.role === "admin" ? "管理员" : "用户",
    status: item.active === false ? "已停用" : "正常",
    papers: 0,
  };
}

export async function listAdminUsers(): Promise<UserRecord[]> {
  const response = await fetch(`${API_BASE_URL}/admin/users`, { credentials: "include" });
  if (!response.ok) throw new Error("用户列表读取失败");
  return (await response.json() as Array<Record<string, unknown>>).map(mapAdminUser);
}

export async function createAdminUser(email: string, temporaryPassword: string): Promise<UserRecord> {
  const response = await fetch(`${API_BASE_URL}/admin/users`, {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ email, temporary_password: temporaryPassword, role: "user" }),
  });
  if (!response.ok) throw new Error("用户创建失败，请检查邮箱或临时密码");
  return mapAdminUser(await response.json() as Record<string, unknown>);
}

export async function setAdminUserActive(userId: string, active: boolean): Promise<UserRecord> {
  const response = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ active }),
  });
  if (!response.ok) throw new Error("用户状态更新失败");
  return mapAdminUser(await response.json() as Record<string, unknown>);
}

function mapAdminJob(item: Record<string, unknown>): AdminJob {
  return {
    id: String(item.id),
    paperId: item.paper_id ? String(item.paper_id) : undefined,
    type: String(item.type),
    status: item.status as AdminJob["status"],
    progress: Number(item.progress ?? 0),
    attempts: Number(item.attempts ?? 0),
    maxAttempts: Number(item.max_attempts ?? 0),
    errorCode: item.error_code ? String(item.error_code) : undefined,
  };
}

export async function listAdminJobs(): Promise<AdminJob[]> {
  const response = await fetch(`${API_BASE_URL}/admin/jobs`, { credentials: "include" });
  if (!response.ok) throw new Error("任务列表读取失败");
  return (await response.json() as Array<Record<string, unknown>>).map(mapAdminJob);
}

export async function retryAdminJob(jobId: string): Promise<AdminJob> {
  const response = await fetch(`${API_BASE_URL}/admin/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST", credentials: "include", headers: mutationHeaders() });
  if (!response.ok) throw new Error("任务当前无法重试");
  return mapAdminJob(await response.json() as Record<string, unknown>);
}

export interface PaperLeafDataSource {
  listPapers(): Promise<Paper[]>;
  getPaper(paperId: string): Promise<Paper>;
  searchArxiv(query: string): Promise<ArxivResult[]>;
  importArxiv(arxivId: string): Promise<void>;
  ask(question: string, paperIds?: string[]): Promise<AgentAnswer>;
  upload(file: File, onProgress: (value: number) => void): Promise<Paper>;
  updatePaper(paperId: string, input: PaperUpdateInput): Promise<Paper>;
  deletePaper(paperId: string): Promise<void>;
  retryPaper(paperId: string): Promise<Paper>;
  summarizePaper(paperId: string): Promise<PaperSummary>;
  buildStructureGraph(paperId: string): Promise<PaperStructureGraph>;
  fileUrl(paperId: string): string;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export const demoDataSource: PaperLeafDataSource = {
  async listPapers() { await wait(120); return papers; },
  async getPaper(paperId) { await wait(80); return papers.find((paper) => paper.id === paperId) ?? papers[0]; },
  async searchArxiv(query) { await wait(220); return arxivResults.filter((item) => `${item.title} ${item.summary}`.toLowerCase().includes(query.toLowerCase()) || !query); },
  async importArxiv() { await wait(320); },
  async ask(question) { await wait(420); return { ...groundedAnswer, question }; },
  async upload(file, onProgress) {
    for (const value of [18, 42, 71, 100]) { await wait(130); onProgress(value); }
    return { id: `local-${Date.now()}`, title: file.name.replace(/\.pdf$/i, ""), authors: "待识别", year: new Date().getFullYear(), venue: "本地上传", pages: 0, status: "indexing", progress: 0, tags: [], abstract: "PDF 已上传，正在解析元数据与页面文本。" };
  },
  async updatePaper(paperId, input) {
    await wait(220);
    const current = papers.find((paper) => paper.id === paperId) ?? papers[0];
    return { ...current, ...input, authors: input.authors.join("、"), abstract: input.abstract ?? "", doi: input.doi };
  },
  async deletePaper() { await wait(260); },
  async retryPaper(paperId) {
    await wait(220);
    const current = papers.find((paper) => paper.id === paperId) ?? papers[0];
    return { ...current, status: "indexing", progress: 0 };
  },
  async summarizePaper(paperId) { await wait(420); return { ...paperSummary, paperId }; },
  async buildStructureGraph(paperId) { await wait(520); return { ...paperStructureGraph, paperId }; },
  fileUrl(paperId) { return `/demo?paper=${paperId}`; },
};

export const realDataSource: PaperLeafDataSource = {
  async listPapers() {
    const r = await fetch(`${API_BASE_URL}/papers`, { credentials: "include" });
    if (!r.ok) throw new Error("文献读取失败");
    const raw = await r.json() as Array<Record<string, unknown>>;
    return raw.map(mapPaper);
  },
  async getPaper(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}`, { credentials: "include" });
    if (!r.ok) throw new Error("文献信息读取失败");
    return mapPaper(await r.json() as Record<string, unknown>);
  },
  async searchArxiv(query) {
    const r = await fetch(`${API_BASE_URL}/discover/arxiv/search?q=${encodeURIComponent(query)}`, { credentials: "include" }); if (!r.ok) throw new Error("arXiv 搜索失败");
    const raw = await r.json() as Array<Record<string, unknown>>;
    return raw.map((item) => ({ id: String(item.arxiv_id), title: String(item.title), authors: Array.isArray(item.authors) ? item.authors.join("、") : "", year: Number(String(item.published ?? "").slice(0, 4)), summary: String(item.abstract ?? "") }));
  },
  async importArxiv(arxivId) { const r = await fetch(`${API_BASE_URL}/discover/arxiv/import`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ arxiv_id: arxivId }) }); if (!r.ok) throw new Error("arXiv 导入失败"); },
  async ask(question, paperIds = []) {
    const r = await fetch(`${API_BASE_URL}/chat/sessions/default/messages`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ content: question, scope: paperIds.length === 1 ? "paper" : paperIds.length > 1 ? "selection" : "library", selected_paper_ids: paperIds, web_enabled: false }) });
    if (!r.ok) throw new Error("提问失败");
    let answer = ""; const citations: AgentAnswer["citations"] = [];
    for await (const event of readAgentStream(r)) {
      if (event.type === "message_delta" && typeof event.data === "object" && event.data && "delta" in event.data) answer += String((event.data as { delta: unknown }).delta);
      if (event.type === "citation" && typeof event.data === "object" && event.data) { const item = event.data as Record<string, unknown>; const page = Number(item.physical_page ?? item.page ?? 1); citations.push({ id: String(item.chunk_id ?? `c${citations.length + 1}`), chunkId: String(item.chunk_id ?? ""), paperId: String(item.paper_id ?? ""), paperTitle: String(item.paper_title ?? "文献"), page, quote: String(item.quote ?? item.text ?? ""), href: `${API_BASE_URL}/papers/${encodeURIComponent(String(item.paper_id ?? ""))}/file#page=${page}` }); }
      if (event.type === "error") throw new Error("Agent 运行失败");
    }
    return { question, answer, citations };
  },
  async upload(file, onProgress) { const body = new FormData(); body.set("file", file); onProgress(10); const r = await fetch(`${API_BASE_URL}/papers`, { method: "POST", credentials: "include", headers: mutationHeaders(), body }); if (!r.ok) throw new Error("上传失败"); onProgress(100); return mapPaper(await r.json() as Record<string, unknown>); },
  async updatePaper(paperId, input) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}`, { method: "PATCH", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify(input) });
    if (!r.ok) throw new Error("文献信息保存失败");
    return mapPaper(await r.json() as Record<string, unknown>);
  },
  async deletePaper(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error("文献删除失败");
  },
  async retryPaper(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/retry`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error(r.status === 409 ? "当前处理状态不能重试" : "重新处理失败");
    return mapPaper(await r.json() as Record<string, unknown>);
  },
  async summarizePaper(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/summary`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error(r.status === 409 ? "论文还没有完成索引" : "论文总结生成失败");
    const item = await r.json() as Record<string, unknown>;
    return { paperId: String(item.paper_id), content: String(item.content), mode: item.mode === "model" ? "model" : "extractive", citations: (item.citations as Array<Record<string, unknown>> ?? []).map((citation) => ({ chunkId: String(citation.chunk_id), physicalPage: Number(citation.physical_page) })) };
  },
  async buildStructureGraph(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/structure-graph`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error(r.status === 409 ? "论文还没有完成索引" : "结构图生成失败");
    const item = await r.json() as Record<string, unknown>;
    return {
      paperId: String(item.paper_id),
      mermaid: String(item.mermaid),
      nodes: (item.nodes as Array<Record<string, unknown>> ?? []).map((node) => ({ id: String(node.id), label: String(node.label), physicalPage: Number(node.physical_page), chunkId: String(node.chunk_id) })),
      edges: (item.edges as Array<Record<string, unknown>> ?? []).map((edge) => ({ source: String(edge.source), target: String(edge.target) })),
    };
  },
  fileUrl(paperId) { return `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/file`; },
};

export const getDataSource = (): PaperLeafDataSource => process.env.NEXT_PUBLIC_DATA_MODE === "real" ? realDataSource : demoDataSource;

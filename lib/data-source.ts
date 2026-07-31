import { arxivResults, groundedAnswer, papers, paperStructureGraph, paperSummary } from "./fixtures";
import { readAgentStream } from "./sse";
import type { AdminJob, AgentAnswer, AgentEvidenceQuality, ArxivResult, BulkPaperActionInput, CollectionInput, Paper, PaperCollection, PaperStructureGraph, PaperSummary, PaperTag, PaperUpdateInput, SessionUser, TagInput, UserRecord } from "./types";

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
    archivedAt: item.archived_at ? String(item.archived_at) : undefined,
    lastOpenedAt: item.last_opened_at ? String(item.last_opened_at) : undefined,
  };
}

function mapCollection(item: Record<string, unknown>): PaperCollection {
  return {
    id: String(item.id),
    name: String(item.name),
    description: item.description ? String(item.description) : undefined,
    paperIds: (item.paper_ids as unknown[] ?? []).map(String),
  };
}

function mapTag(item: Record<string, unknown>): PaperTag {
  return {
    id: String(item.id),
    name: String(item.name),
    color: item.color ? String(item.color) : undefined,
    paperIds: (item.paper_ids as unknown[] ?? []).map(String),
  };
}

function mapEvidenceQuality(item: Record<string, unknown>): AgentEvidenceQuality {
  return {
    grade: item.grade === "sufficient" ? "sufficient" : "insufficient",
    confidence: Number(item.confidence ?? 0),
    reasonCode: String(item.reason_code ?? "unknown"),
    summary: String(item.summary ?? "检索质量未知"),
    evidenceCount: Number(item.evidence_count ?? 0),
    pageCount: Number(item.page_count ?? 0),
    paperCount: Number(item.paper_count ?? 0),
    channels: Array.isArray(item.channels) ? item.channels.map(String) : [],
    retrievalGrade: item.retrieval_grade === "sufficient" ? "sufficient" : "insufficient",
    answerSupportGrade: item.answer_support_grade === "supported" ? "supported" : item.answer_support_grade === "unsupported" ? "unsupported" : "not_checked",
    answerSupportConfidence: item.answer_support_confidence === null || item.answer_support_confidence === undefined ? undefined : Number(item.answer_support_confidence),
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
  listCollections(): Promise<PaperCollection[]>;
  createCollection(input: CollectionInput): Promise<PaperCollection>;
  updateCollection(collectionId: string, input: CollectionInput): Promise<PaperCollection>;
  deleteCollection(collectionId: string): Promise<void>;
  listTags(): Promise<PaperTag[]>;
  createTag(input: TagInput): Promise<PaperTag>;
  updateTag(tagId: string, input: TagInput): Promise<PaperTag>;
  deleteTag(tagId: string): Promise<void>;
  bulkPapers(input: BulkPaperActionInput): Promise<void>;
  recordPaperOpened(paperId: string): Promise<Paper>;
  fileUrl(paperId: string): string;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let demoPapers = papers.map((paper) => ({ ...paper, tags: [...paper.tags] }));
let demoCollections: PaperCollection[] = [
  { id: "core-methods", name: "核心方法", description: "反复查阅的基础方法论文", paperIds: ["attention", "bert", "rag"] },
  { id: "experiments", name: "实验参考", description: "训练与工程实现相关资料", paperIds: ["lora"] },
  { id: "follow-up", name: "近期跟进", description: "仍需继续核对的新方向", paperIds: ["graphrag"] },
];
let demoTags: PaperTag[] = [
  { id: "nlp", name: "NLP", color: "#AFC3CE", paperIds: ["attention", "bert"] },
  { id: "rag-tag", name: "RAG", color: "#B8C9BC", paperIds: ["rag", "graphrag"] },
  { id: "training", name: "训练", color: "#C9BFAE", paperIds: [] },
];

function updateDemoOrganizationTags(): void {
  demoPapers = demoPapers.map((paper) => ({
    ...paper,
    tags: demoTags.filter((tag) => tag.paperIds.includes(paper.id)).map((tag) => tag.name),
  }));
}

function demoId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}`;
}

export const demoDataSource: PaperLeafDataSource = {
  async listPapers() { await wait(120); updateDemoOrganizationTags(); return demoPapers.map((paper) => ({ ...paper, tags: [...paper.tags] })); },
  async getPaper(paperId) { await wait(80); return demoPapers.find((paper) => paper.id === paperId) ?? demoPapers[0]; },
  async searchArxiv(query) { await wait(220); return arxivResults.filter((item) => `${item.title} ${item.summary}`.toLowerCase().includes(query.toLowerCase()) || !query); },
  async importArxiv() { await wait(320); },
  async ask(question) { await wait(420); return { ...groundedAnswer, question }; },
  async upload(file, onProgress) {
    for (const value of [18, 42, 71, 100]) { await wait(130); onProgress(value); }
    const uploaded: Paper = { id: `local-${Date.now()}`, title: file.name.replace(/\.pdf$/i, ""), authors: "待识别", year: new Date().getFullYear(), venue: "本地上传", pages: 0, status: "indexing", progress: 0, tags: [], abstract: "PDF 已上传，正在解析元数据与页面文本。", createdAt: new Date().toISOString() };
    demoPapers = [uploaded, ...demoPapers];
    return uploaded;
  },
  async updatePaper(paperId, input) {
    await wait(220);
    const index = demoPapers.findIndex((paper) => paper.id === paperId);
    const current = index >= 0 ? demoPapers[index] : demoPapers[0];
    const updated = { ...current, ...input, authors: input.authors.join("、"), abstract: input.abstract ?? "", doi: input.doi };
    if (index >= 0) demoPapers[index] = updated;
    return updated;
  },
  async deletePaper(paperId) { await wait(260); demoPapers = demoPapers.map((paper) => paper.id === paperId ? { ...paper, status: "deleting" } : paper); },
  async retryPaper(paperId) {
    await wait(220);
    const current = demoPapers.find((paper) => paper.id === paperId) ?? demoPapers[0];
    const updated: Paper = { ...current, status: "indexing", progress: 0 };
    demoPapers = demoPapers.map((paper) => paper.id === paperId ? updated : paper);
    return updated;
  },
  async summarizePaper(paperId) { await wait(420); return { ...paperSummary, paperId }; },
  async buildStructureGraph(paperId) { await wait(520); return { ...paperStructureGraph, paperId }; },
  async listCollections() { await wait(100); return demoCollections.map((item) => ({ ...item, paperIds: [...item.paperIds] })); },
  async createCollection(input) { await wait(180); const item = { id: demoId("collection"), ...input, paperIds: [] }; demoCollections = [...demoCollections, item]; return item; },
  async updateCollection(collectionId, input) { await wait(160); const current = demoCollections.find((item) => item.id === collectionId); if (!current) throw new Error("集合不存在"); const updated = { ...current, ...input }; demoCollections = demoCollections.map((item) => item.id === collectionId ? updated : item); return updated; },
  async deleteCollection(collectionId) { await wait(160); demoCollections = demoCollections.filter((item) => item.id !== collectionId); },
  async listTags() { await wait(100); return demoTags.map((item) => ({ ...item, paperIds: [...item.paperIds] })); },
  async createTag(input) { await wait(180); const item = { id: demoId("tag"), ...input, paperIds: [] }; demoTags = [...demoTags, item]; return item; },
  async updateTag(tagId, input) { await wait(160); const current = demoTags.find((item) => item.id === tagId); if (!current) throw new Error("标签不存在"); const updated = { ...current, ...input }; demoTags = demoTags.map((item) => item.id === tagId ? updated : item); updateDemoOrganizationTags(); return updated; },
  async deleteTag(tagId) { await wait(160); demoTags = demoTags.filter((item) => item.id !== tagId); updateDemoOrganizationTags(); },
  async bulkPapers(input) {
    await wait(220);
    const ids = new Set(input.paperIds);
    if (input.action === "archive" || input.action === "unarchive") {
      demoPapers = demoPapers.map((paper) => ids.has(paper.id) ? { ...paper, archivedAt: input.action === "archive" ? new Date().toISOString() : undefined } : paper);
      return;
    }
    if (!input.targetId) throw new Error("整理操作缺少目标");
    const target = input.action.endsWith("collection") ? demoCollections.find((item) => item.id === input.targetId) : demoTags.find((item) => item.id === input.targetId);
    if (!target) throw new Error("整理目标不存在");
    const add = input.action.startsWith("add_");
    target.paperIds = add ? Array.from(new Set([...target.paperIds, ...input.paperIds])) : target.paperIds.filter((paperId) => !ids.has(paperId));
    updateDemoOrganizationTags();
  },
  async recordPaperOpened(paperId) { await wait(80); const current = demoPapers.find((paper) => paper.id === paperId) ?? demoPapers[0]; const updated = { ...current, lastOpenedAt: new Date().toISOString() }; demoPapers = demoPapers.map((paper) => paper.id === paperId ? updated : paper); return updated; },
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
    let answer = ""; const citations: AgentAnswer["citations"] = []; let evidenceQuality: AgentEvidenceQuality | undefined;
    for await (const event of readAgentStream(r)) {
      if (event.type === "message_delta" && typeof event.data === "object" && event.data && "delta" in event.data) answer += String((event.data as { delta: unknown }).delta);
      if (event.type === "tool_finished" && typeof event.data === "object" && event.data && "evidence_quality" in event.data) { const quality = (event.data as { evidence_quality?: unknown }).evidence_quality; if (typeof quality === "object" && quality) evidenceQuality = mapEvidenceQuality(quality as Record<string, unknown>); }
      if (event.type === "citation" && typeof event.data === "object" && event.data) { const item = event.data as Record<string, unknown>; const page = Number(item.physical_page ?? item.page ?? 1); citations.push({ id: String(item.chunk_id ?? `c${citations.length + 1}`), chunkId: String(item.chunk_id ?? ""), paperId: String(item.paper_id ?? ""), paperTitle: String(item.paper_title ?? "文献"), page, quote: String(item.excerpt ?? item.quote ?? item.text ?? ""), href: `${API_BASE_URL}/papers/${encodeURIComponent(String(item.paper_id ?? ""))}/file#page=${page}` }); }
      if (event.type === "error") throw new Error("Agent 运行失败");
    }
    return { question, answer, citations, evidenceQuality };
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
  async listCollections() {
    const r = await fetch(`${API_BASE_URL}/collections`, { credentials: "include" });
    if (!r.ok) throw new Error("集合读取失败");
    return (await r.json() as Array<Record<string, unknown>>).map(mapCollection);
  },
  async createCollection(input) {
    const r = await fetch(`${API_BASE_URL}/collections`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify(input) });
    if (!r.ok) throw new Error(r.status === 409 ? "集合名称已存在" : "集合创建失败");
    return mapCollection(await r.json() as Record<string, unknown>);
  },
  async updateCollection(collectionId, input) {
    const r = await fetch(`${API_BASE_URL}/collections/${encodeURIComponent(collectionId)}`, { method: "PATCH", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify(input) });
    if (!r.ok) throw new Error(r.status === 409 ? "集合名称已存在" : "集合保存失败");
    return mapCollection(await r.json() as Record<string, unknown>);
  },
  async deleteCollection(collectionId) {
    const r = await fetch(`${API_BASE_URL}/collections/${encodeURIComponent(collectionId)}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error("集合删除失败");
  },
  async listTags() {
    const r = await fetch(`${API_BASE_URL}/tags`, { credentials: "include" });
    if (!r.ok) throw new Error("标签读取失败");
    return (await r.json() as Array<Record<string, unknown>>).map(mapTag);
  },
  async createTag(input) {
    const r = await fetch(`${API_BASE_URL}/tags`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify(input) });
    if (!r.ok) throw new Error(r.status === 409 ? "标签名称已存在" : "标签创建失败");
    return mapTag(await r.json() as Record<string, unknown>);
  },
  async updateTag(tagId, input) {
    const r = await fetch(`${API_BASE_URL}/tags/${encodeURIComponent(tagId)}`, { method: "PATCH", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify(input) });
    if (!r.ok) throw new Error(r.status === 409 ? "标签名称已存在" : "标签保存失败");
    return mapTag(await r.json() as Record<string, unknown>);
  },
  async deleteTag(tagId) {
    const r = await fetch(`${API_BASE_URL}/tags/${encodeURIComponent(tagId)}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error("标签删除失败");
  },
  async bulkPapers(input) {
    const r = await fetch(`${API_BASE_URL}/papers/bulk`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ paper_ids: input.paperIds, action: input.action, target_id: input.targetId }) });
    if (!r.ok) throw new Error("批量整理失败，请刷新后重试");
  },
  async recordPaperOpened(paperId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/opened`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error("阅读时间记录失败");
    return mapPaper(await r.json() as Record<string, unknown>);
  },
  fileUrl(paperId) { return `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/file`; },
};

export const getDataSource = (): PaperLeafDataSource => process.env.NEXT_PUBLIC_DATA_MODE === "real" ? realDataSource : demoDataSource;

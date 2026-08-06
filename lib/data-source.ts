import { arxivResults, groundedAnswer, papers, paperStructureGraph, paperSummary } from "./fixtures";
import { readAgentStream } from "./sse";
import { collectionForest, findCollection, flattenCollections, recursivePaperIds } from "./collections";
import type { AdminJob, AgentActivity, AgentAnswer, AgentAskStreamHandlers, AgentEvidenceQuality, ArxivResult, BulkPaperActionInput, CollectionInput, ModelPurposeHealth, ModelRuntimeHealth, Paper, PaperCollection, PaperStructureGraph, PaperSummary, PaperTranslation, PaperTranslationPage, PaperUpdateInput, SessionUser, UserRecord } from "./types";

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
    publication: String(item.publication ?? ""),
    pages: Number(item.page_count ?? 0),
    status: rawStatus === "ready" ? "ready" : rawStatus === "failed" ? "failed" : rawStatus === "partial" ? "partial" : rawStatus === "deleting" ? "deleting" : "indexing",
    progress: item.progress === undefined || item.progress === null ? undefined : Number(item.progress),
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

function mapCollection(item: Record<string, unknown>, nestedParentId: string | null = null): PaperCollection {
  const children = (Array.isArray(item.children) ? item.children : []).map((child) => mapCollection(child as Record<string, unknown>, String(item.id)));
  const paperIds = (item.paper_ids as unknown[] ?? []).map(String);
  return {
    id: String(item.id),
    name: String(item.name),
    description: item.description ? String(item.description) : undefined,
    parentId: item.parent_id ? String(item.parent_id) : nestedParentId,
    paperIds,
    recursivePaperCount: Number(item.recursive_paper_count ?? item.paper_count ?? paperIds.length),
    children,
  };
}

function mapPaperTranslation(item: Record<string, unknown>, paperId: string): PaperTranslation {
  const status = String(item.status ?? "queued");
  const mappedStatus: PaperTranslation["status"] = status === "running" || status === "partial" || status === "completed" || status === "failed" || status === "cancelled" ? status : "queued";
  const totalPages = Number(item.total_pages ?? item.page_count ?? 0);
  const completedPages = Number(item.completed_pages ?? 0);
  const pageRatio = totalPages > 0 ? Math.round(completedPages / totalPages * 100) : 0;
  const progress = mappedStatus === "completed" || mappedStatus === "partial" || mappedStatus === "failed"
    ? 100
    : Number(item.progress ?? pageRatio);
  return {
    id: String(item.id ?? item.translation_id ?? ""),
    paperId: String(item.paper_id ?? paperId),
    targetLanguage: String(item.target_language ?? "zh-CN"),
    status: mappedStatus,
    progress,
    completedPages,
    failedPages: Number(item.failed_pages ?? 0),
    totalPages,
    error: item.error_message ? String(item.error_message) : item.error ? String(item.error) : undefined,
  };
}

function mapPaperTranslationPage(item: Record<string, unknown>, page: number): PaperTranslationPage {
  const status = String(item.status ?? "queued");
  return {
    page: Number(item.page ?? item.physical_page ?? page),
    status: status === "running" || status === "completed" || status === "no_text" || status === "failed" || status === "cancelled" ? status : "queued",
    text: String(item.text ?? item.translated_text ?? ""),
    error: item.error_message ? String(item.error_message) : item.error ? String(item.error) : undefined,
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
    claimCount: Number(item.claim_count ?? 0),
    citedClaimCount: Number(item.cited_claim_count ?? 0),
    supportedClaimCount: Number(item.supported_claim_count ?? 0),
    claimCitationCoverage: Number(item.claim_citation_coverage ?? 0),
    claimSupportCoverage: Number(item.claim_support_coverage ?? 0),
  };
}

async function apiError(response: Response, fallback: string): Promise<Error> {
  try {
    const payload = await response.json() as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      return new Error(payload.detail.trim());
    }
    if (payload.detail && typeof payload.detail === "object") {
      const message = (payload.detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return new Error(message.trim());
    }
  } catch {
    // 非 JSON 错误响应继续使用面向用户的本地兜底文案。
  }
  return new Error(fallback);
}

function visibleAgentAnswer(raw: string): string {
  return raw
    .replace(/\s*\[chunk:[^\]]+\]/g, "")
    .replace(/\s*\[chunk:[^\]]*$/g, "")
    .trim();
}

function mapAgentCitation(item: Record<string, unknown>, fallbackIndex: number): AgentAnswer["citations"][number] {
  const page = Number(item.physical_page ?? item.page ?? 1);
  const paperId = String(item.paper_id ?? "");
  return {
    id: String(item.chunk_id ?? `c${fallbackIndex + 1}`),
    chunkId: String(item.chunk_id ?? ""),
    paperId,
    paperTitle: String(item.paper_title ?? "文献"),
    page,
    quote: String(item.excerpt ?? item.quote ?? item.text ?? ""),
    href: `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/file#page=${page}`,
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
    name: String(item.display_name ?? "").trim() || email.split("@")[0],
    email,
    role: item.role === "admin" ? "管理员" : "用户",
    status: item.active === false ? "已停用" : "正常",
    papers: 0,
  };
}

export async function listAdminUsers(): Promise<UserRecord[]> {
  const response = await fetch(`${API_BASE_URL}/admin/users`, { credentials: "include" });
  if (!response.ok) throw await apiError(response, "用户列表读取失败");
  return (await response.json() as Array<Record<string, unknown>>).map(mapAdminUser);
}

export async function createAdminUser(email: string, temporaryPassword: string): Promise<UserRecord> {
  const response = await fetch(`${API_BASE_URL}/admin/users`, {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ email, temporary_password: temporaryPassword, role: "user" }),
  });
  if (!response.ok) throw await apiError(response, "用户创建失败，请检查邮箱或临时密码");
  return mapAdminUser(await response.json() as Record<string, unknown>);
}

export async function setAdminUserActive(userId: string, active: boolean): Promise<UserRecord> {
  const response = await fetch(`${API_BASE_URL}/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ active }),
  });
  if (!response.ok) throw await apiError(response, "用户状态更新失败");
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
    errorMessage: item.error_message ? String(item.error_message) : undefined,
  };
}

export async function listAdminJobs(): Promise<AdminJob[]> {
  const response = await fetch(`${API_BASE_URL}/admin/jobs`, { credentials: "include" });
  if (!response.ok) throw await apiError(response, "任务列表读取失败");
  return (await response.json() as Array<Record<string, unknown>>).map(mapAdminJob);
}

export async function retryAdminJob(jobId: string): Promise<AdminJob> {
  const response = await fetch(`${API_BASE_URL}/admin/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST", credentials: "include", headers: mutationHeaders() });
  if (!response.ok) throw await apiError(response, "任务当前无法重试");
  return mapAdminJob(await response.json() as Record<string, unknown>);
}

function mapPurposeHealth(item: Record<string, unknown>): ModelPurposeHealth {
  const status = String(item.status ?? "closed");
  return {
    configured: item.configured === true,
    status: status === "open" ? "open" : status === "half_open" ? "half_open" : "closed",
    consecutiveFailures: Number(item.consecutive_failures ?? 0),
    retryAfterMs: Number(item.retry_after_ms ?? 0),
  };
}

export async function getAdminModelHealth(): Promise<ModelRuntimeHealth> {
  const response = await fetch(`${API_BASE_URL}/admin/model-health`, { credentials: "include" });
  if (!response.ok) throw await apiError(response, "AI 能力状态读取失败");
  const raw = await response.json() as Record<string, unknown>;
  const policy = (raw.policy ?? {}) as Record<string, unknown>;
  const providers = Array.isArray(raw.providers) ? raw.providers : [];
  return {
    configured: raw.configured === true,
    providers: providers.map((entry) => {
      const item = entry as Record<string, unknown>;
      const rawPurposes = (item.purposes ?? {}) as Record<string, unknown>;
      return {
        provider: String(item.provider ?? "unknown"),
        purposes: Object.fromEntries(Object.entries(rawPurposes).map(([key, value]) => [key, mapPurposeHealth((value ?? {}) as Record<string, unknown>)])),
      };
    }),
    policy: {
      timeoutSeconds: Number(policy.timeout_seconds ?? 0),
      attemptsPerProvider: Number(policy.attempts_per_provider ?? 0),
      failureThreshold: Number(policy.failure_threshold ?? 0),
      cooldownSeconds: Number(policy.cooldown_seconds ?? 0),
    },
  };
}

const nodeLabels: Record<string, string> = {
  validate_request: "检查问题范围",
  classify_intent: "识别研究意图",
  retrieve_library: "检索文献证据",
  grade_evidence: "核验证据质量",
  search_arxiv: "搜索 arXiv",
  propose_import: "等待导入确认",
  summarize_paper: "整理论文概览",
  build_structure_graph: "构建证据结构",
  generate_answer: "组织证据回答",
  validate_citations: "校验页码引用",
  grade_answer_support: "逐条核验回答",
  suppress_unsupported_answer: "拦截无依据回答",
  finalize: "完成证据回答",
  abstain: "说明证据不足",
};

function mapAgentActivity(data: unknown, status: AgentActivity["status"]): AgentActivity | null {
  if (!data || typeof data !== "object") return null;
  const item = data as Record<string, unknown>;
  const node = String(item.node ?? "");
  if (!node) return null;
  const step = Number(item.step ?? 0);
  return {
    key: `${step}:${node}`,
    node,
    label: nodeLabels[node] ?? "处理研究任务",
    step,
    status,
    durationMs: item.duration_ms === undefined ? undefined : Number(item.duration_ms),
  };
}

function upsertActivity(items: AgentActivity[], next: AgentActivity): AgentActivity[] {
  const index = items.findIndex((item) => item.key === next.key);
  if (index < 0) return [...items, next];
  return items.map((item, itemIndex) => itemIndex === index ? { ...item, ...next } : item);
}

export interface PaperLeafDataSource {
  listPapers(options?: { collectionId?: string }): Promise<Paper[]>;
  getPaper(paperId: string): Promise<Paper>;
  searchArxiv(query: string): Promise<ArxivResult[]>;
  importArxiv(arxivId: string): Promise<void>;
  ask(question: string, paperIds?: string[], handlers?: AgentAskStreamHandlers, scope?: { collectionId?: string }): Promise<AgentAnswer>;
  upload(file: File, onProgress: (value: number) => void): Promise<Paper>;
  updatePaper(paperId: string, input: PaperUpdateInput): Promise<Paper>;
  deletePaper(paperId: string): Promise<void>;
  retryPaper(paperId: string): Promise<Paper>;
  summarizePaper(paperId: string): Promise<PaperSummary>;
  buildStructureGraph(paperId: string): Promise<PaperStructureGraph>;
  createPaperTranslation(paperId: string, targetLanguage: string, priorityPage: number): Promise<PaperTranslation>;
  getPaperTranslation(paperId: string, translationId: string): Promise<PaperTranslation>;
  getPaperTranslationPage(paperId: string, translationId: string, page: number): Promise<PaperTranslationPage>;
  cancelPaperTranslation(paperId: string, translationId: string): Promise<PaperTranslation>;
  listCollections(): Promise<PaperCollection[]>;
  createCollection(input: CollectionInput): Promise<PaperCollection>;
  updateCollection(collectionId: string, input: CollectionInput): Promise<PaperCollection>;
  deleteCollection(collectionId: string): Promise<void>;
  bulkPapers(input: BulkPaperActionInput): Promise<void>;
  recordPaperOpened(paperId: string): Promise<Paper>;
  fileUrl(paperId: string): string;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let demoPapers = papers.map((paper) => ({ ...paper }));
const demoTranslations = new Map<string, PaperTranslation>();
let demoCollections: PaperCollection[] = [
  { id: "core-methods", name: "核心方法", description: "反复查阅的基础方法论文", parentId: null, paperIds: ["attention"], recursivePaperCount: 3, children: [
    { id: "transformers", name: "Transformer", description: "架构与预训练", parentId: "core-methods", paperIds: ["bert"], recursivePaperCount: 1, children: [] },
    { id: "retrieval", name: "检索增强", description: "RAG 方法", parentId: "core-methods", paperIds: ["rag"], recursivePaperCount: 1, children: [] },
  ] },
  { id: "experiments", name: "实验参考", description: "训练与工程实现相关资料", parentId: null, paperIds: [], recursivePaperCount: 0, children: [] },
  { id: "follow-up", name: "近期跟进", description: "仍需继续核对的新方向", parentId: null, paperIds: ["graphrag"], recursivePaperCount: 1, children: [] },
];

function demoId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}`;
}

function flattenDemoCollections(collections: PaperCollection[]): PaperCollection[] {
  return flattenCollections(collections).map(({ collection }) => ({ ...collection, children: [] }));
}

export const demoDataSource: PaperLeafDataSource = {
  async listPapers(options) {
    await wait(120);
    if (!options?.collectionId) return demoPapers.map((paper) => ({ ...paper }));
    const collection = findCollection(demoCollections, options.collectionId);
    if (!collection) return [];
    const ids = new Set(recursivePaperIds(collection));
    return demoPapers.filter((paper) => ids.has(paper.id)).map((paper) => ({ ...paper }));
  },
  async getPaper(paperId) { await wait(80); return demoPapers.find((paper) => paper.id === paperId) ?? demoPapers[0]; },
  async searchArxiv(query) { await wait(220); return arxivResults.filter((item) => `${item.title} ${item.summary}`.toLowerCase().includes(query.toLowerCase()) || !query); },
  async importArxiv() { await wait(320); },
  async ask(question, _paperIds, handlers) {
    const steps = ["validate_request", "retrieve_library", "grade_evidence", "generate_answer", "validate_citations"];
    const activities: AgentActivity[] = [];
    for (const [index, node] of steps.entries()) {
      const running = mapAgentActivity({ node, step: index + 1 }, "running")!;
      handlers?.onActivity?.(running);
      await wait(90);
      const completed = { ...running, status: "completed" as const, durationMs: 72 + index * 11 };
      activities.push(completed);
      handlers?.onActivity?.(completed);
    }
    const result = { ...groundedAnswer, question, activities };
    if (result.evidenceQuality) handlers?.onEvidenceQualityUpdate?.(result.evidenceQuality);
    handlers?.onAnswerUpdate?.(result.answer);
    const streamedCitations: AgentAnswer["citations"] = [];
    for (const citation of result.citations) {
      streamedCitations.push(citation);
      handlers?.onCitationsUpdate?.([...streamedCitations]);
    }
    return result;
  },
  async upload(file, onProgress) {
    for (const value of [18, 42, 71, 100]) { await wait(130); onProgress(value); }
    const uploaded: Paper = { id: `local-${Date.now()}`, title: file.name.replace(/\.pdf$/i, ""), authors: "待识别", year: new Date().getFullYear(), venue: "本地上传", publication: "", pages: 0, status: "indexing", progress: 0, abstract: "PDF 已上传，正在解析元数据与页面文本。", createdAt: new Date().toISOString() };
    demoPapers = [uploaded, ...demoPapers];
    return uploaded;
  },
  async updatePaper(paperId, input) {
    await wait(220);
    const index = demoPapers.findIndex((paper) => paper.id === paperId);
    const current = index >= 0 ? demoPapers[index] : demoPapers[0];
    const updated = { ...current, ...input, authors: input.authors.join("、"), abstract: input.abstract ?? "", doi: input.doi, publication: input.publication ?? current.publication };
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
  async createPaperTranslation(paperId, targetLanguage, priorityPage) {
    await wait(180);
    void priorityPage;
    const current = [...demoTranslations.values()].find((item) => item.paperId === paperId && item.targetLanguage === targetLanguage && item.status !== "cancelled");
    if (current) return { ...current };
    const paper = demoPapers.find((item) => item.id === paperId) ?? demoPapers[0];
    const translation: PaperTranslation = { id: demoId("translation"), paperId, targetLanguage, status: "running", progress: 36, completedPages: Math.min(5, paper.pages), failedPages: 0, totalPages: paper.pages };
    demoTranslations.set(translation.id, translation);
    return { ...translation };
  },
  async getPaperTranslation(paperId, translationId) {
    await wait(80);
    const translation = demoTranslations.get(translationId);
    if (!translation || translation.paperId !== paperId) throw new Error("翻译任务不存在或已失效");
    return { ...translation };
  },
  async getPaperTranslationPage(paperId, translationId, page) {
    await wait(90);
    const translation = demoTranslations.get(translationId);
    if (!translation || translation.paperId !== paperId) throw new Error("翻译任务不存在或已失效");
    if (page === 7) return { page, status: "no_text", text: "" };
    return {
      page,
      status: "completed",
      text: page === 2
        ? "本文提出 Transformer：一种完全基于注意力机制的网络架构，不再依赖循环与卷积。\n\n自注意力能够以固定数量的顺序操作连接所有位置，从而提高训练阶段的并行能力。\n\nAttention(Q, K, V) = softmax(QKᵀ / √dₖ)V"
        : `第 ${page} 页译文已缓存。公式、引用编号与专有名词会尽量保持原样。`,
    };
  },
  async cancelPaperTranslation(paperId, translationId) {
    await wait(100);
    const current = demoTranslations.get(translationId);
    if (!current || current.paperId !== paperId) throw new Error("翻译任务不存在或已失效");
    const cancelled: PaperTranslation = { ...current, status: "cancelled" };
    demoTranslations.set(translationId, cancelled);
    return { ...cancelled };
  },
  async listCollections() { await wait(100); return collectionForest(demoCollections); },
  async createCollection(input) {
    await wait(180);
    const item: PaperCollection = { id: demoId("collection"), name: input.name, description: input.description, parentId: input.parentId ?? null, paperIds: [], recursivePaperCount: 0, children: [] };
    demoCollections = [...flattenDemoCollections(demoCollections), item].map((collection) => ({ ...collection, recursivePaperCount: 0 }));
    return item;
  },
  async updateCollection(collectionId, input) {
    await wait(160);
    const flat = flattenDemoCollections(demoCollections);
    const current = flat.find((item) => item.id === collectionId);
    if (!current) throw new Error("集合不存在");
    const updated = { ...current, ...input, parentId: input.parentId === undefined ? current.parentId : input.parentId, children: [] };
    demoCollections = flat.map((item) => ({ ...(item.id === collectionId ? updated : item), children: [], recursivePaperCount: 0 }));
    return updated;
  },
  async deleteCollection(collectionId) {
    await wait(160);
    const flat = flattenDemoCollections(demoCollections);
    const deleted = flat.find((item) => item.id === collectionId);
    demoCollections = flat.filter((item) => item.id !== collectionId).map((item) => item.parentId === collectionId ? { ...item, parentId: deleted?.parentId ?? null, children: [], recursivePaperCount: 0 } : { ...item, children: [], recursivePaperCount: 0 });
  },
  async bulkPapers(input) {
    await wait(220);
    const ids = new Set(input.paperIds);
    if (input.action === "archive" || input.action === "unarchive") {
      demoPapers = demoPapers.map((paper) => ids.has(paper.id) ? { ...paper, archivedAt: input.action === "archive" ? new Date().toISOString() : undefined } : paper);
      return;
    }
    if (!input.targetId) throw new Error("整理操作缺少目标");
    const flat = flattenDemoCollections(demoCollections);
    const target = flat.find((item) => item.id === input.targetId);
    if (!target) throw new Error("整理目标不存在");
    const add = input.action.startsWith("add_");
    target.paperIds = add ? Array.from(new Set([...target.paperIds, ...input.paperIds])) : target.paperIds.filter((paperId) => !ids.has(paperId));
    demoCollections = flat.map((collection) => ({ ...collection, recursivePaperCount: 0 }));
  },
  async recordPaperOpened(paperId) { await wait(80); const current = demoPapers.find((paper) => paper.id === paperId) ?? demoPapers[0]; const updated = { ...current, lastOpenedAt: new Date().toISOString() }; demoPapers = demoPapers.map((paper) => paper.id === paperId ? updated : paper); return updated; },
  fileUrl(paperId) { return `/demo?paper=${paperId}`; },
};

export const realDataSource: PaperLeafDataSource = {
  async listPapers(options) {
    const query = options?.collectionId ? `?collection_id=${encodeURIComponent(options.collectionId)}` : "";
    const r = await fetch(`${API_BASE_URL}/papers${query}`, { credentials: "include" });
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
  async ask(question, paperIds = [], handlers, scope) {
    let r: Response;
    try {
      r = await fetch(`${API_BASE_URL}/chat/sessions/default/messages`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({
        content: question,
        scope: scope?.collectionId ? "collection" : paperIds.length === 1 ? "paper" : paperIds.length > 1 ? "selection" : "library",
        selected_collection_id: scope?.collectionId,
        selected_paper_ids: scope?.collectionId ? [] : paperIds,
        web_enabled: false,
      }) });
    } catch {
      throw new Error("网络连接失败，请检查后重试");
    }
    if (!r.ok) throw new Error(r.status >= 500 ? "问答服务暂时不可用，请稍后重试" : `提问失败（HTTP ${r.status}）`);

    let rawAnswer = "";
    let visibleAnswer = "";
    const citations: AgentAnswer["citations"] = [];
    let evidenceQuality: AgentEvidenceQuality | undefined;
    let activities: AgentActivity[] = [];
    let finished = false;

    try {
      for await (const event of readAgentStream(r)) {
        if (event.type === "node_started" || event.type === "node_finished") {
          const activity = mapAgentActivity(event.data, event.type === "node_started" ? "running" : ((event.data as Record<string, unknown>)?.status === "failed" ? "failed" : "completed"));
          if (activity) { activities = upsertActivity(activities, activity); handlers?.onActivity?.(activity); }
        }
        if (event.type === "message_delta" && typeof event.data === "object" && event.data && "delta" in event.data) {
          rawAnswer += String((event.data as { delta: unknown }).delta);
          visibleAnswer = visibleAgentAnswer(rawAnswer);
          handlers?.onAnswerUpdate?.(visibleAnswer);
        }
        if (event.type === "tool_finished" && typeof event.data === "object" && event.data && "evidence_quality" in event.data) {
          const quality = (event.data as { evidence_quality?: unknown }).evidence_quality;
          if (typeof quality === "object" && quality) {
            evidenceQuality = mapEvidenceQuality(quality as Record<string, unknown>);
            handlers?.onEvidenceQualityUpdate?.(evidenceQuality);
          }
        }
        if (event.type === "citation" && typeof event.data === "object" && event.data) {
          const citation = mapAgentCitation(event.data as Record<string, unknown>, citations.length);
          const currentIndex = citations.findIndex((item) => item.id === citation.id);
          if (currentIndex >= 0) citations[currentIndex] = citation;
          else citations.push(citation);
          handlers?.onCitationsUpdate?.([...citations]);
        }
        if (event.type === "interrupt") throw new Error("问答运行正在等待用户确认");
        if (event.type === "error") {
          const data = event.data as Record<string, unknown> | null;
          throw new Error(data && typeof data.message === "string" ? data.message : "Agent 运行失败");
        }
        if (event.type === "run_finished") {
          finished = true;
          const status = event.data && typeof event.data === "object" ? String((event.data as Record<string, unknown>).status ?? "completed") : "completed";
          if (status === "cancelled") throw new Error("问答运行已取消");
          if (status === "failed") throw new Error("Agent 运行失败");
        }
      }
    } catch (error) {
      if (error instanceof TypeError) throw new Error("网络连接中断，请重试");
      throw error;
    }
    if (!finished) throw new Error("回答连接提前结束，请重试");
    return { question, answer: visibleAnswer, citations, evidenceQuality, activities };
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
    return collectionForest((await r.json() as Array<Record<string, unknown>>).map((item) => mapCollection(item)));
  },
  async createPaperTranslation(paperId, targetLanguage, priorityPage) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/translations`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ target_language: targetLanguage, priority_page: priorityPage }) });
    if (!r.ok) throw await apiError(r, "全文翻译任务创建失败");
    return mapPaperTranslation(await r.json() as Record<string, unknown>, paperId);
  },
  async getPaperTranslation(paperId, translationId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/translations/${encodeURIComponent(translationId)}`, { credentials: "include" });
    if (!r.ok) throw await apiError(r, "翻译进度读取失败");
    return mapPaperTranslation(await r.json() as Record<string, unknown>, paperId);
  },
  async getPaperTranslationPage(paperId, translationId, page) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/translations/${encodeURIComponent(translationId)}/pages/${page}`, { credentials: "include" });
    if (!r.ok) throw await apiError(r, r.status === 404 ? "当前页译文仍在处理中" : "当前页译文读取失败");
    return mapPaperTranslationPage(await r.json() as Record<string, unknown>, page);
  },
  async cancelPaperTranslation(paperId, translationId) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/translations/${encodeURIComponent(translationId)}/cancel`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw await apiError(r, "翻译任务取消失败");
    return mapPaperTranslation(await r.json() as Record<string, unknown>, paperId);
  },
  async createCollection(input) {
    const r = await fetch(`${API_BASE_URL}/collections`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ name: input.name, description: input.description, parent_id: input.parentId ?? null }) });
    if (!r.ok) throw new Error(r.status === 409 ? "集合名称已存在" : "集合创建失败");
    return mapCollection(await r.json() as Record<string, unknown>);
  },
  async updateCollection(collectionId, input) {
    const r = await fetch(`${API_BASE_URL}/collections/${encodeURIComponent(collectionId)}`, { method: "PATCH", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ name: input.name, description: input.description, parent_id: input.parentId ?? null }) });
    if (!r.ok) throw new Error(r.status === 409 ? "集合名称已存在" : "集合保存失败");
    return mapCollection(await r.json() as Record<string, unknown>);
  },
  async deleteCollection(collectionId) {
    const r = await fetch(`${API_BASE_URL}/collections/${encodeURIComponent(collectionId)}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw new Error("集合删除失败");
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

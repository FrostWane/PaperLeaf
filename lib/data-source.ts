import { arxivResults, groundedAnswer, papers, paperStructureGraph, paperSummary } from "./fixtures";
import { readAgentStream } from "./sse";
import { collectionForest, findCollection, flattenCollections, recursivePaperIds } from "./collections";
import { artifactFailureMessage, normalizeArtifactStatus, structureNodeTypes, summarySectionKeys, summarySectionTitles, uniqueArtifactCitations } from "./artifacts";
import type { AdminJob, AgentActivity, AgentAnswer, AgentAskStreamHandlers, AgentEvent, AgentEventSubscriptionHandlers, AgentEvidenceQuality, AgentRunSnapshot, AgentRunStatus, ArxivResult, ArtifactCitation, BulkPaperActionInput, ChatMessage, ChatMessageSubmission, ChatSession, ChatSessionInput, ChatSessionType, Citation, CollectionInput, ModelPurposeHealth, ModelRuntimeHealth, Paper, PaperCollection, PaperStructureGraph, PaperSummary, PaperTranslation, PaperTranslationPage, PaperUpdateInput, SessionUser, StructureEdge, StructureNode, StructureNodeType, SummaryFact, SummarySection, SummarySectionKey, UserRecord } from "./types";

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

function mapArtifactCitation(value: unknown): ArtifactCitation | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const chunkId = String(item.chunk_id ?? item.chunkId ?? "");
  const physicalPage = Number(item.physical_page ?? item.physicalPage ?? item.page ?? 0);
  if (!chunkId || !Number.isInteger(physicalPage) || physicalPage < 1) return null;
  return { chunkId, physicalPage, quote: item.quote || item.excerpt ? String(item.quote ?? item.excerpt) : undefined };
}

function artifactCitations(value: unknown): ArtifactCitation[] {
  return uniqueArtifactCitations((Array.isArray(value) ? value : []).map(mapArtifactCitation).filter((item): item is ArtifactCitation => Boolean(item)));
}

function mapSummaryFacts(value: unknown): SummaryFact[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (typeof entry === "string") return [];
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    const text = String(item.text ?? item.fact ?? item.content ?? "").trim();
    const citations = artifactCitations(item.citations);
    return text && citations.length > 0 ? [{ text, citations }] : [];
  });
}

function mapSummarySections(value: unknown): SummarySection[] {
  const raw = Array.isArray(value)
    ? value
    : value && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>).map(([key, facts]) => ({ key, facts }))
      : [];
  return raw.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    const rawKey = String(item.key ?? item.type ?? item.section ?? "");
    const keyAliases: Record<string, SummarySectionKey> = { research_question: "research_problem", experimental_setup: "experiment_setup", limitations_scope: "limitations" };
    const key = (keyAliases[rawKey] ?? rawKey) as SummarySectionKey;
    if (!summarySectionKeys.has(key)) return [];
    const facts = mapSummaryFacts(item.facts ?? item.items);
    return facts.length > 0 ? [{ key, title: String(item.title ?? summarySectionTitles[key]), facts }] : [];
  });
}

function mapPaperSummaryResponse(item: Record<string, unknown>, paperId: string): PaperSummary {
  const stale = item.stale === true || item.status === "stale";
  const mode = item.mode === "extractive" ? "extractive" : "model";
  const sections = mapSummarySections(item.sections);
  const canonicalKeys = new Set(sections.map((section) => section.key));
  const structuredModelOutputValid = mode !== "model" || summarySectionKeys.size === canonicalKeys.size;
  const fallbackReason = item.fallback_reason ?? item.failure_reason;
  const explicitStatus = normalizeArtifactStatus(item.artifact_status ?? item.status, stale);
  const status = explicitStatus === "ready" && !structuredModelOutputValid ? "failed" : explicitStatus;
  const citations = uniqueArtifactCitations([
    ...artifactCitations(item.citations),
    ...sections.flatMap((section) => section.facts.flatMap((fact) => fact.citations)),
  ]);
  return {
    paperId: String(item.paper_id ?? paperId),
    content: item.content ? String(item.content) : undefined,
    sections: status === "failed" && mode === "model" ? [] : sections,
    citations,
    mode,
    status,
    stale,
    fallbackReason: String(fallbackReason ?? (!structuredModelOutputValid ? "invalid_output" : "")) || undefined,
  };
}

function structureNodeType(value: unknown): StructureNodeType | null {
  const aliases: Record<string, StructureNodeType> = {
    problem: "research_problem", limitation: "limitation", limitations: "limitation", results: "result", methods: "method", experiments: "experiment",
    "研究问题": "research_problem", "背景": "background", "方法": "method", "数据": "data", "实验": "experiment", "结果": "result", "局限": "limitation",
  };
  const raw = String(value ?? "");
  const normalized = (aliases[raw] ?? raw) as StructureNodeType;
  return structureNodeTypes.has(normalized) ? normalized : null;
}

function graphHasCycle(nodes: StructureNode[], edges: StructureEdge[]): boolean {
  const adjacency = new Map(nodes.map((node) => [node.id, [] as string[]]));
  edges.forEach((edge) => adjacency.get(edge.source)?.push(edge.target));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string): boolean => {
    if (visiting.has(id)) return true;
    if (visited.has(id)) return false;
    visiting.add(id);
    if ((adjacency.get(id) ?? []).some(visit)) return true;
    visiting.delete(id);
    visited.add(id);
    return false;
  };
  return nodes.some((node) => visit(node.id));
}

function mapPaperStructureResponse(item: Record<string, unknown>, paperId: string): PaperStructureGraph {
  const stale = item.stale === true || item.status === "stale";
  const explicitStatus = normalizeArtifactStatus(item.artifact_status ?? item.status, stale);
  const nodes: StructureNode[] = (Array.isArray(item.nodes) ? item.nodes : []).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const node = entry as Record<string, unknown>;
    const type = structureNodeType(node.type ?? node.node_type);
    const citations = artifactCitations(node.citations ?? (node.chunk_id ? [{ chunk_id: node.chunk_id, physical_page: node.physical_page }] : []));
    const id = String(node.id ?? "").trim();
    const label = String(node.label ?? node.title ?? "").trim();
    const summary = String(node.summary ?? node.description ?? label).trim();
    return id && label && summary && type && citations.length > 0 ? [{ id, type, label, summary, citations }] : [];
  });
  const edges: StructureEdge[] = (Array.isArray(item.edges) ? item.edges : []).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const edge = entry as Record<string, unknown>;
    const source = String(edge.source ?? "");
    const target = String(edge.target ?? "");
    return source && target ? [{ source, target }] : [];
  });
  const ids = new Set(nodes.map((node) => node.id));
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  const validEdges = edges.every((edge) => ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target);
  edges.forEach((edge) => { degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1); degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1); });
  const validGraph = nodes.length >= 5 && nodes.length <= 12 && ids.size === nodes.length && validEdges && nodes.every((node) => (degree.get(node.id) ?? 0) > 0) && !graphHasCycle(nodes, edges) && Boolean(String(item.mermaid ?? "").trim());
  const status = explicitStatus === "ready" && !validGraph ? "failed" : explicitStatus;
  const fallbackReason = item.fallback_reason ?? item.failure_reason;
  return {
    paperId: String(item.paper_id ?? paperId),
    nodes: status === "failed" ? [] : nodes,
    edges: status === "failed" ? [] : edges,
    mermaid: status === "failed" ? "" : String(item.mermaid ?? ""),
    status,
    stale,
    fallbackReason: String(fallbackReason ?? (!validGraph ? "invalid_output" : "")) || undefined,
    evidenceExcerpt: item.evidence_excerpt ? String(item.evidence_excerpt) : undefined,
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

async function artifactApiError(response: Response, fallback: string): Promise<Error> {
  try {
    const payload = await response.json() as Record<string, unknown>;
    const detail = payload.detail;
    const detailObject = detail && typeof detail === "object" ? detail as Record<string, unknown> : undefined;
    const reason = payload.fallback_reason ?? payload.failure_reason ?? detailObject?.reason_code ?? detailObject?.code ?? (typeof detail === "string" ? detail : undefined);
    if (reason) return new Error(artifactFailureMessage(String(reason)));
    const message = detailObject?.message;
    if (typeof message === "string" && message.trim()) return new Error(message.trim());
  } catch {
    // 无结构化原因时使用本地兜底文案。
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

function mapChatSession(item: Record<string, unknown>): ChatSession {
  const type = String(item.type ?? "library");
  const rawStatus = item.current_run_status ? String(item.current_run_status) : undefined;
  const statuses: AgentRunStatus[] = ["pending", "running", "interrupted", "completed", "failed", "cancelled"];
  return {
    id: String(item.id ?? item.session_id ?? ""),
    title: String(item.title ?? "新对话"),
    type: (type === "paper" || type === "collection" ? type : "library") as ChatSessionType,
    paperId: item.paper_id ? String(item.paper_id) : undefined,
    collectionId: item.collection_id ? String(item.collection_id) : undefined,
    currentRunId: item.current_run_id ? String(item.current_run_id) : undefined,
    currentRunStatus: rawStatus && statuses.includes(rawStatus as AgentRunStatus) ? rawStatus as AgentRunStatus : undefined,
    createdAt: String(item.created_at ?? new Date(0).toISOString()),
    updatedAt: String(item.updated_at ?? item.created_at ?? new Date(0).toISOString()),
  };
}

function mapChatMessage(item: Record<string, unknown>): ChatMessage {
  const rawCitations = Array.isArray(item.citations) ? item.citations : [];
  return {
    id: String(item.id ?? ""),
    sessionId: String(item.session_id ?? ""),
    role: item.role === "assistant" ? "assistant" : "user",
    sequence: Number(item.sequence ?? 1),
    status: item.status === "pending" || item.status === "streaming" || item.status === "failed" || item.status === "cancelled" ? item.status : "completed",
    content: String(item.content ?? ""),
    citations: rawCitations.map((citation, index) => mapAgentCitation(citation as Record<string, unknown>, index)),
    runId: item.run_id ? String(item.run_id) : undefined,
    createdAt: String(item.created_at ?? new Date(0).toISOString()),
    updatedAt: String(item.updated_at ?? item.created_at ?? new Date(0).toISOString()),
  };
}

function publicAgentError(value: unknown): string | undefined {
  if (!value) return undefined;
  const code = String(value);
  const known: Record<string, string> = {
    model_timeout: "模型响应超时，请稍后重试",
    invalid_citation: "回答引用未通过核验",
    citation_validation_failed: "回答引用未通过核验",
    model_unavailable: "回答模型暂时不可用",
    cancelled: "问答已取消",
    internal_error: "问答运行遇到内部错误",
  };
  return known[code] ?? (/^[a-z0-9_.-]+$/i.test(code) ? "问答运行失败，请稍后重试" : code);
}

function mapAgentRun(item: Record<string, unknown>): AgentRunSnapshot {
  const rawStatus = String(item.status ?? "pending");
  const status: AgentRunStatus = rawStatus === "running" || rawStatus === "interrupted" || rawStatus === "completed" || rawStatus === "failed" || rawStatus === "cancelled" ? rawStatus : "pending";
  const rawCitations = Array.isArray(item.citations) ? item.citations : [];
  const rawQuality = item.evidence_quality;
  const rawAction = item.pending_action;
  const pendingAction = rawAction && typeof rawAction === "object" ? rawAction as Record<string, unknown> : undefined;
  return {
    runId: String(item.run_id ?? item.id ?? ""),
    sessionId: String(item.session_id ?? ""),
    status,
    cancelRequested: item.cancel_requested === true,
    pendingAction: pendingAction ? {
      actionId: String(pendingAction.action_id ?? ""),
      type: String(pendingAction.type ?? ""),
      riskMessage: String(pendingAction.risk_message ?? "此操作需要你确认"),
      allowedDecisions: Array.isArray(pendingAction.allowed_decisions) ? pendingAction.allowed_decisions.map(String) : [],
      candidates: Array.isArray(pendingAction.candidates) ? pendingAction.candidates.filter((candidate) => candidate && typeof candidate === "object").map((candidate) => {
        const raw = candidate as Record<string, unknown>;
        return { arxivId: raw.arxiv_id ? String(raw.arxiv_id) : undefined, title: raw.title ? String(raw.title) : undefined, authors: Array.isArray(raw.authors) ? raw.authors.map(String) : raw.authors ? String(raw.authors) : undefined, abstract: raw.abstract ? String(raw.abstract) : undefined, published: raw.published ? String(raw.published) : undefined, pdfUrl: raw.pdf_url ? String(raw.pdf_url) : undefined, journalRef: raw.journal_ref ? String(raw.journal_ref) : undefined };
      }) : [],
    } : undefined,
    answer: visibleAgentAnswer(String(item.answer ?? "")),
    citations: rawCitations.map((citation, index) => mapAgentCitation(citation as Record<string, unknown>, index)),
    evidenceQuality: rawQuality && typeof rawQuality === "object" ? mapEvidenceQuality(rawQuality as Record<string, unknown>) : undefined,
    error: publicAgentError(item.error),
    createdAt: String(item.created_at ?? new Date(0).toISOString()),
    updatedAt: String(item.updated_at ?? item.created_at ?? new Date(0).toISOString()),
  };
}

function isTerminalRun(status: AgentRunStatus): boolean {
  return status === "interrupted" || status === "completed" || status === "failed" || status === "cancelled";
}

interface AgentEventAccumulator {
  answer: string;
  citations: Citation[];
}

function dispatchAgentEvent(event: AgentEvent, handlers: AgentEventSubscriptionHandlers, accumulator: AgentEventAccumulator): void {
  const addCitations = (rawCitations: unknown[]): boolean => {
    let changed = false;
    for (const rawCitation of rawCitations) {
      if (!rawCitation || typeof rawCitation !== "object") continue;
      const citation = mapAgentCitation(rawCitation as Record<string, unknown>, accumulator.citations.length);
      if (accumulator.citations.some((item) => item.chunkId === citation.chunkId && item.paperId === citation.paperId && item.page === citation.page)) continue;
      accumulator.citations.push(citation);
      changed = true;
    }
    if (changed) handlers.onCitationsUpdate?.(accumulator.citations.map((item) => ({ ...item })));
    return changed;
  };
  handlers.onEvent?.(event);
  if (event.type === "node_started" || event.type === "node_finished") {
    const activity = mapAgentActivity(event.data, event.type === "node_started" ? "running" : ((event.data as Record<string, unknown>)?.status === "failed" ? "failed" : "completed"));
    if (activity) handlers.onActivity?.(activity);
  }
  if (event.type === "message_delta" && event.data && typeof event.data === "object" && "delta" in event.data) {
    const delta = event.data as { delta: unknown; citations?: unknown };
    if (Array.isArray(delta.citations)) addCitations(delta.citations);
    accumulator.answer += String(delta.delta);
    handlers.onAnswerUpdate?.(visibleAgentAnswer(accumulator.answer));
  }
  if (event.type === "citation" && event.data && typeof event.data === "object") {
    addCitations([event.data]);
  }
  if (event.type === "tool_finished" && event.data && typeof event.data === "object" && "evidence_quality" in event.data) {
    const quality = (event.data as { evidence_quality?: unknown }).evidence_quality;
    if (quality && typeof quality === "object") handlers.onEvidenceQualityUpdate?.(mapEvidenceQuality(quality as Record<string, unknown>));
  }
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
  summarizePaper(paperId: string, options?: { refresh?: boolean }): Promise<PaperSummary>;
  buildStructureGraph(paperId: string, options?: { refresh?: boolean }): Promise<PaperStructureGraph>;
  createPaperTranslation(paperId: string, targetLanguage: string, priorityPage: number, options?: { refresh?: boolean }): Promise<PaperTranslation>;
  getPaperTranslation(paperId: string, translationId: string): Promise<PaperTranslation>;
  getPaperTranslationPage(paperId: string, translationId: string, page: number): Promise<PaperTranslationPage>;
  cancelPaperTranslation(paperId: string, translationId: string): Promise<PaperTranslation>;
  listCollections(): Promise<PaperCollection[]>;
  createCollection(input: CollectionInput): Promise<PaperCollection>;
  updateCollection(collectionId: string, input: CollectionInput): Promise<PaperCollection>;
  deleteCollection(collectionId: string): Promise<void>;
  bulkPapers(input: BulkPaperActionInput): Promise<void>;
  recordPaperOpened(paperId: string): Promise<Paper>;
  listChatSessions(): Promise<ChatSession[]>;
  createChatSession(input: ChatSessionInput): Promise<ChatSession>;
  updateChatSession(sessionId: string, title: string): Promise<ChatSession>;
  deleteChatSession(sessionId: string): Promise<void>;
  listChatMessages(sessionId: string): Promise<ChatMessage[]>;
  submitChatMessage(sessionId: string, content: string, idempotencyKey: string, options?: { webEnabled?: boolean }): Promise<ChatMessageSubmission>;
  getAgentRun(runId: string): Promise<AgentRunSnapshot>;
  subscribeAgentRun(runId: string, handlers: AgentEventSubscriptionHandlers, options?: { signal?: AbortSignal; lastEventId?: number }): Promise<void>;
  cancelAgentRun(runId: string): Promise<AgentRunSnapshot>;
  resumeAgentRun(runId: string, actionId: string, decision: string): Promise<AgentRunSnapshot>;
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

let demoSequence = 20;
const demoNow = "2026-08-06T09:30:00.000Z";
const initialDemoChatSessions: ChatSession[] = [
  { id: "demo-session-paper", title: "Transformer 的核心贡献", type: "paper", paperId: "attention", createdAt: demoNow, updatedAt: "2026-08-06T10:12:00.000Z" },
  { id: "demo-session-library", title: "核心方法对比", type: "collection", collectionId: "core-methods", createdAt: demoNow, updatedAt: "2026-08-06T10:06:00.000Z" },
];
const initialDemoChatMessages: Array<[string, ChatMessage[]]> = [
  ["demo-session-paper", [
    { id: "demo-message-p1", sessionId: "demo-session-paper", role: "user", sequence: 1, status: "completed", content: "这篇论文解决了什么问题？", citations: [], createdAt: demoNow, updatedAt: demoNow },
    { id: "demo-message-p2", sessionId: "demo-session-paper", role: "assistant", sequence: 2, status: "completed", content: "## 研究问题\n\n论文希望在不使用循环或卷积的前提下完成序列建模，并提升训练并行度。[^1]\n\n**核心判断：** 自注意力把任意位置之间的路径长度缩短为常数级，同时保留可并行计算的矩阵操作。\n\n| 维度 | Transformer | RNN |\n| --- | --- | --- |\n| 顺序计算 | 少 | 多 |\n| 长距离路径 | 常数级 | 随距离增长 |\n\n```text\nAttention(Q, K, V) = softmax(QKᵀ / √dₖ)V\n```", citations: groundedAnswer.citations.map((citation) => ({ ...citation })), createdAt: "2026-08-06T10:12:00.000Z", updatedAt: "2026-08-06T10:12:00.000Z" },
  ]],
  ["demo-session-library", [
    { id: "demo-message-l1", sessionId: "demo-session-library", role: "user", sequence: 1, status: "completed", content: "这些论文怎样处理外部证据？", citations: [], createdAt: demoNow, updatedAt: demoNow },
    { id: "demo-message-l2", sessionId: "demo-session-library", role: "assistant", sequence: 2, status: "completed", content: "不同方法都把外部证据作为可回读的上下文，但检索粒度和证据组织方式不同。当前回答保留了物理页码，便于回到原文核对。", citations: groundedAnswer.citations.slice(0, 2), createdAt: "2026-08-06T10:06:00.000Z", updatedAt: "2026-08-06T10:06:00.000Z" },
  ]],
];
let demoChatSessions: ChatSession[] = initialDemoChatSessions.map(demoSessionCopy);
const demoChatMessages = new Map<string, ChatMessage[]>(initialDemoChatMessages.map(([sessionId, messages]) => [sessionId, messages.map((message) => ({ ...message, citations: message.citations.map((citation) => ({ ...citation })) }))]));
const demoRuns = new Map<string, AgentRunSnapshot>();
const demoRunEvents = new Map<string, AgentEvent[]>();
const demoIdempotency = new Map<string, { content: string; submission: ChatMessageSubmission }>();
const demoExecutingRuns = new Set<string>();
const DEMO_CHAT_STORAGE_KEY = "paperleaf:demo-chat:v1";
let demoChatHydrated = false;

type PersistedDemoChatState = {
  sequence: number;
  sessions: ChatSession[];
  messages: Array<[string, ChatMessage[]]>;
  runs: Array<[string, AgentRunSnapshot]>;
  events: Array<[string, AgentEvent[]]>;
  idempotency: Array<[string, { content: string; submission: ChatMessageSubmission }]>;
};

function persistDemoChatState(): void {
  if (typeof window === "undefined") return;
  const state: PersistedDemoChatState = {
    sequence: demoSequence,
    sessions: demoChatSessions,
    messages: [...demoChatMessages.entries()],
    runs: [...demoRuns.entries()],
    events: [...demoRunEvents.entries()],
    idempotency: [...demoIdempotency.entries()],
  };
  window.localStorage.setItem(DEMO_CHAT_STORAGE_KEY, JSON.stringify(state));
}

function ensureDemoChatHydrated(): void {
  if (demoChatHydrated || typeof window === "undefined") return;
  demoChatHydrated = true;
  try {
    const raw = window.localStorage.getItem(DEMO_CHAT_STORAGE_KEY);
    if (raw) {
      const stored = JSON.parse(raw) as PersistedDemoChatState;
      demoSequence = Number.isFinite(stored.sequence) ? stored.sequence : demoSequence;
      demoChatSessions = Array.isArray(stored.sessions) ? stored.sessions.map(demoSessionCopy) : demoChatSessions;
      demoChatMessages.clear();
      for (const [sessionId, messages] of stored.messages ?? []) demoChatMessages.set(sessionId, messages);
      demoRuns.clear();
      for (const [runId, run] of stored.runs ?? []) demoRuns.set(runId, run);
      demoRunEvents.clear();
      for (const [runId, events] of stored.events ?? []) demoRunEvents.set(runId, events);
      demoIdempotency.clear();
      for (const [key, value] of stored.idempotency ?? []) demoIdempotency.set(key, value);
    }
  } catch {
    window.localStorage.removeItem(DEMO_CHAT_STORAGE_KEY);
  }
  for (const run of demoRuns.values()) {
    if (run.status !== "pending" && run.status !== "running") continue;
    const question = [...(demoChatMessages.get(run.sessionId) ?? [])].reverse().find((message) => message.role === "user" && message.runId === run.runId)?.content;
    if (!question) continue;
    // 整页导航会销毁旧计时器；Demo 从已持久化的用户消息重新执行确定性流程。
    run.answer = "";
    run.citations = [];
    demoRunEvents.set(run.runId, []);
    void executeDemoRun(run.runId, question);
  }
}

function demoId(prefix: string): string {
  demoSequence += 1;
  return `${prefix}-${demoSequence}`;
}

function demoSessionCopy(session: ChatSession): ChatSession {
  return { ...session };
}

/** 仅供测试与 Storybook 隔离模块级 Demo 状态；产品代码不应调用。 */
export function resetDemoChatStateForTests(): void {
  demoSequence = 20;
  demoChatSessions = initialDemoChatSessions.map(demoSessionCopy);
  demoChatMessages.clear();
  for (const [sessionId, messages] of initialDemoChatMessages) {
    demoChatMessages.set(sessionId, messages.map((message) => ({ ...message, citations: message.citations.map((citation) => ({ ...citation })) })));
  }
  demoRuns.clear();
  demoRunEvents.clear();
  demoIdempotency.clear();
  demoExecutingRuns.clear();
  demoChatHydrated = true;
  if (typeof window !== "undefined") window.localStorage.removeItem(DEMO_CHAT_STORAGE_KEY);
}

function emitDemoRunEvent(runId: string, type: AgentEvent["type"], data: unknown): void {
  const events = demoRunEvents.get(runId) ?? [];
  events.push({ id: String(events.length + 1), type, data });
  demoRunEvents.set(runId, events);
  persistDemoChatState();
}

function updateDemoSessionRun(sessionId: string, run: AgentRunSnapshot): void {
  demoChatSessions = demoChatSessions.map((session) => session.id === sessionId ? {
    ...session,
    currentRunId: run.runId,
    currentRunStatus: run.status,
    updatedAt: run.updatedAt,
  } : session);
  persistDemoChatState();
}

async function executeDemoRun(runId: string, question: string): Promise<void> {
  const run = demoRuns.get(runId);
  if (!run || demoExecutingRuns.has(runId)) return;
  demoExecutingRuns.add(runId);
  try {
  run.status = "running";
  run.updatedAt = new Date().toISOString();
  updateDemoSessionRun(run.sessionId, run);
  emitDemoRunEvent(runId, "run_started", { status: "running" });
  const nodes = [
    ["retrieve_library", "正在检索文献证据"],
    ["generate_answer", "正在生成已核验段落"],
    ["validate_citations", "正在核验引用页码"],
  ] as const;
  for (const [index, [node]] of nodes.entries()) {
    if (run.cancelRequested) break;
    emitDemoRunEvent(runId, "node_started", { node, step: index + 1 });
    await wait(150);
    emitDemoRunEvent(runId, "node_finished", { node, step: index + 1, status: "completed", duration_ms: 110 + index * 18 });
  }
  if (run.cancelRequested) {
    run.status = "cancelled";
    run.updatedAt = new Date().toISOString();
    updateDemoSessionRun(run.sessionId, run);
    emitDemoRunEvent(runId, "run_finished", { status: "cancelled" });
    return;
  }
  const paragraphs = [
    `## 回答\n\n针对“${question}”，现有证据显示：Transformer 通过自注意力缩短长距离依赖的计算路径，并允许训练阶段并行处理序列位置。`,
    "\n\n**核验结论：** 这一判断来自论文方法与实验部分；引用按钮可以直接回到对应物理页。",
  ];
  const citations = groundedAnswer.citations.slice(0, 2).map((item) => ({ ...item }));
  for (const paragraph of paragraphs) {
    run.answer += paragraph;
    run.updatedAt = new Date().toISOString();
    emitDemoRunEvent(runId, "message_delta", { delta: paragraph });
    await wait(100);
  }
  run.citations = citations;
  citations.forEach((citation) => emitDemoRunEvent(runId, "citation", {
    paper_id: citation.paperId,
    paper_title: citation.paperTitle,
    physical_page: citation.page,
    chunk_id: citation.chunkId,
    excerpt: citation.quote,
  }));
  run.status = "completed";
  run.evidenceQuality = groundedAnswer.evidenceQuality;
  run.updatedAt = new Date().toISOString();
  const messages = demoChatMessages.get(run.sessionId) ?? [];
  messages.push({ id: demoId("assistant-message"), sessionId: run.sessionId, role: "assistant", sequence: messages.length + 1, status: "completed", content: run.answer, citations, runId, createdAt: run.updatedAt, updatedAt: run.updatedAt });
  demoChatMessages.set(run.sessionId, messages);
  updateDemoSessionRun(run.sessionId, run);
  emitDemoRunEvent(runId, "run_finished", { status: "completed" });
  } finally {
    demoExecutingRuns.delete(runId);
  }
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
  async createPaperTranslation(paperId, targetLanguage, priorityPage, options) {
    await wait(180);
    void priorityPage;
    const current = [...demoTranslations.values()].find((item) => item.paperId === paperId && item.targetLanguage === targetLanguage && item.status !== "cancelled");
    if (current && !options?.refresh) return { ...current };
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
  async listChatSessions() {
    await wait(50);
    return [...demoChatSessions].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).map(demoSessionCopy);
  },
  async createChatSession(input) {
    await wait(70);
    const timestamp = new Date().toISOString();
    const session: ChatSession = {
      id: demoId("session"),
      title: input.title?.trim() || "新对话",
      type: input.type,
      paperId: input.paperId,
      collectionId: input.collectionId,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    demoChatSessions = [session, ...demoChatSessions];
    demoChatMessages.set(session.id, []);
    persistDemoChatState();
    return demoSessionCopy(session);
  },
  async updateChatSession(sessionId, title) {
    await wait(60);
    const current = demoChatSessions.find((session) => session.id === sessionId);
    if (!current) throw new Error("对话不存在");
    const updated = { ...current, title: title.trim() || current.title, updatedAt: new Date().toISOString() };
    demoChatSessions = demoChatSessions.map((session) => session.id === sessionId ? updated : session);
    persistDemoChatState();
    return demoSessionCopy(updated);
  },
  async deleteChatSession(sessionId) {
    await wait(60);
    const active = demoChatSessions.find((session) => session.id === sessionId)?.currentRunStatus;
    if (active === "pending" || active === "running" || active === "interrupted") throw new Error("请先取消正在运行的问答");
    demoChatSessions = demoChatSessions.filter((session) => session.id !== sessionId);
    demoChatMessages.delete(sessionId);
    persistDemoChatState();
  },
  async listChatMessages(sessionId) {
    await wait(45);
    return (demoChatMessages.get(sessionId) ?? []).map((message) => ({ ...message, citations: message.citations.map((item) => ({ ...item })) }));
  },
  async submitChatMessage(sessionId, content, idempotencyKey) {
    await wait(55);
    const idempotencyScope = `${sessionId}:${idempotencyKey}`;
    const replay = demoIdempotency.get(idempotencyScope);
    if (replay) {
      if (replay.content !== content) throw new Error("同一幂等键不能提交不同问题");
      return { ...replay.submission, replayed: true };
    }
    const session = demoChatSessions.find((item) => item.id === sessionId);
    if (!session) throw new Error("对话不存在");
    if (session.currentRunStatus === "pending" || session.currentRunStatus === "running" || session.currentRunStatus === "interrupted") throw new Error("当前对话仍在运行，请等待完成或主动取消");
    const timestamp = new Date().toISOString();
    const messageId = demoId("user-message");
    const runId = demoId("run");
    const messages = demoChatMessages.get(sessionId) ?? [];
    messages.push({ id: messageId, sessionId, role: "user", sequence: messages.length + 1, status: "completed", content, citations: [], runId, createdAt: timestamp, updatedAt: timestamp });
    demoChatMessages.set(sessionId, messages);
    const run: AgentRunSnapshot = { runId, sessionId, status: "pending", cancelRequested: false, answer: "", citations: [], createdAt: timestamp, updatedAt: timestamp };
    demoRuns.set(runId, run);
    demoRunEvents.set(runId, []);
    updateDemoSessionRun(sessionId, run);
    const submission: ChatMessageSubmission = { sessionId, messageId, runId, status: "pending", replayed: false };
    demoIdempotency.set(idempotencyScope, { content, submission });
    persistDemoChatState();
    void executeDemoRun(runId, content);
    return { ...submission };
  },
  async getAgentRun(runId) {
    await wait(35);
    const run = demoRuns.get(runId);
    if (!run) throw new Error("问答运行不存在");
    return { ...run, citations: run.citations.map((item) => ({ ...item })) };
  },
  async subscribeAgentRun(runId, handlers, options) {
    let cursor = options?.lastEventId ?? 0;
    const accumulator: AgentEventAccumulator = { answer: "", citations: [] };
    const pendingEvents = new Map<number, AgentEvent>();
    handlers.onConnectionState?.("connected");
    while (!options?.signal?.aborted) {
      const events = demoRunEvents.get(runId) ?? [];
      for (const event of events) {
        const sequence = Number(event.id ?? 0);
        if (sequence <= cursor) continue;
        pendingEvents.set(sequence, event);
        while (pendingEvents.has(cursor + 1)) {
          const next = pendingEvents.get(cursor + 1)!;
          pendingEvents.delete(cursor + 1);
          cursor += 1;
          dispatchAgentEvent(next, handlers, accumulator);
        }
      }
      const run = demoRuns.get(runId);
      if (!run) throw new Error("问答运行不存在");
      if (options?.signal?.aborted) return;
      handlers.onRunUpdate?.({ ...run, citations: run.citations.map((item) => ({ ...item })) });
      if (isTerminalRun(run.status)) return;
      await wait(40);
    }
  },
  async cancelAgentRun(runId) {
    await wait(40);
    const run = demoRuns.get(runId);
    if (!run) throw new Error("问答运行不存在");
    if (run.status === "completed" || run.status === "failed") throw new Error("已结束的问答不能取消");
    run.cancelRequested = true;
    run.updatedAt = new Date().toISOString();
    persistDemoChatState();
    return { ...run, citations: run.citations.map((item) => ({ ...item })) };
  },
  async resumeAgentRun(runId, actionId, decision) {
    await wait(40);
    const run = demoRuns.get(runId);
    if (!run || run.status !== "interrupted" || run.pendingAction?.actionId !== actionId) throw new Error("当前运行没有可恢复的确认动作");
    if (!run.pendingAction.allowedDecisions.includes(decision)) throw new Error("确认决定不在允许范围内");
    run.status = "pending";
    run.pendingAction = undefined;
    run.updatedAt = new Date().toISOString();
    updateDemoSessionRun(run.sessionId, run);
    return { ...run, citations: run.citations.map((item) => ({ ...item })) };
  },
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
  async summarizePaper(paperId, options) {
    const query = options?.refresh ? "?refresh=true" : "";
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/summary${query}`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw await artifactApiError(r, r.status === 409 ? "论文还没有完成索引" : "论文总结生成失败，请稍后重试");
    return mapPaperSummaryResponse(await r.json() as Record<string, unknown>, paperId);
  },
  async buildStructureGraph(paperId, options) {
    const query = options?.refresh ? "?refresh=true" : "";
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/structure-graph${query}`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!r.ok) throw await artifactApiError(r, r.status === 409 ? "论文还没有完成索引" : "结构图生成失败，请稍后重试");
    return mapPaperStructureResponse(await r.json() as Record<string, unknown>, paperId);
  },
  async listCollections() {
    const r = await fetch(`${API_BASE_URL}/collections`, { credentials: "include" });
    if (!r.ok) throw new Error("集合读取失败");
    return collectionForest((await r.json() as Array<Record<string, unknown>>).map((item) => mapCollection(item)));
  },
  async createPaperTranslation(paperId, targetLanguage, priorityPage, options) {
    const r = await fetch(`${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/translations`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ target_language: targetLanguage, priority_page: priorityPage, refresh: Boolean(options?.refresh) }) });
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
  async listChatSessions() {
    const response = await fetch(`${API_BASE_URL}/chat/sessions`, { credentials: "include", cache: "no-store" });
    if (!response.ok) throw await apiError(response, "对话历史读取失败");
    return (await response.json() as Array<Record<string, unknown>>).map(mapChatSession);
  },
  async createChatSession(input) {
    const response = await fetch(`${API_BASE_URL}/chat/sessions`, {
      method: "POST",
      credentials: "include",
      headers: mutationHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ type: input.type, title: input.title, paper_id: input.paperId, collection_id: input.collectionId }),
    });
    if (!response.ok) throw await apiError(response, "新建对话失败");
    return mapChatSession(await response.json() as Record<string, unknown>);
  },
  async updateChatSession(sessionId, title) {
    const response = await fetch(`${API_BASE_URL}/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      credentials: "include",
      headers: mutationHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ title }),
    });
    if (!response.ok) throw await apiError(response, "对话重命名失败");
    return mapChatSession(await response.json() as Record<string, unknown>);
  },
  async deleteChatSession(sessionId) {
    const response = await fetch(`${API_BASE_URL}/chat/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE", credentials: "include", headers: mutationHeaders() });
    if (!response.ok) throw await apiError(response, "对话删除失败");
  },
  async listChatMessages(sessionId) {
    const response = await fetch(`${API_BASE_URL}/chat/sessions/${encodeURIComponent(sessionId)}/messages`, { credentials: "include", cache: "no-store" });
    if (!response.ok) throw await apiError(response, "对话消息读取失败");
    return (await response.json() as Array<Record<string, unknown>>).map(mapChatMessage);
  },
  async submitChatMessage(sessionId, content, idempotencyKey, options) {
    const response = await fetch(`${API_BASE_URL}/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      credentials: "include",
      headers: mutationHeaders({ "content-type": "application/json", "Idempotency-Key": idempotencyKey }),
      body: JSON.stringify({ content, web_enabled: options?.webEnabled === true }),
    });
    if (!response.ok) throw await apiError(response, response.status === 409 ? "当前对话仍在运行，请等待完成或主动取消" : "问题提交失败");
    const item = await response.json() as Record<string, unknown>;
    return {
      sessionId: String(item.session_id ?? sessionId),
      messageId: String(item.message_id ?? ""),
      runId: String(item.run_id ?? ""),
      status: "pending",
      replayed: item.replayed === true,
    };
  },
  async getAgentRun(runId) {
    const response = await fetch(`${API_BASE_URL}/agent/runs/${encodeURIComponent(runId)}`, { credentials: "include", cache: "no-store" });
    if (!response.ok) throw await apiError(response, "问答运行状态读取失败");
    return mapAgentRun(await response.json() as Record<string, unknown>);
  },
  async subscribeAgentRun(runId, handlers, options) {
    let lastEventId = options?.lastEventId ?? 0;
    let reconnecting = false;
    const accumulator: AgentEventAccumulator = { answer: "", citations: [] };
    const pendingEvents = new Map<number, AgentEvent>();
    while (!options?.signal?.aborted) {
      try {
        if (reconnecting) handlers.onConnectionState?.("reconnecting");
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (lastEventId > 0) headers["Last-Event-ID"] = String(lastEventId);
        const response = await fetch(`${API_BASE_URL}/agent/runs/${encodeURIComponent(runId)}/events`, { credentials: "include", cache: "no-store", headers, signal: options?.signal });
        if (!response.ok) {
          if (response.status >= 500) throw new TypeError("temporary event stream failure");
          throw await apiError(response, "回答事件读取失败");
        }
        reconnecting = false;
        handlers.onConnectionState?.("connected");
        for await (const event of readAgentStream(response)) {
          if (options?.signal?.aborted) return;
          const sequence = Number(event.id ?? 0);
          if (sequence <= 0) {
            dispatchAgentEvent(event, handlers, accumulator);
            continue;
          }
          if (sequence <= lastEventId) continue;
          pendingEvents.set(sequence, event);
          while (pendingEvents.has(lastEventId + 1)) {
            const next = pendingEvents.get(lastEventId + 1)!;
            pendingEvents.delete(lastEventId + 1);
            lastEventId += 1;
            dispatchAgentEvent(next, handlers, accumulator);
          }
        }
        const run = await realDataSource.getAgentRun(runId);
        if (options?.signal?.aborted) return;
        handlers.onRunUpdate?.(run);
        if (isTerminalRun(run.status)) return;
        reconnecting = true;
      } catch (error) {
        if (options?.signal?.aborted || (error instanceof DOMException && error.name === "AbortError")) return;
        if (!(error instanceof TypeError)) throw error;
        reconnecting = true;
      }
      await wait(350);
    }
  },
  async cancelAgentRun(runId) {
    const response = await fetch(`${API_BASE_URL}/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", credentials: "include", headers: mutationHeaders() });
    if (!response.ok) throw await apiError(response, response.status === 409 ? "问答已经结束，无需取消" : "取消问答失败");
    return mapAgentRun(await response.json() as Record<string, unknown>);
  },
  async resumeAgentRun(runId, actionId, decision) {
    const response = await fetch(`${API_BASE_URL}/agent/runs/${encodeURIComponent(runId)}/resume`, { method: "POST", credentials: "include", headers: mutationHeaders({ "content-type": "application/json" }), body: JSON.stringify({ action_id: actionId, decision }) });
    if (!response.ok) throw await apiError(response, "恢复问答失败");
    return mapAgentRun(await response.json() as Record<string, unknown>);
  },
  fileUrl(paperId) { return `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/file`; },
};

export const getDataSource = (): PaperLeafDataSource => {
  if (process.env.NEXT_PUBLIC_DATA_MODE === "real") return realDataSource;
  ensureDemoChatHydrated();
  return demoDataSource;
};

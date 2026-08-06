export type PaperStatus = "ready" | "indexing" | "partial" | "failed" | "deleting";

export interface Paper {
  id: string;
  title: string;
  authors: string;
  year: number;
  venue: string;
  publication: string;
  pages: number;
  status: PaperStatus;
  progress?: number;
  abstract: string;
  arxivId?: string;
  doi?: string;
  filename?: string;
  sizeBytes?: number;
  createdAt?: string;
  archivedAt?: string;
  lastOpenedAt?: string;
}

export interface PaperCollection {
  id: string;
  name: string;
  description?: string;
  parentId: string | null;
  paperIds: string[];
  recursivePaperCount: number;
  children: PaperCollection[];
}

export interface CollectionInput {
  name: string;
  description?: string;
  parentId?: string | null;
}

export type BulkPaperAction =
  | "archive"
  | "unarchive"
  | "add_collection"
  | "remove_collection";

export interface BulkPaperActionInput {
  paperIds: string[];
  action: BulkPaperAction;
  targetId?: string;
}

export interface PaperUpdateInput {
  title: string;
  authors: string[];
  year?: number;
  abstract?: string;
  doi?: string;
  publication?: string;
}

export interface ArtifactCitation {
  chunkId: string;
  physicalPage: number;
}

export interface PaperSummary {
  paperId: string;
  content: string;
  citations: ArtifactCitation[];
  mode: "model" | "extractive";
}

export interface StructureNode {
  id: string;
  label: string;
  physicalPage: number;
  chunkId: string;
}

export interface StructureEdge {
  source: string;
  target: string;
}

export interface PaperStructureGraph {
  paperId: string;
  nodes: StructureNode[];
  edges: StructureEdge[];
  mermaid: string;
}

export interface Citation {
  id: string;
  paperId: string;
  paperTitle: string;
  page: number;
  chunkId: string;
  quote: string;
  href: string;
}

export interface AgentAnswer {
  question: string;
  answer: string;
  citations: Citation[];
  evidenceQuality?: AgentEvidenceQuality;
  activities?: AgentActivity[];
}

export interface AgentActivity {
  key: string;
  node: string;
  label: string;
  step: number;
  status: "running" | "completed" | "failed";
  durationMs?: number;
}

export interface AgentAskStreamHandlers {
  onActivity?: (activity: AgentActivity) => void;
  /** 每个 message_delta 到达后，返回累计且已隐藏内部 Chunk 标记的可见回答。 */
  onAnswerUpdate?: (answer: string) => void;
  /** 每个 citation 事件到达后，返回截至当前的完整引用列表。 */
  onCitationsUpdate?: (citations: Citation[]) => void;
  onEvidenceQualityUpdate?: (quality: AgentEvidenceQuality) => void;
}

export interface AgentEvidenceQuality {
  grade: "sufficient" | "insufficient";
  confidence: number;
  reasonCode: string;
  summary: string;
  evidenceCount: number;
  pageCount: number;
  paperCount: number;
  channels: string[];
  retrievalGrade?: "sufficient" | "insufficient";
  answerSupportGrade?: "supported" | "unsupported" | "not_checked";
  answerSupportConfidence?: number;
  claimCount?: number;
  citedClaimCount?: number;
  supportedClaimCount?: number;
  claimCitationCoverage?: number;
  claimSupportCoverage?: number;
}

export interface ArxivResult {
  id: string;
  title: string;
  authors: string;
  year: number;
  summary: string;
}

export interface UserRecord {
  id: string;
  name: string;
  email: string;
  role: "管理员" | "用户";
  status: "正常" | "已停用";
  papers: number;
}

export interface SessionUser {
  id: string;
  email: string;
  role: "admin" | "user";
  active: boolean;
  mustChangePassword: boolean;
}

export interface AdminJob {
  id: string;
  paperId?: string;
  type: string;
  status: "queued" | "running" | "completed" | "failed";
  progress: number;
  attempts: number;
  maxAttempts: number;
  errorCode?: string;
  /** 后端允许向管理员公开的失败说明；不得包含论文正文或服务端堆栈。 */
  errorMessage?: string;
}

export interface ModelPurposeHealth {
  configured: boolean;
  status: "closed" | "open" | "half_open";
  consecutiveFailures: number;
  retryAfterMs: number;
}

export interface ModelProviderHealth {
  provider: string;
  purposes: Record<string, ModelPurposeHealth>;
}

export interface ModelRuntimeHealth {
  configured: boolean;
  providers: ModelProviderHealth[];
  policy: {
    timeoutSeconds: number;
    attemptsPerProvider: number;
    failureThreshold: number;
    cooldownSeconds: number;
  };
}

export type AgentEventType =
  | "run_started"
  | "node_started"
  | "node_finished"
  | "tool_started"
  | "tool_finished"
  | "message_delta"
  | "citation"
  | "interrupt"
  | "error"
  | "run_finished";

export interface AgentEvent<T = unknown> {
  type: AgentEventType;
  data: T;
  id?: string;
}

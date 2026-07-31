export type PaperStatus = "ready" | "indexing" | "partial" | "failed" | "deleting";

export interface Paper {
  id: string;
  title: string;
  authors: string;
  year: number;
  venue: string;
  pages: number;
  status: PaperStatus;
  progress?: number;
  tags: string[];
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
  paperIds: string[];
}

export interface PaperTag {
  id: string;
  name: string;
  color?: string;
  paperIds: string[];
}

export interface CollectionInput {
  name: string;
  description?: string;
}

export interface TagInput {
  name: string;
  color?: string;
}

export type BulkPaperAction =
  | "archive"
  | "unarchive"
  | "add_collection"
  | "remove_collection"
  | "add_tag"
  | "remove_tag";

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

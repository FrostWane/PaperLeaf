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
  quote?: string;
}

export type PaperArtifactStatus = "ready" | "stale" | "failed" | "processing";

export type PaperArtifactFallbackReason =
  | "model_not_configured"
  | "model_timeout"
  | "citation_validation_failed"
  | "invalid_output"
  | string;

export type SummarySectionKey = "research_problem" | "core_method" | "experiment_setup" | "main_results" | "limitations";

export interface SummaryFact {
  text: string;
  citations: ArtifactCitation[];
}

export interface SummarySection {
  key: SummarySectionKey;
  title: string;
  facts: SummaryFact[];
}

export interface PaperSummary {
  paperId: string;
  content?: string;
  sections: SummarySection[];
  citations: ArtifactCitation[];
  mode: "model" | "extractive";
  status: PaperArtifactStatus;
  stale: boolean;
  fallbackReason?: PaperArtifactFallbackReason;
}

export type StructureNodeType = "research_problem" | "background" | "method" | "data" | "experiment" | "result" | "limitation";

export interface StructureNode {
  id: string;
  type: StructureNodeType;
  label: string;
  summary: string;
  citations: ArtifactCitation[];
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
  status: PaperArtifactStatus;
  stale: boolean;
  fallbackReason?: PaperArtifactFallbackReason;
  evidenceExcerpt?: string;
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

export type PaperTranslationStatus = "queued" | "running" | "partial" | "completed" | "failed" | "cancelled";
export type PaperTranslationPageStatus = "queued" | "running" | "completed" | "no_text" | "failed" | "cancelled";

export interface PaperTranslationPage {
  page: number;
  status: PaperTranslationPageStatus;
  text: string;
  error?: string;
}

export interface PaperTranslation {
  id: string;
  paperId: string;
  targetLanguage: string;
  status: PaperTranslationStatus;
  progress: number;
  completedPages: number;
  failedPages: number;
  totalPages: number;
  error?: string;
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

export interface AdminRagObservability {
  windowHours: number;
  generatedAt: string;
  limitReached: boolean;
  totals: {
    runs: number;
    terminalRuns: number;
    completedRuns: number;
    failedRuns: number;
    citedAnswers: number;
    groundedAnswers: number;
    ragIssueRuns: number;
    telemetryRuns: number;
    telemetryCoverage: number;
    completionRate: number;
    failureRate: number;
    citedAnswerRate: number;
    ragIssueRate: number;
  };
  funnel: Array<{ key: string; label: string; count: number; rate: number }>;
  latency: {
    overall: { samples: number; p50Ms?: number; p95Ms?: number };
    stages: Array<{ stage: string; samples: number; p50Ms?: number; p95Ms?: number }>;
  };
  retrievalChannels: Array<{
    channel: string;
    label: string;
    runs: number;
    citedAnswerRate: number;
    sufficientEvidenceRate: number;
    retrievalP95Ms?: number;
  }>;
  intents: Array<{
    intent: string;
    label: string;
    runs: number;
    citedAnswerRate: number;
    sufficientEvidenceRate: number;
    p95Ms?: number;
  }>;
  failures: Array<{ category: string; label: string; count: number; rate: number }>;
  chunkingStrategies: Array<{ strategy: string; runs: number }>;
  runtimeStore: {
    backend: string;
    status: "available" | "degraded";
    usedMemoryBytes?: number;
    maxMemoryBytes?: number;
    keyCount?: number;
    connectedClients?: number;
  };
  privacy: { contentCollected: boolean; identifiersCollected: boolean };
}

export type ChatSessionType = "paper" | "collection" | "library";
export type AgentRunStatus = "pending" | "running" | "interrupted" | "completed" | "failed" | "cancelled";

export interface ChatSession {
  id: string;
  title: string;
  type: ChatSessionType;
  paperId?: string;
  collectionId?: string;
  currentRunId?: string;
  currentRunStatus?: AgentRunStatus;
  createdAt: string;
  updatedAt: string;
}

export interface ChatMessage {
  id: string;
  sessionId: string;
  role: "user" | "assistant";
  sequence: number;
  status: "pending" | "streaming" | "completed" | "failed" | "cancelled";
  content: string;
  citations: Citation[];
  runId?: string;
  createdAt: string;
  updatedAt: string;
}

export interface ChatSessionInput {
  type: ChatSessionType;
  title?: string;
  paperId?: string;
  collectionId?: string;
}

export interface ChatMessageSubmission {
  sessionId: string;
  messageId: string;
  runId: string;
  status: "pending";
  replayed: boolean;
}

export interface AgentRunSnapshot {
  runId: string;
  sessionId: string;
  status: AgentRunStatus;
  cancelRequested: boolean;
  pendingAction?: {
    actionId: string;
    type: string;
    riskMessage: string;
    allowedDecisions: string[];
    candidates: Array<{ arxivId?: string; title?: string; authors?: string[] | string; abstract?: string; published?: string; pdfUrl?: string; journalRef?: string }>;
  };
  answer: string;
  citations: Citation[];
  evidenceQuality?: AgentEvidenceQuality;
  error?: string;
  createdAt: string;
  updatedAt: string;
}

export interface AgentEventSubscriptionHandlers extends AgentAskStreamHandlers {
  onEvent?: (event: AgentEvent) => void;
  onConnectionState?: (state: "connected" | "reconnecting") => void;
  onRunUpdate?: (run: AgentRunSnapshot) => void;
}

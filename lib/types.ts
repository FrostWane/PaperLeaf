export type PaperStatus = "ready" | "indexing" | "partial" | "failed";

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

export type AgentEventType =
  | "run_started"
  | "node_started"
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

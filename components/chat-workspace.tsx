"use client";

import { Check, ChevronLeft, ChevronRight, History, LoaderCircle, MessageSquarePlus, Pencil, RotateCw, Send, Square, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { getDataSource, type PaperLeafDataSource } from "@/lib/data-source";
import type { AgentActivity, AgentRunSnapshot, ChatMessage, ChatSession, ChatSessionInput, Citation } from "@/lib/types";
import { chatCitationSources, CitationSources } from "./citation-sources";
import { SafeMarkdown } from "./safe-markdown";

export type ChatBinding =
  | { type: "paper"; paperId: string }
  | { type: "collection"; collectionId: string }
  | { type: "library" };

const activeStatuses = new Set(["pending", "running", "interrupted"]);
const subscribedStatuses = new Set(["pending", "running"]);
const historyPageSize = 15;
const exampleQuestions = [
  "比较这些论文所采用的方法与关键假设",
  "哪些结论有直接实验结果支持？",
  "这些研究还存在哪些共同局限？",
];

function subscribeChatNarrow(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
  const query = window.matchMedia("(max-width: 720px)");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function getChatNarrow(): boolean {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(max-width: 720px)").matches);
}

export function progressiveChunkSize(remainingCharacters: number): number {
  if (remainingCharacters > 1200) return 12;
  if (remainingCharacters > 600) return 9;
  if (remainingCharacters > 240) return 6;
  if (remainingCharacters > 100) return 4;
  if (remainingCharacters > 36) return 2;
  return 1;
}

function useProgressiveAnswer(target: string, resetKey: string): string {
  const [progress, setProgress] = useState({ key: resetKey, text: "" });
  const progressRef = useRef(progress);
  const targetRef = useRef(target);
  const startRef = useRef<() => void>(() => undefined);
  const visible = progress.key === resetKey && target.startsWith(progress.text) ? progress.text : "";

  useEffect(() => {
    targetRef.current = target;
    startRef.current();
  }, [target]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const reduceMotion = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    function revealNextChunk() {
      if (stopped) return;
      timer = undefined;
      const nextTarget = targetRef.current;
      const current = progressRef.current;
      const currentText = current.key === resetKey && nextTarget.startsWith(current.text) ? current.text : "";
      if (!nextTarget) {
        if (current.key !== resetKey || current.text) {
          progressRef.current = { key: resetKey, text: "" };
          setProgress(progressRef.current);
        }
        return;
      }
      if (currentText === nextTarget) return;
      const remaining = Array.from(nextTarget.slice(currentText.length));
      const chunkSize = reduceMotion ? remaining.length : progressiveChunkSize(remaining.length);
      progressRef.current = { key: resetKey, text: currentText + remaining.slice(0, chunkSize).join("") };
      setProgress(progressRef.current);
      if (progressRef.current.text !== nextTarget) timer = window.setTimeout(revealNextChunk, 22);
    }

    function start() {
      if (!stopped && timer === undefined) timer = window.setTimeout(revealNextChunk, 0);
    }

    startRef.current = start;
    start();
    return () => {
      stopped = true;
      startRef.current = () => undefined;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [resetKey]);

  return visible;
}

function ChatCitations({ citations, onOpenCitation }: { citations: Citation[]; onOpenCitation?: (citation: Citation) => void }) {
  const sources = chatCitationSources(citations);
  const citationsByChunk = new Map(citations.map((citation) => [citation.chunkId, citation]));
  return <CitationSources sources={sources} onOpen={(source) => {
    const citation = citationsByChunk.get(source.key);
    if (citation) onOpenCitation?.(citation);
  }} />;
}

function inputFromBinding(binding: ChatBinding, title = "新对话"): ChatSessionInput {
  if (binding.type === "paper") return { type: "paper", paperId: binding.paperId, title };
  if (binding.type === "collection") return { type: "collection", collectionId: binding.collectionId, title };
  return { type: "library", title };
}

function matchesBinding(session: ChatSession, binding: ChatBinding): boolean {
  if (session.type !== binding.type) return false;
  if (binding.type === "paper") return session.paperId === binding.paperId;
  if (binding.type === "collection") return session.collectionId === binding.collectionId;
  return true;
}

function belongsToWorkspace(session: ChatSession, binding: ChatBinding): boolean {
  return binding.type === "paper" ? session.type === "paper" && session.paperId === binding.paperId : session.type !== "paper";
}

function bindingFromSession(session: ChatSession): ChatBinding {
  if (session.type === "paper") return { type: "paper", paperId: session.paperId ?? "" };
  if (session.type === "collection") return { type: "collection", collectionId: session.collectionId ?? "" };
  return { type: "library" };
}

function isActiveRun(run: AgentRunSnapshot | null | undefined): boolean {
  return Boolean(run && activeStatuses.has(run.status));
}

function shouldSubscribeRun(run: AgentRunSnapshot | null | undefined): boolean {
  return Boolean(run && subscribedStatuses.has(run.status));
}

function isActiveSession(session: ChatSession | null | undefined): boolean {
  return Boolean(session?.currentRunStatus && activeStatuses.has(session.currentRunStatus));
}

function phaseLabel(status: string | undefined): string {
  if (status === "completed") return "已完成";
  if (status === "running") return "进行中";
  if (status === "failed") return "失败";
  return "等待";
}

function runStatusText(run: AgentRunSnapshot | null, connection: "connected" | "reconnecting"): string {
  if (connection === "reconnecting" && isActiveRun(run)) return "正在恢复连接…";
  if (!run) return "";
  if (run.error?.includes("暂时无法恢复")) return run.error;
  if (run.cancelRequested && isActiveRun(run)) return "正在取消后台问答…";
  if (run.status === "pending") return "正在等待处理，可离开页面";
  if (run.status === "running") return "正在回答，可离开页面";
  if (run.status === "interrupted") return "运行已暂停，正在等待恢复";
  if (run.status === "completed") return "回答已完成并持久化";
  if (run.status === "cancelled") return "问答已取消";
  return run.error ? `问答失败：${run.error}` : "问答运行失败";
}

function sortedWorkspaceSessions(sessions: ChatSession[], binding: ChatBinding): ChatSession[] {
  return sessions
    .filter((session) => belongsToWorkspace(session, binding))
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
}

function historyPageForSession(sessions: ChatSession[], sessionId: string | null): number {
  if (!sessionId) return 1;
  const index = sessions.findIndex((session) => session.id === sessionId);
  return index < 0 ? 1 : Math.floor(index / historyPageSize) + 1;
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `paperleaf-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function fallbackRun(session: ChatSession, message: string): AgentRunSnapshot | null {
  if (!session.currentRunId || !session.currentRunStatus) return null;
  return {
    runId: session.currentRunId,
    sessionId: session.id,
    status: session.currentRunStatus,
    cancelRequested: false,
    answer: "",
    citations: [],
    error: message,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
  };
}

export function ChatWorkspace({
  binding,
  scopeLabel,
  compact = false,
  disabled = false,
  webEnabled = false,
  dataSource = getDataSource(),
  onBindingChange,
  onOpenCitation,
}: {
  binding: ChatBinding;
  scopeLabel: string;
  compact?: boolean;
  disabled?: boolean;
  webEnabled?: boolean;
  dataSource?: PaperLeafDataSource;
  onBindingChange?: (binding: ChatBinding) => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [run, setRun] = useState<AgentRunSnapshot | null>(null);
  const [liveAnswer, setLiveAnswer] = useState("");
  const [liveCitations, setLiveCitations] = useState<Citation[]>([]);
  const [activities, setActivities] = useState<AgentActivity[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);
  const [historyOverride, setHistoryOverride] = useState<boolean | null>(null);
  const [historyPage, setHistoryPage] = useState(1);
  const [connection, setConnection] = useState<"connected" | "reconnecting">("connected");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const terminalReloadRef = useRef<string | null>(null);
  const submittingRef = useRef(false);
  const creatingSessionRef = useRef(false);
  const submissionAttemptRef = useRef<{ sessionId: string; content: string; key: string } | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const narrowViewport = useSyncExternalStore(subscribeChatNarrow, getChatNarrow, () => false);
  const historyOpen = historyOverride ?? (!compact && !narrowViewport);

  const workspaceKey = binding.type === "paper" ? `paper:${binding.paperId}` : "library";
  const bindingKey = binding.type === "paper" ? `paper:${binding.paperId}` : binding.type === "collection" ? `collection:${binding.collectionId}` : "library";
  const stableBinding = useMemo<ChatBinding>(() => bindingKey.startsWith("paper:")
    ? { type: "paper", paperId: bindingKey.slice("paper:".length) }
    : bindingKey.startsWith("collection:")
      ? { type: "collection", collectionId: bindingKey.slice("collection:".length) }
      : { type: "library" }, [bindingKey]);
  const selected = useMemo(() => {
    const candidate = sessions.find((item) => item.id === selectedId);
    return candidate && matchesBinding(candidate, stableBinding) ? candidate : null;
  }, [selectedId, sessions, stableBinding]);
  const currentRun = selected && run?.sessionId === selected.id ? run : null;
  const active = submitting || (currentRun ? isActiveRun(currentRun) : isActiveSession(selected));
  const progressiveAnswer = useProgressiveAnswer(liveAnswer, currentRun?.runId ?? selected?.id ?? "empty");
  const visibleMessages = useMemo(() => messages.filter((message) => message.sessionId === selected?.id && message.content.trim() && !(Boolean(currentRun && liveAnswer) && message.role === "assistant" && message.runId === currentRun?.runId)), [currentRun, liveAnswer, messages, selected?.id]);
  const visibleSessions = useMemo(() => sortedWorkspaceSessions(sessions, stableBinding), [sessions, stableBinding]);
  const historyPageCount = Math.max(1, Math.ceil(visibleSessions.length / historyPageSize));
  const effectiveHistoryPage = Math.min(Math.max(1, historyPage), historyPageCount);
  const pagedSessions = compact
    ? visibleSessions
    : visibleSessions.slice((effectiveHistoryPage - 1) * historyPageSize, effectiveHistoryPage * historyPageSize);

  useEffect(() => {
    selectedIdRef.current = selected?.id ?? null;
  }, [selected?.id]);

  const refreshSessions = useCallback(async (preferredId?: string) => {
    const next = await dataSource.listChatSessions();
    setSessions(next);
    const stored = typeof window === "undefined" ? null : window.localStorage.getItem(`paperleaf:chat:${workspaceKey}`);
    const preferred = preferredId ?? selectedId ?? stored;
    const candidate = next.find((item) => item.id === preferred && belongsToWorkspace(item, stableBinding))
      ?? next.find((item) => belongsToWorkspace(item, stableBinding) && matchesBinding(item, stableBinding));
    setSelectedId(candidate?.id ?? null);
    if (!compact) setHistoryPage(historyPageForSession(sortedWorkspaceSessions(next, stableBinding), candidate?.id ?? null));
    return next;
  }, [compact, dataSource, selectedId, stableBinding, workspaceKey]);

  useEffect(() => {
    let stopped = false;
    void dataSource.listChatSessions().then((next) => {
      if (stopped) return;
      setSessions(next);
      const stored = window.localStorage.getItem(`paperleaf:chat:${workspaceKey}`);
      const candidate = next.find((item) => item.id === stored && belongsToWorkspace(item, stableBinding))
        ?? next.find((item) => belongsToWorkspace(item, stableBinding) && matchesBinding(item, stableBinding));
      setSelectedId(candidate?.id ?? null);
      if (!compact) setHistoryPage(historyPageForSession(sortedWorkspaceSessions(next, stableBinding), candidate?.id ?? null));
    }).catch((reason: unknown) => {
      if (!stopped) setError(reason instanceof Error ? reason.message : "对话历史读取失败");
    }).finally(() => {
      if (!stopped) setLoading(false);
    });
    return () => { stopped = true; };
  }, [compact, dataSource, stableBinding, workspaceKey]);

  useEffect(() => {
    if (!selectedId) {
      queueMicrotask(() => {
        setMessages([]);
        setRun(null);
        setLiveAnswer("");
        setLiveCitations([]);
        setActivities([]);
      });
      return;
    }
    window.localStorage.setItem(`paperleaf:chat:${workspaceKey}`, selectedId);
    let stopped = false;
    void Promise.all([
      dataSource.listChatMessages(selectedId),
      selected?.currentRunId ? dataSource.getAgentRun(selected.currentRunId).catch(() => fallbackRun(selected, "运行状态暂时无法恢复，可重新连接或取消运行")) : Promise.resolve(null),
    ]).then(([nextMessages, nextRun]) => {
      if (!stopped) {
        setMessages(nextMessages);
        setRun(nextRun);
        setLiveAnswer(isActiveRun(nextRun) ? nextRun?.answer ?? "" : "");
        setLiveCitations(isActiveRun(nextRun) ? nextRun?.citations ?? [] : []);
        setActivities([]);
        if (nextRun?.error?.includes("暂时无法恢复")) setError("问题已经在后台受理，但运行状态暂时无法恢复。请重新连接或取消运行，不要重复提交。");
      }
    }).catch((reason: unknown) => {
      if (!stopped) setError(reason instanceof Error ? reason.message : "对话内容读取失败");
    }).finally(() => {
      if (!stopped) setLoading(false);
    });
    return () => { stopped = true; };
  }, [dataSource, selected, selectedId, workspaceKey]);

  useEffect(() => {
    const runId = currentRun?.runId;
    const runStatus = currentRun?.status;
    if (!runId || !runStatus || !subscribedStatuses.has(runStatus)) return;
    const controller = new AbortController();
    terminalReloadRef.current = null;
    void dataSource.subscribeAgentRun(runId, {
      onConnectionState: (state) => { if (!controller.signal.aborted) setConnection(state); },
      onActivity: (activity) => { if (!controller.signal.aborted) setActivities((items) => items.some((item) => item.key === activity.key)
        ? items.map((item) => item.key === activity.key ? activity : item)
        : [...items, activity]); },
      onAnswerUpdate: (answer) => { if (!controller.signal.aborted) setLiveAnswer(answer); },
      onCitationsUpdate: (citations) => { if (!controller.signal.aborted) setLiveCitations(citations); },
      onEvidenceQualityUpdate: (evidenceQuality) => { if (!controller.signal.aborted) setRun((current) => current?.runId === runId ? { ...current, evidenceQuality } : current); },
      onRunUpdate: (nextRun) => {
        if (controller.signal.aborted || selectedIdRef.current !== nextRun.sessionId) return;
        setRun(nextRun);
        if (!activeStatuses.has(nextRun.status) && terminalReloadRef.current !== runId) {
          terminalReloadRef.current = runId;
          void Promise.all([dataSource.listChatMessages(nextRun.sessionId), dataSource.listChatSessions()]).then(([nextMessages, nextSessions]) => {
            if (controller.signal.aborted || selectedIdRef.current !== nextRun.sessionId) return;
            setMessages(nextMessages);
            setSessions(nextSessions);
          }).catch((reason: unknown) => { if (!controller.signal.aborted && selectedIdRef.current === nextRun.sessionId) setError(reason instanceof Error ? reason.message : "完成后的回答同步失败"); });
        }
      },
    }, { signal: controller.signal }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "回答事件连接失败");
    });
    return () => controller.abort();
  }, [currentRun?.runId, currentRun?.status, dataSource]);

  useEffect(() => {
    if (!currentRun || activeStatuses.has(currentRun.status) || !liveAnswer || progressiveAnswer !== liveAnswer) return;
    const persisted = messages.some((message) => message.role === "assistant" && message.runId === currentRun.runId && message.content.trim());
    if (!persisted) return;
    const timer = window.setTimeout(() => {
      setLiveAnswer("");
      setLiveCitations([]);
    }, 80);
    return () => window.clearTimeout(timer);
  }, [currentRun, liveAnswer, messages, progressiveAnswer]);

  async function createSession() {
    if (disabled || creatingSessionRef.current) return;
    creatingSessionRef.current = true;
    setCreatingSession(true);
    setError("");
    let shouldFocus = false;
    try {
      const next = await dataSource.createChatSession(inputFromBinding(stableBinding));
      await refreshSessions(next.id);
      setSelectedId(next.id);
      setHistoryPage(1);
      setHistoryOverride(compact || narrowViewport ? false : true);
      shouldFocus = true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "新建对话失败");
    } finally {
      creatingSessionRef.current = false;
      setCreatingSession(false);
      if (shouldFocus) window.requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }

  function selectSession(session: ChatSession) {
    setSelectedId(session.id);
    onBindingChange?.(bindingFromSession(session));
    if (compact || narrowViewport) setHistoryOverride(false);
  }

  async function saveRename(sessionId: string) {
    const title = renameDraft.trim();
    if (!title) return;
    try {
      const updated = await dataSource.updateChatSession(sessionId, title);
      setSessions((items) => items.map((item) => item.id === sessionId ? updated : item));
      setRenamingId(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "对话重命名失败");
    }
  }

  async function deleteSession(session: ChatSession) {
    if (!window.confirm(`删除对话“${session.title}”？此操作不会删除论文。`)) return;
    setError("");
    try {
      await dataSource.deleteChatSession(session.id);
      const next = await refreshSessions();
      if (!next.some((item) => item.id === selectedId)) setSelectedId(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "对话删除失败");
    }
  }

  async function submitQuestion() {
    const question = draft.trim();
    if (question.length < 3) {
      setError("请输入至少 3 个字符的具体问题");
      textareaRef.current?.focus();
      return;
    }
    if (active || disabled || creatingSessionRef.current || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError("");
    try {
      let session = selected;
      if (!session || !matchesBinding(session, stableBinding)) {
        session = await dataSource.createChatSession(inputFromBinding(stableBinding, question.slice(0, 32)));
        await refreshSessions(session.id);
        setSelectedId(session.id);
      } else if (messages.length === 0 && session.title === "新对话") {
        session = await dataSource.updateChatSession(session.id, question.slice(0, 32));
        setSessions((items) => items.map((item) => item.id === session?.id ? session : item));
      }
      const previousAttempt = submissionAttemptRef.current;
      const idempotencyKey = previousAttempt?.sessionId === session.id && previousAttempt.content === question ? previousAttempt.key : createIdempotencyKey();
      submissionAttemptRef.current = { sessionId: session.id, content: question, key: idempotencyKey };
      const submission = await dataSource.submitChatMessage(session.id, question, idempotencyKey, { webEnabled });
      submissionAttemptRef.current = null;
      const timestamp = new Date().toISOString();
      const pendingRun: AgentRunSnapshot = { runId: submission.runId, sessionId: session.id, status: "pending", cancelRequested: false, answer: "", citations: [], createdAt: timestamp, updatedAt: timestamp };
      setMessages((items) => items.some((item) => item.id === submission.messageId) ? items : [...items, { id: submission.messageId, sessionId: session!.id, role: "user", sequence: items.filter((item) => item.sessionId === session!.id).length + 1, status: "completed", content: question, citations: [], runId: submission.runId, createdAt: timestamp, updatedAt: timestamp }]);
      setSessions((items) => items.map((item) => item.id === session?.id ? { ...item, currentRunId: submission.runId, currentRunStatus: "pending", updatedAt: timestamp } : item));
      setRun(pendingRun);
      setLiveAnswer("");
      setLiveCitations([]);
      setDraft("");
      setActivities([]);
      try {
        const nextRun = await dataSource.getAgentRun(submission.runId);
        if (selectedIdRef.current === session.id) {
          setRun(nextRun);
          setLiveAnswer(nextRun.answer);
          setLiveCitations(nextRun.citations);
        }
        await refreshSessions(session.id);
      } catch {
        setError("问题已受理，后台会继续运行；当前状态正在恢复，请不要重复提交。");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "问题提交失败");
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  async function cancelRun() {
    if (!currentRun || !isActiveRun(currentRun)) return;
    try {
      const cancelled = await dataSource.cancelAgentRun(currentRun.runId);
      setRun(cancelled);
      const [nextMessages, nextSessions] = await Promise.all([dataSource.listChatMessages(currentRun.sessionId), dataSource.listChatSessions()]);
      if (selectedIdRef.current === currentRun.sessionId) setMessages(nextMessages);
      setSessions(nextSessions);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消问答失败");
    }
  }

  async function resumeRun(decision: string) {
    if (!currentRun?.pendingAction) return;
    try {
      const resumed = await dataSource.resumeAgentRun(currentRun.runId, currentRun.pendingAction.actionId, decision);
      setRun(resumed);
      setSessions((items) => items.map((item) => item.id === resumed.sessionId ? { ...item, currentRunId: resumed.runId, currentRunStatus: resumed.status, updatedAt: resumed.updatedAt } : item));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复问答失败");
    }
  }

  async function recoverRun() {
    if (!currentRun) return;
    try {
      setRun(await dataSource.getAgentRun(currentRun.runId));
      setError("");
    } catch {
      setError("运行状态仍未恢复，后台任务不会重复提交；你也可以取消本次运行。");
    }
  }

  function fillPrompt(prompt: string) {
    setDraft(prompt);
    setError("");
    textareaRef.current?.focus();
  }

  const phaseNodes = {
    retrieval: activities.filter((item) => item.node.includes("retrieve") || item.node.includes("search")).at(-1)?.status,
    generation: activities.filter((item) => item.node.includes("generate")).at(-1)?.status,
    validation: activities.filter((item) => item.node.includes("validate") || item.node.includes("grade")).at(-1)?.status,
  };

  return (
    <div className={compact ? "chat-workspace compact" : "chat-workspace"} data-active-run={active || creatingSession ? "true" : "false"}>
      <header className="chat-toolbar">
        <div><strong>{selected?.title ?? "新对话"}</strong><small>{scopeLabel}</small></div>
        <div className="chat-toolbar-actions">
          <button type="button" className="secondary-button" aria-expanded={historyOpen} onClick={() => setHistoryOverride(!historyOpen)}><History size={15} />历史</button>
          <button type="button" className="secondary-button" disabled={disabled || creatingSession} onClick={() => void createSession()}><MessageSquarePlus size={15} />新对话</button>
        </div>
      </header>

      <div className="chat-body">
        {historyOpen && <aside className="chat-history" aria-label="历史对话">
          <div className="chat-history-head"><strong>历史对话</strong>{(compact || narrowViewport) && <button type="button" className="icon-button" aria-label="关闭历史对话" onClick={() => setHistoryOverride(false)}><X size={15} /></button>}</div>
          {visibleSessions.length === 0 && <p>还没有对话。新问题会自动保存在这里。</p>}
          <div className="chat-session-list">
            {pagedSessions.map((session) => <div className={session.id === selectedId ? "chat-session active" : "chat-session"} key={session.id}>
              {renamingId === session.id ? <form onSubmit={(event) => { event.preventDefault(); void saveRename(session.id); }}><input autoFocus aria-label="对话标题" value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} maxLength={80} /><button type="submit" className="icon-button" aria-label="保存标题"><Check size={14} /></button><button type="button" className="icon-button" aria-label="取消重命名" onClick={() => setRenamingId(null)}><X size={14} /></button></form> : <>
                <button type="button" className="chat-session-select" onClick={() => selectSession(session)}><span>{session.title}</span><small>{session.currentRunStatus && activeStatuses.has(session.currentRunStatus) ? "后台运行中" : new Date(session.updatedAt).toLocaleDateString("zh-CN")}</small></button>
                <button type="button" className="icon-button" aria-label={`重命名对话 ${session.title}`} disabled={isActiveSession(session)} onClick={() => { setRenamingId(session.id); setRenameDraft(session.title); }}><Pencil size={13} /></button>
                <button type="button" className="icon-button" aria-label={`删除对话 ${session.title}`} disabled={isActiveSession(session)} onClick={() => void deleteSession(session)}><Trash2 size={13} /></button>
              </>}
            </div>)}
          </div>
          {!compact && visibleSessions.length > historyPageSize && <nav className="chat-history-pagination" aria-label="历史对话分页">
            <button type="button" className="icon-button" aria-label="上一页" disabled={effectiveHistoryPage <= 1} onClick={() => setHistoryPage(Math.max(1, effectiveHistoryPage - 1))}><ChevronLeft size={15} /></button>
            <span aria-live="polite">第 {effectiveHistoryPage} / {historyPageCount} 页</span>
            <button type="button" className="icon-button" aria-label="下一页" disabled={effectiveHistoryPage >= historyPageCount} onClick={() => setHistoryPage(Math.min(historyPageCount, effectiveHistoryPage + 1))}><ChevronRight size={15} /></button>
          </nav>}
        </aside>}

        <section className="chat-thread" aria-label="对话消息" aria-busy={active || creatingSession}>
          {loading && <div className="chat-empty" role="status"><LoaderCircle className="spinner-icon" size={20} />正在恢复对话…</div>}
          {!loading && visibleMessages.length === 0 && !liveAnswer && <div className="chat-empty"><strong>向文献提问</strong><div className="chat-prompts"><span>可以这样问</span>{exampleQuestions.map((prompt) => <button type="button" key={prompt} onClick={() => fillPrompt(prompt)}>{prompt}<ChevronRight size={14} /></button>)}</div></div>}
          {visibleMessages.map((message) => <article key={message.id} className={`chat-message ${message.role}`} aria-label={message.role === "user" ? "你的消息" : "PaperLeaf 回复"}>
            <span className="chat-message-author">{message.role === "user" ? "你" : "PaperLeaf"}{message.role === "assistant" && message.status !== "completed" ? ` · ${message.status === "failed" ? "回答失败，已保留核验段落" : message.status === "cancelled" ? "已取消，已保留核验段落" : "正在写入"}` : ""}</span>
            {message.role === "assistant" ? <SafeMarkdown content={message.content} citations={message.citations} onOpenCitation={onOpenCitation} /> : <p>{message.content}</p>}
            {message.role === "assistant" && <ChatCitations citations={message.citations} onOpenCitation={onOpenCitation} />}
          </article>)}
          {currentRun && liveAnswer && <article className="chat-message assistant live" aria-label="PaperLeaf 正在逐字生成已核验回答"><span className="chat-message-author">PaperLeaf · 已核验内容正在生成</span><div className={progressiveAnswer === liveAnswer ? "progressive-answer" : "progressive-answer typing"}><SafeMarkdown content={progressiveAnswer} citations={liveCitations} onOpenCitation={onOpenCitation} /></div><ChatCitations citations={liveCitations} onOpenCitation={onOpenCitation} /></article>}
          {currentRun && <div className={`chat-run-state ${currentRun.status}`} role="status">
            <div><span>{runStatusText(currentRun, connection)}</span>{isActiveRun(currentRun) && <button type="button" className="secondary-button" disabled={currentRun.cancelRequested} onClick={() => void cancelRun()}><Square size={13} />{currentRun.cancelRequested ? "正在取消" : "取消运行"}</button>}</div>
            {currentRun.pendingAction && <div className="chat-pending-action"><strong>需要你的确认</strong><p>{currentRun.pendingAction.riskMessage}</p>{currentRun.pendingAction.candidates.slice(0, 3).map((candidate) => <span key={candidate.arxivId ?? candidate.title}>{candidate.title ?? candidate.arxivId}</span>)}<div>{currentRun.pendingAction.allowedDecisions.includes("approve") && <button type="button" className="primary-button" onClick={() => void resumeRun("approve")}>确认导入并继续</button>}{currentRun.pendingAction.allowedDecisions.includes("reject") && <button type="button" className="secondary-button" onClick={() => void resumeRun("reject")}>不导入，继续回答</button>}</div></div>}
            {shouldSubscribeRun(currentRun) && <ol aria-label="问答处理阶段">
              <li data-status={phaseNodes.retrieval ?? (currentRun.status === "pending" ? "pending" : undefined)}><span>1</span>检索 <small>{phaseLabel(phaseNodes.retrieval)}</small></li>
              <li data-status={phaseNodes.generation}><span>2</span>生成 <small>{phaseLabel(phaseNodes.generation)}</small></li>
              <li data-status={phaseNodes.validation}><span>3</span>核验 <small>{phaseLabel(phaseNodes.validation)}</small></li>
            </ol>}
            {currentRun.error?.includes("暂时无法恢复") && <button type="button" className="text-button" onClick={() => void recoverRun()}><RotateCw size={13} />重新连接运行状态</button>}
            {currentRun.status === "failed" && <button type="button" className="text-button" onClick={() => { setRun(null); textareaRef.current?.focus(); }}><RotateCw size={13} />重新编辑问题</button>}
          </div>}
          {error && <p className="field-error chat-error" role="alert">{error}</p>}
        </section>
      </div>

      <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void submitQuestion(); }}>
        <label><span className="sr-only">向文献提问</span><textarea ref={textareaRef} rows={compact ? 2 : 3} value={draft} disabled={disabled || creatingSession} onChange={(event) => { setDraft(event.target.value); if (error) setError(""); }} onKeyDown={(event) => {
          if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          event.preventDefault();
          if (!active && !disabled && !creatingSessionRef.current && draft.trim().length >= 3) void submitQuestion();
        }} placeholder={disabled ? "等待论文完成索引后可提问" : creatingSession ? "正在新建对话…" : "输入问题；回答将附上可回读页码…"} /><button type="submit" className="send-button" disabled={active || disabled || creatingSession || draft.trim().length < 3} aria-label="发送问题"><Send size={16} /></button></label>
        <div><span>{creatingSession ? "正在新建对话…" : active ? "正在回答，可离开页面" : ""}</span><span>Enter 发送 · Shift + Enter 换行</span></div>
      </form>
    </div>
  );
}

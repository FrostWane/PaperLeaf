"use client";

import { AlignLeft, ArrowLeft, ChevronLeft, ChevronRight, FileText, Focus, Info, Languages, Maximize2, MessageSquare, Minus, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, PencilLine, Plus, Quote, Send, X } from "lucide-react";
import dynamic from "next/dynamic";
import { Fragment, useEffect, useRef, useState } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { z } from "zod";
import { demoDataSource, getDataSource } from "@/lib/data-source";
import { groundedAnswer, papers } from "@/lib/fixtures";
import { demoCurrentUser, getCurrentUser, updateUserPreferences } from "@/lib/preferences-api";
import { useWorkspaceStore, type MobilePane } from "@/lib/store";
import type { AgentActivity, AgentAnswer, Paper, PaperStructureGraph, PaperSummary, PaperTranslation, PaperTranslationPage, PaperUpdateInput } from "@/lib/types";
import { AgentRunProgress } from "./agent-run-progress";
import { EvidenceQualityStrip } from "./evidence-quality-strip";
import { PaperDetailsDialog } from "./paper-details-dialog";
import { StructureDiagram } from "./structure-diagram";
import { SummaryContent } from "./summary-content";
import { TranslationConfirmDialog, translationLanguageLabel } from "./translation-confirm-dialog";

const RealPdfDocument = dynamic(
  () => import("./real-pdf-document").then((module) => module.RealPdfDocument),
  { ssr: false, loading: () => <p role="status">正在准备 PDF 阅读器…</p> },
);

const questionSchema = z.object({
  question: z.string().trim().min(3, "问题至少需要 3 个字符").max(500, "问题不能超过 500 个字符"),
});
type QuestionInput = z.infer<typeof questionSchema>;
type AssistantView = "ask" | "summary" | "structure";

const tabItems: { id: MobilePane; label: string; icon: typeof FileText }[] = [
  { id: "pdf", label: "论文", icon: FileText },
  { id: "ask", label: "提问", icon: MessageSquare },
  { id: "info", label: "信息", icon: Info },
];

const assistantTabs: { id: AssistantView; label: string; icon: typeof MessageSquare }[] = [
  { id: "ask", label: "问答", icon: MessageSquare },
  { id: "summary", label: "概览", icon: AlignLeft },
  { id: "structure", label: "结构", icon: Network },
];

function ResizeSeparator({ label }: { label: string }) {
  return <Separator className="resize-handle" aria-label={label} />;
}

function PaperState({ paper }: { paper: Paper }) {
  if (paper.status === "ready") return <span className="status-pill ready"><span>✓</span>已建立索引</span>;
  if (paper.status === "partial") return <span className="status-pill partial"><span>!</span>部分可用</span>;
  if (paper.status === "failed") return <span className="status-pill failed"><span>×</span>处理失败</span>;
  if (paper.status === "deleting") return <span className="status-pill neutral"><span>…</span>正在删除</span>;
  return <span className="status-pill indexing"><span className="spinner" />正在建立索引</span>;
}

function clampZoom(value: number): number {
  return Math.max(50, Math.min(200, Math.round(value / 10) * 10));
}

function translationStatusLabel(translation: PaperTranslation): string {
  if (translation.status === "queued") return "等待后台处理";
  if (translation.status === "running") return `正在翻译 · ${translation.completedPages}/${translation.totalPages} 页`;
  if (translation.status === "partial") return `处理结束：成功 ${translation.completedPages} 页，失败 ${translation.failedPages} 页`;
  if (translation.status === "completed") return "全文翻译已完成";
  if (translation.status === "cancelled") return "翻译任务已取消，已完成译文仍可查看";
  return translation.error ? `翻译失败：${translation.error}` : "翻译任务失败";
}

export function PaperWorkspace({ paperId = "attention", demo = false, initialPage }: { paperId?: string; demo?: boolean; initialPage?: number }) {
  const isReal = !demo && process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const dataSource = demo ? demoDataSource : getDataSource();
  const fallbackPaper = papers.find((item) => item.id === paperId) ?? papers[0];
  const [paper, setPaper] = useState<Paper | null>(isReal ? null : fallbackPaper);
  const [currentPage, setCurrentPage] = useState(Math.max(1, initialPage ?? (demo ? 2 : 1)));
  const [mobilePane, setMobilePane] = useState<MobilePane>("pdf");
  const [assistantView, setAssistantView] = useState<AssistantView>("ask");
  const [answer, setAnswer] = useState<AgentAnswer | null>(isReal ? null : groundedAnswer);
  const [activities, setActivities] = useState<AgentActivity[]>([]);
  const [summary, setSummary] = useState<PaperSummary | null>(null);
  const [structure, setStructure] = useState<PaperStructureGraph | null>(null);
  const [busy, setBusy] = useState<"ask" | "summary" | "structure" | null>(null);
  const [loadMessage, setLoadMessage] = useState("");
  const [askMessage, setAskMessage] = useState("");
  const [questionDraft, setQuestionDraft] = useState("");
  const [questionError, setQuestionError] = useState("");
  const [artifactMessage, setArtifactMessage] = useState("");
  const [manageMessage, setManageMessage] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [leftPanelOpen, setLeftPanelOpen] = useState(true);
  const [assistantPanelOpen, setAssistantPanelOpen] = useState(true);
  const [zoomPercent, setZoomPercent] = useState(100);
  const [fitWidth, setFitWidth] = useState(false);
  const [preferencesReady, setPreferencesReady] = useState(demo);
  const [panelMessage, setPanelMessage] = useState("");
  const [translationDialogOpen, setTranslationDialogOpen] = useState(false);
  const [translationLanguage, setTranslationLanguage] = useState(demoCurrentUser.preferences.translationLanguage);
  const [translation, setTranslation] = useState<PaperTranslation | null>(null);
  const [translationVisible, setTranslationVisible] = useState(false);
  const [translationPage, setTranslationPage] = useState<PaperTranslationPage | null>(null);
  const [translationBusy, setTranslationBusy] = useState(false);
  const [translationMessage, setTranslationMessage] = useState("");
  const [translationPageMessage, setTranslationPageMessage] = useState<{ page: number; text: string } | null>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const lastPersistedZoomRef = useRef(100);
  const focusRestoreRef = useRef({ left: true, assistant: true });
  const setSelectedPaperId = useWorkspaceStore((state) => state.setSelectedPaperId);
  const libraryHref = demo ? "/library?demo=1" : "/library";
  const activeTranslationId = translation?.id;
  const activeTranslationStatus = translation?.status;
  const activeTranslationCompletedPages = translation?.completedPages;

  useEffect(() => {
    if (!isReal) return;
    let stopped = false;
    async function load() {
      try {
        const next = await dataSource.getPaper(paperId);
        if (stopped) return;
        setPaper(next);
        setLoadMessage("");
      } catch (error) {
        if (!stopped) setLoadMessage(error instanceof Error ? error.message : "文献信息读取失败");
      }
    }
    void load();
    return () => { stopped = true; };
  }, [dataSource, isReal, paperId]);

  useEffect(() => {
    if (!isReal || (paper?.status !== "indexing" && paper?.status !== "deleting")) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void dataSource.getPaper(paperId).then((next) => {
        if (!stopped) { setPaper(next); setLoadMessage(""); }
      }).catch((error: unknown) => {
        if (!stopped) setLoadMessage(error instanceof Error ? error.message : "处理状态刷新失败");
      });
    }, 3_000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [dataSource, isReal, paper?.status, paperId]);

  useEffect(() => {
    setSelectedPaperId(paperId);
    workspaceRef.current?.setAttribute("data-client-ready", "true");
  }, [demo, paperId, setSelectedPaperId]);

  useEffect(() => {
    void dataSource.recordPaperOpened(paperId).then(() => {
      window.dispatchEvent(new Event("paperleaf:papers-changed"));
    }).catch(() => {
      // 最近阅读是辅助信息；记录失败不应打断 PDF 阅读与问答主流程。
    });
  }, [dataSource, paperId]);

  useEffect(() => {
    let stopped = false;
    async function loadPreferences() {
      try {
        const user = demo ? demoCurrentUser : await getCurrentUser();
        if (stopped) return;
        setLeftPanelOpen(user.preferences.leftPanelOpen);
        setAssistantPanelOpen(user.preferences.assistantPanelOpen);
        if (user.preferences.leftPanelOpen || user.preferences.assistantPanelOpen) focusRestoreRef.current = { left: user.preferences.leftPanelOpen, assistant: user.preferences.assistantPanelOpen };
        const nextZoom = clampZoom(user.preferences.pdfZoom);
        setZoomPercent(nextZoom);
        lastPersistedZoomRef.current = nextZoom;
        setTranslationLanguage(user.preferences.translationLanguage);
      } catch (error) {
        if (!stopped) setPanelMessage(error instanceof Error ? error.message : "阅读偏好读取失败，已使用默认布局");
      } finally {
        if (!stopped) setPreferencesReady(true);
      }
    }
    void loadPreferences();
    return () => { stopped = true; };
  }, [demo]);

  useEffect(() => {
    if (demo || !preferencesReady || fitWidth || lastPersistedZoomRef.current === zoomPercent) return;
    const timer = window.setTimeout(() => {
      void updateUserPreferences({ pdfZoom: zoomPercent }).then(() => {
        lastPersistedZoomRef.current = zoomPercent;
        setPanelMessage("");
      }).catch((error: unknown) => {
        setPanelMessage(error instanceof Error ? error.message : "PDF 缩放偏好保存失败");
      });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [demo, fitWidth, preferencesReady, zoomPercent]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const translationId = window.localStorage.getItem(`paperleaf:translation:${paperId}`);
    if (!translationId) return;
    let stopped = false;
    void dataSource.getPaperTranslation(paperId, translationId).then((next) => {
      if (!stopped) { setTranslation(next); setTranslationLanguage(next.targetLanguage); setTranslationVisible(true); }
    }).catch(() => {
      window.localStorage.removeItem(`paperleaf:translation:${paperId}`);
    });
    return () => { stopped = true; };
  }, [dataSource, paperId]);

  useEffect(() => {
    if (!translation || (translation.status !== "queued" && translation.status !== "running")) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void dataSource.getPaperTranslation(paperId, translation.id).then((next) => {
        if (!stopped) setTranslation(next);
      }).catch((error: unknown) => {
        if (!stopped) setTranslationMessage(error instanceof Error ? error.message : "翻译进度读取失败");
      });
    }, 2_000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [dataSource, paperId, translation]);

  useEffect(() => {
    if (!activeTranslationId) return;
    let stopped = false;
    void dataSource.getPaperTranslationPage(paperId, activeTranslationId, currentPage).then((next) => {
      if (!stopped) { setTranslationPage(next); setTranslationPageMessage(null); }
    }).catch((error: unknown) => {
      if (!stopped) setTranslationPageMessage({ page: currentPage, text: error instanceof Error ? error.message : "当前页译文读取失败" });
    });
    return () => { stopped = true; };
  }, [activeTranslationCompletedPages, activeTranslationId, activeTranslationStatus, currentPage, dataSource, paperId]);

  function openCitation(page: number) {
    setCurrentPage(Math.max(1, Math.min(paper?.pages || page, page)));
    setMobilePane("pdf");
  }

  async function submit(values: QuestionInput) {
    const question = values.question;
    setBusy("ask");
    setAskMessage("");
    setActivities([]);
    setAnswer({ question, answer: "", citations: [], activities: [] });
    try {
      const next = await dataSource.ask(question, [paperId], {
        onActivity: (activity) => {
          setActivities((items) => items.some((item) => item.key === activity.key) ? items.map((item) => item.key === activity.key ? activity : item) : [...items, activity]);
        },
        onAnswerUpdate: (nextAnswer) => {
          setAnswer((current) => current?.question === question ? { ...current, answer: nextAnswer } : current);
        },
        onCitationsUpdate: (citations) => {
          setAnswer((current) => current?.question === question ? { ...current, citations } : current);
        },
        onEvidenceQualityUpdate: (evidenceQuality) => {
          setAnswer((current) => current?.question === question ? { ...current, evidenceQuality } : current);
        },
      });
      setAnswer(next);
      setActivities(next.activities ?? []);
      setQuestionDraft("");
    }
    catch (error) { setAskMessage(error instanceof Error ? error.message : "提问失败"); }
    finally { setBusy(null); }
  }

  async function validateAndSubmitQuestion() {
    const parsed = questionSchema.safeParse({ question: questionDraft });
    if (!parsed.success) {
      setQuestionError(parsed.error.issues[0]?.message ?? "请输入有效问题");
      return;
    }
    setQuestionError("");
    await submit(parsed.data);
  }

  async function generateSummary() {
    setBusy("summary");
    setArtifactMessage("");
    try { setSummary(await dataSource.summarizePaper(paperId)); }
    catch (error) { setArtifactMessage(error instanceof Error ? error.message : "论文总结生成失败"); }
    finally { setBusy(null); }
  }

  async function generateStructure() {
    setBusy("structure");
    setArtifactMessage("");
    try { setStructure(await dataSource.buildStructureGraph(paperId)); }
    catch (error) { setArtifactMessage(error instanceof Error ? error.message : "结构图生成失败"); }
    finally { setBusy(null); }
  }

  async function updatePaper(input: PaperUpdateInput) {
    const updated = await dataSource.updatePaper(paperId, input);
    setPaper(updated);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
  }

  async function retryPaper() {
    const updated = await dataSource.retryPaper(paperId);
    setPaper(updated);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
  }

  async function deletePaper() {
    await dataSource.deletePaper(paperId);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
    if (isReal) { window.location.assign("/library"); return; }
    setPaper((current) => current ? { ...current, status: "deleting" } : current);
    setDetailsOpen(false);
    setManageMessage("演示模式已模拟删除；固定文献不会从公开 Demo 中移除。");
  }

  function savePanelPreferences(leftOpen: boolean, assistantOpen: boolean) {
    if (demo || !preferencesReady) return;
    void updateUserPreferences({ leftPanelOpen: leftOpen, assistantPanelOpen: assistantOpen }).then(() => {
      setPanelMessage("");
    }).catch((error: unknown) => {
      setPanelMessage(error instanceof Error ? error.message : "阅读布局偏好保存失败");
    });
  }

  function changePanelVisibility(nextLeft: boolean, nextAssistant: boolean) {
    if (nextLeft || nextAssistant) focusRestoreRef.current = { left: nextLeft, assistant: nextAssistant };
    setLeftPanelOpen(nextLeft);
    setAssistantPanelOpen(nextAssistant);
    savePanelPreferences(nextLeft, nextAssistant);
  }

  function toggleFocusMode() {
    if (!leftPanelOpen && !assistantPanelOpen) {
      const previous = focusRestoreRef.current;
      changePanelVisibility(previous.left, previous.assistant);
      return;
    }
    focusRestoreRef.current = { left: leftPanelOpen, assistant: assistantPanelOpen };
    changePanelVisibility(false, false);
  }

  function openTranslationDialog() {
    setTranslationMessage("");
    setTranslationDialogOpen(true);
  }

  function changeZoom(delta: number) {
    setFitWidth(false);
    setZoomPercent((current) => clampZoom(current + delta));
  }

  async function startTranslation() {
    setTranslationBusy(true);
    setTranslationMessage("");
    try {
      const next = await dataSource.createPaperTranslation(paperId, translationLanguage, currentPage);
      setTranslation(next);
      setTranslationVisible(true);
      setTranslationDialogOpen(false);
      if (typeof window !== "undefined") window.localStorage.setItem(`paperleaf:translation:${paperId}`, next.id);
      if (!demo) void updateUserPreferences({ translationLanguage }).catch(() => undefined);
    } catch (error) {
      setTranslationMessage(error instanceof Error ? error.message : "全文翻译任务创建失败");
    } finally {
      setTranslationBusy(false);
    }
  }

  async function cancelTranslation() {
    if (!translation) return;
    setTranslationBusy(true);
    setTranslationMessage("");
    try {
      setTranslation(await dataSource.cancelPaperTranslation(paperId, translation.id));
    } catch (error) {
      setTranslationMessage(error instanceof Error ? error.message : "翻译任务取消失败");
    } finally {
      setTranslationBusy(false);
    }
  }

  if (!paper) {
    return <div ref={workspaceRef} className="paper-workspace workspace-loading" data-client-ready="true"><FileText size={22} /><strong>{loadMessage ? "无法打开论文" : "正在载入论文工作台"}</strong><p role={loadMessage ? "alert" : "status"}>{loadMessage || "正在读取元数据与私有 PDF…"}</p><a className="secondary-button" href={libraryHref}>返回文献库</a></div>;
  }

  const pageShortcuts = paper.pages ? [...new Set([1, 2, Math.ceil(paper.pages / 2), paper.pages])].filter((page) => page > 0 && page <= paper.pages) : [1];

  const evidencePages = [...new Set([
    ...(answer?.citations.map((item) => item.page) ?? []),
    ...(summary?.citations.map((item) => item.physicalPage) ?? []),
    ...(structure?.nodes.map((item) => item.physicalPage) ?? []),
  ])];
  const readyForArtifacts = paper.status === "ready" || paper.status === "partial";

  const infoPane = <aside className={`workspace-info pane-view ${mobilePane === "info" ? "mobile-active" : ""}`} aria-label="论文信息">
    <div className="pane-heading"><a href={libraryHref} className="back-link"><ArrowLeft size={14} />返回文献库</a><button className="icon-button pane-settings" aria-label="编辑文献信息" onClick={() => setDetailsOpen(true)}><PencilLine size={14} /></button></div>
    <div className="paper-summary"><span className="paper-index">{isReal ? `PL–${paper.id.slice(0, 8).toUpperCase()}` : "PL–001"}</span><h2>{paper.title}</h2><p>{paper.authors || "作者待识别"}{paper.year > 0 ? ` · ${paper.year}` : ""}</p><PaperState paper={paper} /></div>
    <dl className="metadata"><div><dt>出版物</dt><dd>{paper.publication || "待识别"}</dd></div><div><dt>页数</dt><dd>{paper.pages ? `${paper.pages} 页` : "待识别"}</dd></div><div><dt>{paper.arxivId ? "arXiv" : "DOI"}</dt><dd className="mono">{paper.arxivId ?? paper.doi ?? "—"}</dd></div></dl>
    <div className="outline-list"><span className="eyebrow">{isReal ? "页码导航" : "论文目录"}</span>{isReal ? pageShortcuts.map((page) => <button key={page} className={currentPage === page ? "active" : ""} onClick={() => setCurrentPage(page)}><span>{String(page).padStart(2, "0")}</span>{page === 1 ? "论文首页" : page === paper.pages ? "最后一页" : `跳到第 ${page} 页`}</button>) : ["摘要", "1. Introduction", "2. Background", "3. Model Architecture", "4. Why Self-Attention", "5. Training", "6. Results"].map((item, index) => <button key={item} className={currentPage === index + 1 ? "active" : ""} onClick={() => setCurrentPage(index + 1)}><span>{String(index + 1).padStart(2, "0")}</span>{item}</button>)}</div>
    <div className="paper-pane-actions"><button className="secondary-button" onClick={() => setDetailsOpen(true)}><PencilLine size={14} />文献设置</button>{manageMessage && <p role="status">{manageMessage}</p>}</div>
  </aside>;

  const pdfPage = isReal
    ? <RealPdfDocument url={dataSource.fileUrl(paperId)} page={currentPage} fitWidth={fitWidth} scalePercent={zoomPercent} onPageCount={(count) => { setPaper((item) => item ? { ...item, pages: count } : item); if (currentPage > count) setCurrentPage(count); }} />
    : <div className="mock-pdf-scale" style={{ width: fitWidth ? "min(700px, 100%)" : `${zoomPercent}%` }}><article className="mock-paper" aria-label={`模拟 PDF 第 ${currentPage} 页`}><div className="pdf-running"><span>NEURIPS 2017</span><span>ARXIV:{paper.arxivId}</span></div><h1>{paper.title}</h1><p className="pdf-authors">Ashish Vaswani · Noam Shazeer · Niki Parmar · Jakob Uszkoreit · Llion Jones · Aidan N. Gomez</p><h2>{currentPage === 2 ? "1 · Introduction" : `Section · Page ${currentPage}`}</h2><p>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.</p><p className={currentPage === 2 ? "pdf-highlight" : ""}>We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.</p><p>Recurrent models typically factor computation along the symbol positions of the input and output sequences. This inherently sequential nature precludes parallelization within training examples.</p><div className="formula">Attention(Q, K, V) = softmax(QKᵀ / √dₖ)V</div><p>Self-attention connects all positions with a constant number of sequentially executed operations.</p><span className="pdf-page-number">{currentPage}</span></article></div>;

  const currentTranslationPage = translationPage?.page === currentPage ? translationPage : null;
  let translatedPageContent = <p role="status">当前页等待翻译，后台会优先处理正在阅读的页面。</p>;
  if (currentTranslationPage?.status === "running") translatedPageContent = <p role="status">正在翻译第 {currentPage} 页…</p>;
  if (currentTranslationPage?.status === "no_text") translatedPageContent = <p role="status">此页暂无可翻译文本，可能是图片页或尚未完成 OCR。</p>;
  if (currentTranslationPage?.status === "failed") translatedPageContent = <p role="alert">此页翻译失败：{currentTranslationPage.error || "可稍后重新创建翻译任务重试。"}</p>;
  if (currentTranslationPage?.status === "cancelled") translatedPageContent = <p role="status">此页翻译已取消。</p>;
  if (currentTranslationPage?.status === "completed") translatedPageContent = currentTranslationPage.text
    ? <div className="translated-text">{currentTranslationPage.text.split(/\n{2,}/).map((paragraph, index) => <p key={`${currentPage}-${index}`}>{paragraph}</p>)}</div>
    : <p role="status">此页暂无可翻译文本。</p>;

  const readerPane = <section className={`workspace-reader pane-view ${mobilePane === "pdf" ? "mobile-active" : ""}`} aria-label="PDF 阅读器">
    <div className="reader-toolbar" role="toolbar" aria-label="PDF 阅读工具栏">
      <div className="reader-tool-group page-control">
        <button className="icon-button" aria-label="上一页" title="上一页" disabled={currentPage <= 1} onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}><ChevronLeft size={17} /></button>
        <span className="mono" aria-label={`第 ${currentPage} 页，共 ${paper.pages || "未知"} 页`}>{currentPage} / {paper.pages || "—"}</span>
        <button className="icon-button" aria-label="下一页" title="下一页" disabled={Boolean(paper.pages && currentPage >= paper.pages)} onClick={() => setCurrentPage(Math.min(paper.pages || currentPage + 1, currentPage + 1))}><ChevronRight size={17} /></button>
      </div>
      <div className="reader-tool-group translation-tool-group">
        <button className={translationVisible ? "reader-tool-button active" : "reader-tool-button"} type="button" aria-pressed={translationVisible} onClick={() => translation ? setTranslationVisible((current) => !current) : openTranslationDialog()}><Languages size={15} />{translation ? translationVisible ? "隐藏译文" : "显示译文" : "翻译全文"}</button>
      </div>
      <div className="reader-tool-group zoom-tools">
        <button className="icon-button" aria-label="缩小 PDF" title="缩小" disabled={!fitWidth && zoomPercent <= 50} onClick={() => changeZoom(-10)}><Minus size={16} /></button>
        <output className="zoom-value" aria-live="polite">{fitWidth ? "自适应" : `${zoomPercent}%`}</output>
        <button className="icon-button" aria-label="放大 PDF" title="放大" disabled={!fitWidth && zoomPercent >= 200} onClick={() => changeZoom(10)}><Plus size={16} /></button>
        <button className={fitWidth ? "reader-tool-button active" : "reader-tool-button"} type="button" aria-pressed={fitWidth} onClick={() => setFitWidth(true)}><Maximize2 size={15} />适合宽度</button>
      </div>
      <details className="reader-layout-menu reader-tool-group">
        <summary className="reader-tool-button"><Focus size={15} />阅读布局</summary>
        <div className="reader-layout-actions">
          <button className="icon-button" aria-label={leftPanelOpen ? "收起资料栏" : "显示资料栏"} title={leftPanelOpen ? "收起资料栏" : "显示资料栏"} aria-controls="info" aria-expanded={leftPanelOpen} onClick={() => changePanelVisibility(!leftPanelOpen, assistantPanelOpen)}>{leftPanelOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}</button>
          <button className="icon-button" aria-label={assistantPanelOpen ? "收起助手栏" : "显示助手栏"} title={assistantPanelOpen ? "收起助手栏" : "显示助手栏"} aria-controls="assistant" aria-expanded={assistantPanelOpen} onClick={() => changePanelVisibility(leftPanelOpen, !assistantPanelOpen)}>{assistantPanelOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}</button>
          <button className={!leftPanelOpen && !assistantPanelOpen ? "reader-tool-button active" : "reader-tool-button"} type="button" aria-controls="info assistant" aria-pressed={!leftPanelOpen && !assistantPanelOpen} onClick={toggleFocusMode}><Focus size={15} />{!leftPanelOpen && !assistantPanelOpen ? "退出专注阅读" : "专注阅读"}</button>
        </div>
      </details>
    </div>
    <div className={translation && translationVisible ? "document-stage translation-stage" : "document-stage"} tabIndex={0} aria-label="PDF 页面，可滚动浏览">
      <div className="translation-original">{pdfPage}<div className="citation-rail" aria-hidden="true">{evidencePages.map((page) => <span key={page} className={page === currentPage ? "active" : ""} />)}</div></div>
      {translation && translationVisible && <aside className="translation-page" aria-label={`${translationLanguageLabel(translation.targetLanguage)}译文，第 ${currentPage} 页`}>
        <div className="translation-page-head"><div><span>{translationLanguageLabel(translation.targetLanguage)}译文</span><strong>第 {currentPage} 页</strong></div><button type="button" className="icon-button" aria-label="关闭译文双栏" onClick={() => setTranslationVisible(false)}><X size={16} /></button></div>
        <div className="translation-progress" role="status"><span>{translationStatusLabel(translation)}</span><progress aria-label={`翻译进度 ${translation.progress}%`} max={100} value={translation.progress}>{translation.progress}%</progress></div>
        <div className="translation-page-body">{translatedPageContent}</div>
        {(translation.status === "queued" || translation.status === "running") && <button type="button" className="secondary-button translation-cancel" disabled={translationBusy} onClick={() => void cancelTranslation()}>{translationBusy ? "正在取消…" : "取消后台翻译"}</button>}
        {translation.status === "partial" && <button type="button" className="secondary-button translation-cancel" disabled={translationBusy} onClick={openTranslationDialog}>重试失败页</button>}
        {(translation.status === "failed" || translation.status === "cancelled") && <button type="button" className="secondary-button translation-cancel" disabled={translationBusy} onClick={openTranslationDialog}>重新创建翻译任务</button>}
        {translationMessage && <p className="field-error" role="alert">{translationMessage}</p>}
        {translationPageMessage?.page === currentPage && <p className="field-error" role="alert">{translationPageMessage.text}</p>}
      </aside>}
    </div>
    <div className="reader-status"><strong>{evidencePages.includes(currentPage) ? "证据页已定位" : "论文页面"}</strong><span>第 {currentPage} 页</span><span>{translation && translationVisible ? `原文 + ${translationLanguageLabel(translation.targetLanguage)}译文` : isReal ? "原始 PDF" : "模拟文本层"}</span></div>
  </section>;

  const askContent = <div className="conversation">
    <AgentRunProgress activities={activities} />
    {answer ? <><div className={`run-note ${answer.evidenceQuality?.grade === "insufficient" ? "quality-insufficient" : ""}`} role="status">{answer.evidenceQuality?.summary ?? (busy === "ask" ? "问题已提交，正在检索并等待回答事件" : `已保留 ${answer.citations.length} 条可验证证据`)}</div><EvidenceQualityStrip quality={answer.evidenceQuality} /><span className="eyebrow">你的问题</span><p className="question-text">{answer.question}</p><span className="eyebrow">{busy === "ask" && !answer.answer ? "回答状态" : answer.evidenceQuality?.grade === "insufficient" ? "证据状态" : "基于原文回答"}</span><p className="answer-text">{answer.answer || (busy === "ask" ? "正在准备基于文献证据的回答…" : "本次运行未返回回答。")} {answer.citations.map((citation, index) => <button key={citation.id} className="inline-citation" onClick={() => openCitation(citation.page)} aria-label={`引用 [${index + 1}]，查看 PDF 第 ${citation.page} 页`}>[{index + 1}]</button>)}</p>{answer.citations.length > 0 && <div className="citation-list" aria-label="回答引用">{answer.citations.map((citation, index) => <button className="citation-row" key={citation.id} onClick={() => openCitation(citation.page)}><span className="citation-no">{String(index + 1).padStart(2, "0")}</span><q>{citation.quote}</q><span className="citation-page">PDF {String(citation.page).padStart(2, "0")}</span></button>)}</div>}</> : <div className="assistant-empty"><Quote size={19} /><strong>从原文开始提问</strong><p>回答只使用当前论文中已完成索引的内容，并附上可回读的物理页码。</p></div>}
    {askMessage && <p className="field-error" role="alert">{askMessage}</p>}
  </div>;

  const summaryContent = <div className="artifact-panel">
    <div className="artifact-heading"><div><span className="eyebrow">证据化概览</span><h3>证据化论文概览</h3><p>围绕研究问题、方法、结果与限制整理；不会补写证据中不存在的内容。</p></div><button className="secondary-button" disabled={!readyForArtifacts || Boolean(busy)} onClick={() => void generateSummary()}>{busy === "summary" ? "正在生成" : summary ? "重新生成" : "生成概览"}</button></div>
    {!summary && <div className="artifact-empty"><AlignLeft size={20} /><strong>{readyForArtifacts ? "尚未生成概览" : "等待论文完成索引"}</strong><p>{readyForArtifacts ? "生成后，点击页码可以回到对应 PDF 证据。" : "PDF 仍可阅读；总结功能会在索引就绪后开放。"}</p></div>}
    {summary && <article className="summary-artifact"><div className="artifact-mode"><span>{summary.mode === "model" ? "模型归纳" : "原文保底摘录"}</span><em>{summary.citations.length} 个证据页</em></div>{summary.mode === "extractive" && <p className="artifact-fallback-note">AI 归纳本次未成功；当前展示的是从论文不同页面抽取的可回读原文，不是最终概览。你可以点击“重新生成”再试一次。</p>}<SummaryContent content={summary.content} citations={summary.citations} onOpenPage={openCitation} /><div className="artifact-citations">{summary.citations.map((citation, index) => <button key={citation.chunkId} onClick={() => openCitation(citation.physicalPage)}><span>{String(index + 1).padStart(2, "0")}</span>PDF {citation.physicalPage}<small className="mono">{citation.chunkId}</small></button>)}</div></article>}
    {artifactMessage && <p className="field-error" role="alert">{artifactMessage}</p>}
  </div>;

  const structureContent = <div className="artifact-panel">
    <div className="artifact-heading"><div><span className="eyebrow">证据结构图</span><h3>论文结构图</h3><p>结构节点保留原始 Chunk 和物理页，图形只表达已抽取证据之间的关系。</p></div><button className="secondary-button" disabled={!readyForArtifacts || Boolean(busy)} onClick={() => void generateStructure()}>{busy === "structure" ? "正在构建" : structure ? "重新构建" : "构建结构"}</button></div>
    {!structure && <div className="artifact-empty"><Network size={20} /><strong>{readyForArtifacts ? "尚未构建结构图" : "等待论文完成索引"}</strong><p>{readyForArtifacts ? "结构图使用 Mermaid strict mode，并提供可键盘访问的证据目录。" : "索引完成后再提取论文结构。"}</p></div>}
    {structure && <StructureDiagram key={structure.mermaid} graph={structure} onOpenPage={openCitation} />}
    {artifactMessage && <p className="field-error" role="alert">{artifactMessage}</p>}
  </div>;

  const askPane = <aside className={`workspace-assistant pane-view ${mobilePane === "ask" ? "mobile-active" : ""}`} aria-label="论文助手">
    <div className="assistant-head"><span className="assistant-title"><Quote size={14} />论文助手</span><nav className="assistant-tabs" aria-label="论文助手视图">{assistantTabs.map(({ id, label, icon: Icon }) => <button key={id} className={assistantView === id ? "active" : ""} aria-current={assistantView === id ? "page" : undefined} onClick={() => { setAssistantView(id); setArtifactMessage(""); }}><Icon size={13} />{label}</button>)}</nav></div>
    {assistantView === "ask" ? askContent : assistantView === "summary" ? summaryContent : structureContent}
    {assistantView === "ask" && <form className="composer" onSubmit={(event) => { event.preventDefault(); void validateAndSubmitQuestion(); }}><label className="composer-box"><span className="sr-only">向论文提问</span><textarea name="question" value={questionDraft} onChange={(event) => { setQuestionDraft(event.target.value); if (questionError) setQuestionError(""); }} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void validateAndSubmitQuestion(); } }} placeholder="继续追问这篇论文…" rows={2} /><button className="send-button" disabled={busy === "ask" || !readyForArtifacts} aria-label="发送问题"><Send size={15} /></button></label>{questionError && <span className="field-error">{questionError}</span>}<div className="composer-meta"><span>{readyForArtifacts ? "仅依据当前论文回答" : "等待索引完成后可提问"}</span><span>{busy === "ask" ? "正在检索…" : "Ctrl + Enter"}</span></div></form>}
  </aside>;

  return (
    <div ref={workspaceRef} className="paper-workspace" data-client-ready="false">
      <nav className="mobile-workspace-tabs" aria-label="移动端工作区">{tabItems.map(({ id, label, icon: Icon }) => <button key={id} onClick={() => setMobilePane(id)} className={mobilePane === id ? "active" : ""} aria-current={mobilePane === id ? "page" : undefined}><Icon size={15} />{label}<span className="sr-only">{mobilePane === id ? "，当前视图" : ""}</span></button>)}</nav>
      <div className="workspace-desktop"><Group orientation="horizontal" id={demo ? "demo-workspace" : "paper-workspace"}>{leftPanelOpen && <Fragment key="info-zone"><Panel id="info" defaultSize="20%" minSize="16%" maxSize="28%">{infoPane}</Panel><ResizeSeparator label="调整论文信息栏宽度" /></Fragment>}<Panel key="reader-zone" id="reader" defaultSize={leftPanelOpen && assistantPanelOpen ? "49%" : leftPanelOpen ? "72%" : assistantPanelOpen ? "69%" : "100%"} minSize={leftPanelOpen || assistantPanelOpen ? "42%" : "100%"}>{readerPane}</Panel>{assistantPanelOpen && <Fragment key="assistant-zone"><ResizeSeparator label="调整论文助手栏宽度" /><Panel id="assistant" defaultSize="31%" minSize="25%" maxSize="39%">{askPane}</Panel></Fragment>}</Group></div>
      <div className="workspace-mobile">{infoPane}{readerPane}{askPane}</div>
      {panelMessage && <p className="workspace-preference-error" role="alert">{panelMessage}</p>}
      <PaperDetailsDialog paper={paper} open={detailsOpen} onOpenChange={setDetailsOpen} onSave={updatePaper} onDelete={deletePaper} onRetry={retryPaper} />
      <TranslationConfirmDialog open={translationDialogOpen} pages={paper.pages} targetLanguage={translationLanguage} busy={translationBusy} error={translationMessage} onTargetLanguageChange={setTranslationLanguage} onOpenChange={setTranslationDialogOpen} onConfirm={() => void startTranslation()} />
    </div>
  );
}
